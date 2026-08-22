#!/usr/bin/env python3
"""Register each DXF floor onto the Polycam mesh.

Why this exists
---------------
The floor-plan DXF and the mesh are in different coordinate frames. The DXF is
a presentation sheet: floors are drawn side by side, separated in X purely for
layout, with an arbitrary origin. The mesh is in true world coordinates. Until
the two are related, three things are impossible:

  * sampling the texture atlas for a wall (no idea where the wall is in mesh space)
  * knowing a floor's elevation (the DXF is 2D)
  * placing floors correctly relative to each other (the sheet offset is fake)

All three fall out of one 2D rigid transform per floor.

Method
------
Register on WALLS, not floors. In a handheld scan the walls are captured well
(here ~87% of the CSV's wall area) while the floor is heavily holed (~55%), so
walls are the reliable signal.

  1. Take mesh faces that are near-vertical -- these are walls -- and project
     their centroids to XY. That is the target point cloud.
  2. Sample points densely along the DXF wall centrelines for one floor.
  3. Search over rotation, and over both handednesses (Polycam's plan may be
     mirrored relative to the mesh), aligning centroids for the translation.
     Score by the MEDIAN nearest-neighbour distance, which ignores the portion
     of the plan the scan simply missed.
  4. Refine the best candidate with a local search over (dx, dy, theta).

Then, with the transform known, the floor's elevation is read from the Z values
of the mesh wall points that the floor actually matched.

Requires the mesh exported with 'Mesh up axis: Z axis up'.

Run from PowerShell (see CLAUDE.md). Usage:
    python register_to_mesh.py home.json mesh.obj -o registered.json
"""

import argparse
import json
import math
from pathlib import Path

import numpy as np
import trimesh
from scipy.spatial import cKDTree

CM_TO_M = 0.01


def load_wall_points(mesh_path, vertical_tol=0.2):
    """Centroids of near-vertical mesh faces: the wall surfaces."""
    scene = trimesh.load(mesh_path, process=False)
    meshes = list(scene.geometry.values()) if hasattr(scene, "geometry") else [scene]

    pts = []
    for g in meshes:
        n = g.face_normals
        vertical = np.abs(n[:, 2]) < vertical_tol
        if vertical.any():
            pts.append(g.triangles_center[vertical])
    if not pts:
        raise SystemExit("no vertical faces found -- was the mesh exported Z-up?")
    return np.vstack(pts)


def sample_along_walls(walls, step_m=0.05):
    """Dense points along DXF wall centrelines, in metres."""
    out = []
    for w in walls:
        a = np.array([w["xStart"], w["yStart"]]) * CM_TO_M
        b = np.array([w["xEnd"], w["yEnd"]]) * CM_TO_M
        length = np.linalg.norm(b - a)
        n = max(2, int(length / step_m))
        for t in np.linspace(0, 1, n):
            out.append(a + (b - a) * t)
    return np.array(out)


def transform(pts, theta, tx, ty, mirror):
    p = pts.copy()
    if mirror:
        p[:, 1] = -p[:, 1]
    c, s = math.cos(theta), math.sin(theta)
    r = np.array([[c, -s], [s, c]])
    return p @ r.T + np.array([tx, ty])


def score(pts, tree, cap=1.0):
    """Median nearest-neighbour distance, robust to unscanned plan regions."""
    d, _ = tree.query(pts, k=1, distance_upper_bound=cap)
    d = d[np.isfinite(d)]
    if len(d) < len(pts) * 0.2:
        return float("inf"), 0.0
    return float(np.median(d)), len(d) / len(pts)


def register(plan_pts, target_xy, tree, coarse_step_deg=2.0, force_mirror=None):
    """Fit a 2D rigid transform placing the plan points onto the mesh walls.

    force_mirror pins the handedness. Mirroring is a property of the DXF export
    as a whole, so once the best-constrained floor has chosen, every other floor
    must agree -- otherwise a floor with only a couple of walls can 'fit' a
    mirrored corner anywhere in the mesh and win on score while being nonsense.
    """
    target_c = target_xy.mean(axis=0)
    best = None

    mirrors = (False, True) if force_mirror is None else (force_mirror,)
    for mirror in mirrors:
        base = plan_pts.copy()
        if mirror:
            base = base.copy()
            base[:, 1] = -base[:, 1]
        base_c = base.mean(axis=0)

        for deg in np.arange(0, 360, coarse_step_deg):
            theta = math.radians(deg)
            c, s = math.cos(theta), math.sin(theta)
            r = np.array([[c, -s], [s, c]])
            rotated_c = r @ base_c
            tx, ty = target_c - rotated_c
            moved = base @ r.T + np.array([tx, ty])
            med, cover = score(moved, tree)
            if best is None or med < best[0]:
                best = (med, cover, theta, tx, ty, mirror)

    # Local refinement around the coarse winner.
    med, cover, theta, tx, ty, mirror = best
    for _ in range(3):
        improved = False
        for dth in (-0.02, -0.005, 0, 0.005, 0.02):
            for dx in (-0.10, -0.02, 0, 0.02, 0.10):
                for dy in (-0.10, -0.02, 0, 0.02, 0.10):
                    cand = transform(plan_pts, theta + dth, tx + dx, ty + dy, mirror)
                    m, c2 = score(cand, tree)
                    if m < med:
                        med, cover, improved = m, c2, True
                        theta, tx, ty = theta + dth, tx + dx, ty + dy
        if not improved:
            break

    return {"median_error_m": med, "coverage": cover,
            "theta_rad": theta, "tx": tx, "ty": ty, "mirror": mirror}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("json_path")
    ap.add_argument("mesh")
    ap.add_argument("-o", "--out", default="registered.json")
    args = ap.parse_args()

    model = json.loads(Path(args.json_path).read_text(encoding="utf-8"))
    wall_pts_3d = load_wall_points(args.mesh)
    target_xy = wall_pts_3d[:, :2]
    tree = cKDTree(target_xy)

    print(f"mesh wall points : {len(target_xy):,}")
    print(f"mesh z range     : {wall_pts_3d[:,2].min():.2f} .. {wall_pts_3d[:,2].max():.2f} m")
    print()

    # Register the best-constrained floor first -- the one with the most walls --
    # and let it decide handedness for the rest.
    order = sorted(
        (i for i, lv in enumerate(model["levels"]) if lv["walls"]),
        key=lambda i: len(model["levels"][i]["walls"]),
        reverse=True,
    )
    forced_mirror = None

    for i in order:
        lv = model["levels"][i]
        plan_pts = sample_along_walls(lv["walls"])
        fit = register(plan_pts, target_xy, tree, force_mirror=forced_mirror)
        if forced_mirror is None:
            forced_mirror = fit["mirror"]
            print(f"handedness fixed by {lv['name']} "
                  f"({len(lv['walls'])} walls): mirror={forced_mirror}\n")

        # Elevation: Z of the mesh wall points this floor actually matched,
        # taken near the bottom of the matched band (the floor line).
        placed = transform(plan_pts, fit["theta_rad"], fit["tx"], fit["ty"], fit["mirror"])
        d, idx = tree.query(placed, k=1, distance_upper_bound=0.30)
        hit = np.isfinite(d)
        if hit.any():
            zs = wall_pts_3d[idx[hit], 2]
            floor_z = float(np.percentile(zs, 5))
        else:
            floor_z = float("nan")

        lv["registration"] = {
            "theta_deg": round(math.degrees(fit["theta_rad"]) % 360, 2),
            "tx_m": round(fit["tx"], 4),
            "ty_m": round(fit["ty"], 4),
            "mirror": fit["mirror"],
            "median_error_m": round(fit["median_error_m"], 4),
            "coverage": round(fit["coverage"], 3),
            "floor_z_m": None if math.isnan(floor_z) else round(floor_z, 3),
        }

        r = lv["registration"]
        print(f"{lv['name']}")
        print(f"  rotation      : {r['theta_deg']:.2f} deg   mirror={r['mirror']}")
        print(f"  translation   : ({r['tx_m']:.3f}, {r['ty_m']:.3f}) m")
        print(f"  median error  : {r['median_error_m'] * 100:.1f} cm   "
              f"coverage={r['coverage'] * 100:.0f}%")
        print(f"  floor z       : {r['floor_z_m']} m")
        print()

    zs = [lv["registration"]["floor_z_m"] for lv in model["levels"]
          if lv.get("registration") and lv["registration"]["floor_z_m"] is not None]
    if len(zs) >= 2:
        base = min(zs)
        print("ELEVATIONS (lowest floor as datum)")
        for lv in model["levels"]:
            r = lv.get("registration")
            if r and r["floor_z_m"] is not None:
                elev_cm = (r["floor_z_m"] - base) * 100
                lv["elevation_cm"] = round(elev_cm, 1)
                print(f"  {lv['name']:<10} {elev_cm:7.1f} cm")

    Path(args.out).write_text(json.dumps(model, indent=2), encoding="utf-8")
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
