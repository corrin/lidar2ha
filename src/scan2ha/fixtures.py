#!/usr/bin/env python3
"""Find light fittings in a capture taken with every light switched on.

A geometry capture answers "what shape is this house". A *fixture* capture
answers "where are the lights", and is taken differently: every light on, the
phone aimed at each fitting, geometry quality sacrificed deliberately. It
contributes only light positions -- never walls, rooms or textures.

Why this works when reading the ceiling did not
-----------------------------------------------
An earlier attempt looked for fittings in an ordinary capture and failed. That
was not the algorithm, it was the input: the camera meters for the room, so
ceilings come out near-black, and with the lights off the brightest thing up
there is noise. A local-contrast threshold degenerated and returned scan edges.

With every light on and the phone pointed at each fitting, the fittings become
both the brightest and the best-photographed surfaces in the atlas. The signal
is real, so a simple method suffices.

Method
------
Cluster bright faces in 3D, rather than detecting blobs in a projected image.
Projection was the complicated part of the earlier attempt and it buys nothing:
a fitting is a compact cluster of bright faces wherever it sits, and working in
3D handles ceiling roses, wall sconces and table lamps with one code path.

  1. Sample each face's colour from the atlas and compute its luma.
  2. Keep faces above a brightness threshold, chosen from the distribution so a
     dim capture and a bright one both yield candidates.
  3. Cluster those faces by proximity in 3D.
  4. Reject clusters that are too small (noise) or too large (a sunlit window,
     a white wall) -- a fitting is compact.
  5. Classify each by the orientation of its faces: ceiling, wall, or free
     standing, which is a useful sanity check rather than a filter.

Everything is reported for a human to confirm. A bright compact thing is
*probably* a lamp, but it might be a window reflection or a mirror, and this
does not pretend to know.

Usage:
    python -m scan2ha.fixtures mesh.obj -o fixtures.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import trimesh

# Luma weights: human brightness perception, so a warm bulb and a cool one both
# read as bright.
LUMA = np.array([0.2126, 0.7152, 0.0722])

CLUSTER_M = 0.22        # faces closer than this belong to the same fitting
MIN_FACES = 6           # fewer is a speckle
MAX_EXTENT_M = 1.20     # a fitting is compact; a window or wall is not
CEILING_NZ = -0.5
WALL_NZ = 0.4


def load_faces(mesh_path: str):
    """Face centroids, normals, atlas colours, and where each face sits in its atlas.

    The atlas index and pixel coordinates are kept so a candidate can be cropped
    back out of the photograph it came from. A coordinate nobody can look at is
    not reviewable, and every candidate here needs a human to confirm it.
    """
    scene = trimesh.load(mesh_path, process=False)
    geoms = list(scene.geometry.values()) if hasattr(scene, "geometry") else [scene]

    centers: list = []
    normals: list = []
    colours: list = []
    atlas_ids: list = []
    atlas_xy: list = []
    atlases: list = []
    for g in geoms:
        vis = getattr(g, "visual", None)
        uv = getattr(vis, "uv", None)
        mat = getattr(vis, "material", None)
        img = getattr(mat, "image", None)
        faces = getattr(g, "faces", None)
        face_normals = getattr(g, "face_normals", None)
        centres = getattr(g, "triangles_center", None)
        # A loaded scene can also hold Path3D and PointCloud entries, which have
        # no faces at all -- they are not surfaces and cannot carry a fitting.
        if uv is None or img is None or faces is None or face_normals is None:
            continue
        atlas = np.asarray(img.convert("RGB"))
        ah, aw = atlas.shape[:2]
        c = uv[faces].mean(axis=1)
        sx = np.clip((c[:, 0] * (aw - 1)).astype(int), 0, aw - 1)
        sy = np.clip(((1 - c[:, 1]) * (ah - 1)).astype(int), 0, ah - 1)
        centers.append(centres)
        normals.append(face_normals)
        colours.append(atlas[sy, sx])
        atlas_ids.append(np.full(len(sx), len(atlases)))
        atlas_xy.append(np.stack([sx, sy], axis=1))
        atlases.append(atlas)

    if not centers:
        raise SystemExit(f"no textured triangle geometry in {mesh_path}")
    return (np.vstack(centers), np.vstack(normals), np.vstack(colours),
            np.concatenate(atlas_ids), np.vstack(atlas_xy), atlases)


def save_crop(atlases, atlas_id: int, xy: np.ndarray, path: Path, size: int = 220):
    """Cut the region of the original photo a candidate was found in."""
    from PIL import Image

    atlas = atlases[atlas_id]
    ah, aw = atlas.shape[:2]
    cx, cy = int(np.median(xy[:, 0])), int(np.median(xy[:, 1]))
    half = size // 2
    x0 = int(np.clip(cx - half, 0, max(aw - size, 0)))
    y0 = int(np.clip(cy - half, 0, max(ah - size, 0)))
    Image.fromarray(atlas[y0:y0 + size, x0:x0 + size]).save(path)


def cluster(points: np.ndarray, radius: float) -> list[np.ndarray]:
    """Group points within `radius` of each other. Returns index arrays.

    A KD-tree ball query plus breadth-first merging. Simple, and the point count
    after brightness filtering is small enough that nothing cleverer is needed.
    """
    from scipy.spatial import cKDTree

    tree = cKDTree(points)
    seen = np.zeros(len(points), dtype=bool)
    groups = []
    for i in range(len(points)):
        if seen[i]:
            continue
        stack, comp = [i], []
        seen[i] = True
        while stack:
            j = stack.pop()
            comp.append(j)
            for k in tree.query_ball_point(points[j], radius):
                if not seen[k]:
                    seen[k] = True
                    stack.append(k)
        groups.append(np.array(comp))
    return groups


def classify(normals: np.ndarray) -> str:
    """What kind of surface this cluster sits on."""
    nz = normals[:, 2]
    if np.median(nz) < CEILING_NZ:
        return "ceiling"
    if np.median(np.abs(nz)) < WALL_NZ:
        return "wall"
    return "free"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("mesh")
    ap.add_argument("-o", "--out", default="fixtures.json")
    ap.add_argument("--percentile", type=float, default=99.0,
                    help="brightness percentile above which a face is a candidate")
    ap.add_argument("--min-luma", type=float, default=200.0,
                    help="absolute floor, so a dark capture cannot promote grey to bright")
    ap.add_argument("--crops", metavar="DIR",
                    help="save a crop of the source photo around each candidate, "
                         "so a human can see what was actually detected")
    args = ap.parse_args()

    centers, normals, colours, atlas_ids, atlas_xy, atlases = load_faces(args.mesh)
    luma = colours.astype(float) @ LUMA

    cutoff = max(float(np.percentile(luma, args.percentile)), args.min_luma)
    bright = luma >= cutoff
    print(f"faces            : {len(luma):,}")
    print(f"brightness cutoff: {cutoff:.0f}  (p{args.percentile:g}, floor {args.min_luma:g})")
    print(f"bright faces     : {bright.sum():,}\n")

    if bright.sum() < MIN_FACES:
        raise SystemExit("almost nothing is bright -- were the lights actually on?")

    idx = np.where(bright)[0]
    groups = cluster(centers[idx], CLUSTER_M)

    found = []
    for g in groups:
        sel = idx[g]
        if len(sel) < MIN_FACES:
            continue
        pts = centers[sel]
        extent = float(np.linalg.norm(pts.max(axis=0) - pts.min(axis=0)))
        if extent > MAX_EXTENT_M:
            continue          # a window, a sunlit wall, a whole bright surface
        found.append({
            "x": round(float(pts[:, 0].mean()), 3),
            "y": round(float(pts[:, 1].mean()), 3),
            "z": round(float(pts[:, 2].mean()), 3),
            "faces": int(len(sel)),
            "extent_m": round(extent, 3),
            "luma": round(float(luma[sel].mean()), 1),
            "surface": classify(normals[sel]),
            "_sel": sel,
        })

    # Brightest and biggest first: the most confident candidates at the top.
    found.sort(key=lambda f: (f["faces"], f["luma"]), reverse=True)

    if args.crops:
        crops = Path(args.crops)
        crops.mkdir(parents=True, exist_ok=True)
        for i, f in enumerate(found):
            sel = f["_sel"]
            # Faces of one fitting can straddle two atlases; crop from whichever
            # holds most of it.
            ids, counts = np.unique(atlas_ids[sel], return_counts=True)
            aid = int(ids[counts.argmax()])
            on_atlas = sel[atlas_ids[sel] == aid]
            save_crop(atlases, aid, atlas_xy[on_atlas], crops / f"{i:02d}.png")
        print(f"wrote {len(found)} crops to {crops}\n")

    for f in found:
        del f["_sel"]

    print(f"{'#':>3}  {'x':>7} {'y':>7} {'z':>7}  {'faces':>6} {'extent':>7} "
          f"{'luma':>6}  surface")
    print("-" * 62)
    for i, f in enumerate(found):
        print(f"{i:>3}  {f['x']:>7.2f} {f['y']:>7.2f} {f['z']:>7.2f}  "
              f"{f['faces']:>6} {f['extent_m']:>7.2f} {f['luma']:>6.1f}  {f['surface']}")

    Path(args.out).write_text(json.dumps(found, indent=2), encoding="utf-8")
    print(f"\nwrote {args.out}  ({len(found)} candidate fittings)")
    by_surface: dict[str, int] = {}
    for f in found:
        by_surface[f["surface"]] = by_surface.get(f["surface"], 0) + 1
    print(f"  by surface: {by_surface}")
    print("\nEvery candidate needs confirming. Bright and compact is probably a")
    print("fitting, but a mirror or a window reflection looks the same from here.")


if __name__ == "__main__":
    main()
