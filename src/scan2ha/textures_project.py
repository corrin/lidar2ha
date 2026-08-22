#!/usr/bin/env python3
"""Project the Polycam texture atlas onto each wall: per-wall rectified images.

This is "wrap the actual photo over it". Instead of one tiled patch repeated
across every surface, each wall gets its own unique image showing what that
wall really looks like, at the atlas's native detail.

Ceiling on quality: the atlas holds ~384 px per metre of real surface (measured,
not assumed). That is the limit -- no export setting produces more, because it
is what the camera captured. What rectification buys is that the texture never
repeats and every wall is itself, which is what actually makes a tiled model
look fake.

Method
------
Needs the DXF-to-mesh registration from register_to_mesh.py, which supplies a
2D rigid transform per level. For each wall:

  1. Map its centreline into mesh coordinates with that transform.
  2. Build a wall-local frame: u along the wall, v vertically from the floor.
  3. Select mesh triangles that lie near the wall plane and face roughly the
     same way -- rejecting the rest of the room, and rejecting geometry behind
     the wall (which, in a room with a mirror, includes a phantom reflected
     copy of the room).
  4. Rasterise those triangles into (u, v) pixel space, interpolating atlas UVs
     barycentrically, keeping the nearest surface per pixel.
  5. Fill pixels no triangle covered -- the scan's holes -- with the median of
     what was found, so gaps read as plain wall rather than black.

Each side of a wall is projected separately, since the two faces of a wall are
different surfaces.

Run from PowerShell (see CLAUDE.md). Usage:
    python project_wall_textures.py registered.json mesh.obj -o walltex/
"""

import argparse
import json
import math
from pathlib import Path

import numpy as np
import trimesh
from PIL import Image

from .schema import WallTexture, load_model

CM_TO_M = 0.01
PX_PER_M = 384.0          # measured native density of the Polycam atlas
MAX_PLANE_DIST = 0.25     # m, how far off the plane a face may sit
MIN_FACING = 0.55         # |cos| between face normal and wall normal
MIN_COVERAGE = 0.04       # below this, the wall was not really scanned


def load_geoms(mesh_path):
    scene = trimesh.load(mesh_path, process=False)
    geoms = list(scene.geometry.values()) if hasattr(scene, "geometry") else [scene]
    out = []
    for g in geoms:
        vis = getattr(g, "visual", None)
        uv = getattr(vis, "uv", None)
        mat = getattr(vis, "material", None)
        img = getattr(mat, "image", None)
        if uv is None or img is None:
            continue
        out.append({
            "tris": g.triangles,                       # (F,3,3)
            "uv": uv[g.faces],                          # (F,3,2)
            "normals": g.face_normals,
            "centers": g.triangles_center,
            "atlas": np.asarray(img.convert("RGB")),
        })
    return out


def plan_to_mesh(pt_m, reg):
    """Apply a level's registration to a plan point given in metres."""
    x, y = pt_m
    if reg.mirror:
        y = -y
    th = math.radians(reg.theta_deg)
    c, s = math.cos(th), math.sin(th)
    return np.array([c * x - s * y + reg.tx_m, s * x + c * y + reg.ty_m])


def rasterise(tri_uvpix, tri_atlas_uv, tri_depth, img, zbuf, atlas):
    """Fill one triangle into img, keeping the nearest surface per pixel."""
    h, w = zbuf.shape
    xs, ys = tri_uvpix[:, 0], tri_uvpix[:, 1]
    x0 = max(int(np.floor(xs.min())), 0)
    x1 = min(int(np.ceil(xs.max())) + 1, w)
    y0 = max(int(np.floor(ys.min())), 0)
    y1 = min(int(np.ceil(ys.max())) + 1, h)
    if x1 <= x0 or y1 <= y0:
        return

    yy, xx = np.mgrid[y0:y1, x0:x1]
    px = xx + 0.5
    py = yy + 0.5

    ax, ay = tri_uvpix[0]
    bx, by = tri_uvpix[1]
    cx, cy = tri_uvpix[2]
    det = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
    if abs(det) < 1e-9:
        return

    l1 = ((by - cy) * (px - cx) + (cx - bx) * (py - cy)) / det
    l2 = ((cy - ay) * (px - cx) + (ax - cx) * (py - cy)) / det
    l3 = 1.0 - l1 - l2
    inside = (l1 >= 0) & (l2 >= 0) & (l3 >= 0)
    if not inside.any():
        return

    depth = l1 * tri_depth[0] + l2 * tri_depth[1] + l3 * tri_depth[2]
    sub = zbuf[y0:y1, x0:x1]
    better = inside & (depth < sub)
    if not better.any():
        return

    au = (l1 * tri_atlas_uv[0, 0] + l2 * tri_atlas_uv[1, 0] + l3 * tri_atlas_uv[2, 0])
    av = (l1 * tri_atlas_uv[0, 1] + l2 * tri_atlas_uv[1, 1] + l3 * tri_atlas_uv[2, 1])
    ah, aw = atlas.shape[:2]
    sx = np.clip((au * (aw - 1)).astype(int), 0, aw - 1)
    sy = np.clip(((1.0 - av) * (ah - 1)).astype(int), 0, ah - 1)

    region = img[y0:y1, x0:x1]
    region[better] = atlas[sy[better], sx[better]]
    sub[better] = depth[better]


def project_wall(geoms, a_xy, b_xy, base_z, height_m, side):
    """Rectified image of one side of one wall, plus its coverage fraction."""
    d = b_xy - a_xy
    length = float(np.linalg.norm(d))
    if length < 0.05:
        return None, 0.0
    u_hat = d / length
    n_hat = np.array([-u_hat[1], u_hat[0]])

    w_px = max(8, int(round(length * PX_PER_M)))
    h_px = max(8, int(round(height_m * PX_PER_M)))
    img = np.zeros((h_px, w_px, 3), dtype=np.uint8)
    zbuf = np.full((h_px, w_px), np.inf)

    for g in geoms:
        rel = g["centers"][:, :2] - a_xy
        along = rel @ u_hat
        off = rel @ n_hat
        z = g["centers"][:, 2]

        facing = np.abs(g["normals"][:, :2] @ n_hat)
        keep = (
            (np.abs(off) < MAX_PLANE_DIST)
            & (off * side > 0)
            & (along > -0.2) & (along < length + 0.2)
            & (z > base_z - 0.2) & (z < base_z + height_m + 0.2)
            & (facing > MIN_FACING)
        )
        idx = np.where(keep)[0]
        if len(idx) == 0:
            continue

        tris = g["tris"][idx]
        uvs = g["uv"][idx]
        rel_v = tris[:, :, :2] - a_xy
        u_all = rel_v @ u_hat
        v_all = tris[:, :, 2] - base_z
        depth_all = np.abs(rel_v @ n_hat)

        px_all = u_all * PX_PER_M
        # v grows upward in the room; image rows run top-down.
        py_all = (height_m - v_all) * PX_PER_M

        for k in range(len(idx)):
            pts = np.stack([px_all[k], py_all[k]], axis=1)
            rasterise(pts, uvs[k], depth_all[k], img, zbuf, g["atlas"])

    filled = np.isfinite(zbuf)
    coverage = float(filled.mean())
    if coverage < MIN_COVERAGE:
        return None, coverage

    # Holes in the scan become plain wall rather than black.
    med = np.median(img[filled].reshape(-1, 3), axis=0).astype(np.uint8)
    img[~filled] = med
    return img, coverage


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("registered_json")
    ap.add_argument("mesh")
    ap.add_argument("-o", "--out", default="walltex")
    args = ap.parse_args()

    model = load_model(args.registered_json)
    geoms = load_geoms(args.mesh)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    manifest: list[WallTexture] = []
    for li, lv in enumerate(model.levels):
        reg = lv.registration
        if not reg or reg.floor_z_m is None:
            print(f"{lv.name}: no registration, skipped")
            continue
        base_z = reg.floor_z_m
        level_height_m = lv.ceiling_height_cm * CM_TO_M

        print(f"\n{lv.name}  (base z={base_z:.2f} m, tallest room {level_height_m:.2f} m)")
        for wi, w in enumerate(lv.walls):
            # Sample each wall over ITS OWN height, not the level's tallest.
            # The writer applies the image at the wall's real size, so a 4.7 m
            # image on a 2.2 m wall gets squashed -- putting the wrong part of
            # the room on every short wall in the house.
            height_m = w.height * CM_TO_M
            a = plan_to_mesh((w.x_start * CM_TO_M, w.y_start * CM_TO_M), reg)
            b = plan_to_mesh((w.x_end * CM_TO_M, w.y_end * CM_TO_M), reg)

            entry = WallTexture(level=li, wall=wi)
            for side, tag in ((+1, "left"), (-1, "right")):
                img, cov = project_wall(geoms, a, b, base_z, height_m, side)
                if img is None:
                    print(f"  wall {wi:>2} {tag:<5}: coverage {cov * 100:4.1f}%  -- skipped")
                    continue
                path = out / f"L{li}_W{wi}_{tag}.png"
                Image.fromarray(img).save(path)
                setattr(entry, tag, path.resolve())
                setattr(entry, f"{tag}_coverage", round(cov, 3))
                print(f"  wall {wi:>2} {tag:<5}: {img.shape[1]}x{img.shape[0]} px  "
                      f"coverage {cov * 100:4.1f}%  -> {path.name}")
            if entry.left or entry.right:
                manifest.append(entry)

    man_path = out / "manifest.json"
    man_path.write_text(
        json.dumps([e.model_dump(mode="json", exclude_none=True) for e in manifest], indent=2),
        encoding="utf-8",
    )
    print(f"\nwrote {man_path}  ({len(manifest)} walls textured)")


if __name__ == "__main__":
    main()
