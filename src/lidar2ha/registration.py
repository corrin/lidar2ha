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
    python -m lidar2ha.registration home.json mesh.obj -o registered.json
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

# How many starting points to refine. Refinement is the expensive half -- 375
# scorings against one for ranking a seed -- and `plan_fit` is called on every
# pair of captures, so this trades directly against combining a dozen captures
# in reasonable time.
#
# Measured over this house's captures, the best seed is the one that wins: 2
# and 8 return the same transform on every pair tried, and 3 is one spare
# against a seed ranked well on its unrefined score and beaten after
# refinement. What would raise it: a capture that is demonstrably placeable --
# a hand-checked overlay -- still being missed, with its correct transform
# appearing among the seeds but outside the top few.
REFINE_TOP = 3


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


def sample_segments(segments, step_m=0.05, include_end=True):
    """Dense points along a list of (a, b) segments, in whatever unit they are.

    `include_end` is the only difference between sampling an open run of wall
    centrelines and sampling a closed room outline. On a ring the last point of
    each edge is the first point of the next, so including it doubles every
    corner and quietly weights corners twice in any nearest-neighbour statistic
    taken over the result.
    """
    out = []
    for a, b in segments:
        a = np.asarray(a, dtype=float)
        b = np.asarray(b, dtype=float)
        n = max(2, int(np.linalg.norm(b - a) / step_m))
        for t in np.linspace(0, 1, n, endpoint=include_end):
            out.append(a + (b - a) * t)
    return np.array(out) if out else np.empty((0, 2))


def sample_along_walls(walls, step_m=0.05):
    """Dense points along DXF wall centrelines, in metres."""
    return sample_segments(
        [((w.x_start * CM_TO_M, w.y_start * CM_TO_M),
          (w.x_end * CM_TO_M, w.y_end * CM_TO_M)) for w in walls],
        step_m,
    )


def handed(pts, mirror):
    """`pts` in the chosen handedness. Reflection is applied before rotation."""
    p = np.asarray(pts, dtype=float).copy()
    if mirror:
        p[:, 1] = -p[:, 1]
    return p


def rotation(theta):
    c, s = math.cos(theta), math.sin(theta)
    return np.array([[c, -s], [s, c]])


def transform(pts, theta, tx, ty, mirror):
    return handed(pts, mirror) @ rotation(theta).T + np.array([tx, ty])


def _grid_resultant(walls) -> tuple[float, float, float]:
    """Length-weighted sum over FOUR TIMES each wall angle, and the total length.

    Quadrupling is what makes this a statement about a grid rather than a
    direction: mod 90 a wall and its perpendicular describe the same grid, and
    at 4x they land on each other and reinforce instead of cancelling.
    """
    sx = sy = total = 0.0
    for wall in walls:
        dx, dy = wall.x_end - wall.x_start, wall.y_end - wall.y_start
        length = math.hypot(dx, dy)
        if length < 1e-9:
            continue
        angle = 4 * math.atan2(dy, dx)
        sx += length * math.cos(angle)
        sy += length * math.sin(angle)
        total += length
    return sx, sy, total


def grid_bearing(walls) -> float | None:
    """The dominant direction of these walls, in degrees mod 90.

    None when there are no walls to average. Ask `grid_concentration` whether
    the answer means anything before acting on it: this returns an angle for
    any set of walls whatever, including ones that agree on nothing.
    """
    sx, sy, _ = _grid_resultant(walls)
    if sx == 0.0 and sy == 0.0:
        return None
    return (math.degrees(math.atan2(sy, sx)) / 4) % 90.0


def grid_concentration(walls) -> float:
    """How much these walls agree on one grid: 0 scattered, 1 perfectly square.

    `grid_bearing` is a mean, and a mean of scattered directions is a number
    with no content. Measured on this house, a capture reading 0.341 here
    returned a bearing 4 degrees away from what every other capture of the same
    storey reported, purely from scatter -- and that phantom 4 degrees then
    read as a wrong basin on every pairing it appeared in.
    """
    sx, sy, total = _grid_resultant(walls)
    if total == 0.0:
        return 0.0
    return math.hypot(sx, sy) / total


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


def _candidate(base, tree, theta, c_src, c_tgt):
    """Score the placement that carries `c_src` onto `c_tgt` at rotation `theta`.

    THE TRANSLATION IS A FUNCTION OF THE ROTATION. Rotation is about the origin,
    so t = c_tgt - R(theta) @ c_src must be re-derived at every angle tried.
    Holding one translation while sweeping theta swings the far end of a 6.5 m
    plan by about 9 cm per degree: on one real pair that is the whole difference
    between a 10.3 cm reading and the 5.9 cm the same correspondence gives when
    t moves with the angle.
    """
    r = rotation(theta)
    tx, ty = c_tgt - r @ c_src
    med, cover, cost = score(base @ r.T + np.array([tx, ty]), tree)
    return {"median_error_m": med, "coverage": cover, "fit_cost_m": cost,
            "theta_rad": float(theta), "tx": float(tx), "ty": float(ty)}


def _coarse(plan_pts, target_c, tree, mirror, coarse_step_deg):
    """Best rotation for one handedness, translating by centroid alignment.

    Centroid alignment assumes the two clouds cover the same ground. Where they
    do not -- one bedroom against a ten-room survey -- it is the wrong
    translation at EVERY rotation including the correct one, so the correct
    basin scores worse than the noise and never reaches `_refine`, which
    travels 0.30 m in total and could not walk back to it anyway. `anchors`
    exists to supply the correspondences this cannot guess.
    """
    base = handed(plan_pts, mirror)
    base_c = base.mean(axis=0)
    return min((_candidate(base, tree, math.radians(deg), base_c, target_c)
                for deg in np.arange(0, 360, coarse_step_deg)),
               key=lambda f: f["fit_cost_m"])


def _seeded(plan_pts, tree, mirror, rotations, anchors):
    """Every (rotation, correspondence) pair, scored once and deduplicated.

    A correspondence is a pair of points believed to be the same place in the
    two captures -- room centroids, mostly. It is a CANDIDATE GENERATOR and not
    an optimiser: the pairing that maximises room overlap is measurably not the
    one that minimises wall error, by 0.3 to 1.2 degrees, and on the worst real
    pair that gap is 10.0 cm against 5.9. So these are starting points for
    `_refine` to argue with, never answers.
    """
    base = handed(plan_pts, mirror)
    seen: set[tuple[int, int, int]] = set()
    out = []
    for c_src, c_tgt in anchors:
        m_src = handed(np.asarray([c_src], dtype=float), mirror)[0]
        for theta in rotations:
            cand = _candidate(base, tree, theta, m_src, c_tgt)
            if not math.isfinite(cand["fit_cost_m"]):
                continue
            # Distinct rooms of one capture often sit a few centimetres apart
            # once placed; refining the same transform five times buys nothing.
            key = (round(cand["theta_rad"], 3), round(cand["tx"], 2),
                   round(cand["ty"], 2))
            if key in seen:
                continue
            seen.add(key)
            out.append(cand)
    return out


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


def register(plan_pts, target_xy, tree, coarse_step_deg=2.0, force_mirror=None,
             rotations=None, anchors=None, refine_top=REFINE_TOP):
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

    `rotations` and `anchors` are optional prior knowledge the caller has and
    this function cannot: the rotations a shared wall grid admits, and points
    believed to be the same place in both. Supplying them adds starting points;
    it never removes the blind sweep, which is always refined alongside them, so
    a caller that guesses badly can only spend time, not lose a fit it had.
    Plan-to-mesh passes neither -- a mesh point cloud has no walls to take a
    bearing from and no rooms to pair -- which is why this is a parameter rather
    than something computed here.
    """
    target_c = target_xy.mean(axis=0)
    mirrors = (False, True) if force_mirror is None else (force_mirror,)

    fits = []
    for mirror in mirrors:
        # The blind sweep's winner is refined unconditionally. Seeds are ranked
        # on one unrefined scoring, which is a weak ranking -- a candidate that
        # would refine well can sit outside the top few -- so this is what makes
        # the seeded search unable to return a worse answer than not seeding.
        coarse = _coarse(plan_pts, target_c, tree, mirror, coarse_step_deg)
        starts = [coarse]
        if rotations is not None and anchors is not None:
            seeded = _seeded(plan_pts, tree, mirror, rotations, anchors)
            seeded.sort(key=lambda f: f["fit_cost_m"])
            starts += seeded[: max(0, refine_top - 1)]
        fits += [_refine(plan_pts, tree, s, mirror) for s in starts]

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

    # A fixture pass still gets registered -- placefixtures needs exactly this
    # transform to carry a fitting out of the fixture mesh. What it must not
    # get is an elevation: the pass was walked pointing at ceilings with the
    # geometry deliberately sacrificed, so its floor line is the one number
    # here that is worth nothing, and `build` would use it as a level height.
    zs = [lv.registration.floor_z_m for lv in model.levels
          if lv.registration and lv.registration.floor_z_m is not None]
    if model.role == "fixtures":
        print("ELEVATIONS: not recorded -- this capture is a fixture pass.")
        print("  The per-level registrations above are kept; they are what")
        print("  placefixtures uses. The floor heights are not trustworthy.")
    elif len(zs) >= 2:
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
