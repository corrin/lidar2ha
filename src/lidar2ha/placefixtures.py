#!/usr/bin/env python3
"""Put detected fittings into the rooms of the authoritative capture(s).

A fixture capture knows where the lights are but has poor geometry; a geometry
capture has good rooms but does not know where the lights are. This joins them,
so each fitting is reported as "in the master bedroom" rather than as a bare
coordinate nobody can check.

Three hops, all reusing registration's own maths so the conventions cannot drift:

    fixture position (fixture capture's MESH frame, metres)
      -> invert that capture's own plan-to-mesh registration
    fixture plan frame (cm)
      -> fit the fixture capture's plan onto a geometry capture's plan
    geometry capture's model frame (cm)
      -> point-in-polygon against its named rooms

The middle hop is the one that can fail: it is a rigid fit between two plans of
the same rooms, and a deliberately-poor capture may not give it much to hold on
to. Its coverage and median error are printed, and should be read before any of
the room assignments are believed.

ONE FIXTURE PASS CAN SPAN SEVERAL GEOMETRY CAPTURES. Geometry does not merge
across captures, so a house scanned in two goes has two models -- but a person
walking a level with every light on does not stop at that boundary. Every
geometry model given is fitted separately, and each fitting goes to whichever
one actually contains it, rather than the caller having to split the fixture
pass by hand along a line that exists only in the scanning history.

DIFFERENCING AGAINST AN ORDINARY CAPTURE separates fittings from windows
mechanically, which brightness alone cannot do. Pass `--daylight-mesh`, and see
lidar2ha.daylight for why that works; without it every record simply carries no
verdict and this behaves exactly as it did before.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np
from scipy.spatial import cKDTree
from shapely.geometry import Point, Polygon

from .daylight import RADIUS_M, WINDOW_LUMA, Reference, load_reference, summarise, verdict_at
from .registration import register, sample_along_walls, transform
from .schema import Level, Model, Registration, Room, load_model

M_TO_CM = 100.0
# How far outside a room a fitting may fall and still be attributed to it. A
# fitting just over a wall line is common -- two registrations of a deliberately
# poor scan compose their errors -- and still useful, so it is flagged rather
# than lost.
NEAR_MISS_CM = 150.0


def mesh_to_plan_cm(pts_m: np.ndarray, reg: Registration) -> np.ndarray:
    """Undo a plan-to-mesh registration.

    Forward is: mirror y, rotate by theta, translate by t. So the inverse is
    translate back, rotate by -theta, then mirror again -- mirroring being its
    own inverse.
    """
    p = pts_m - np.array([reg.tx_m, reg.ty_m])
    th = -math.radians(reg.theta_deg)
    c, s = math.cos(th), math.sin(th)
    p = p @ np.array([[c, -s], [s, c]]).T
    if reg.mirror:
        p = p.copy()
        p[:, 1] = -p[:, 1]
    return p * M_TO_CM


def plan_cm_to_mesh_m(plan_cm: np.ndarray, reg: Registration) -> np.ndarray:
    """Redo a plan-to-mesh registration: plan centimetres -> mesh metres.

    The exact inverse of `mesh_to_plan_cm`, and deliberately expressed through
    `registration.transform` -- the very function that put the plan onto the
    mesh in the first place -- rather than by writing the rotation out again.
    Two hand-written copies of one transform is how a sign error survives: both
    look right, and getting it wrong yields a reflected plan, which is still a
    perfectly plausible plan.

    Needed to ask an ORDINARY capture what it saw at a fitting's position: the
    position is known in the geometry model's plan frame, and that capture's
    atlas is indexed in its mesh frame.
    """
    return transform(np.asarray(plan_cm, dtype=float) / M_TO_CM,
                     math.radians(reg.theta_deg), reg.tx_m, reg.ty_m, reg.mirror)


def rooms_of(model: Model) -> list[tuple[Level, Room, Polygon]]:
    """(level, room, polygon) for every room, keeping the level.

    The level is not decoration. Its registration is what carries a point back
    into the mesh, and its floor height is what an elevation is measured from,
    so a room without its level cannot be differenced against anything.
    """
    return [(lv, r, Polygon([(x, y) for x, y in r.points]))
            for lv in model.levels for r in lv.rooms]


def fit_plans(fix_model: Model, geo_model: Model) -> dict:
    """Rigid fit of the fixture capture's plan onto one geometry capture's."""
    src = sample_along_walls([w for lv in fix_model.levels for w in lv.walls])
    tgt = sample_along_walls([w for lv in geo_model.levels for w in lv.walls])
    return register(src, tgt, cKDTree(tgt), force_mirror=False)


def assign(positions_cm: list[np.ndarray], room_sets: list[list[tuple]]
           ) -> tuple[int | None, str, tuple[Level, Room] | None]:
    """Which geometry capture owns this fitting, and which of its rooms.

    `positions_cm[i]` is the fitting's position in capture i's own plan frame,
    because each capture has its own frame and there is no shared one.

    CONTAINMENT IS CHECKED ACROSS EVERY CAPTURE BEFORE ANY NEAR MISS. Doing it
    per capture in turn would let a fitting that merely sits near a wall in the
    first capture be claimed by it, while the capture that genuinely contains
    the fitting is never consulted -- and the answer would then depend on the
    order the models were listed on the command line.

    Returns (winning capture index, the room label, the (level, room) pair).
    """
    for i, (xy, rooms) in enumerate(zip(positions_cm, room_sets, strict=True)):
        point = Point(xy[0], xy[1])
        for level, room, poly in rooms:
            if poly.contains(point):
                return i, str(room.name), (level, room)

    best: tuple[float, int, str, tuple[Level, Room]] | None = None
    for i, (xy, rooms) in enumerate(zip(positions_cm, room_sets, strict=True)):
        point = Point(xy[0], xy[1])
        for level, room, poly in rooms:
            distance = poly.distance(point)
            if best is None or distance < best[0]:
                best = (distance, i, str(room.name), (level, room))
    if best is not None and best[0] < NEAR_MISS_CM:
        return best[1], f"~{best[2]} (+{best[0]:.0f}cm)", best[3]
    return None, "OUTSIDE", None


def check_roles(fix_model: Model, geo_models: list[Model], names: list[str]) -> None:
    """Say so when a model is being used as something it never declared itself.

    Not fatal. A model.json written before `role` existed carries the default,
    and refusing those would break every project already on disk -- but a
    geometry capture used as a fixture pass, or the reverse, is worth one line
    before anyone reads the coordinates below as measurements.
    """
    if fix_model.role != "fixtures":
        print(f"  note: {fix_model.source!r} is not marked `role: fixtures`. If it is a "
              f"fixture pass, re-run polycam with --role fixtures.")
    for model, name in zip(geo_models, names, strict=True):
        if model.role != "geometry":
            print(f"  WARNING: {Path(name).name} is a {model.role} capture. Its rooms "
                  f"come from a scan whose geometry was sacrificed on purpose.")


def judge(record: dict, pair: tuple[Level, Room] | None, reference: Reference,
          radius_m: float, cutoff: float) -> tuple[str, str]:
    """This candidate's daylight verdict, and why it could not be reached.

    Every way of failing to get an answer returns "unseen" WITH A REASON,
    because the alternatives -- an absent key, or a verdict quietly defaulting
    to "fitting" -- are how a window ends up in the finished render with nobody
    able to say which step decided it was a lamp.

    Mutates `record` to add the measurement behind a real verdict, so the
    number that produced the call is readable next to the call itself.
    """
    if pair is None:
        return "unseen", "in no room, so there is no level registration to place it with"
    level, _room = pair
    if level.registration is None:
        return "unseen", f"level {level.name!r} of the geometry capture is not registered"
    if level.registration.floor_z_m is None:
        return "unseen", f"level {level.name!r} has no floor height in its own mesh"
    if record.get("elevation_cm") is None:
        return "unseen", "the fixture capture had no floor to measure a height from"

    xy = plan_cm_to_mesh_m(
        np.array([[record["plan_x_cm"], record["plan_y_cm"]]]), level.registration)[0]
    z = level.registration.floor_z_m + float(record["elevation_cm"]) / M_TO_CM
    verdict, luma, faces = verdict_at(
        reference, np.array([xy[0], xy[1], z]), radius_m, cutoff)
    if verdict == "unseen":
        return verdict, f"no faces within {radius_m:.2f} m in the ordinary capture"
    record["daylight_luma"] = round(luma, 1) if luma is not None else None
    record["daylight_faces"] = faces
    return verdict, ""


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("fixtures", help="fixtures.json from lidar2ha.fixtures")
    ap.add_argument("fixture_model", help="the fixture capture's registered model json")
    ap.add_argument("geometry_models", nargs="+",
                    help="the geometry captures' NAMED model jsons. Give several when "
                         "one fixture pass spans more than one geometry capture")
    ap.add_argument("-o", "--out", default="fixtures_placed.json")
    ap.add_argument("--daylight-mesh",
                    help="an ORDINARY capture of the same rooms, lights off. A window is "
                         "bright in it too and a fitting is not, which separates them "
                         "mechanically -- see lidar2ha.daylight")
    ap.add_argument("--daylight-radius", type=float, default=RADIUS_M,
                    help="how far from a candidate to sample the ordinary capture, in m")
    ap.add_argument("--daylight-cutoff", type=float, default=WINDOW_LUMA,
                    help="luma at or above which the ordinary capture counts as bright")
    args = ap.parse_args()

    found = json.loads(Path(args.fixtures).read_text(encoding="utf-8"))
    fix_model = load_model(args.fixture_model)
    geo_models = [load_model(p) for p in args.geometry_models]
    check_roles(fix_model, geo_models, args.geometry_models)

    reg = fix_model.levels[0].registration
    if reg is None:
        raise SystemExit("the fixture capture is not registered to its own mesh")
    print(f"fixture capture self-registration: {reg.theta_deg:.2f} deg, "
          f"{reg.median_error_m * 100:.1f} cm, {reg.coverage * 100:.0f}% coverage")

    # --- hop 2: fit the fixture plan onto each geometry plan -------------------
    pts_m = np.array([[f["x"], f["y"]] for f in found])
    plan_cm = mesh_to_plan_cm(pts_m, reg)

    targets = []
    for name, geo_model in zip(args.geometry_models, geo_models, strict=True):
        fit = fit_plans(fix_model, geo_model)
        print(f"plan-to-plan fit  : {Path(name).stem:<20} "
              f"{math.degrees(fit['theta_rad']) % 360:7.2f} deg, "
              f"{fit['median_error_m'] * 100:5.1f} cm, "
              f"{fit['coverage'] * 100:3.0f}% coverage")
        if fit["coverage"] < 0.9:
            print(f"    WARNING: below 90%. Every room assignment to "
                  f"{Path(name).stem} below is unreliable.")
        placed_m = transform(plan_cm / M_TO_CM,
                             fit["theta_rad"], fit["tx"], fit["ty"], False)
        targets.append({"name": name, "model": geo_model,
                        "rooms": rooms_of(geo_model),
                        "placed_cm": placed_m * M_TO_CM})

    reference = None
    if args.daylight_mesh:
        print(f"\nreading the ordinary capture {args.daylight_mesh} ...")
        reference = load_reference(args.daylight_mesh)
        print(f"  {reference.faces:,} faces to difference against")

    print(f"\n{'#':>3} {'room':<22} {'capture':<14} {'x':>8} {'y':>8} {'z(m)':>7} "
          f"{'faces':>6} {'surface':>8}  verdict")
    print("-" * 96)

    out = []
    verdicts: list[str] = []
    for i, f in enumerate(found):
        which, room, pair = assign([t["placed_cm"][i] for t in targets],
                                   [t["rooms"] for t in targets])
        # An OUTSIDE fitting belongs to no capture, but its coordinates still
        # have to be reported in some frame; the first is as good as any and
        # `capture` records which it was.
        target = targets[which if which is not None else 0]
        x, y = target["placed_cm"][i]

        record = dict(f)
        record.update({"room": room, "plan_x_cm": round(float(x), 1),
                       "plan_y_cm": round(float(y), 1),
                       "capture": target["model"].source})
        # Height above this level's floor, which is the whole point of measuring
        # fittings rather than guessing them: `z` is a mesh coordinate, and the
        # registration already knows where the floor sits in that same frame.
        if reg.floor_z_m is not None:
            record["elevation_cm"] = round((float(f["z"]) - reg.floor_z_m) * M_TO_CM, 1)

        verdict = ""
        if reference is not None:
            verdict, note = judge(record, pair, reference,
                                  args.daylight_radius, args.daylight_cutoff)
            record["verdict"] = verdict
            if note:
                record["verdict_note"] = note
            verdicts.append(verdict)

        out.append(record)
        print(f"{i:>3} {str(room):<22} {Path(target['name']).stem:<14} {x:>8.0f} "
              f"{y:>8.0f} {f['z']:>7.2f} {f['faces']:>6} {f['surface']:>8}  {verdict}")

    Path(args.out).write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"\nwrote {args.out}")

    by_room: dict[str, int] = {}
    for record in out:
        by_room[str(record["room"])] = by_room.get(str(record["room"]), 0) + 1
    print("\nfittings per room:")
    for room, n in sorted(by_room.items(), key=lambda kv: -kv[1]):
        print(f"  {room:<24} {n}")

    if verdicts:
        counts = summarise(verdicts)
        print(f"\ndifferenced against {args.daylight_mesh}:")
        print(f"  {counts['fitting']:>3} read as fittings -- dark there with the lights off")
        print(f"  {counts['window']:>3} read as windows  -- bright there in both captures")
        print(f"  {counts['unseen']:>3} could not be judged -- the ordinary capture has")
        print("      nothing at that point, which is not evidence either way")
        print("  The contact sheet puts the windows last. Look at them before `lights`")
        print("  drops them: the cutoff is a guess until it has been read against a sheet.")


if __name__ == "__main__":
    main()
