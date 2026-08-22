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

Usage:
    python -m scan2ha.registration home.json mesh.obj -o registered.json
"""

import argparse
import math

import numpy as np
import trimesh
from scipy.spatial import cKDTree

from .schema import Registration, load_model, save_model

CM_TO_M = 0.01

# Below this, a fit is not trustworthy however good its median error looks.
LOW_COVERAGE = 0.90


def load_wall_points(mesh_path, vertical_tol=0.20):
    """Centroids of near-vertical mesh faces: the wall surfaces.

    vertical_tol is |n_z|, so 0.20 admits faces within about 11 degrees of
    vertical and 0.10 within about 6.

    A double-height capture once fitted at a confident 51 degrees and 52%
    coverage here, and tightening to 0.10 fixed it -- which made the extra
    faces look like the cause. They were not. Sloped ceilings and stair soffits
    are structured noise rather than scatter, so they do generate coherent
    spurious candidates, but the reason one of them WON was that fits were
    compared on a median taken over matched points only. With that fixed (see
    score) the same capture fits correctly at 0.20, and slightly better than at
    0.10 -- 2.3 cm against 2.7.

    So the looser default stands: a tighter one rescues nothing now and
    discards a third of the wall points, which on a sparser capture would cost
    coverage rather than buy accuracy. --vertical-tol stays exposed because a
    capture dominated by sloped surfaces may still want it.
    """
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
        a = np.array([w.x_start, w.y_start]) * CM_TO_M
        b = np.array([w.x_end, w.y_end]) * CM_TO_M
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
    """Return (median matched distance, coverage, capped mean).

    The median is taken over matched points only, which makes it a bad thing to
    choose a fit BY: a transform that lands half the plan on some wall and
    abandons the rest reports the median of its good half, and can beat a
    transform that lands all of it. That is not hypothetical -- one capture
    picked a 51 degree rotation reading 4.7 cm at 52% coverage over the correct
    one at 1.9 cm and 100%, because 4.7 cm was the median of the half that fit.

    The capped mean is the number to minimise instead: every unmatched point is
    charged the full cap, so abandoning the plan costs exactly as much as
    placing it badly. The median and coverage are still returned, because they
    are what a human reads to judge whether a fit is trustworthy.
    """
    d, _ = tree.query(pts, k=1, distance_upper_bound=cap)
    matched = d[np.isfinite(d)]
    if len(matched) < len(pts) * 0.2:
        return float("inf"), 0.0, float("inf")
    unmatched = len(pts) - len(matched)
    capped_mean = float((matched.sum() + unmatched * cap) / len(pts))
    return float(np.median(matched)), len(matched) / len(pts), capped_mean


def _coarse(plan_pts, target_c, tree, mirror, coarse_step_deg):
    """Best rotation for one handedness, at coarse resolution."""
    base = plan_pts.copy()
    if mirror:
        base[:, 1] = -base[:, 1]
    base_c = base.mean(axis=0)

    best = None
    for deg in np.arange(0, 360, coarse_step_deg):
        theta = math.radians(deg)
        c, s = math.cos(theta), math.sin(theta)
        r = np.array([[c, -s], [s, c]])
        tx, ty = target_c - r @ base_c
        med, cover, cost = score(base @ r.T + np.array([tx, ty]), tree)
        if best is None or cost < best["fit_cost_m"]:
            best = {"median_error_m": med, "coverage": cover, "fit_cost_m": cost,
                    "theta_rad": theta, "tx": tx, "ty": ty}
    return best


def _refine(plan_pts, tree, start, mirror):
    """Local descent from a coarse candidate."""
    med, cover, cost = start["median_error_m"], start["coverage"], start["fit_cost_m"]
    theta, tx, ty = start["theta_rad"], start["tx"], start["ty"]
    for _ in range(3):
        improved = False
        for dth in (-0.02, -0.005, 0, 0.005, 0.02):
            for dx in (-0.10, -0.02, 0, 0.02, 0.10):
                for dy in (-0.10, -0.02, 0, 0.02, 0.10):
                    cand = transform(plan_pts, theta + dth, tx + dx, ty + dy, mirror)
                    m, c2, k = score(cand, tree)
                    if k < cost:
                        med, cover, cost, improved = m, c2, k, True
                        theta, tx, ty = theta + dth, tx + dx, ty + dy
        if not improved:
            break
    return {"median_error_m": med, "coverage": cover, "fit_cost_m": cost,
            "theta_rad": theta, "tx": tx, "ty": ty, "mirror": mirror}


def register(plan_pts, target_xy, tree, coarse_step_deg=2.0, force_mirror=None):
    """Fit a 2D rigid transform placing the plan points onto the mesh walls.

    Each handedness is refined and only then compared. Choosing between them on
    the coarse score alone -- 2 degree steps, translation by centroid
    alignment -- decides the single most consequential thing this function does
    at its least reliable resolution, and lets a coarse winner that refines
    badly beat a coarse loser that would refine well. On one capture that
    returned mirror=True at 6.9 cm where mirror=False refines to 4.8 cm.

    force_mirror pins the handedness. Mirroring is a property of the DXF export
    as a whole, so once the best-constrained floor has chosen, every other floor
    must agree -- otherwise a floor with only a couple of walls can 'fit' a
    mirrored corner anywhere in the mesh and win on score while being nonsense.
    """
    target_c = target_xy.mean(axis=0)
    mirrors = (False, True) if force_mirror is None else (force_mirror,)

    fits = []
    for mirror in mirrors:
        coarse = _coarse(plan_pts, target_c, tree, mirror, coarse_step_deg)
        fits.append(_refine(plan_pts, tree, coarse, mirror))

    return min(fits, key=lambda f: f["fit_cost_m"])



def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("json_path")
    ap.add_argument("mesh")
    ap.add_argument("-o", "--out", default="registered.json")
    ap.add_argument("--vertical-tol", type=float, default=0.20,
                    help="|n_z| below which a mesh face counts as a wall. Loosening "
                         "this admits sloped ceilings and stair soffits, which are "
                         "structured noise and can capture the fit")
    args = ap.parse_args()

    model = load_model(args.json_path)
    wall_pts_3d = load_wall_points(args.mesh, vertical_tol=args.vertical_tol)
    target_xy = wall_pts_3d[:, :2]
    tree = cKDTree(target_xy)

    print(f"mesh wall points : {len(target_xy):,}")
    print(f"mesh z range     : {wall_pts_3d[:,2].min():.2f} .. {wall_pts_3d[:,2].max():.2f} m")
    print()

    # Register the best-constrained floor first -- the one with the most walls --
    # and let it decide handedness for the rest.
    order = sorted(
        (i for i, lv in enumerate(model.levels) if lv.walls),
        key=lambda i: len(model.levels[i].walls),
        reverse=True,
    )
    forced_mirror = None

    for i in order:
        lv = model.levels[i]
        plan_pts = sample_along_walls(lv.walls)
        fit = register(plan_pts, target_xy, tree, force_mirror=forced_mirror)
        if forced_mirror is None:
            forced_mirror = fit["mirror"]
            print(f"handedness fixed by {lv.name} "
                  f"({len(lv.walls)} walls): mirror={forced_mirror}\n")

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

        lv.registration = Registration(
            theta_deg=round(math.degrees(fit["theta_rad"]) % 360, 2),
            tx_m=round(fit["tx"], 4),
            ty_m=round(fit["ty"], 4),
            mirror=fit["mirror"],
            median_error_m=round(fit["median_error_m"], 4),
            coverage=round(fit["coverage"], 3),
            floor_z_m=None if math.isnan(floor_z) else round(floor_z, 3),
        )

        r = lv.registration
        print(f"{lv.name}")
        print(f"  rotation      : {r.theta_deg:.2f} deg   mirror={r.mirror}")
        print(f"  translation   : ({r.tx_m:.3f}, {r.ty_m:.3f}) m")
        print(f"  median error  : {r.median_error_m * 100:.1f} cm   "
              f"coverage={r.coverage * 100:.0f}%")
        if r.coverage < LOW_COVERAGE:
            # Read this before the error. The error is a median over matched
            # points only, so a fit that abandoned half the plan reports the
            # median of the half it kept and can look better than a good one.
            print(f"  ** LOW COVERAGE: {r.coverage * 100:.0f}% of the plan found "
                  f"no wall within a metre. Treat the {r.median_error_m * 100:.1f} cm "
                  f"above as unreliable -- it describes only the part that fitted.")
            print("     A double-height or heavily sloped space may need a tighter "
                  "--vertical-tol; see load_wall_points.")
        print(f"  floor z       : {r.floor_z_m} m")
        print()

    zs = [lv.registration.floor_z_m for lv in model.levels
          if lv.registration and lv.registration.floor_z_m is not None]
    if len(zs) >= 2:
        base = min(zs)
        print("ELEVATIONS (lowest floor as datum)")
        for lv in model.levels:
            r = lv.registration
            if r and r.floor_z_m is not None:
                elev_cm = (r.floor_z_m - base) * 100
                lv.elevation_cm = round(elev_cm, 1)
                print(f"  {lv.name:<10} {elev_cm:7.1f} cm")

    save_model(model, args.out)
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
