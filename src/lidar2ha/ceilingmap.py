#!/usr/bin/env python3
"""Photograph the ceiling from below, and find the light fittings in it.

Placing a light at a room's centre is a guess. The scan already knows where the
fittings are: the atlas holds the ceiling's real appearance, and a lit or
white-shaded fixture is markedly brighter than the plaster around it.

This renders the ceiling as seen from underneath -- the mirror image of
floormap, using DOWN-facing faces -- and then looks for bright compact blobs in
it. Each blob is a candidate fitting, reported in mesh coordinates so it can be
turned into a light placement.

Brightness alone is not enough: a window in a sloped ceiling, or a blown-out
patch of scan, is also bright. So candidates are scored on being brighter than
their immediate surroundings AND compact, and every one is reported for a human
to confirm rather than used automatically.

An investigation, not a pipeline stage -- if it earns its place, the capability
belongs in lidar2ha's floormap alongside the floor view.
"""

from __future__ import annotations

import argparse

import numpy as np
import trimesh
from PIL import Image, ImageDraw, ImageFilter

PX_PER_M = 100
DOWN_FACING = -0.80
MARGIN = 40


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("mesh")
    ap.add_argument("-o", "--out", default="ceiling_map")
    ap.add_argument("--min-height", type=float, default=1.6,
                    help="metres above the lowest floor; excludes table undersides")
    args = ap.parse_args()

    scene = trimesh.load(args.mesh, process=False)
    geoms = list(scene.geometry.values()) if hasattr(scene, "geometry") else [scene]

    pts, cols = [], []
    for g in geoms:
        vis = getattr(g, "visual", None)
        uv = getattr(vis, "uv", None)
        mat = getattr(vis, "material", None)
        img = getattr(mat, "image", None)
        n = getattr(g, "face_normals", None)
        if n is None or uv is None or img is None:
            continue
        sel = np.where(n[:, 2] < DOWN_FACING)[0]
        if len(sel) == 0:
            continue
        pts.append(g.triangles_center[sel])
        atlas = np.asarray(img.convert("RGB"))
        ah, aw = atlas.shape[:2]
        c = uv[g.faces[sel]].mean(axis=1)
        sx = np.clip((c[:, 0] * (aw - 1)).astype(int), 0, aw - 1)
        sy = np.clip(((1 - c[:, 1]) * (ah - 1)).astype(int), 0, ah - 1)
        cols.append(atlas[sy, sx])

    pts = np.vstack(pts)
    cols = np.vstack(cols)

    base = np.percentile(pts[:, 2], 2)
    keep = pts[:, 2] > base + args.min_height
    pts, cols = pts[keep], cols[keep]
    print(f"ceiling faces: {len(pts):,}  (above {base + args.min_height:.2f} m)")

    lo = pts[:, :2].min(axis=0)
    hi = pts[:, :2].max(axis=0)
    W = int((hi[0] - lo[0]) * PX_PER_M) + 2 * MARGIN
    H = int((hi[1] - lo[1]) * PX_PER_M) + 2 * MARGIN

    xs = (MARGIN + (pts[:, 0] - lo[0]) * PX_PER_M).astype(int)
    ys = (H - MARGIN - (pts[:, 1] - lo[1]) * PX_PER_M).astype(int)
    ok = (xs >= 0) & (xs < W) & (ys >= 0) & (ys < H)

    canvas = np.zeros((H, W, 3), dtype=np.uint8)
    canvas[ys[ok], xs[ok]] = cols[ok]

    im = Image.fromarray(canvas)
    # Close the gaps between scattered face centres so blobs are contiguous.
    filled = im.filter(ImageFilter.MaxFilter(5))
    grey = np.asarray(filled.convert("L")).astype(float)

    # Bright relative to the LOCAL surroundings, not to the whole image: a room
    # with a dark ceiling and a room with a white one both have fittings.
    local = np.asarray(Image.fromarray(grey.astype(np.uint8))
                       .filter(ImageFilter.GaussianBlur(30))).astype(float)
    excess = grey - local

    thresh = max(28.0, float(np.percentile(excess[grey > 0], 99.3)))
    mask = (excess > thresh) & (grey > 120)
    print(f"bright-spot threshold: +{thresh:.0f} over local background")

    # Connected components, without scipy.ndimage: flood fill on a coarse grid.
    ys_i, xs_i = np.nonzero(mask)
    seen = set()
    blobs = []
    pixels = set(zip(ys_i.tolist(), xs_i.tolist(), strict=True))
    for seed in list(pixels):
        if seed in seen:
            continue
        stack, comp = [seed], []
        seen.add(seed)
        while stack:
            cy, cx = stack.pop()
            comp.append((cy, cx))
            for dy in (-2, -1, 0, 1, 2):
                for dx in (-2, -1, 0, 1, 2):
                    p = (cy + dy, cx + dx)
                    if p in pixels and p not in seen:
                        seen.add(p)
                        stack.append(p)
        if 25 <= len(comp) <= 6000:
            cy = sum(p[0] for p in comp) / len(comp)
            cx = sum(p[1] for p in comp) / len(comp)
            blobs.append((len(comp), cx, cy))

    blobs.sort(reverse=True)
    d = ImageDraw.Draw(im)
    print(f"\n{len(blobs)} candidate fittings (mesh metres):")
    for i, (area, cx, cy) in enumerate(blobs[:20]):
        mx = lo[0] + (cx - MARGIN) / PX_PER_M
        my = lo[1] + (H - MARGIN - cy) / PX_PER_M
        d.ellipse([cx - 18, cy - 18, cx + 18, cy + 18], outline=(255, 0, 0), width=3)
        d.text((cx + 22, cy - 8), str(i), fill=(255, 0, 0))
        print(f"  {i:>2}  ({mx:7.2f}, {my:7.2f})   {area:>5} px")

    im.save(f"{args.out}.png")
    print(f"\nwrote {args.out}.png  ({W}x{H})")


if __name__ == "__main__":
    main()
