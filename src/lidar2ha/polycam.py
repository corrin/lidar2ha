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
import numpy as np
from scipy.optimize import linear_sum_assignment

from .schema import Door, Level, Model, Room, Wall, save_model

M_TO_CM = 100.0

# One storey, in metres. Used two ways: rooms whose ceilings sit closer together
# than half of this are on the same floor, and a room spanning more than this is
# a shaft rather than a room with a tall ceiling.
#
# 2.7 m is a guess from one house, whose storeys came out 270 cm apart with
# ceilings of 210, 480 and 710 cm above the capture datum. What would move it: a
# building with a mezzanine or a split level closer than half a storey, which
# would merge two real floors -- the symptom is a level whose rooms disagree
# about their ceiling by more than a normal room-to-room variation.
STOREY_M = 2.7

# How far from a band's rooms a wall still counts as one of that band's. Half a
# metre: wide enough to catch a wall drawn along the outside of a room outline
# rather than through it, narrow enough not to reach the next room along.
# Measured, 0.1 to 0.6 m all give the same walls on the capture this was built
# for, so the answer is not sensitive to it -- 1.0 m starts pulling in
# neighbours and the fit degrades.
BAND_REACH_M = 0.5


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


def split_into_storeys(rooms, storey_m=STOREY_M):
    """One cluster's rooms grouped into the storeys they are actually on.

    `split_into_floors` separates the sheet's side-by-side layout, and on most
    captures that is the storey. On one it was not: walking a whole house in one
    go produced a cluster holding rooms whose ceilings sit at 210, 480 and 710
    cm -- three storeys stacked, a `Bedroom` at cx 2.22 m and an `Office` at
    2.45 m, 23 cm apart in plan and a storey apart in the building.

    POLYCAM REPORTS A CEILING ABOVE THE CAPTURE DATUM, not above the room's own
    floor, so the height IS the storey and the bands come out cleanly separated.
    `Floor N` is a real field and simply not that: it is where the sheet drew
    the room.

    A room taller than a storey belongs to no band. A stairwell is the one room
    that genuinely spans them, and its span identifies it without a mesh -- so
    it is returned separately for the caller to place and flag rather than
    being averaged into whichever band its midpoint happens to land in.

    Returns [(band_centre_m, rooms)], lowest first, and the shafts.
    """
    banded, shafts = [], []
    for order, room in enumerate(rooms):
        low, high = room.get("ceiling_low"), room.get("ceiling_high")
        if low is None or high is None:
            banded.append((None, order, room))
        elif high - low > storey_m:
            shafts.append(room)
        else:
            banded.append(((low + high) / 2, order, room))

    # A room with no ceiling reading cannot be placed by height. It stays with
    # the first band rather than becoming a storey of its own -- it is missing
    # evidence, not evidence of a separate floor.
    unknown = [(order, r) for mid, order, r in banded if mid is None]
    known = sorted(((mid, order, r) for mid, order, r in banded if mid is not None),
                   key=lambda t: t[0])

    bands: list[list[tuple[float, int, dict]]] = []
    for mid, order, room in known:
        if bands and mid - bands[-1][-1][0] < storey_m / 2:
            bands[-1].append((mid, order, room))
        else:
            bands.append([(mid, order, room)])

    # SORTED TO FIND THE BANDS, EMITTED IN THE ORDER THEY ARRIVED. Height order
    # is how a band is discovered and is not otherwise meaningful, and emitting
    # it would silently reshuffle the rooms of every single-band capture --
    # which is every capture that was already working.
    out = []
    for band in bands:
        centre = sum(m for m, _, _ in band) / len(band)
        out.append((centre, [r for _, _, r in sorted(band, key=lambda t: t[1])]))
    if unknown:
        rooms_unknown = [r for _, r in sorted(unknown, key=lambda t: t[0])]
        if out:
            out[0][1].extend(rooms_unknown)
        else:
            out = [(0.0, rooms_unknown)]
    return out, shafts


def band_footprint(rooms):
    """One band's rooms as a single shapely geometry, for distance tests."""
    from shapely.geometry import Polygon
    from shapely.ops import unary_union
    polys = [Polygon(r["points"]).buffer(0) for r in rooms if len(r["points"]) >= 3]
    return unary_union(polys) if polys else None


def walls_touching(walls, footprint, reach_m=BAND_REACH_M):
    """Every wall running along this band's rooms. NOT an exclusive partition.

    A WALL CAN BELONG TO MORE THAN ONE STOREY, and in a stacked capture most
    do: the building's outer envelope runs the full height, so it is a wall of
    the ground floor and of the top floor alike. Splitting it exclusively would
    give one storey the envelope and leave the others with the partitions only.

    Nor is there a rule that could split it. Polycam keys ceiling heights by
    ROOM, so a wall carries no height at all, and the storeys of one cluster
    are stacked -- a ground-floor bedroom wall and a top-floor office wall sit
    centimetres apart in plan. Measured, assigning exclusively by nearest
    centroid put the top band 22.1 cm from the storey it belongs to and by
    nearest footprint 20.5 cm, where taking every wall that touches puts it at
    4.3 cm.

    The cost is that an interior partition appears on two levels. That is a
    duplicate for `combine` to drop -- it already deduplicates walls two
    captures both drew -- and the alternative is a storey missing the walls
    that bound it.
    """
    from shapely.geometry import LineString
    if footprint is None:
        return list(walls)
    near = footprint.buffer(reach_m)
    return [w for w in walls
            if LineString([w["start"], w["end"]]).intersects(near)]


def doors_touching(doors, footprint, reach_m=BAND_REACH_M):
    """Doors are points, so this is a containment test rather than a crossing."""
    from shapely.geometry import Point
    if footprint is None:
        return list(doors)
    near = footprint.buffer(reach_m)
    return [d for d in doors if near.contains(Point(d["cx"], d["cy"]))]


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


def assign_room_labels(rooms, room_labels):
    """Match room polygons to sheet labels one-to-one.

    Taking each room's nearest label independently is the obvious approach and
    it is wrong: nothing stops two polygons claiming the same label, and nothing
    ensures every label gets used. On a capture with small rooms off a hallway
    that produced two rooms both named "Bathroom 1" and no Hallway at all --
    and a duplicate name is not merely cosmetic, because `rooms` keys by name
    and so keeps only the last of them.

    linear_sum_assignment gives the exact minimum-total-distance matching, so a
    room that "wants" a label another room wants more has to take its second
    choice instead of both winning. Matching across all floors at once also
    keeps a label from being spent on a room a floor away.

    Returns (unnamed_rooms, unused_labels) for the caller to report.
    """
    for r in rooms:
        r["name"] = None
    if not rooms or not room_labels:
        return list(rooms), [lbl["name"] for lbl in room_labels]

    cost = np.array([
        [(lbl["x"] - r["cx"]) ** 2 + (lbl["y"] - r["cy"]) ** 2 for lbl in room_labels]
        for r in rooms
    ])
    rows, cols = linear_sum_assignment(cost)
    for i, j in zip(rows, cols, strict=True):
        rooms[i]["name"] = room_labels[j]["name"]

    unnamed = [r for k, r in enumerate(rooms) if k not in set(rows)]
    unused = [lbl["name"] for j, lbl in enumerate(room_labels) if j not in set(cols)]
    return unnamed, unused


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
    ap.add_argument("--role", choices=("geometry", "fixtures"), default="geometry",
                    help="what this capture is for. A fixture pass is scanned with "
                         "every light on and the geometry sacrificed, so marking it "
                         "keeps its walls and floor heights out of the building")
    ap.add_argument("--default-height", type=float, default=2.4,
                    help="ceiling height in metres when the CSV has none")
    ap.add_argument("--storey-m", type=float, default=STOREY_M,
                    help="one storey in metres. Rooms whose ceilings sit closer "
                         "together than half of this are on the same floor, and "
                         "a room spanning more than it is a shaft. Raise it for "
                         "a building with tall storeys; lower it for a mezzanine "
                         "that is being merged into the floor below")
    ap.add_argument("--band-reach-m", type=float, default=BAND_REACH_M,
                    help="how far from a storey's rooms a wall still counts as "
                         "one of that storey's")
    args = ap.parse_args()

    # VALIDATED AT THE BOUNDARY, because a non-positive storey height does not
    # fail here -- it makes every room its own band, or none, and the model
    # that comes out is a plausible one with the wrong number of floors in it.
    for flag, value in (("--storey-m", args.storey_m),
                        ("--band-reach-m", args.band_reach_m),
                        ("--default-height", args.default_height)):
        if not value > 0:
            raise SystemExit(
                f"{flag} must be greater than zero, got {value}. These are "
                f"lengths in metres; a zero or negative one silently changes "
                f"how many storeys this capture appears to have.")

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

    unnamed, unused = assign_room_labels(
        [r for group in room_groups for r in group], room_labels)
    if unnamed or unused:
        # Silence here is what produced two "Bathroom 1" and no Hallway.
        print(f"WARNING: {len(unnamed)} room(s) got no label and {len(unused)} "
              f"label(s) went unused: {sorted(unused)}")
        print("  Unlabelled rooms fall back to their floor's name; check the "
              "room list below before continuing.")

    levels = []
    for i in range(n_floors):
        label = floor_labels[i]["name"] if i < len(floor_labels) else f"Floor {i + 1}"
        wg = wall_groups[i] if i < len(wall_groups) else []
        rg = room_groups[i] if i < len(room_groups) else []
        dg = door_groups[i] if i < len(door_groups) else []

        # Names were assigned one-to-one across every floor before this loop.
        # Attaching ceilings has to come after, because the CSV keys them by
        # room name.
        for r in rg:
            r["name"] = r["name"] or label
            low, high = ceilings.get(r["name"], (args.default_height,) * 2)
            r["ceiling_low"] = low
            r["ceiling_high"] = high

        # THE SHEET POSITION IS NOT ALWAYS THE STOREY. Split the cluster on
        # ceiling band before building anything: a level holding two storeys is
        # one `combine` fits as a rigid body and `compare` averages. Done here
        # rather than earlier because the band is read off the ceilings, which
        # were only attached just above.
        storeys, shafts = split_into_storeys(rg, args.storey_m)

        # The shaft goes on the lowest band and is flagged there. It is really
        # on all of them; dropping it would lose a room, and filing it quietly
        # would make a stairwell indistinguishable from a tall room.
        # A LABELLED FLOOR WITH NO ROOM POLYGONS STILL EXISTS. Polycam does not
        # always close a room, and a cluster of walls with none would otherwise
        # produce no bands and so no level at all -- the storey and every wall
        # on it vanishing because its floors were not traced.
        if not storeys and not shafts:
            storeys = [(0.0, [])]

        if shafts:
            if storeys:
                order = {id(r): i for i, r in enumerate(rg)}
                merged = sorted(storeys[0][1] + shafts,
                                key=lambda r: order.get(id(r), 0))
                storeys[0] = (storeys[0][0], merged)
            else:
                storeys = [(0.0, list(shafts))]

        if len(storeys) > 1:
            bands = ", ".join(f"{c * M_TO_CM:.0f}cm x{len(rs)}" for c, rs in storeys)
            print(f"WARNING: {label} holds {len(rg)} room(s) across "
                  f"{len(storeys)} ceiling bands: {bands}")
            print("  Polycam reports a ceiling above the CAPTURE DATUM, so the "
                  "band is the storey")
            print(f"  and {label!r} is only where the sheet drew them. Splitting "
                  f"it -- a level holding")
            print("  two storeys is one nothing downstream can fit.")
        for shaft in shafts:
            span = (shaft["ceiling_high"] - shaft["ceiling_low"]) * M_TO_CM
            print(f"  {str(shaft['name'])!r} spans {span:.0f}cm, more than a "
                  f"storey: a shaft, on no single")
            print("  floor. Kept on the lowest band and flagged.")

        for centre, band_rooms in storeys:
            if len(storeys) > 1:
                # Named for the band so two storeys off one cluster are tellable
                # apart. `Floor N` is kept because it is a real field from the
                # file -- it is simply where the sheet drew the room.
                name = f"{label} ({centre * M_TO_CM:.0f}cm)"
                cut_from = label
                footprint = band_footprint(band_rooms)
                wg_band = walls_touching(wg, footprint, args.band_reach_m)
                dg_band = doors_touching(dg, footprint, args.band_reach_m)
            else:
                name, cut_from, wg_band, dg_band = label, None, wg, dg

            levels.append(_build_level(args, name, band_rooms, wg_band, dg_band,
                                       cut_from))
    _report(args, levels)


def _build_level(args, name, rg, wg, dg, from_level=None):
    """One Level from one storey's rooms, walls and doors.

    `from_level` is the Polycam level this band was cut out of, and None when
    the level was not split. `rooms` reads it to tell which levels share a
    frame, so it is the fact rather than the `(480cm)` in the name.
    """

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

    return Level(
        name=name,
        from_level=from_level,
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
    )


def _report(args, levels):
    """Write the model, then say what is in it."""
    storey_cm = getattr(args, "storey_m", STOREY_M) * M_TO_CM
    model = Model(source=Path(args.dxf).name, role=args.role, units="cm", levels=levels)
    save_model(model, args.out)

    print(f"wrote {args.out}  (role: {model.role})")
    if model.role == "fixtures":
        print("  A fixture pass. Its walls register onto its own mesh, which is what")
        print("  placefixtures needs, but its geometry and floor heights are not")
        print("  evidence about the building and later stages will refuse them.")
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
            mark = ("   SHAFT -- spans storeys, on no single floor"
                    if lo is not None and hi is not None
                    and hi - lo > storey_cm else "")
            print(f"      {str(r.name):<14} {len(r.points):>2} pts   "
                  f"ceiling {span}{mark}")


if __name__ == "__main__":
    main()
