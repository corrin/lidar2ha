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

Usage:
    python -m lidar2ha.compare source.json target.json
"""

from __future__ import annotations

import argparse

import numpy as np
from scipy.spatial import cKDTree

from .registration import register, sample_along_walls, transform
from .schema import load_model

MATCH_LIMIT_M = 1.0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("source", help="model json to be moved")
    ap.add_argument("target", help="model json held fixed")
    args = ap.parse_args()

    src = load_model(args.source)
    tgt = load_model(args.target)

    src_walls = [w for lv in src.levels for w in lv.walls]
    tgt_walls = [w for lv in tgt.levels for w in lv.walls]
    if not src_walls or not tgt_walls:
        raise SystemExit("both captures need walls to compare")

    src_pts = sample_along_walls(src_walls)
    tgt_pts = sample_along_walls(tgt_walls)
    tree = cKDTree(tgt_pts)

    print(f"source {len(src_walls)} walls   target {len(tgt_walls)} walls\n")

    fit = register(src_pts, tgt_pts, tree, force_mirror=False)
    placed = transform(src_pts, fit["theta_rad"], fit["tx"], fit["ty"], False)

    d, _ = tree.query(placed, k=1, distance_upper_bound=MATCH_LIMIT_M)
    matched = d[np.isfinite(d)]

    print(f"  rotation      : {np.degrees(fit['theta_rad']) % 360:.2f} deg")
    print(f"  translation   : ({fit['tx']:.3f}, {fit['ty']:.3f}) m")
    print(f"  median error  : {fit['median_error_m'] * 100:.1f} cm")
    print(f"  coverage      : {fit['coverage'] * 100:.0f}%   "
          f"(a capture wholly inside the other should approach 100%)")
    if len(matched):
        print(f"  agreement     : p50 {np.percentile(matched, 50) * 100:5.1f} cm   "
              f"p90 {np.percentile(matched, 90) * 100:5.1f} cm   "
              f"max {matched.max() * 100:5.1f} cm")

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
