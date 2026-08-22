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

Assumes plan and mesh share a frame up to cm-vs-m scaling, which holds when
registration is near-identity; the reported face counts show if it does not.

Usage:
    python -m scan2ha.ceilings model.json mesh.obj
"""

from __future__ import annotations

import argparse

import numpy as np
import trimesh
from shapely.geometry import Point, Polygon

from .schema import load_model

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
    args = ap.parse_args()

    model = load_model(args.model)
    down, up = _classified_faces(args.mesh)
    mesh_top = float(max(down[:, 2].max(), up[:, 2].max()))

    print(f"ceiling faces {len(down):,}   floor faces {len(up):,}   "
          f"mesh top z {mesh_top:.2f} m\n")
    print(f"{'room':<16}{'floor z':>9}{'height p50':>12}{'height p95':>12}   note")
    print("-" * 72)

    for lv in model.levels:
        for r in lv.rooms:
            poly = Polygon([(x * CM_TO_M, y * CM_TO_M) for x, y in r.points])
            if not poly.is_valid:
                poly = poly.buffer(0)

            c_in, f_in = _inside(down, poly), _inside(up, poly)
            if len(c_in) < MIN_FACES or len(f_in) < MIN_FACES:
                print(f"{str(r.name):<16}  too few faces "
                      f"(ceiling {len(c_in)}, floor {len(f_in)}) -- outside the "
                      f"mesh, or plan and mesh are not in the same frame")
                continue

            fz = float(np.percentile(f_in[:, 2], 5))
            c50 = float(np.percentile(c_in[:, 2], 50))
            c95 = float(np.percentile(c_in[:, 2], 95))

            note = ""
            if c95 > mesh_top - TRUNCATION_MARGIN_M:
                note = "TRUNCATED - scan stopped here, treat as a LOWER BOUND"

            print(f"{str(r.name):<16}{fz:>9.2f}{(c50 - fz) * 100:>11.0f}cm"
                  f"{(c95 - fz) * 100:>11.0f}cm   {note}")


if __name__ == "__main__":
    main()
