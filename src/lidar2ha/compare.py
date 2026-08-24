#!/usr/bin/env python3
"""Fit one capture's plan onto another's, and report where they disagree.

Two scans of overlapping space are the only way to check a capture against
anything other than itself. Comparing their TOTALS is meaningless -- a scan that
covers more ground is bigger, which says nothing about accuracy. The comparison
that means something is over the region they share.

Method: sample points along each plan's wall centrelines and fit one onto the
other with the same rigid fit used to register a plan onto a mesh. No mesh is
involved, so this works even when one capture's mesh is unusable.

Handedness is NOT searched here. Mirroring is a property of the export format,
so two plans from one scanner share it; letting the fit choose would invent a
reflected building to paper over a poor overlap.

What the numbers are for: on one real pair this reported 17 cm median and 80 cm
at p90 disagreement, against 1-5 cm for plan-to-mesh registration. That gap is
the argument for SELECTING geometry from a single capture per room rather than
averaging or compositing it -- blending outlines 17 cm apart matches neither
wall.

`plan_fit` returns that fit rather than printing it, because two callers need
the transform itself: `placefixtures`, to carry a fitting out of a fixture
capture, and `combine`, to put every capture of a level in one frame.

Usage:
    python -m lidar2ha.compare source.json target.json
"""

from __future__ import annotations

import argparse
import math
from typing import Any, TypedDict

import numpy as np
from scipy.spatial import cKDTree

from .registration import (
    CM_TO_M,
    grid_bearing,
    register,
    sample_along_walls,
    transform,
)
from .rooms import polygon_of
from .schema import Model, load_model

MATCH_LIMIT_M = 1.0

# A room is a usable correspondence only if the other capture drew one of about
# the same size. Area is nearly rotation-invariant and nearly capture-invariant
# -- one real room reads 13.0 m2 in both captures that saw it, an alcove 4.3 in
# both -- which makes it a cheap, strong filter on a pairing that would
# otherwise be every room against every room.
ANCHOR_AREA_RATIO = (0.6, 1.6)
# Below this a room is too small for its centroid to locate anything: scanners
# emit slivers, and a 1 m2 artefact pairs with any other 1 m2 artefact.
ANCHOR_MIN_AREA_M2 = 4.0


class Fit(TypedDict):
    """One plan-to-plan fit, typed once at the boundary.

    `register` returns a bare dict of numpy scalars. Converting it here rather
    than at each call site is what keeps `Any` from spreading through every
    caller that only wants to read a rotation.
    """

    theta_rad: float
    tx: float
    ty: float
    median_error_m: float
    coverage: float
    # None when the fit matched nothing at all, which is not the same as 0.
    p90_m: float | None
    matched: int
    sampled: int


def grid_rotations(src_walls, tgt_walls) -> list[float]:
    """The only rotations that can relate two captures of one building, radians.

    Both saw the same wall grid, so the answer is `(g_tgt - g_src) mod 90` plus
    a quarter turn, four classes and nothing else. Four candidates in place of
    the 180 a two-degree blind sweep tries, and by construction none of them can
    land off the grid.

    Empty when either capture has no walls to take a bearing from -- a capture
    too wall-poor to have a grid gets the blind sweep and nothing else.
    """
    g_src, g_tgt = grid_bearing(src_walls), grid_bearing(tgt_walls)
    if g_src is None or g_tgt is None:
        return []
    base = math.radians((g_tgt - g_src) % 90.0)
    return [base + k * math.pi / 2 for k in range(4)]


def room_anchors(src: Model, tgt: Model, *,
                 min_area_m2: float = ANCHOR_MIN_AREA_M2,
                 ratio: tuple[float, float] = ANCHOR_AREA_RATIO,
                 ) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """Points believed to be the same place in both captures, in metres.

    Room centroids, paired where the two rooms are of similar area, PLUS each
    capture's whole floor area paired the same way. The union is not a
    tidy-up: SEGMENTATION IS NOT SHARED BETWEEN CAPTURES, and where the two
    disagree about it no room-to-room pairing can be correct. One real capture
    cut a space into a 15.6 m2 room and a 13.4 m2 room that the other saw
    whole, as one 29.9 m2 room -- 29.0 against 29.9 pairs on area, either half
    against the whole does not, and that capture is discarded today.

    So the union stands in for "however this capture chose to divide it", and
    is paired against single rooms in both directions. Union against union is
    always offered and never filtered, because two captures of the same
    building that both saw all of it need no other correspondence.

    A capture with no rooms contributes nothing here and falls back to the
    blind sweep's centroid alignment, which is exactly what it gets today.
    """
    def described(model: Model) -> list[tuple[float, tuple[float, float]]]:
        out = []
        for lv in model.levels:
            for room in lv.rooms:
                if len(room.points) < 3:
                    continue
                poly = polygon_of(room)
                area = poly.area * CM_TO_M * CM_TO_M
                if area < min_area_m2:
                    continue
                out.append((area, (poly.centroid.x * CM_TO_M,
                                   poly.centroid.y * CM_TO_M)))
        return out

    def union(rooms) -> tuple[float, tuple[float, float]] | None:
        """Total area, and the centroid of all of it.

        Area-weighted, which for disjoint room polygons is the centroid of
        their union exactly -- not the mean of the polygon vertices, which
        counts a finely-traced wall more than a straight one.
        """
        total = sum(a for a, _ in rooms)
        if total <= 0:
            return None
        cx = sum(a * c[0] for a, c in rooms) / total
        cy = sum(a * c[1] for a, c in rooms) / total
        return total, (cx, cy)

    src_rooms, tgt_rooms = described(src), described(tgt)
    src_all, tgt_all = union(src_rooms), union(tgt_rooms)
    lo, hi = ratio

    left = src_rooms + ([src_all] if src_all else [])
    right = tgt_rooms + ([tgt_all] if tgt_all else [])
    pairs = [(c_src, c_tgt)
             for a_src, c_src in left
             for a_tgt, c_tgt in right
             if lo < a_src / a_tgt < hi]

    if src_all and tgt_all and (src_all[1], tgt_all[1]) not in pairs:
        pairs.append((src_all[1], tgt_all[1]))
    return pairs


def plan_fit(src: Model, tgt: Model, match_limit_m: float = MATCH_LIMIT_M) -> Fit:
    """Fit `src`'s plan onto `tgt`'s, and grade the agreement.

    Raises rather than returning a degenerate fit: a capture with no walls gives
    the fitter nothing to hold onto, and a transform derived from that is a
    confident-looking number describing nothing.
    """
    src_walls = [w for lv in src.levels for w in lv.walls]
    tgt_walls = [w for lv in tgt.levels for w in lv.walls]
    if not src_walls or not tgt_walls:
        raise ValueError("both captures need walls to compare")

    src_pts = sample_along_walls(src_walls)
    tgt_pts = sample_along_walls(tgt_walls)
    tree = cKDTree(tgt_pts)

    raw: Any = register(src_pts, tgt_pts, tree, force_mirror=False,
                        rotations=grid_rotations(src_walls, tgt_walls),
                        anchors=room_anchors(src, tgt))
    placed = transform(src_pts, raw["theta_rad"], raw["tx"], raw["ty"], False)
    d, _ = tree.query(placed, k=1, distance_upper_bound=match_limit_m)
    matched = d[np.isfinite(d)]

    return Fit(
        theta_rad=float(raw["theta_rad"]),
        tx=float(raw["tx"]),
        ty=float(raw["ty"]),
        median_error_m=float(raw["median_error_m"]),
        coverage=float(raw["coverage"]),
        p90_m=float(np.percentile(matched, 90)) if len(matched) else None,
        matched=int(len(matched)),
        sampled=int(len(src_pts)),
    )


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("source", help="model json to be moved")
    ap.add_argument("target", help="model json held fixed")
    args = ap.parse_args()

    src = load_model(args.source)
    tgt = load_model(args.target)

    src_walls = sum(len(lv.walls) for lv in src.levels)
    tgt_walls = sum(len(lv.walls) for lv in tgt.levels)
    print(f"source {src_walls} walls   target {tgt_walls} walls\n")

    try:
        fit = plan_fit(src, tgt)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    print(f"  rotation      : {np.degrees(fit['theta_rad']) % 360:.2f} deg")
    print(f"  translation   : ({fit['tx']:.3f}, {fit['ty']:.3f}) m")
    print(f"  median error  : {fit['median_error_m'] * 100:.1f} cm")
    print(f"  coverage      : {fit['coverage'] * 100:.0f}%   "
          f"(a capture wholly inside the other should approach 100%)")
    if fit["p90_m"] is not None:
        print(f"  agreement     : p50 {fit['median_error_m'] * 100:5.1f} cm   "
              f"p90 {fit['p90_m'] * 100:5.1f} cm   "
              f"over {fit['matched']:,} of {fit['sampled']:,} sampled points")

    print("\n  ceilings reported for the same physical space:")
    for label, model in (("source", src), ("target", tgt)):
        for lv in model.levels:
            for r in lv.rooms:
                hi, lo = r.ceiling_high_cm, r.ceiling_low_cm
                span = (f"{lo:.0f}-{hi:.0f}" if (hi and lo and hi != lo)
                        else (f"{hi:.0f}" if hi else "unknown"))
                print(f"    {label:<8} {str(r.name):<16} {span:>10} cm")


if __name__ == "__main__":
    main()
