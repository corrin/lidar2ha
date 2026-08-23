#!/usr/bin/env python3
"""Find where the flooring changes, by walking the floor and watching it.

A room boundary can be a baby gate, a step, and a change from wood to carpet.
None of those is in the plan geometry, but all three are in the mesh:
colour comes from the atlas, height from the vertices. So instead of guessing a
seam, sweep a line across the floor and look for the row where the floor stops
being one thing and starts being another.

For each band across the sweep axis this reports the median floor height and
median colour, plus a change score combining the two. The largest score is the
threshold.

Colour distance is computed in a rough perceptual sense (weighting green most,
blue least), because wood-to-carpet is a hue change more than a brightness one
and plain RGB distance under-reads it.

Usage:
    python -m lidar2ha.thresholds mesh.obj --axis y
"""

from __future__ import annotations

import argparse

import numpy as np
import trimesh

UP_FACING = 0.85
BAND_M = 0.20


def colour_distance(a, b):
    """Weighted RGB distance -- closer to how a hue change actually reads."""
    d = a.astype(float) - b.astype(float)
    return float(np.sqrt((d[0] * 0.9) ** 2 + (d[1] * 1.2) ** 2 + (d[2] * 0.6) ** 2))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("mesh")
    ap.add_argument("--axis", choices=["x", "y"], default="y")
    ap.add_argument("--max-height", type=float, default=0.6,
                    help="metres above floor to include; excludes furniture tops")
    args = ap.parse_args()

    scene = trimesh.load(args.mesh, process=False)
    geoms = list(scene.geometry.values()) if hasattr(scene, "geometry") else [scene]

    pts, cols = [], []
    for g in geoms:
        vis, mat = getattr(g, "visual", None), None
        uv = getattr(vis, "uv", None)
        mat = getattr(vis, "material", None)
        img = getattr(mat, "image", None)
        sel = np.where(g.face_normals[:, 2] > UP_FACING)[0]
        if len(sel) == 0 or uv is None or img is None:
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

    floor_z = np.percentile(pts[:, 2], 2)
    keep = pts[:, 2] < floor_z + args.max_height
    pts, cols = pts[keep], cols[keep]

    axis = 0 if args.axis == "x" else 1
    v = pts[:, axis]
    edges = np.arange(v.min(), v.max() + BAND_M, BAND_M)

    rows = []
    for i in range(len(edges) - 1):
        m = (v >= edges[i]) & (v < edges[i + 1])
        if m.sum() < 40:
            continue
        rows.append({
            "at": (edges[i] + edges[i + 1]) / 2,
            "n": int(m.sum()),
            "z": float(np.median(pts[m, 2])),
            "rgb": np.median(cols[m], axis=0),
        })

    print(f"floor faces {len(pts):,}   sweeping {args.axis}   band {BAND_M} m\n")
    print(f"{'at (m)':>8}{'n':>7}{'z (m)':>9}  colour      dz(cm)  dcolour   score")
    print("-" * 66)

    best = []
    for i, r in enumerate(rows):
        if i == 0:
            dz = dc = 0.0
        else:
            dz = abs(r["z"] - rows[i - 1]["z"]) * 100
            dc = colour_distance(r["rgb"], rows[i - 1]["rgb"])
        # Both signals matter and either alone is weak: a step with no material
        # change, or a material change with no step, is still a boundary.
        score = dz / 5.0 + dc / 12.0
        hexc = "#{:02X}{:02X}{:02X}".format(*r["rgb"].astype(int))
        flag = "  <<<" if score > 3.0 else ""
        print(f"{r['at']:>8.2f}{r['n']:>7}{r['z']:>9.2f}  {hexc}  "
              f"{dz:>7.1f}  {dc:>7.1f}  {score:>6.1f}{flag}")
        if i:
            best.append((score, r["at"], dz, dc))

    best.sort(reverse=True)
    print("\nstrongest transitions:")
    for score, at, dz, dc in best[:5]:
        print(f"  {args.axis} = {at:6.2f} m   step {dz:5.1f} cm   "
              f"colour shift {dc:5.1f}   score {score:.1f}")


if __name__ == "__main__":
    main()
