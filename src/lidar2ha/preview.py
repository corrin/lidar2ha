#!/usr/bin/env python3
"""Draw a model's plan with a coordinate grid, so seams can be placed.

Splitting an over-merged room means naming two points to cut between, and those
points have to be read off something. The scanner's own floor-plan image has no
usable axes, and the model's coordinates are what the tooling consumes -- so
this renders the model itself: walls, room outlines, names, measured ceilings,
and a metre grid labelled in centimetres.

This is also the review artefact for the import and rooms stages: it is the
picture to answer "do these rooms look like your house?" against.

Usage:
    python -m lidar2ha.preview model.json -o plan.png
"""

from __future__ import annotations

import argparse

from PIL import Image, ImageDraw

from .schema import load_model

MARGIN = 90
GRID_CM = 100
TARGET_PX = 1150

# Distinct enough to tell adjacent rooms apart, pale enough for walls to read.
FILLS = [(255, 236, 214), (222, 240, 255), (232, 255, 232), (255, 226, 240),
         (240, 232, 255), (255, 250, 214), (226, 255, 250), (245, 245, 245)]


def render(model, out_path: str) -> tuple[int, int]:
    min_x, min_y, max_x, max_y = model.bounds()
    span_x, span_y = max_x - min_x, max_y - min_y
    scale = (TARGET_PX - 2 * MARGIN) / max(span_x, span_y, 1.0)

    W = int(span_x * scale) + 2 * MARGIN
    H = int(span_y * scale) + 2 * MARGIN

    def px(x, y):
        # Y is flipped for DISPLAY only. Coordinates read off this image are
        # model coordinates, which is what seams and seeds want.
        return (MARGIN + (x - min_x) * scale, H - MARGIN - (y - min_y) * scale)

    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)

    gx = int(min_x // GRID_CM) * GRID_CM
    while gx <= max_x:
        x0, _ = px(gx, min_y)
        d.line([(x0, MARGIN), (x0, H - MARGIN)], fill=(232, 232, 238))
        d.text((x0 - 12, H - MARGIN + 8), f"{int(gx)}", fill=(120, 120, 130))
        gx += GRID_CM
    gy = int(min_y // GRID_CM) * GRID_CM
    while gy <= max_y:
        _, y0 = px(min_x, gy)
        d.line([(MARGIN, y0), (W - MARGIN, y0)], fill=(232, 232, 238))
        d.text((10, y0 - 6), f"{int(gy)}", fill=(120, 120, 130))
        gy += GRID_CM

    for i, lv in enumerate(model.levels):
        for j, r in enumerate(lv.rooms):
            pts = [px(x, y) for x, y in r.points]
            d.polygon(pts, fill=FILLS[(i * 3 + j) % len(FILLS)],
                      outline=(150, 150, 160))
            cx = sum(p[0] for p in pts) / len(pts)
            cy = sum(p[1] for p in pts) / len(pts)
            d.text((cx - 30, cy - 8), str(r.name or "?"), fill=(30, 30, 40))
            if r.ceiling_high_cm:
                d.text((cx - 30, cy + 6), f"{r.ceiling_high_cm:.0f} cm",
                       fill=(110, 110, 120))

    # Walls last and thick, so they read over the room fills.
    for lv in model.levels:
        for w in lv.walls:
            d.line([px(w.x_start, w.y_start), px(w.x_end, w.y_end)],
                   fill=(40, 40, 55), width=4)

    # Vertices marked, because a seam usually starts at one.
    for lv in model.levels:
        for r in lv.rooms:
            for x, y in r.points:
                cx, cy = px(x, y)
                d.ellipse([cx - 3, cy - 3, cx + 3, cy + 3], fill=(200, 40, 40))

    d.text((MARGIN, 18), f"grid = {GRID_CM} cm; labels are model coordinates",
           fill=(30, 30, 40))
    img.save(out_path)
    return W, H


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("model")
    ap.add_argument("-o", "--out", default="plan.png")
    args = ap.parse_args()

    model = load_model(args.model)
    W, H = render(model, args.out)
    min_x, min_y, max_x, max_y = model.bounds()

    print(f"wrote {args.out}  ({W}x{H})")
    print(f"  x: {min_x:.0f} .. {max_x:.0f} cm      y: {min_y:.0f} .. {max_y:.0f} cm")
    for lv in model.levels:
        for r in lv.rooms:
            print(f"  room {str(r.name):<16} {len(r.points):>2} pts")


if __name__ == "__main__":
    main()
