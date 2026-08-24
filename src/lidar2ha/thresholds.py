#!/usr/bin/env python3
"""Find where the flooring changes, by walking the floor and watching it.

A room boundary can be a baby gate, a step, and a change from wood to carpet.
None of those is in the plan geometry, but all three are in the mesh:
colour comes from the atlas, height from the vertices. So instead of guessing a
seam, sweep a line across the floor and look for the row where the floor stops
being one thing and starts being another.

For each band across the sweep axis this reports the median floor height and
median colour, plus a change score combining the two. The largest score is the
threshold.

Colour distance is computed in a rough perceptual sense (weighting green most,
blue least), because wood-to-carpet is a hue change more than a brightness one
and plain RGB distance under-reads it.

Usage:
    python -m lidar2ha.thresholds mesh.obj --axis y
"""

from __future__ import annotations

import argparse
import math
from dataclasses import dataclass
from typing import Any

import numpy as np
import trimesh

UP_FACING = 0.85
BAND_M = 0.20
MAX_HEIGHT_M = 0.6
MIN_BAND_SAMPLES = 40

# A step and a material change are weighed against each other by dividing each
# by what a convincing one looks like: 5 cm of rise, 12 units of colour. Both
# signals matter and either alone is weak -- a step with no material change, or
# a material change with no step, is still a boundary.
STEP_CM_SCALE = 5.0
COLOUR_SCALE = 12.0
STRONG_SCORE = 3.0

# How far either side of a declared boundary to look for the real one. Wider and
# a boundary is credited with the flooring change belonging to the next room
# along; narrower and an honest trace a hand's width out reads as unsupported.
WINDOW_M = 0.6


@dataclass(frozen=True)
class FloorSample:
    """Up-facing faces with the colour the camera saw, in mesh metres."""

    points: np.ndarray            # (N, 3)
    colours: np.ndarray           # (N, 3), RGB


@dataclass(frozen=True)
class Support:
    """The strongest flooring transition found near a line, and how far off."""

    step_cm: float
    colour: float
    score: float
    offset_cm: float
    samples: int

    @property
    def corroborated(self) -> bool:
        return self.score > STRONG_SCORE


def colour_distance(a, b):
    """Weighted RGB distance -- closer to how a hue change actually reads."""
    d = a.astype(float) - b.astype(float)
    return float(np.sqrt((d[0] * 0.9) ** 2 + (d[1] * 1.2) ** 2 + (d[2] * 0.6) ** 2))


def floor_samples(mesh_path: str, *,
                  max_height_m: float = MAX_HEIGHT_M) -> FloorSample:
    """Every up-facing face near the floor, painted with the atlas colour."""
    scene: Any = trimesh.load(mesh_path, process=False)
    geoms = list(scene.geometry.values()) if hasattr(scene, "geometry") else [scene]

    pts, cols = [], []
    for g in geoms:
        vis = getattr(g, "visual", None)
        uv = getattr(vis, "uv", None)
        mat = getattr(vis, "material", None)
        img = getattr(mat, "image", None)
        sel = np.where(g.face_normals[:, 2] > UP_FACING)[0]
        if len(sel) == 0 or uv is None or img is None:
            continue
        pts.append(g.triangles_center[sel])
        atlas = np.asarray(img.convert("RGB"))
        ah, aw = atlas.shape[:2]
        c = uv[g.faces[sel]].mean(axis=1)
        sx = np.clip((c[:, 0] * (aw - 1)).astype(int), 0, aw - 1)
        sy = np.clip(((1 - c[:, 1]) * (ah - 1)).astype(int), 0, ah - 1)
        cols.append(atlas[sy, sx])

    if not pts:
        return FloorSample(np.zeros((0, 3)), np.zeros((0, 3)))

    points, colours = np.vstack(pts), np.vstack(cols)
    floor_z = np.percentile(points[:, 2], 2)
    keep = points[:, 2] < floor_z + max_height_m
    return FloorSample(points[keep], colours[keep])


def _transition(bands) -> Support | None:
    """The adjacent pair of bands that differ most, as a Support."""
    best: Support | None = None
    for (at0, z0, c0), (at1, z1, c1) in zip(bands, bands[1:], strict=False):
        step_cm = abs(z1 - z0) * 100
        colour = colour_distance(c0, c1)
        score = step_cm / STEP_CM_SCALE + colour / COLOUR_SCALE
        if best is None or score > best.score:
            best = Support(step_cm=step_cm, colour=colour, score=score,
                           offset_cm=(at0 + at1) / 2 * 100, samples=0)
    return best


def boundary_support(a_m, b_m, floor: FloorSample, *,
                     window_m: float = WINDOW_M, band_m: float = BAND_M,
                     min_samples: int = MIN_BAND_SAMPLES) -> Support | None:
    """Is there a step or a flooring change along this line? In mesh metres.

    None means NOT LOOKED AT rather than unsupported: the ordinary capture never
    photographed enough floor along that strip to say. Folding that into "no
    evidence" would invent a verdict about a boundary nobody measured, which is
    the same mistake `daylight.verdict_at` returns `unseen` to avoid.
    """
    (ax, ay), (bx, by) = a_m, b_m
    length = math.hypot(bx - ax, by - ay)
    if length < 1e-9 or len(floor.points) == 0:
        return None
    ux, uy = (bx - ax) / length, (by - ay) / length

    rel = floor.points[:, :2] - np.array([ax, ay])
    along = rel[:, 0] * ux + rel[:, 1] * uy
    across = rel[:, 0] * -uy + rel[:, 1] * ux
    near = (along >= 0) & (along <= length) & (np.abs(across) <= window_m)
    if int(near.sum()) < min_samples * 2:
        return None

    d, z, c = across[near], floor.points[near, 2], floor.colours[near]
    edges = np.arange(-window_m, window_m + band_m, band_m)
    bands = []
    for i in range(len(edges) - 1):
        m = (d >= edges[i]) & (d < edges[i + 1])
        if int(m.sum()) < min_samples:
            continue
        bands.append(((edges[i] + edges[i + 1]) / 2, float(np.median(z[m])),
                      np.median(c[m], axis=0)))
    if len(bands) < 2:
        return None

    best = _transition(bands)
    if best is None:
        return None
    return Support(best.step_cm, best.colour, best.score, best.offset_cm,
                   int(near.sum()))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("mesh")
    ap.add_argument("--axis", choices=["x", "y"], default="y")
    ap.add_argument("--max-height", type=float, default=MAX_HEIGHT_M,
                    help="metres above floor to include; excludes furniture tops")
    args = ap.parse_args()

    floor = floor_samples(args.mesh, max_height_m=args.max_height)
    pts, cols = floor.points, floor.colours

    axis = 0 if args.axis == "x" else 1
    v = pts[:, axis]
    edges = np.arange(v.min(), v.max() + BAND_M, BAND_M)

    rows = []
    for i in range(len(edges) - 1):
        m = (v >= edges[i]) & (v < edges[i + 1])
        if int(m.sum()) < MIN_BAND_SAMPLES:
            continue
        rows.append({
            "at": (edges[i] + edges[i + 1]) / 2,
            "n": int(m.sum()),
            "z": float(np.median(pts[m, 2])),
            "rgb": np.median(cols[m], axis=0),
        })

    print(f"floor faces {len(pts):,}   sweeping {args.axis}   band {BAND_M} m\n")
    print(f"{'at (m)':>8}{'n':>7}{'z (m)':>9}  colour      dz(cm)  dcolour   score")
    print("-" * 66)

    best = []
    for i, r in enumerate(rows):
        if i == 0:
            dz = dc = 0.0
        else:
            dz = abs(r["z"] - rows[i - 1]["z"]) * 100
            dc = colour_distance(r["rgb"], rows[i - 1]["rgb"])
        score = dz / STEP_CM_SCALE + dc / COLOUR_SCALE
        hexc = "#{:02X}{:02X}{:02X}".format(*r["rgb"].astype(int))
        flag = "  <<<" if score > STRONG_SCORE else ""
        print(f"{r['at']:>8.2f}{r['n']:>7}{r['z']:>9.2f}  {hexc}  "
              f"{dz:>7.1f}  {dc:>7.1f}  {score:>6.1f}{flag}")
        if i:
            best.append((score, r["at"], dz, dc))

    best.sort(reverse=True)
    print("\nstrongest transitions:")
    for score, at, dz, dc in best[:5]:
        print(f"  {args.axis} = {at:6.2f} m   step {dz:5.1f} cm   "
              f"colour shift {dc:5.1f}   score {score:.1f}")


if __name__ == "__main__":
    main()
