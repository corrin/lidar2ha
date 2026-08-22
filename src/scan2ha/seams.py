#!/usr/bin/env python3
"""Cut an over-merged room in two along a seam.

The counterpart to `rooms.merge`. A scanner segments space by its own logic,
which sometimes fuses rooms a person -- and Home Assistant -- treat as separate.
One capture returned a den and a front entrance as a single 27.7 m2 room with
one 5.2 m ceiling, which erases the lower space's height and makes per-room
selection across captures impossible.

A seam is two points. The polygon is cut by the infinite line through them and
each side becomes a room. Ceilings are NOT inherited: a fused room reports one
height for two spaces, so each piece takes its own, which is the entire reason
for splitting. Measure them with `ceilings` afterwards rather than guessing.

Where to put the seam is not a matter of taste. `thresholds` sweeps the mesh
floor for the step and the flooring change that mark a real boundary; on the
capture above, a 19 cm step and a wood-to-carpet edge landed on the same line.

Usage:
    python -m scan2ha.seams model.json -o split.json --room "Living Room" \\
        --seam -246,-159.3 16.8,-173.1 --names stairwell den
"""

from __future__ import annotations

import argparse

from shapely.geometry import LineString, Polygon
from shapely.ops import split as shapely_split

from .schema import Room, load_model, save_model

MIN_PIECE_CM2 = 1_000     # 0.1 m2; below this it is a sliver, not a room
EXTEND_CM = 10_000        # push the seam well past the polygon so it cuts clean


def pair(text: str) -> tuple[float, float]:
    x, y = text.split(",")
    return float(x), float(y)


def split_room(poly: Polygon, a, b) -> list[Polygon]:
    """Cut a polygon by the infinite line through a and b."""
    if not poly.is_valid:
        poly = poly.buffer(0)

    dx, dy = b[0] - a[0], b[1] - a[1]
    n = (dx ** 2 + dy ** 2) ** 0.5
    if n < 1e-9:
        raise ValueError("seam endpoints are the same point")
    ux, uy = dx / n, dy / n

    seam = LineString([(a[0] - ux * EXTEND_CM, a[1] - uy * EXTEND_CM),
                       (b[0] + ux * EXTEND_CM, b[1] + uy * EXTEND_CM)])

    pieces = [p for p in shapely_split(poly, seam).geoms if p.area > MIN_PIECE_CM2]

    # Order by which side of the seam each piece lies on, so the caller's names
    # and ceilings attach predictably instead of by shapely's internal ordering.
    def side(p: Polygon) -> float:
        r = p.representative_point()
        return (r.x - a[0]) * uy - (r.y - a[1]) * ux

    return sorted(pieces, key=side)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("model")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--room", required=True)
    ap.add_argument("--seam", nargs=2, required=True, metavar="X,Y")
    ap.add_argument("--names", nargs=2, required=True,
                    help="names for the two pieces, ordered by side of the seam")
    ap.add_argument("--ceilings", nargs=2, type=float,
                    help="ceiling height in cm per piece; prefer measuring with "
                         "`ceilings` to asserting a number here")
    args = ap.parse_args()

    model = load_model(args.model)
    a, b = pair(args.seam[0]), pair(args.seam[1])
    found = False

    for lv in model.levels:
        target = next((r for r in lv.rooms if r.name == args.room), None)
        if target is None:
            continue
        found = True

        pieces = split_room(Polygon([(x, y) for x, y in target.points]), a, b)
        if len(pieces) != 2:
            raise SystemExit(
                f"seam produced {len(pieces)} piece(s), expected 2 -- "
                "does the line actually cross the room?")

        new_rooms = []
        for i, piece in enumerate(pieces):
            ceiling = args.ceilings[i] if args.ceilings else None
            new_rooms.append(Room(
                name=args.names[i],
                points=[(round(x, 1), round(y, 1))
                        for x, y in piece.exterior.coords[:-1]],
                scanner_name=target.scanner_name or target.name,
                ceiling_high_cm=ceiling,
                ceiling_low_cm=ceiling,
                source=target.source,
            ))
            print(f"  {args.names[i]:<14} {piece.area / 10_000:6.2f} m2   "
                  f"{len(new_rooms[-1].points):>2} pts   "
                  f"ceiling {ceiling if ceiling else 'unmeasured'}")

        lv.rooms = [r for r in lv.rooms if r is not target] + new_rooms

    if not found:
        raise SystemExit(f"no room named {args.room!r} in {args.model}")

    save_model(model, args.out)
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
