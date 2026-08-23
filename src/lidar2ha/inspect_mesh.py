#!/usr/bin/env python3
"""Measure a mesh export: size, extent, and how much of it is real surface.

The question this answers before anything downstream runs: is this scan worth
registering? A capture that came back mostly ragged edge, or in hundreds of
disconnected bodies, will register badly and texture worse, and it is cheaper to
know that here than to discover it in a render.

`wall_area_m2` is also what grades a capture against the scanner's own figure --
see `schema.Capture.wall_area_ratio`. Near 1.0 is a good scan; well under it
means the scan missed walls the floor plan believes exist, which is the
signature of a mirror or a room the phone never pointed at.

Usage:
    python -m lidar2ha.inspect_mesh mesh.obj
"""

from __future__ import annotations

import argparse

import numpy as np
import trimesh

# Z is up because the Polycam export sets 'Mesh up axis: Z'.
HORIZONTAL = 0.9
VERTICAL = 0.2


def measure(mesh_path: str) -> dict:
    """Summarise a mesh. Areas are m^2, extents metres."""
    m = trimesh.load(mesh_path, force="mesh", process=False)
    if not isinstance(m, trimesh.Trimesh):
        raise SystemExit(f"{mesh_path} holds no triangle geometry")

    normals = m.face_normals
    areas = m.area_faces
    up = np.abs(normals[:, 2])

    horizontal = float(areas[up > HORIZONTAL].sum())
    vertical = float(areas[up < VERTICAL].sum())
    oblique = float(areas[(up >= VERTICAL) & (up <= HORIZONTAL)].sum())

    # Open boundary edges are holes. A clean closed room scan has few.
    _, counts = np.unique(np.asarray(m.edges_sorted), axis=0, return_counts=True)

    return {
        "vertices": len(m.vertices),
        "faces": len(m.faces),
        "bounds_min": m.bounds[0],
        "bounds_max": m.bounds[1],
        "extent": m.bounds[1] - m.bounds[0],
        "watertight": bool(m.is_watertight),
        "area_m2": float(m.area),
        "bodies": int(m.body_count),
        "floor_ceiling_area_m2": horizontal,
        "wall_area_m2": vertical,
        "oblique_area_m2": oblique,
        "oblique_fraction": oblique / float(m.area) if m.area else 0.0,
        "boundary_edges": int((counts == 1).sum()),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("mesh")
    args = ap.parse_args()

    r = measure(args.mesh)
    print(f"vertices        : {r['vertices']:,}")
    print(f"faces           : {r['faces']:,}")
    print(f"bounds min      : {np.round(r['bounds_min'], 3)}")
    print(f"bounds max      : {np.round(r['bounds_max'], 3)}")
    print(f"extent X,Y,Z    : {np.round(r['extent'], 3)}  (metres)")
    print(f"watertight      : {r['watertight']}")
    print(f"surface area    : {r['area_m2']:,.2f} m^2")
    print(f"bodies          : {r['bodies']}")
    print(f"horizontal area : {r['floor_ceiling_area_m2']:8.2f} m^2   (floor + ceiling)")
    print(f"vertical area   : {r['wall_area_m2']:8.2f} m^2   (walls)")
    print(f"oblique area    : {r['oblique_area_m2']:8.2f} m^2   (ragged edges, clutter, noise)")
    print(f"oblique fraction: {r['oblique_fraction'] * 100:5.1f}%")
    print(f"boundary edges  : {r['boundary_edges']:,}  (holes / open edges)")

    # A capture in hundreds of pieces is the mirror failure: the scanner built a
    # phantom copy of the room behind the glass and could not close either.
    if r["bodies"] > 50:
        print(f"\nWARNING: {r['bodies']} disconnected bodies. Covered mirrors?")


if __name__ == "__main__":
    main()
