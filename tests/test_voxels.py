"""Recovering a room's air from the mesh, when the mesh is not watertight.

`ceilings` gives a room one number. That number cannot describe a room with a void
over part of it, and it does not exist at all for a room the scan could not see above.
So volumes built by extruding a footprint are either wrong or missing, and one house
had a 3.97 m den filled in at 2.10 m from the median of its neighbours.

Casting a ray up each column needs no ceiling height and, more importantly, no
watertight shell -- which is the property every LiDAR capture lacks. These tests pin
the behaviours that earn that claim: air between a floor and the ceiling above it, a
hole in a wall that does NOT drain the room, a void counted once, and a wall gap that
merges two rooms until it is closed.
"""

from __future__ import annotations

import numpy as np

from lidar2ha.schema import Level, Model, Room
from lidar2ha.voxels import (
    Grid,
    air_from_columns,
    bodies,
    label_columns,
    room_floor_index,
    volumes,
)

CELL = 0.1          # 10 cm, the default


def blank(shape: tuple[int, int, int]) -> dict[str, np.ndarray]:
    return {k: np.zeros(shape, dtype=bool) for k in ("up", "down", "side")}


def box(grids: dict[str, np.ndarray], i0: int, i1: int, j0: int, j1: int,
        floor_k: int, ceil_k: int) -> None:
    """A floor, a ceiling, and four walls, as a scanner would see them from inside."""
    grids["up"][i0:i1, j0:j1, floor_k] = True
    grids["down"][i0:i1, j0:j1, ceil_k] = True
    for i in (i0, i1 - 1):
        grids["side"][i, j0:j1, floor_k:ceil_k] = True
    for j in (j0, j1 - 1):
        grids["side"][i0:i1, j, floor_k:ceil_k] = True


def room_at(name: str, x0: float, y0: float, w: float, h: float) -> Room:
    """A room in plan centimetres, from metres."""
    c = 100.0
    return Room(name=name, points=[(x0 * c, y0 * c), ((x0 + w) * c, y0 * c),
                                   ((x0 + w) * c, (y0 + h) * c),
                                   (x0 * c, (y0 + h) * c)])


def model_of(*rooms: Room) -> Model:
    return Model(source="t.dxf", units="cm",
                 levels=[Level(name="L", ceiling_height_cm=250, rooms=list(rooms))])


def test_air_fills_between_the_floor_and_the_ceiling_above_it():
    g = blank((20, 20, 30))
    box(g, 2, 12, 2, 12, floor_k=3, ceil_k=23)
    air = air_from_columns(g, CELL)

    # 8 x 8 interior columns, 19 cells of air between the slabs
    assert air[:, :, :3].sum() == 0, "nothing below the floor"
    assert air[:, :, 24:].sum() == 0, "nothing above the ceiling"
    assert abs(air.sum() * CELL ** 3 - 8 * 8 * 19 * CELL ** 3) < 1e-9


def test_a_hole_in_a_wall_does_not_drain_the_room():
    """The whole reason for casting columns. A 3D flood leaks through any hole, and a
    real capture is full of them -- windows, doorways, the edge where the walk stopped.
    On one three-storey mesh a flood found 1.1 m3 against 265 m3 from this method."""
    sealed = blank((20, 20, 30))
    box(sealed, 2, 12, 2, 12, floor_k=3, ceil_k=23)
    holed = {k: v.copy() for k, v in sealed.items()}
    holed["side"][2, 5:9, 8:16] = False            # a window-sized hole
    holed["down"][4:9, 4:9, 23] = False            # and an unscanned patch of ceiling

    before = air_from_columns(sealed, CELL).sum()
    after = air_from_columns(holed, CELL).sum()
    # the unscanned ceiling patch loses those columns; the wall hole costs nothing
    assert after > before * 0.7
    assert after > 0


def test_a_double_height_void_is_one_run_and_is_counted_once():
    """A void spanning two storeys has no ceiling at the lower slab, so the column
    stays open through it. An extruded prism cannot represent this at all."""
    g = blank((20, 20, 60))
    box(g, 2, 12, 2, 12, floor_k=3, ceil_k=53)     # 5 m of headroom
    g["up"][2:12, 12:20, 3] = True                 # a normal room beside it
    g["down"][2:12, 12:20, 26] = True

    air = air_from_columns(g, CELL)
    tall = air[5, 5].sum()
    short = air[5, 15].sum()
    assert tall > short * 2, "the void should be far taller than the room beside it"
    # one contiguous run, not two stacked storeys
    runs = np.diff(np.concatenate([[0], air[5, 5].astype(int), [0]]))
    assert (runs == 1).sum() == 1


def test_a_floor_hidden_under_furniture_is_opened_at_the_rooms_own_slab():
    """A bed's underside faces down, so the column's lowest hit is a ceiling and the
    column never opens. Left alone this costs 15-50% of a room's footprint."""
    g = blank((20, 20, 30))
    box(g, 2, 12, 2, 12, floor_k=3, ceil_k=23)
    g["up"][4:8, 4:8, 3] = False                   # the floor under the bed is unseen
    g["down"][4:8, 4:8, 8] = True                  # the bed's underside

    labels = np.zeros((20, 20), dtype=np.int32)
    labels[2:12, 2:12] = 1
    fi = room_floor_index(g["up"], labels, 2)
    assert fi[1] == 3, "the room's floor is the lowest well-populated up-facing band"

    without = air_from_columns(g, CELL).sum()
    with_fill = air_from_columns(g, CELL, floor_index=fi, labels=labels).sum()
    assert with_fill > without


def test_two_rooms_merge_through_a_wall_gap_until_it_is_closed():
    """Speckle in a scanned wall is one missing cell wide and merges two rooms, so raw
    connectivity calls a whole storey one space. Closing is a flood BARRIER only and
    must never be subtracted from a volume."""
    g = blank((30, 20, 30))
    box(g, 2, 15, 2, 12, floor_k=3, ceil_k=23)
    box(g, 14, 27, 2, 12, floor_k=3, ceil_k=23)
    g["side"][14, 6:8, 8:12] = False               # 20 cm of missing wall
    g["side"][15, 6:8, 8:12] = False

    air = air_from_columns(g, CELL)
    labels = np.zeros((30, 20), dtype=np.int32)
    labels[2:15, 2:12] = 1
    labels[14:27, 2:12] = 2
    grid = Grid(air=air, up=g["up"], down=g["down"], side=g["side"], labels=labels,
                names=["<unlabelled>", "a", "b"], origin_m=np.zeros(3), cell_m=CELL)

    open_bodies = bodies(grid, close_m=0.0)
    assert sorted(open_bodies[0].rooms) == ["a", "b"], "the gap merges them"

    closed = bodies(grid, close_m=0.30)
    assert all(len(b.rooms) == 1 for b in closed[:2]), "closing separates them"
    # and the volume is untouched by the closing
    assert (sum(b.volume_m3 for b in closed)
            <= sum(b.volume_m3 for b in open_bodies) + 1e-9)


def test_volume_is_attributed_to_the_room_whose_footprint_holds_the_column():
    g = blank((30, 20, 30))
    box(g, 2, 15, 2, 12, floor_k=3, ceil_k=23)
    box(g, 16, 27, 2, 12, floor_k=3, ceil_k=13)    # half the height
    air = air_from_columns(g, CELL)
    labels = np.zeros((30, 20), dtype=np.int32)
    labels[2:15, 2:12] = 1
    labels[16:27, 2:12] = 2
    grid = Grid(air=air, up=g["up"], down=g["down"], side=g["side"], labels=labels,
                names=["<unlabelled>", "tall", "short"], origin_m=np.zeros(3),
                cell_m=CELL)

    rows = {r.name: r for r in volumes(grid)}
    assert rows["tall"].volume_m3 > rows["short"].volume_m3
    assert rows["tall"].mean_height_m > rows["short"].mean_height_m * 1.5


def test_repeated_room_names_get_their_own_label():
    """Room names are not unique in a real model: one capture carries three `hallway`
    and two `stairwell`. Silently overwriting loses a zone from every total."""
    model = model_of(room_at("hallway", 0, 0, 2, 2), room_at("hallway", 3, 0, 2, 2))
    labels, names = label_columns(model.levels[0], (60, 40, 10),
                                  np.array([-1.0, -1.0, 0.0]), CELL)

    assert names == ["<unlabelled>", "hallway", "hallway#2"]
    assert set(np.unique(labels)) == {0, 1, 2}


def test_a_worktop_does_not_open_the_column_through_its_own_ceiling():
    """An up-facing surface with no underside must not leak air to the grid top.

    A worktop, a shelf, a windowsill and a stair tread are all up-facing with a
    side-facing front and nothing looking down, because a handheld walk never gets
    under them. Counting floors against ceilings leaves each one adding a permanent
    1 to the balance, so the room's own ceiling only brings it back to 1 and the air
    runs up through every storey above.
    """
    bare = blank((10, 10, 60))
    box(bare, 0, 10, 0, 10, floor_k=3, ceil_k=23)
    worktop = blank((10, 10, 60))
    box(worktop, 0, 10, 0, 10, floor_k=3, ceil_k=23)
    worktop["up"][3:7, 3:7, 9] = True
    worktop["side"][3:7, 3:7, 8] = True

    before = air_from_columns(bare, CELL)
    after = air_from_columns(worktop, CELL)

    assert before[:, :, 24:].sum() == 0, "if this fails the test proves nothing"
    assert after[:, :, 24:].sum() == 0, "air escaped above the ceiling"
    assert after.sum() < before.sum(), (
        "a worktop displaces air, so the room cannot hold more with one in it")
