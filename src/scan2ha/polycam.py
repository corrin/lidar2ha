#!/usr/bin/env python3
"""Parse a Polycam floor-plan DXF + CSV into an intermediate JSON model.

This is the middle step of the pipeline in 3D_Floorplan_Pipeline_Test.md:

    Polycam DXF/CSV  ->  [this script]  ->  home.json  ->  GenHome.java  ->  .sh3d

Only the mapping is ours. Polycam produces the geometry; Sweet Home 3D's own
classes write the file.

What Polycam gives us, and how it is read here
----------------------------------------------
* Units are metres ($INSUNITS = 6). Sweet Home 3D works in centimetres, so
  everything is multiplied by 100 on the way out.
* `Poly-Walls` holds each wall TWICE, as identical polylines. Deduplicated by
  exact point sequence.
* A wall is a 7-point polyline (the last point repeats the first) describing
  the wall's OUTLINE, not its centreline. The vertex order is:
      p0 = midpoint of end-cap A      <- centreline start
      p1, p2 = one long side
      p3 = midpoint of end-cap B      <- centreline end
      p4, p5 = the other long side
  So the centreline is simply (p0, p3), and the thickness is twice the
  perpendicular distance from that line to p1. This holds for diagonal walls,
  where an axis-aligned bounding box would not.
* Both floors share one sheet, laid out side by side with a gap between them.
  Floors are separated by clustering entity centroids on X and splitting at the
  largest gaps -- the number of clusters comes from the count of `Floor Label`
  texts, so this generalises past two floors. Nothing is hard-coded to this
  particular capture.
* Ceiling heights come from the CSV, which the DXF has no room for.

"""

import argparse
import csv
import math
import re
from pathlib import Path

import ezdxf

from .schema import Door, Level, Model, Room, Wall, save_model

M_TO_CM = 100.0


def strip_mtext(raw):
    """Polycam wraps MTEXT in formatting codes, e.g. '\\A1;Living Room'."""
    txt = re.sub(r"\\[A-Za-z][^;\\]*;", "", raw)
    txt = re.sub(r"[{}]", "", txt)
    return txt.strip()


def points_of(entity):
    return [(round(p[0], 4), round(p[1], 4)) for p in entity.get_points()]


def centreline_and_thickness(pts):
    """Return (start, end, thickness) for a Polycam wall outline.

    pts is the 7-point outline; p0 and p3 are the end-cap midpoints, so they
    ARE the centreline. Thickness is twice the perpendicular distance from that
    centreline to a corner.
    """
    if len(pts) < 6:
        return None
    a, b, corner = pts[0], pts[3], pts[1]

    dx, dy = b[0] - a[0], b[1] - a[1]
    length = math.hypot(dx, dy)
    if length < 1e-9:
        return None

    ux, uy = dx / length, dy / length
    vx, vy = corner[0] - a[0], corner[1] - a[1]
    perp = abs(vx * -uy + vy * ux)

    return a, b, perp * 2.0


def centroid(pts):
    return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))


def split_into_floors(items, n_floors):
    """Cluster items on X, splitting at the largest gaps.

    n_floors comes from the DXF's own 'Floor Label' count, so the split is
    driven by the file rather than by an assumption about this house.
    """
    if n_floors <= 1 or not items:
        return [items] + [[] for _ in range(n_floors - 1)]

    ordered = sorted(items, key=lambda it: it["cx"])
    if len(ordered) < n_floors:
        return [[it] for it in ordered] + [[]] * (n_floors - len(ordered))

    gaps = [(ordered[i + 1]["cx"] - ordered[i]["cx"], i) for i in range(len(ordered) - 1)]
    gaps.sort(reverse=True)
    cut_after = sorted(i for _, i in gaps[: n_floors - 1])

    clusters, start = [], 0
    for idx in cut_after:
        clusters.append(ordered[start:idx + 1])
        start = idx + 1
    clusters.append(ordered[start:])
    return clusters


def _parse_height(value):
    """Metres from a Polycam ceiling-height cell.

    Sloped and double-height spaces are reported as a RANGE, e.g. '3.2 - 4.7'.
    Returns (low, high); equal for a flat ceiling. Returning the high value is
    what matters downstream -- walls must reach the highest point or the room
    renders with a hole where its ceiling should be.
    """
    text = (value or "").strip().replace("m", "").strip()
    parts = [p.strip() for p in text.split("-") if p.strip()]
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return None
    if not nums:
        return None
    return min(nums), max(nums)


def read_room_ceilings(csv_path):
    """Map room name -> (low, high) ceiling height in metres.

    Ceiling height is a property of the ROOM, not the level: one Polycam CSV
    here reports a 2.2 m laundry beside a 3.2-4.7 m double-height dining space.
    Collapsing those to one number per level destroys exactly the geometry that
    makes cross-floor light spill worth rendering.

    Two CSV shapes exist in the wild. Multi-floor captures carry a leading
    'Floor' column; single-floor ones do not. Keying on 'Room' works for both.
    """
    heights = {}
    if not csv_path or not Path(csv_path).exists():
        return heights
    with open(csv_path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            if "Ceiling height" not in (row.get("Description") or ""):
                continue
            room = (row.get("Room") or "").strip()
            parsed = _parse_height(row.get("Value"))
            if room and parsed:
                heights[room] = parsed
    return heights


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("dxf")
    ap.add_argument("--csv")
    ap.add_argument("-o", "--out", default="home.json")
    ap.add_argument("--default-height", type=float, default=2.4,
                    help="ceiling height in metres when the CSV has none")
    args = ap.parse_args()

    msp = ezdxf.readfile(args.dxf).modelspace()

    floor_labels = [
        {"name": strip_mtext(e.dxf.text), "x": e.dxf.insert.x, "y": e.dxf.insert.y}
        for e in msp.query('MTEXT[layer=="Floor Label"]')
    ]
    floor_labels.sort(key=lambda f: f["x"])
    n_floors = max(1, len(floor_labels))

    room_labels = [
        {"name": strip_mtext(e.dxf.text), "x": e.dxf.insert.x, "y": e.dxf.insert.y}
        for e in msp.query('MTEXT[layer=="Poly-RoomLabels"]')
    ]

    # --- walls, deduplicated -------------------------------------------------
    seen, walls = set(), []
    for e in msp.query('LWPOLYLINE[layer=="Poly-Walls"]'):
        pts = points_of(e)
        key = tuple(pts)
        if key in seen:
            continue
        seen.add(key)
        cl = centreline_and_thickness(pts)
        if not cl:
            continue
        a, b, thickness = cl
        walls.append({
            "start": a,
            "end": b,
            "thickness": thickness,
            "cx": (a[0] + b[0]) / 2,
            "cy": (a[1] + b[1]) / 2,
        })

    # --- rooms ---------------------------------------------------------------
    rooms = []
    for e in msp.query('LWPOLYLINE[layer=="Poly-Rooms"]'):
        pts = points_of(e)
        if pts and pts[0] == pts[-1]:
            pts = pts[:-1]
        cx, cy = centroid(pts)
        rooms.append({"points": pts, "cx": cx, "cy": cy})

    # --- doors ---------------------------------------------------------------
    # 2-point entities on this layer are swing arcs and jamb ticks, not
    # openings, so only polygons are kept.
    doors = []
    for e in msp.query('LWPOLYLINE[layer=="Poly-Doors"]'):
        pts = points_of(e)
        if len(pts) < 4:
            continue
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        cx, cy = centroid(pts)
        doors.append({
            "points": pts,
            "cx": cx,
            "cy": cy,
            "width": max(xs) - min(xs),
            "depth": max(ys) - min(ys),
        })

    ceilings = read_room_ceilings(args.csv)
    if args.csv and not ceilings:
        # Silence here would be worse than failure: every wall would quietly get
        # the default height and the model would look plausible but be wrong.
        print(f"WARNING: no ceiling heights parsed from {args.csv}. "
              f"Falling back to {args.default_height} m for every room.")

    wall_groups = split_into_floors(walls, n_floors)
    room_groups = split_into_floors(rooms, n_floors)
    door_groups = split_into_floors(doors, n_floors)

    levels = []
    for i in range(n_floors):
        label = floor_labels[i]["name"] if i < len(floor_labels) else f"Floor {i + 1}"
        wg = wall_groups[i] if i < len(wall_groups) else []
        rg = room_groups[i] if i < len(room_groups) else []
        dg = door_groups[i] if i < len(door_groups) else []

        # Name each room from the nearest room label on the sheet, then attach
        # its own ceiling height. Naming has to happen before heights, because
        # the CSV keys ceilings by room name.
        for r in rg:
            best, bestd = None, float("inf")
            for rl in room_labels:
                d = (rl["x"] - r["cx"]) ** 2 + (rl["y"] - r["cy"]) ** 2
                if d < bestd:
                    best, bestd = rl["name"], d
            r["name"] = best or label
            low, high = ceilings.get(r["name"], (args.default_height,) * 2)
            r["ceiling_low"] = low
            r["ceiling_high"] = high

        # A level's height is its tallest room -- anything less and a
        # double-height space is capped short.
        h = max((r["ceiling_high"] for r in rg), default=args.default_height)

        def wall_height(w, rooms_here=rg, fallback=h):
            """Height of the room a wall bounds, not a single level-wide value.

            A 2.2 m laundry and a 4.7 m void can share a level, so taking the
            nearest room's ceiling keeps each wall the right height. Ties and
            open edges fall back to the tallest room, which errs upward -- too
            tall merely hides behind the ceiling, too short leaves a gap light
            leaks through.
            """
            best, bestd = None, float("inf")
            for r in rooms_here:
                d = (r["cx"] - w["cx"]) ** 2 + (r["cy"] - w["cy"]) ** 2
                if d < bestd:
                    best, bestd = r, d
            return best["ceiling_high"] if best else fallback

        levels.append(Level(
            name=label,
            ceiling_height_cm=h * M_TO_CM,
            # The DXF is 2D, so it carries no floor elevation. Filled in
            # separately from the mesh -- see mesh.py.
            elevation_cm=None,
            walls=[
                Wall(
                    x_start=w["start"][0] * M_TO_CM,
                    y_start=w["start"][1] * M_TO_CM,
                    x_end=w["end"][0] * M_TO_CM,
                    y_end=w["end"][1] * M_TO_CM,
                    thickness=w["thickness"] * M_TO_CM,
                    height=wall_height(w) * M_TO_CM,
                )
                for w in wg
            ],
            rooms=[
                Room(
                    name=r.get("name"),
                    points=[(p[0] * M_TO_CM, p[1] * M_TO_CM) for p in r["points"]],
                    ceiling_low_cm=r["ceiling_low"] * M_TO_CM,
                    ceiling_high_cm=r["ceiling_high"] * M_TO_CM,
                    # A room whose ceiling is a range is sloped or double-height.
                    # These are the candidates for a void through the slab above.
                    sloped=r["ceiling_high"] - r["ceiling_low"] > 0.15,
                )
                for r in rg
            ],
            doors=[
                Door(
                    x=d["cx"] * M_TO_CM,
                    y=d["cy"] * M_TO_CM,
                    width=max(d["width"], d["depth"]) * M_TO_CM,
                )
                for d in dg
            ],
        ))

    model = Model(source=Path(args.dxf).name, units="cm", levels=levels)
    save_model(model, args.out)

    print(f"wrote {args.out}")
    for lv in model.levels:
        print(f"  {lv.name:<10} walls={len(lv.walls):>3} "
              f"rooms={len(lv.rooms)} doors={len(lv.doors)} "
              f"ceiling={lv.ceiling_height_cm:.0f}cm")
        for r in lv.rooms:
            lo, hi = r.ceiling_low_cm, r.ceiling_high_cm
            if hi is None:
                span = "unknown"
            elif r.sloped and lo is not None:
                span = f"{lo:.0f}-{hi:.0f}cm  SLOPED/DOUBLE-HEIGHT"
            else:
                span = f"{hi:.0f}cm"
            print(f"      {str(r.name):<14} {len(r.points):>2} pts   ceiling {span}")


if __name__ == "__main__":
    main()
