#!/usr/bin/env python3
"""Cut real texture patches out of a Polycam mesh's atlas, by surface class.

The target is Doom/Quake-era quality: a small tiled image per surface class
that reads as the right material. Not photogrammetric rectification -- that
needs the DXF-to-mesh registration, which is not yet trustworthy for this
capture.

Faces are classified by orientation alone, so no coordinate frames are
involved:

    up-facing   (nz >  0.9)  -> floor
    down-facing (nz < -0.9)  -> ceiling
    vertical    (|nz| < 0.2) -> wall

For each class it ranks faces by UV footprint (big, well-photographed surfaces
beat slivers of noise), crops a square of the atlas around each candidate, and
scores it.

Scoring is PER CLASS, which matters more than it sounds:

  * A floor wants grain -- variation is signal, so detail scores well.
  * A wall wants to be flat and plain. Scoring a wall for "detail" picks the
    one crop containing a pot plant, which then tiles across the whole room.
    Walls are scored for uniformity instead, and patches containing strong
    colour outliers are rejected outright.

Anything containing large blown-out or black regions is penalised for every
class -- those are holes in the mesh, not surfaces.

If no candidate clears a minimum score, the class falls back to a flat patch of
its median colour, and says so. That happens for the ceiling here: this capture
barely has one.

Usage:
    python -m lidar2ha.textures_tile mesh.obj -o textures/ --size 256
"""

import argparse
from pathlib import Path

import numpy as np
import trimesh
from PIL import Image

# name -> (face predicate on nz, wants_detail)
CLASSES = {
    "floor": (lambda nz: nz > 0.9, True),
    "ceiling": (lambda nz: nz < -0.9, False),
    "wall": (lambda nz: np.abs(nz) < 0.2, False),
}

MIN_SCORE = 0.45


def patch_score(arr, wants_detail):
    """Score a candidate crop. Higher is better; negative is unusable."""
    a = arr.astype(np.float32)
    g = a.mean(axis=2)

    # Exposure: 1.0 at mid-grey, falling off towards black or white.
    exposure = 1.0 - abs(g.mean() - 128.0) / 128.0

    # Holes in the mesh show up as blown or dead pixels.
    clipped = float(((g < 6) | (g > 249)).mean())

    # Colour outliers: how much of the patch is far from its own median colour.
    # This is what rejects the pot plant on an otherwise white wall.
    med = np.median(a.reshape(-1, 3), axis=0)
    dist = np.linalg.norm(a - med, axis=2)
    outliers = float((dist > 60).mean())

    spread = float(g.std())
    if wants_detail:
        # Grain is the point, but noise is not: peak around a moderate spread.
        texture = min(spread / 35.0, 1.0)

        # A hole in the mesh gets filled with a smeared, featureless blur that
        # is neither blown out nor an outlier, so the tests above miss it.
        # Catch it by looking for sub-tiles with almost no local variation.
        tiles = g[: g.shape[0] // 4 * 4, : g.shape[1] // 4 * 4]
        tiles = tiles.reshape(4, tiles.shape[0] // 4, 4, tiles.shape[1] // 4)
        tile_std = tiles.std(axis=(1, 3))
        tile_mean = tiles.mean(axis=(1, 3))
        dead = float((tile_std < 4.0).mean())
        # The smear's real signature is that one corner is far brighter than
        # the rest, so the sub-tile MEANS disagree even though each sub-tile
        # has internal gradient. A real floor is evenly lit across the crop.
        # Real rooms are unevenly lit, so this has to discriminate a smear from
        # ordinary falloff -- hence a generous divisor and a modest weight.
        uneven = min(float(tile_mean.std()) / 38.0, 1.0)

        score = (0.45 * exposure + 0.45 * texture
                 - 0.8 * clipped - 0.6 * outliers - 0.9 * dead - 0.55 * uneven)
    else:
        uniformity = 1.0 - min(spread / 45.0, 1.0)
        score = 0.45 * exposure + 0.45 * uniformity - 0.8 * clipped - 1.6 * outliers

    return score


def extract(geoms, predicate, wants_detail, size, candidates):
    best = None

    for g in geoms:
        vis = getattr(g, "visual", None)
        uv = getattr(vis, "uv", None)
        mat = getattr(vis, "material", None)
        img = getattr(mat, "image", None)
        if uv is None or img is None:
            continue

        n = g.face_normals
        sel = np.where(predicate(n[:, 2]))[0]
        if len(sel) == 0:
            continue

        atlas = np.asarray(img.convert("RGB"))
        ah, aw = atlas.shape[:2]

        tri_uv = uv[g.faces[sel]]
        span = tri_uv.max(axis=1) - tri_uv.min(axis=1)
        footprint = span[:, 0] * span[:, 1]
        order = np.argsort(footprint)[::-1][:candidates]

        for i in order:
            c = tri_uv[i].mean(axis=0)
            # UV origin is bottom-left; image rows run top-down.
            cx = int(np.clip(c[0], 0, 1) * (aw - 1))
            cy = int((1.0 - np.clip(c[1], 0, 1)) * (ah - 1))
            half = size // 2
            x0 = int(np.clip(cx - half, 0, max(aw - size, 0)))
            y0 = int(np.clip(cy - half, 0, max(ah - size, 0)))
            crop = atlas[y0:y0 + size, x0:x0 + size]
            if crop.shape[0] != size or crop.shape[1] != size:
                continue
            s = patch_score(crop, wants_detail)
            if best is None or s > best[0]:
                best = (s, crop)

    return best


def median_colour(geoms, predicate):
    """Median atlas colour over a class, for the flat fallback."""
    samples = []
    for g in geoms:
        vis = getattr(g, "visual", None)
        uv = getattr(vis, "uv", None)
        mat = getattr(vis, "material", None)
        img = getattr(mat, "image", None)
        if uv is None or img is None:
            continue
        n = g.face_normals
        sel = np.where(predicate(n[:, 2]))[0]
        if len(sel) == 0:
            continue
        atlas = np.asarray(img.convert("RGB"))
        ah, aw = atlas.shape[:2]
        c = uv[g.faces[sel]].mean(axis=1)
        xs = np.clip((c[:, 0] * (aw - 1)).astype(int), 0, aw - 1)
        ys = np.clip(((1 - c[:, 1]) * (ah - 1)).astype(int), 0, ah - 1)
        samples.append(atlas[ys, xs])
    if not samples:
        return np.array([200, 200, 195], dtype=np.uint8)
    allc = np.vstack(samples)
    # Drop blown/dead pixels before taking the median.
    g = allc.mean(axis=1)
    keep = (g > 12) & (g < 245)
    if keep.sum() > 100:
        allc = allc[keep]
    return np.median(allc, axis=0).astype(np.uint8)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("mesh")
    ap.add_argument("-o", "--out", default="textures")
    ap.add_argument("--size", type=int, default=256)
    ap.add_argument("--candidates", type=int, default=250)
    args = ap.parse_args()

    scene = trimesh.load(args.mesh, process=False)
    geoms = list(scene.geometry.values()) if hasattr(scene, "geometry") else [scene]

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    for name, (predicate, wants_detail) in CLASSES.items():
        best = extract(geoms, predicate, wants_detail, args.size, args.candidates)
        path = out / f"{name}.png"

        if best is None or best[0] < MIN_SCORE:
            col = median_colour(geoms, predicate)
            Image.fromarray(np.tile(col, (args.size, args.size, 1)).astype(np.uint8)).save(path)
            got = "n/a" if best is None else f"{best[0]:.3f}"
            print(f"{name:<8} -> {path}   FLAT FALLBACK "
                  f"(best candidate {got} < {MIN_SCORE})  "
                  f"#{col[0]:02X}{col[1]:02X}{col[2]:02X}")
            continue

        score, crop = best
        Image.fromarray(crop).save(path)
        avg = crop.reshape(-1, 3).mean(axis=0).astype(int)
        print(f"{name:<8} -> {path}   score={score:.3f}  "
              f"mean #{avg[0]:02X}{avg[1]:02X}{avg[2]:02X}")


if __name__ == "__main__":
    main()
