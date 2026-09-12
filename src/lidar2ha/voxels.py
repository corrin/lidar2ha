#!/usr/bin/env python3
"""Voxelise a capture's mesh, and measure each room's air volume from the geometry.

WHY NOT EXTRUDE THE PLAN. The obvious way to get a room's volume is footprint times
ceiling height, and it is wrong in the cases that matter. A prism has the room's
outline and nothing else: no void over part of it, no rake, no stair run, no
split-level step down to a wing. Worse, it needs a ceiling height for every room, and
`ceilings` correctly refuses to supply one where the scan could not see -- so the
extrusion either drops those rooms or invents a number. One house had a 3.97 m den
filled in at 2.10 m from the median of its neighbours.

The mesh already holds all of it. So the mesh makes the cells and the plan only names
them:

    surface   a cell some triangle passes through, split by which way it faces
    air       a cell between a floor below it and a ceiling above it
    room      which plan polygon the cell's column falls inside, or none

THE AIR IS FOUND BY CASTING A RAY UP EACH COLUMN, not by flooding the interior, and
the reason is that a LiDAR mesh is not watertight. Flooding needs an enclosure and
these shells leak through every window, doorway, unscanned ceiling and open edge where
the walk stopped. Measured on one three-storey walk at 10 cm cells:

    3D flood from the bounding box      1.1 m3 of "interior"
    fill holes slice by slice          86   m3
    cast a ray up each column         265   m3

Walking up a column, an up-facing hit opens air and a down-facing hit closes it, so
the air is where the count of floors passed exceeds the count of ceilings passed. That
needs no enclosure at all. A void spanning two storeys is one open run; a split-level
step shows as neighbouring columns whose runs start 45 cm apart; and none of it has to
be told how many storeys the building has.

Usage:
    python -m lidar2ha.voxels model.json mesh.obj
    python -m lidar2ha.voxels model.json mesh.obj --cm 5 --json volumes.json
    python -m lidar2ha.voxels model.json mesh.obj --no-floor-fill   # show its effect
"""

from __future__ import annotations

import argparse
import json
from typing import Literal, NamedTuple

import numpy as np
import shapely
import trimesh
from shapely.geometry import Polygon

from .ceilings import room_in_mesh_frame
from .schema import Level, Model, load_model

Facing = Literal["up", "down", "side"]
FACINGS: tuple[Facing, ...] = ("up", "down", "side")

# cos 60 deg. A surface within 30 degrees of horizontal reads as floor or ceiling, so a
# raked ceiling at 20 degrees is still a ceiling. `ceilings` uses 0.85 because it wants
# flat slabs to take a percentile of; a ray cast needs the rake too, or a room under a
# pitched roof never closes and its air runs away to the top of the grid.
HORIZONTAL = 0.5
MIN_HEAD_M = 0.40        # a shorter run is the gap under a sofa, not a space
MIN_BODY_M3 = 0.5        # below this a body is quantisation noise
DOOR_W_M = 0.70          # the narrowest real opening, so never close gaps this wide


class Grid(NamedTuple):
    """Occupancy over one lattice, in metres, with the plan's names attached.

    `air`, `up`, `down` and `side` share a shape and an origin. `labels` is 2D because
    a room is a footprint: every cell in a column belongs to the same room, which is
    also what makes a double-height void attribute to the room underneath it.
    """

    air: np.ndarray
    up: np.ndarray
    down: np.ndarray
    side: np.ndarray
    labels: np.ndarray
    names: list[str]
    origin_m: np.ndarray
    cell_m: float

    @property
    def cell_volume_m3(self) -> float:
        return self.cell_m ** 3


def load_triangles(mesh_path: str) -> dict[Facing, np.ndarray]:
    """Triangles grouped by facing, each (N, 3, 3) in metres.

    `ceilings._classified_faces` returns triangle CENTRES, which is all a percentile
    needs; rasterising needs the triangles themselves, so this loads them here rather
    than widening that function's contract.
    """
    scene = trimesh.load(mesh_path, process=False)
    geoms = list(scene.geometry.values()) if hasattr(scene, "geometry") else [scene]
    out: dict[Facing, list[np.ndarray]] = {k: [] for k in FACINGS}
    for g in geoms:
        tris = getattr(g, "triangles", None)
        normals = getattr(g, "face_normals", None)
        if tris is None or normals is None or not len(tris):
            continue
        nz = normals[:, 2]
        out["up"].append(tris[nz > HORIZONTAL])
        out["down"].append(tris[nz < -HORIZONTAL])
        out["side"].append(tris[np.abs(nz) <= HORIZONTAL])
    if not any(out[k] for k in FACINGS):
        raise SystemExit(f"no triangle geometry in {mesh_path}")
    empty = np.zeros((0, 3, 3))
    return {k: (np.vstack(out[k]) if out[k] else empty) for k in FACINGS}


def rasterise(tris: dict[Facing, np.ndarray], cell_m: float, pad: int = 2
              ) -> tuple[dict[Facing, np.ndarray], np.ndarray]:
    """Stamp every triangle into a boolean grid per facing. Returns (grids, origin).

    Triangles are sampled barycentrically at half the cell pitch, bucketed by how many
    samples each needs so one numpy call covers every triangle of a given size.
    Stamping only the three corners leaves holes wherever a triangle spans more than a
    cell, and a hole in a floor is a column that never opens.
    """
    pts = np.vstack([t.reshape(-1, 3) for t in tris.values() if len(t)])
    lo = pts.min(axis=0) - pad * cell_m
    hi = pts.max(axis=0) + pad * cell_m
    shape = tuple(int((hi[k] - lo[k]) / cell_m) + 1 for k in range(3))
    grids = {k: np.zeros(shape, dtype=bool) for k in FACINGS}
    dims = np.array(shape) - 1

    def stamp(arr: np.ndarray, P: np.ndarray) -> None:
        ijk = ((P - lo) / cell_m).astype(np.int32)
        np.clip(ijk, 0, dims, out=ijk)
        arr[ijk[:, 0], ijk[:, 1], ijk[:, 2]] = True

    for key in FACINGS:
        T = tris[key]
        if not len(T):
            continue
        a, b, c = T[:, 0], T[:, 1], T[:, 2]
        arr = grids[key]
        stamp(arr, np.vstack([a, b, c]))
        longest = np.maximum.reduce([np.linalg.norm(b - a, axis=1),
                                     np.linalg.norm(c - b, axis=1),
                                     np.linalg.norm(a - c, axis=1)])
        need = np.ceil(longest / (cell_m / 2.0)).astype(np.int32)
        for n in range(2, int(need.max()) + 1):
            sel = need == n
            if not sel.any():
                continue
            grid_u, grid_v = np.meshgrid(np.linspace(0, 1, n + 1),
                                         np.linspace(0, 1, n + 1))
            uu, vv = grid_u.ravel(), grid_v.ravel()
            keep = (uu + vv) <= 1.0
            A, B, C = a[sel], b[sel], c[sel]
            for u, v in zip(uu[keep], vv[keep], strict=True):
                stamp(arr, A + (B - A) * u + (C - A) * v)
    return grids, lo


def label_columns(level: Level, shape: tuple[int, ...], origin_m: np.ndarray,
                  cell_m: float) -> tuple[np.ndarray, list[str]]:
    """Which room owns each grid column, by the column centre. 0 means none.

    Through `ceilings.room_in_mesh_frame`, so the plan is carried onto the mesh by the
    level's own registration rather than by a second hand-written copy of that
    transform.

    A column inside no polygon stays 0, and that is a real answer rather than a
    failure: the mesh saw space the plan does not cover, usually a stair run, a
    cupboard, or a room nobody traced. It is reported, never folded into a neighbour.

    Room names are not unique in a real model -- one capture carries three `hallway`
    and two `stairwell`, some of them overlapping -- so a repeat gets a `#2` suffix
    instead of silently overwriting whichever came first.
    """
    nx, ny = shape[0], shape[1]
    cx = origin_m[0] + (np.arange(nx) + 0.5) * cell_m
    cy = origin_m[1] + (np.arange(ny) + 0.5) * cell_m
    gx, gy = np.meshgrid(cx, cy, indexing="ij")
    px, py = gx.ravel(), gy.ravel()
    labels = np.zeros((nx, ny), dtype=np.int32)
    names: list[str] = ["<unlabelled>"]
    seen: dict[str, int] = {}
    for room in level.rooms:
        poly: Polygon = room_in_mesh_frame(room, level.registration)
        if not poly.is_valid:
            poly = poly.buffer(0)
        if poly.is_empty or getattr(poly, "exterior", None) is None:
            continue
        raw = room.name or "<unnamed>"
        n = seen[raw] = seen.get(raw, 0) + 1
        names.append(raw if n == 1 else f"{raw}#{n}")
        # shapely rather than `matplotlib.path`, which is not a dependency of this
        # project: the module imported it at call time, so `pip install lidar2ha`
        # produced a `voxels` that raised ModuleNotFoundError on every real model
        # while the tests passed on a machine that happened to have it.
        inside = shapely.contains_xy(poly, px, py)
        labels[inside.reshape(nx, ny)] = len(names) - 1
    return labels, names


def room_floor_index(up: np.ndarray, labels: np.ndarray, n_names: int) -> np.ndarray:
    """Each room's own floor slab, as a z index into the grid. -1 where unknown.

    The mode of the up-facing surface inside a room's footprint is its floor, and
    taking it PER ROOM rather than per level is what a split-level building needs: one
    capture's upstairs splits into two groups of rooms 45 cm apart, and a single floor
    plane puts half of them through the slab.

    Furniture tops are up-facing too and can outvote a floor that is mostly hidden, so
    this takes the lowest z holding a fifth of the modal count rather than the mode.
    """
    out = np.full(n_names, -1, dtype=np.int32)
    ii, jj, kk = np.nonzero(up)
    if not ii.size:
        return out
    owner = labels[ii, jj]
    for r in range(1, n_names):
        ks = kk[owner == r]
        if ks.size < 4:
            continue
        counts = np.bincount(ks)
        threshold = max(2, int(counts.max()) // 5)
        cand = np.nonzero(counts >= threshold)[0]
        out[r] = int(cand.min())
    return out


def air_from_columns(grids: dict[Facing, np.ndarray], cell_m: float, *,
                     min_head_m: float = MIN_HEAD_M,
                     floor_index: np.ndarray | None = None,
                     labels: np.ndarray | None = None) -> np.ndarray:
    """Air between each floor hit and the ceiling hit above it, column by column."""
    up = grids["up"]
    if floor_index is not None and labels is not None:
        # A column whose LOWEST hit faces DOWN never saw its own floor: a bed or a
        # wardrobe stands on it, so nothing there looks up. Such a column opens no air
        # at all, which is why mesh footprints come out 15-50% short of the traced room
        # areas. Open it at that room's measured floor instead.
        up = up.copy()
        hits = up | grids["down"]
        has = hits.any(axis=2)
        first = np.where(has, np.argmax(hits, axis=2), 0)
        lowest_is_up = np.take_along_axis(up, first[:, :, None], axis=2)[:, :, 0]
        i, j = np.nonzero(has & ~lowest_is_up)
        k = floor_index[labels[i, j]]
        ok = (k >= 0) & (k < first[i, j])
        up[i[ok], j[ok], k[ok]] = True

    opens = np.cumsum(up, axis=2)
    closes = np.cumsum(grids["down"], axis=2)
    closes_below = np.zeros_like(closes)
    closes_below[:, :, 1:] = closes[:, :, :-1]
    air = (opens - closes_below) > 0
    air &= ~(grids["up"] | grids["down"] | grids["side"])

    if min_head_m > 0:
        from scipy import ndimage

        # Label along z only, so a run is one column's worth and nothing merges
        # sideways before the short ones are dropped.
        structure = np.zeros((3, 3, 3), dtype=bool)
        structure[1, 1, :] = True
        lab, n = ndimage.label(air, structure=structure)
        if n:
            counts = np.bincount(lab.ravel())
            small = counts < max(1, int(round(min_head_m / cell_m)))
            small[0] = True
            air &= ~small[lab]
    return air


def build(model: Model, mesh_path: str, cell_m: float, *,
          floor_fill: bool = True, min_head_m: float = MIN_HEAD_M,
          level_index: int = 0) -> Grid:
    """The whole pipeline for one level of one capture."""
    tris = load_triangles(mesh_path)
    grids, origin = rasterise(tris, cell_m)
    level = model.levels[level_index]
    labels, names = label_columns(level, grids["up"].shape, origin, cell_m)
    fi = room_floor_index(grids["up"], labels, len(names)) if floor_fill else None
    air = air_from_columns(grids, cell_m, min_head_m=min_head_m, floor_index=fi,
                           labels=labels if floor_fill else None)
    return Grid(air=air, up=grids["up"], down=grids["down"], side=grids["side"],
                labels=labels, names=names, origin_m=origin, cell_m=cell_m)


class RoomVolume(NamedTuple):
    name: str
    volume_m3: float
    footprint_m2: float

    @property
    def mean_height_m(self) -> float:
        return self.volume_m3 / self.footprint_m2 if self.footprint_m2 else 0.0


def volumes(grid: Grid) -> list[RoomVolume]:
    """Measured air volume per room, largest first. No ceiling height is consulted.

    `mean_height_m` is a by-product worth having: it is the height a prism would need
    to hold the same air, which for a room with a void over part of it is neither the
    low ceiling nor the high one.
    """
    owner = np.broadcast_to(grid.labels[:, :, None], grid.air.shape)
    counts = np.bincount(owner[grid.air].ravel(), minlength=len(grid.names))
    cell_area = grid.cell_m ** 2
    out = []
    for i, name in enumerate(grid.names):
        if not counts[i]:
            continue
        footprint = ((grid.labels == i)[:, :, None] & grid.air).any(axis=2)
        out.append(RoomVolume(name, counts[i] * grid.cell_volume_m3,
                              float(footprint.sum()) * cell_area))
    return sorted(out, key=lambda r: -r.volume_m3)


def close_walls(side: np.ndarray, close_m: float, cell_m: float) -> np.ndarray:
    """`side` with pinholes bridged, for use as a flood barrier only.

    NOT by 3D or 2D closing, neither of which can do this job. A wall is one or two
    cells thick, and closing a thin sheet fails in the direction PERPENDICULAR to it:
    the dilation that covers a hole is eroded away again, because the cells on either
    side of the sheet have no solid neighbour of their own to be dilated from.
    Measured on a 2 x 4 cell hole in one wall, 3D closing at one, two and three
    iterations added 144, 432 and 120 cells elsewhere and left all eight hole cells
    open.

    What works is closing along each axis INDEPENDENTLY and taking the union. A gap in
    a wall running north-south is a gap along that axis, so the 1D closing along it
    bridges the gap; the 1D closing across the wall's own thickness is a no-op rather
    than a thickening. A band of wall missing at one height is the same case along z.

    A door is `DOOR_W_M`, so a gap wider than twice the iteration count stays open and
    the flood still passes through real openings.
    """
    from scipy import ndimage

    iterations = max(1, int(round(close_m / cell_m / 2)))
    out = np.zeros_like(side)
    for axis in range(3):
        structure = np.zeros((3, 3, 3), dtype=bool)
        if axis == 0:
            structure[:, 1, 1] = True
        elif axis == 1:
            structure[1, :, 1] = True
        else:
            structure[1, 1, :] = True
        out |= ndimage.binary_closing(side, structure=structure,
                                      iterations=iterations)
    return out


class Body(NamedTuple):
    """One connected body of air, and every room whose footprint it reaches into."""

    volume_m3: float
    rooms: list[str]


def bodies(grid: Grid, close_m: float = 0.0, min_m3: float = MIN_BODY_M3) -> list[Body]:
    """Connected bodies of air, largest first, each with the rooms it spans.

    This answers "are these two spaces one body of air", which a table of shared walls
    cannot: an opening with no door in it and a wall with a door in it have the same
    shared-wall area.

    A scanned wall has pinholes -- centimetres of missing triangles where the walk
    moved fast or a surface was dark -- and one missing cell lets the flood pour into
    the next room, so raw connectivity reports a whole storey as one space. `close_m`
    bridges gaps up to that width.

    THE CLOSED MASK IS A BARRIER, NOT A WALL. It only stops the flood; the volumes are
    measured from the true surface and are untouched by it. Dilating into the same
    array a volume is measured from is a real and costly bug: it made every published
    figure in one project 10-79% too small. Keep `close_m` under `DOOR_W_M` or the
    flood stops passing through real openings.
    """
    from scipy import ndimage

    barrier = grid.side if close_m <= 0 else close_walls(grid.side, close_m,
                                                         grid.cell_m)
    occupancy = grid.air & ~barrier
    lab, n = ndimage.label(occupancy)
    sizes = ndimage.sum(occupancy, lab, range(1, n + 1))
    out: list[Body] = []
    for c in np.argsort(sizes)[::-1]:
        vol = float(sizes[c]) * grid.cell_volume_m3
        if vol < min_m3:
            continue
        footprint = (lab == c + 1).any(axis=2)
        rooms = sorted({grid.names[i] for i in np.unique(grid.labels[footprint]) if i})
        out.append(Body(round(vol, 1), rooms))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("model", help="a capture's model.json, after `rooms`")
    ap.add_argument("mesh", help="that capture's mesh, exported Z-up")
    ap.add_argument("--cm", type=float, default=10.0, help="voxel edge in cm")
    ap.add_argument("--level", type=int, default=0, help="which level of the model")
    ap.add_argument("--no-floor-fill", action="store_true",
                    help="leave columns closed where furniture hides the floor")
    ap.add_argument("--close-cm", type=float, default=0.0,
                    help="bridge wall gaps this wide for connectivity only; "
                         "0 sweeps 0-40 cm and reports each")
    ap.add_argument("--json", help="write the measurements here")
    args = ap.parse_args()

    model = load_model(args.model)
    grid = build(model, args.mesh, args.cm / 100.0,
                 floor_fill=not args.no_floor_fill, level_index=args.level)

    total = float(grid.air.sum()) * grid.cell_volume_m3
    print(f"grid {' x '.join(str(n) for n in grid.air.shape)} at {args.cm:g} cm, "
          f"origin {np.round(grid.origin_m, 2)} m")
    print(f"  surface  up {grid.up.sum()}, down {grid.down.sum()}, "
          f"side {grid.side.sum()} cells")
    print(f"  air      {total:.1f} m3\n")

    rows = volumes(grid)
    print("MEASURED air volume per room. No ceiling height is used.\n")
    print(f"  {'room':<34} {'m3':>8} {'floor m2':>9} {'mean h m':>9}")
    for r in rows:
        print(f"  {r.name:<34} {r.volume_m3:8.1f} {r.footprint_m2:9.1f} "
              f"{r.mean_height_m:9.2f}")
    named = sum(r.volume_m3 for r in rows if r.name != "<unlabelled>")
    share = named / total * 100 if total else 0.0
    print(f"\n  {named:.1f} m3 of {total:.1f} m3 fell inside a traced room "
          f"({share:.0f}%). The rest is space the\n  mesh saw and the plan does not "
          f"cover -- a MISSING ROOM, never something to fold\n  into a neighbour.")

    print(f"\nCONNECTIVITY. Gaps closed for the flood only; the volumes above are\n"
          f"unaffected. A door is {DOOR_W_M * 100:.0f} cm, so closing much past that "
          f"seals real openings.\n")
    print(f"  {'close cm':>9} {'bodies':>7} {'largest m3':>11}   rooms in the largest")
    sweep = [args.close_cm] if args.close_cm else [0.0, 10.0, 20.0, 30.0, 40.0]
    report = []
    for cc in sweep:
        bs = bodies(grid, cc / 100.0)
        top = bs[0] if bs else Body(0.0, [])
        shown = ", ".join(top.rooms[:6]) or "--"
        if len(top.rooms) > 6:
            shown += f", +{len(top.rooms) - 6} more"
        print(f"  {cc:>9.0f} {len(bs):>7} {top.volume_m3:>11}   {shown}")
        report.append({"close_cm": cc,
                       "bodies": [b._asdict() for b in bs]})

    if args.json:
        payload = {
            "voxel_cm": args.cm,
            "origin_m": [round(float(v), 4) for v in grid.origin_m],
            "grid": list(grid.air.shape),
            "method": "mesh voxels, air by column ray cast",
            "air_m3": round(total, 1),
            "rooms": [{"name": r.name, "volume_m3": round(r.volume_m3, 2),
                       "footprint_m2": round(r.footprint_m2, 2),
                       "mean_height_m": round(r.mean_height_m, 3)} for r in rows],
            "connectivity": report,
        }
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=1)
        print(f"\n  wrote {args.json}")


if __name__ == "__main__":
    main()
