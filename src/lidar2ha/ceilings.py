#!/usr/bin/env python3
"""Measure each room's ceiling from the mesh, instead of trusting one number.

Polycam reports one ceiling height per room it recognised. When it fuses two
spaces, that figure belongs to whichever won and the other's is lost; after
`seams` splits them, the pieces have no measured height at all. This supplies
one, from the geometry.

Method: find DOWN-facing faces above each room's footprint -- ceilings face
down, floors face up -- and report their height above that room's floor.

Read the p95, not the median. Down-facing faces also include the undersides of
tables, worktops and stair soffits, which drag the median toward furniture
height; the p95 is the actual ceiling.

TRUNCATION IS THE TRAP. A scan only reaches as high as the phone was pointed.
If a room's p95 sits at the mesh's own upper limit, the scan ran out before the
room did and the number is a LOWER BOUND -- one capture reported 592 cm for a
space the owner's notes put nearer 7 m. This warns when that happens rather
than quietly returning a plausible height.

THE ROOM IS CARRIED ONTO THE MESH BY ITS LEVEL'S REGISTRATION when the level
has one. Where it does not, plan and mesh are assumed to share a frame up to
cm-vs-m scaling -- true of a capture measured against its own mesh, and the
reported face counts show when it is not. That assumption was harmless while
this only printed; it writes now, so a level that HAS a registration must be
read through it rather than around it.

Measuring is not writing. Without `-o` this reports and changes nothing; with
it, the rooms it could measure get a `ceiling_high_cm` and the rooms it could
not are named and left alone. A room with no height is one `lights` falls back
to the LEVEL's ceiling for, which on a level containing a void hangs a lounge
lamp four and a half metres up -- so the absences are as much the output as the
numbers.

Usage:
    python -m lidar2ha.ceilings model.json mesh.obj
    python -m lidar2ha.ceilings model.json mesh.obj -o measured.json
"""

from __future__ import annotations

import argparse
from typing import Literal, NamedTuple

import numpy as np
import trimesh
from shapely.geometry import Point, Polygon

from .placefixtures import plan_cm_to_mesh_m
from .schema import Level, Model, Registration, Room, load_model, save_model

CM_TO_M = 0.01
DOWN_FACING = -0.85
UP_FACING = 0.85
MIN_FACES = 20
TRUNCATION_MARGIN_M = 0.15


def _classified_faces(mesh_path: str):
    scene = trimesh.load(mesh_path, process=False)
    geoms = list(scene.geometry.values()) if hasattr(scene, "geometry") else [scene]
    down, up = [], []
    for g in geoms:
        # A loaded scene can also hold Path3D and PointCloud entries, which have
        # no faces; vstack on the empty result would raise rather than say why.
        n = getattr(g, "face_normals", None)
        c = getattr(g, "triangles_center", None)
        if n is None or c is None:
            continue
        down.append(c[n[:, 2] < DOWN_FACING])
        up.append(c[n[:, 2] > UP_FACING])
    if not down:
        raise SystemExit(f"no triangle geometry in {mesh_path}")
    return np.vstack(down), np.vstack(up)


def mesh_top_z(down: np.ndarray, up: np.ndarray) -> float:
    """The highest face centre in the mesh, from whichever classes have one.

    A mesh can hold faces and still hold none facing a given way -- a partial
    capture of a stairwell, or a export that lost its floors. Taking `.max()`
    of both classes then raises on an empty array, which loses the report that
    would have said every room was unseen and why.
    """
    tops = [float(arr[:, 2].max()) for arr in (down, up) if len(arr)]
    if not tops:
        raise SystemExit("the mesh has no up- or down-facing faces to measure from")
    return max(tops)


class Measured(NamedTuple):
    """One room's ceiling, and whether the mesh could actually say.

    THREE ANSWERS. `measured` is a height; `truncated` is a LOWER BOUND, from a
    scan that stopped before the room did; `unseen` is the mesh having too
    little above that footprint to answer at all -- a room photographed mostly
    from above, or a plan and mesh in different frames.

    Only `measured` may be written to a model. A truncated p95 is known to be
    short and an unseen room has nothing behind it, and either written as a
    height is a fabrication that every stage downstream treats as a
    measurement: `build` puts furniture at it and `render` raytraces from it.
    """

    verdict: Literal["measured", "truncated", "unseen"]
    floor_z_m: float | None
    p50_cm: float | None
    p95_cm: float | None
    ceiling_faces: int
    floor_faces: int

    @property
    def writable(self) -> bool:
        return self.verdict == "measured"


def room_in_mesh_frame(room: Room, reg: Registration | None) -> Polygon:
    """This room's footprint where the mesh has it, in metres.

    Through `plan_cm_to_mesh_m` rather than by writing the rotation out again:
    two hand-written copies of one transform is how a sign error survives.
    """
    if reg is None:
        return Polygon([(x * CM_TO_M, y * CM_TO_M) for x, y in room.points])
    placed = plan_cm_to_mesh_m(np.asarray(room.points, dtype=float), reg)
    return Polygon([(x, y) for x, y in placed])


def measure_room(room: Room, down: np.ndarray, up: np.ndarray, mesh_top: float, *,
                 registration: Registration | None = None,
                 min_faces: int = MIN_FACES,
                 truncation_margin_m: float = TRUNCATION_MARGIN_M) -> Measured:
    """This room's ceiling height above its own floor, or why not."""
    poly = room_in_mesh_frame(room, registration)
    if not poly.is_valid:
        poly = poly.buffer(0)

    c_in, f_in = _inside(down, poly), _inside(up, poly)
    if len(c_in) < min_faces or len(f_in) < min_faces:
        return Measured("unseen", None, None, None, len(c_in), len(f_in))

    fz = float(np.percentile(f_in[:, 2], 5))
    c50 = float(np.percentile(c_in[:, 2], 50))
    c95 = float(np.percentile(c_in[:, 2], 95))
    verdict: Literal["measured", "truncated", "unseen"] = (
        "truncated" if c95 >= mesh_top - truncation_margin_m else "measured")
    return Measured(verdict, fz, (c50 - fz) / CM_TO_M, (c95 - fz) / CM_TO_M,
                    len(c_in), len(f_in))


def measure(model: Model, down: np.ndarray, up: np.ndarray,
            mesh_top: float) -> list[tuple[Level, Room, Measured]]:
    """Every room, IN THE ORDER THE MODEL HOLDS THEM.

    Room names are not unique in a split model -- a piece is named for the area
    it belongs to, and several pieces can belong to one area. One real level
    carries three rooms called `hallway`, at 220, 323 and 264 cm, and two
    called `stairwell` at 446 and 303. So anything keyed by name assigns two
    thirds of them the wrong height, and does it silently. The room object
    itself is the identity; nothing here may reduce it to a label.
    """
    return [(lv, r, measure_room(r, down, up, mesh_top,
                                 registration=lv.registration))
            for lv in model.levels for r in lv.rooms]


class Change(NamedTuple):
    """One room's height, before and after, and what had to give way."""

    room: Room
    before_cm: float | None
    after_cm: float
    cleared_low_cm: float | None


def write_back(measurements: list[tuple[Level, Room, Measured]]) -> list[Change]:
    """Put the measured heights on the rooms, and say what moved.

    `ceiling_high_cm` only. The p50 is reported for a person to read and is NOT
    written, because down-facing faces include the undersides of tables,
    worktops and stair soffits -- so the median is furniture height in exactly
    the rooms that have furniture. `lights.elevation_for` prefers
    `ceiling_low_cm`, so writing a contaminated median there would hang the
    lamp over the dining table at the height of the dining table.

    A STALE LOW IS CLEARED RATHER THAN LEFT TO CONTRADICT THE MEASUREMENT.
    Polycam's own figure for a room is often above what the mesh measures --
    four rooms of one real level came out 4 to 12 cm over -- and leaving it
    would make `ceiling_low_cm` exceed `ceiling_high_cm`, which is not a
    ceiling range at all but two unrelated numbers. Since `elevation_for` reads
    the low one FIRST, leaving it is also how this whole stage would appear to
    run and change nothing for exactly the rooms it corrected.

    A low that still fits under the measurement is left alone: that is a
    genuine range, and nothing here measured the bottom of one.

    A room this could not measure is left exactly as it was found, including
    left as None. An absent height is a question; a fabricated one is an answer
    nobody can tell from a real measurement.
    """
    changes = []
    for _lv, room, m in measurements:
        if not m.writable:
            continue
        assert m.p95_cm is not None
        after = round(m.p95_cm, 1)
        stale = (room.ceiling_low_cm
                 if room.ceiling_low_cm is not None and room.ceiling_low_cm > after
                 else None)
        changes.append(Change(room, room.ceiling_high_cm, after, stale))
        room.ceiling_high_cm = after
        if stale is not None:
            room.ceiling_low_cm = None
    return changes


def _inside(pts: np.ndarray, poly: Polygon) -> np.ndarray:
    """Points within a polygon. Bounding box first -- per-point shapely at
    100k faces is far too slow to be useful."""
    minx, miny, maxx, maxy = poly.bounds
    box = ((pts[:, 0] >= minx) & (pts[:, 0] <= maxx)
           & (pts[:, 1] >= miny) & (pts[:, 1] <= maxy))
    cand = pts[box]
    if not len(cand):
        return cand
    keep = np.fromiter((poly.contains(Point(p[0], p[1])) for p in cand[:, :2]),
                       dtype=bool, count=len(cand))
    return cand[keep]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("model")
    ap.add_argument("mesh")
    ap.add_argument("-o", "--out", help="write the measured heights back into a "
                                        "copy of the model at this path")
    args = ap.parse_args()

    model = load_model(args.model)
    down, up = _classified_faces(args.mesh)
    mesh_top = mesh_top_z(down, up)
    measurements = measure(model, down, up, mesh_top)

    print(f"ceiling faces {len(down):,}   floor faces {len(up):,}   "
          f"mesh top z {mesh_top:.2f} m\n")
    print(f"{'room':<16}{'floor z':>9}{'height p50':>12}{'height p95':>12}   note")
    print("-" * 72)

    for _lv, room, m in measurements:
        name = str(room.name)
        if m.verdict == "unseen":
            print(f"{name:<16}  NOT SEEN -- too few faces (ceiling "
                  f"{m.ceiling_faces}, floor {m.floor_faces}). Outside the mesh, "
                  f"photographed from above, or plan and mesh in different frames")
            continue
        assert m.floor_z_m is not None and m.p50_cm is not None
        assert m.p95_cm is not None
        note = ("TRUNCATED - scan stopped here, treat as a LOWER BOUND"
                if m.verdict == "truncated" else "")
        print(f"{name:<16}{m.floor_z_m:>9.2f}{m.p50_cm:>11.0f}cm"
              f"{m.p95_cm:>11.0f}cm   {note}")

    if not args.out:
        refusals = sum(1 for _, _, m in measurements if not m.writable)
        if refusals:
            print(f"\n  {refusals} room(s) above could not be measured. Pass -o to "
                  "write\n  the ones that could into a model; the rest keep "
                  "whatever height they have.")
        return

    changes = write_back(measurements)
    save_model(model, args.out)
    print(f"\nwrote {args.out}")
    print(f"  {len(changes)} room(s) given a measured ceiling:")
    for change in changes:
        was = "unset" if change.before_cm is None else f"{change.before_cm:.0f} cm"
        note = ("" if change.cleared_low_cm is None else
                f"   (cleared a stale low of {change.cleared_low_cm:.0f} cm, which "
                f"the measurement contradicts)")
        print(f"    {str(change.room.name):<16} {was:>9}  ->  "
              f"{change.after_cm:.0f} cm{note}")

    for _lv, room, m in measurements:
        if m.writable:
            continue
        # Named, never silent. A room left without a height is one `lights` will
        # fall back to the LEVEL's ceiling for -- which on a level containing a
        # void is how a lounge lamp ends up hanging four and a half metres up.
        why = ("the scan stopped before the room did, so its height is only a "
               "lower bound" if m.verdict == "truncated"
               else "the mesh has too little above it to measure")
        keeps = ("no height at all" if room.ceiling_high_cm is None
                 else f"its existing {room.ceiling_high_cm:.0f} cm")
        print(f"    {str(room.name):<16} NOT WRITTEN -- {why}; keeps {keeps}")


if __name__ == "__main__":
    main()
