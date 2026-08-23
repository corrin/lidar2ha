#!/usr/bin/env python3
"""Put detected fittings into the rooms of the authoritative capture.

A fixture capture knows where the lights are but has poor geometry; a geometry
capture has good rooms but does not know where the lights are. This joins them,
so each fitting is reported as "in the master bedroom" rather than as a bare
coordinate nobody can check.

Three hops, all reusing registration's own maths so the conventions cannot drift:

    fixture position (fixture capture's MESH frame, metres)
      -> invert that capture's own plan-to-mesh registration
    fixture plan frame (cm)
      -> fit the fixture capture's plan onto the geometry capture's plan
    geometry capture's model frame (cm)
      -> point-in-polygon against its named rooms

The middle hop is the one that can fail: it is a rigid fit between two plans of
the same rooms, and a deliberately-poor capture may not give it much to hold on
to. Its coverage and median error are printed, and should be read before any of
the room assignments are believed.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np
from scipy.spatial import cKDTree
from shapely.geometry import Point, Polygon

from .registration import register, sample_along_walls, transform
from .schema import load_model

M_TO_CM = 100.0


def mesh_to_plan_cm(pts_m: np.ndarray, reg) -> np.ndarray:
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


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("fixtures", help="fixtures.json from lidar2ha.fixtures")
    ap.add_argument("fixture_model", help="the fixture capture's registered model json")
    ap.add_argument("geometry_model", help="the geometry capture's NAMED model json")
    ap.add_argument("-o", "--out", default="fixtures_placed.json")
    args = ap.parse_args()

    found = json.loads(Path(args.fixtures).read_text(encoding="utf-8"))
    fix_model = load_model(args.fixture_model)
    geo_model = load_model(args.geometry_model)

    reg = fix_model.levels[0].registration
    if reg is None:
        raise SystemExit("the fixture capture is not registered to its own mesh")
    print(f"fixture capture self-registration: {reg.theta_deg:.2f} deg, "
          f"{reg.median_error_m * 100:.1f} cm, {reg.coverage * 100:.0f}% coverage")

    # --- hop 2: fit the fixture plan onto the geometry plan --------------------
    fix_walls = [w for lv in fix_model.levels for w in lv.walls]
    geo_walls = [w for lv in geo_model.levels for w in lv.walls]
    src = sample_along_walls(fix_walls)
    tgt = sample_along_walls(geo_walls)
    fit = register(src, tgt, cKDTree(tgt), force_mirror=False)
    print(f"plan-to-plan fit  : {math.degrees(fit['theta_rad']) % 360:.2f} deg, "
          f"{fit['median_error_m'] * 100:.1f} cm, {fit['coverage'] * 100:.0f}% coverage")
    if fit["coverage"] < 0.9:
        print("  WARNING: coverage below 90%. Room assignments below are unreliable.")

    # --- move every fitting through both hops ---------------------------------
    pts_m = np.array([[f["x"], f["y"]] for f in found])
    plan_cm = mesh_to_plan_cm(pts_m, reg)
    placed_m = transform(plan_cm / M_TO_CM, fit["theta_rad"], fit["tx"], fit["ty"], False)
    placed_cm = placed_m * M_TO_CM

    rooms = [(r, Polygon([(x, y) for x, y in r.points]))
             for lv in geo_model.levels for r in lv.rooms]

    print(f"\n{'#':>3} {'room':<22} {'x':>8} {'y':>8} {'z(m)':>7} "
          f"{'faces':>6} {'surface':>8}")
    print("-" * 68)

    out = []
    for i, (f, (x, y)) in enumerate(zip(found, placed_cm, strict=True)):
        pt = Point(x, y)
        room = next((r.name for r, poly in rooms if poly.contains(pt)), None)
        if room is None:
            # Outside every polygon: nearest room, flagged, since a fitting just
            # over a wall line is common and still useful to a human.
            best, bestd = None, float("inf")
            for r, poly in rooms:
                d = poly.distance(pt)
                if d < bestd:
                    best, bestd = r.name, d
            room = f"~{best} (+{bestd:.0f}cm)" if bestd < 150 else "OUTSIDE"
        rec = dict(f)
        rec.update({"room": room, "plan_x_cm": round(float(x), 1),
                    "plan_y_cm": round(float(y), 1)})
        # Height above this level's floor, which is the whole point of measuring
        # fittings rather than guessing them: `z` is a mesh coordinate, and the
        # registration already knows where the floor sits in that same frame.
        if reg.floor_z_m is not None:
            rec["elevation_cm"] = round((float(f["z"]) - reg.floor_z_m) * M_TO_CM, 1)
        out.append(rec)
        print(f"{i:>3} {str(room):<22} {x:>8.0f} {y:>8.0f} {f['z']:>7.2f} "
              f"{f['faces']:>6} {f['surface']:>8}")

    Path(args.out).write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"\nwrote {args.out}")

    by_room: dict[str, int] = {}
    for r in out:
        by_room[str(r["room"])] = by_room.get(str(r["room"]), 0) + 1
    print("\nfittings per room:")
    for room, n in sorted(by_room.items(), key=lambda kv: -kv[1]):
        print(f"  {room:<24} {n}")


if __name__ == "__main__":
    main()
