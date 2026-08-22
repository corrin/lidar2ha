#!/usr/bin/env python3
"""Recover floor elevations from a Polycam mesh.

The floor-plan DXF is 2D, so it says nothing about how far one floor sits above
another. This is the missing third axis, and it matters: a split level modelled
with both floors at elevation 0 renders as one flat plane.

Method: keep only up-facing horizontal faces (these are floors, not ceilings --
ceilings face down), histogram their area by Z, and report the peaks. Each peak
is a floor slab, weighted by how much surface sits at that height, so clutter
and noise cannot outvote a real floor.

Requires the mesh to have been exported with 'Mesh up axis: Z axis up'.

Run from PowerShell (see CLAUDE.md). Usage:
    python floor_elevations.py <path to .obj> [--bin 0.05] [--min-area 1.0]
"""

import argparse

import numpy as np
import trimesh


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("mesh")
    ap.add_argument("--bin", type=float, default=0.05,
                    help="histogram bin height in metres")
    ap.add_argument("--min-area", type=float, default=1.0,
                    help="ignore candidate slabs with less area than this, m^2")
    args = ap.parse_args()

    m = trimesh.load(args.mesh, force="mesh", process=False)

    n = m.face_normals
    a = m.area_faces
    # Up-facing only: floors. Ceilings have nz <= -0.9 and are excluded.
    up = n[:, 2] > 0.9
    if not up.any():
        raise SystemExit("no up-facing horizontal faces -- was the mesh exported Z-up?")

    z = m.triangles_center[up, 2]
    area = a[up]

    lo, hi = z.min(), z.max()
    bins = np.arange(lo, hi + args.bin, args.bin)
    hist, edges = np.histogram(z, bins=bins, weights=area)

    print(f"up-facing area total : {area.sum():.2f} m^2")
    print(f"z range              : {lo:.2f} .. {hi:.2f} m")
    print()

    # A slab is a local maximum that carries real area.
    peaks = []
    for i, val in enumerate(hist):
        if val < args.min_area:
            continue
        left = hist[i - 1] if i > 0 else 0.0
        right = hist[i + 1] if i + 1 < len(hist) else 0.0
        if val >= left and val >= right:
            peaks.append((float(val), float((edges[i] + edges[i + 1]) / 2)))

    peaks.sort(reverse=True)
    print("CANDIDATE FLOOR SLABS (by area)")
    for area_at, z_at in peaks:
        print(f"  z = {z_at:7.3f} m   area = {area_at:6.2f} m^2")

    if len(peaks) >= 2:
        by_z = sorted(peaks, key=lambda p: p[1])
        print()
        print("PAIRWISE RISES (lowest slab as datum)")
        base_z = by_z[0][1]
        for area_at, z_at in by_z:
            print(f"  z = {z_at:7.3f} m   rise = {(z_at - base_z) * 100:6.1f} cm"
                  f"   area = {area_at:6.2f} m^2")


if __name__ == "__main__":
    main()
