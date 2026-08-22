#!/usr/bin/env python3
"""Top-down photograph of the floor, built from the mesh's own texture.

A room boundary is often invisible in geometry and obvious underfoot: wood gives
way to carpet, a step changes level, a gate stands in a doorway. The scan
already knows all of this -- the atlas holds the floor's real colour and the
mesh holds its height -- it is just never looked at from above.

This renders exactly that: every up-facing face, projected orthographically onto
the XY plane and painted with the colour the camera saw. Where the flooring
changes material, the seam is visible as a colour edge.

Two views are produced:
  <out>.png        floor colour, as photographed
  <out>_height.png the same faces shaded by height, so a step reads as a band

Coordinates are the MESH frame in metres, with a grid, because that is the frame
the mesh and its registration live in.

Usage:
    python -m scan2ha.floormap mesh.obj -o floor
"""

from __future__ import annotations

import argparse

import numpy as np
import trimesh
from PIL import Image, ImageDraw

PX_PER_M = 100          # 1 cm per pixel
UP_FACING = 0.85        # nz above this is floor-ish
MARGIN = 60


def load(mesh_path):
    scene = trimesh.load(mesh_path, process=False)
    return list(scene.geometry.values()) if hasattr(scene, "geometry") else [scene]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("mesh")
    ap.add_argument("-o", "--out", default="floor_map")
    ap.add_argument("--max-height", type=float, default=1.2,
                    help="metres above the lowest floor to include; excludes "
                         "table tops and worktops, which are also up-facing")
    args = ap.parse_args()

    geoms = load(args.mesh)

    # Gather up-facing faces with their colours, in one pass.
    pts, cols, zs = [], [], []
    for g in geoms:
        vis = getattr(g, "visual", None)
        uv = getattr(vis, "uv", None)
        mat = getattr(vis, "material", None)
        img = getattr(mat, "image", None)
        n = g.face_normals
        sel = np.where(n[:, 2] > UP_FACING)[0]
        if len(sel) == 0:
            continue
        centers = g.triangles_center[sel]
        pts.append(centers[:, :2])
        zs.append(centers[:, 2])
        if uv is not None and img is not None:
            atlas = np.asarray(img.convert("RGB"))
            ah, aw = atlas.shape[:2]
            c = uv[g.faces[sel]].mean(axis=1)
            sx = np.clip((c[:, 0] * (aw - 1)).astype(int), 0, aw - 1)
            sy = np.clip(((1 - c[:, 1]) * (ah - 1)).astype(int), 0, ah - 1)
            cols.append(atlas[sy, sx])
        else:
            cols.append(np.full((len(sel), 3), 160, dtype=np.uint8))

    if not pts:
        raise SystemExit("no up-facing faces -- was the mesh exported Z-up?")

    pts = np.vstack(pts)
    cols = np.vstack(cols)
    zs = np.concatenate(zs)

    # Drop anything well above the floor: worktops and tables face up too.
    floor_z = np.percentile(zs, 2)
    keep = zs < floor_z + args.max_height
    pts, cols, zs = pts[keep], cols[keep], zs[keep]
    print(f"floor faces      : {len(pts):,}  (lowest z {floor_z:.2f} m)")

    min_xy = pts.min(axis=0)
    max_xy = pts.max(axis=0)
    span = max_xy - min_xy
    W = int(span[0] * PX_PER_M) + 2 * MARGIN
    H = int(span[1] * PX_PER_M) + 2 * MARGIN

    def to_px(xy):
        x = MARGIN + (xy[:, 0] - min_xy[0]) * PX_PER_M
        # Y up in the world, down in the image.
        y = H - MARGIN - (xy[:, 1] - min_xy[1]) * PX_PER_M
        return x.astype(int), y.astype(int)

    xs, ys = to_px(pts)
    inside = (xs >= 0) & (xs < W) & (ys >= 0) & (ys < H)

    # Colour view -- what the floor actually looks like.
    canvas = np.full((H, W, 3), 255, dtype=np.uint8)
    canvas[ys[inside], xs[inside]] = cols[inside]

    # Height view -- a step shows as a band even when the material does not change.
    lo, hi = np.percentile(zs, [2, 98])
    t = np.clip((zs - lo) / max(hi - lo, 1e-6), 0, 1)
    ramp = np.stack([(t * 255), (80 + t * 60), (255 - t * 255)], axis=1).astype(np.uint8)
    hcanvas = np.full((H, W, 3), 255, dtype=np.uint8)
    hcanvas[ys[inside], xs[inside]] = ramp[inside]

    for name, arr in ((f"{args.out}.png", canvas), (f"{args.out}_height.png", hcanvas)):
        im = Image.fromarray(arr)
        d = ImageDraw.Draw(im)
        gx = np.ceil(min_xy[0])
        while gx <= max_xy[0]:
            x = int(MARGIN + (gx - min_xy[0]) * PX_PER_M)
            d.line([(x, 0), (x, H)], fill=(210, 210, 220))
            d.text((x + 3, 4), f"{gx:.0f}", fill=(90, 90, 100))
            gx += 1
        gy = np.ceil(min_xy[1])
        while gy <= max_xy[1]:
            y = int(H - MARGIN - (gy - min_xy[1]) * PX_PER_M)
            d.line([(0, y), (W, y)], fill=(210, 210, 220))
            d.text((4, y + 3), f"{gy:.0f}", fill=(90, 90, 100))
            gy += 1
        im.save(name)
        print(f"wrote {name}  ({W}x{H})  grid = 1 m, mesh coordinates")

    print(f"  floor z range  : {lo:.2f} .. {hi:.2f} m")


if __name__ == "__main__":
    main()
