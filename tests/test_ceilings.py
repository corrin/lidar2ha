"""Measuring a room's ceiling from the mesh, and writing it back.

`seams` leaves every piece it cuts with no height at all, and `ceilings` could
measure them but only print. One real level ended up with 10 of its 14 rooms
carrying no ceiling, so `lights.elevation_for` fell through to the LEVEL's
height -- which on a level containing a void put 8 of 12 fittings at 450 cm,
including a lounge lamp and an under-cabinet light.

The measurement was right the whole time. It could not reach the model.
"""

from __future__ import annotations

import numpy as np

from lidar2ha.ceilings import measure, measure_room, write_back
from lidar2ha.schema import Level, Model, Room

MIN_FACES = 20


def faces(x0: float, y0: float, w: float, h: float, z: float,
          n: int = 64) -> np.ndarray:
    """Face centres spread over a footprint at height `z`, in metres."""
    side = int(np.sqrt(n))
    xs = np.linspace(x0 + w * 0.15, x0 + w * 0.85, side)
    ys = np.linspace(y0 + h * 0.15, y0 + h * 0.85, side)
    gx, gy = np.meshgrid(xs, ys)
    return np.column_stack([gx.ravel(), gy.ravel(), np.full(gx.size, z)])


def room_at(name: str, x0: float, y0: float, w: float, h: float,
            **kw) -> Room:
    """A room in plan centimetres, from metres."""
    c = 100.0
    return Room(name=name, points=[(x0 * c, y0 * c), ((x0 + w) * c, y0 * c),
                                   ((x0 + w) * c, (y0 + h) * c),
                                   (x0 * c, (y0 + h) * c)], **kw)


def level_of(*rooms: Room) -> Model:
    return Model(source="t.dxf", units="cm",
                 levels=[Level(name="L", ceiling_height_cm=470, rooms=list(rooms))])


def test_a_measured_room_gets_the_height_written():
    """The whole point. Without this the room keeps no height and `lights`
    falls back to the level's, which is the void's height, not the room's."""
    room = room_at("lounge", 0, 0, 4, 3)
    up, down = faces(0, 0, 4, 3, 0.0), faces(0, 0, 4, 3, 2.64)
    m = measure_room(room, down, up, mesh_top=5.0)

    assert m.verdict == "measured"
    assert m.p95_cm is not None and abs(m.p95_cm - 264) < 1
    write_back([(None, room, m)])
    assert room.ceiling_high_cm is not None
    assert abs(room.ceiling_high_cm - 264) < 1


def test_two_rooms_of_the_same_name_get_their_own_heights():
    """The trap in a split model, and the one thing a name-keyed writer gets
    wrong silently.

    A split piece is named for the AREA it belongs to, and several pieces can
    belong to one area -- one real level carries three rooms called `hallway`
    at 220, 323 and 264 cm, and two called `stairwell` at 446 and 303. Keyed by
    name, two thirds of them take another piece's height and nothing says so.
    """
    low = room_at("hallway", 0, 0, 3, 3)
    high = room_at("hallway", 10, 0, 3, 3)
    assert low.name == high.name, "if the names differ this tests nothing"

    down = np.vstack([faces(0, 0, 3, 3, 2.20), faces(10, 0, 3, 3, 3.23)])
    up = np.vstack([faces(0, 0, 3, 3, 0.0), faces(10, 0, 3, 3, 0.0)])

    measured = measure(level_of(low, high), down, up, mesh_top=6.0)
    write_back(measured)

    assert low.ceiling_high_cm is not None and high.ceiling_high_cm is not None
    assert abs(low.ceiling_high_cm - 220) < 2
    assert abs(high.ceiling_high_cm - 323) < 2
    assert low.ceiling_high_cm != high.ceiling_high_cm


def test_a_room_the_scan_did_not_reach_is_left_alone():
    """A truncated p95 is a LOWER BOUND -- the phone stopped before the room
    did. Written as a height it is indistinguishable from a measurement, and
    `build` puts furniture at it and `render` raytraces from it."""
    room = room_at("void", 0, 0, 4, 4, ceiling_high_cm=700.0)
    up, down = faces(0, 0, 4, 4, 0.0), faces(0, 0, 4, 4, 5.92)
    m = measure_room(room, down, up, mesh_top=5.95)

    assert m.verdict == "truncated"
    assert not m.writable
    assert write_back([(None, room, m)]) == []
    assert room.ceiling_high_cm == 700.0, "a lower bound overwrote a real height"


def test_a_room_the_mesh_cannot_see_stays_unset():
    """An absent height is a question a person can answer. A fabricated one is
    an answer nobody can tell from a real measurement."""
    room = room_at("cupboard", 50, 50, 1, 1)
    up, down = faces(0, 0, 4, 4, 0.0), faces(0, 0, 4, 4, 2.4)
    m = measure_room(room, down, up, mesh_top=5.0)

    assert m.verdict == "unseen"
    assert write_back([(None, room, m)]) == []
    assert room.ceiling_high_cm is None


def test_the_median_is_never_written_as_the_low_ceiling():
    """Down-facing faces include the undersides of tables, worktops and stair
    soffits, so the median is furniture height in exactly the rooms that have
    furniture. `lights.elevation_for` prefers `ceiling_low_cm`, so writing the
    median there hangs the lamp over the dining table at the height of the
    dining table."""
    room = room_at("dining", 0, 0, 4, 4)
    # Half the down-facing faces are a table at 75 cm, half the real ceiling.
    down = np.vstack([faces(0, 0, 4, 2, 0.75), faces(0, 2, 4, 2, 2.65)])
    up = faces(0, 0, 4, 4, 0.0)
    m = measure_room(room, down, up, mesh_top=5.0)

    assert m.p50_cm is not None and m.p50_cm < 200, (
        "if the median is not dragged down by the table this proves nothing")
    write_back([(None, room, m)])
    assert room.ceiling_low_cm is None, "furniture height was written as a ceiling"
    assert room.ceiling_high_cm is not None
    assert abs(room.ceiling_high_cm - 265) < 5


def test_the_height_is_measured_above_that_room_s_own_floor():
    """A split-level storey has no single floor. Measuring against a level-wide
    reference gives the lower half a ceiling 50 cm too tall and the upper half
    one 50 cm too short."""
    upper = room_at("bedroom", 0, 0, 3, 3)
    lower = room_at("hall", 10, 0, 3, 3)
    # Both have a 2.4 m ceiling; their floors are half a metre apart.
    down = np.vstack([faces(0, 0, 3, 3, 2.40), faces(10, 0, 3, 3, 1.90)])
    up = np.vstack([faces(0, 0, 3, 3, 0.0), faces(10, 0, 3, 3, -0.50)])

    write_back(measure(level_of(upper, lower), down, up, mesh_top=6.0))
    assert upper.ceiling_high_cm is not None and lower.ceiling_high_cm is not None
    assert abs(upper.ceiling_high_cm - 240) < 2
    assert abs(lower.ceiling_high_cm - 240) < 2


def test_what_changed_is_reported_room_by_room():
    """Nothing is written silently: a height that moves is a height somebody
    may need to argue with, and the before value is the argument."""
    room = room_at("kitchen", 0, 0, 3, 3, ceiling_high_cm=470.0)
    up, down = faces(0, 0, 3, 3, 0.0), faces(0, 0, 3, 3, 2.48)

    changes = write_back([(None, room, measure_room(room, down, up, mesh_top=5.0))])
    assert len(changes) == 1
    assert changes[0].room is room
    assert changes[0].before_cm == 470.0
    assert abs(changes[0].after_cm - 248) < 2


def test_a_stale_low_that_contradicts_the_measurement_is_cleared():
    """The bug the real house caught and the synthetic tests did not.

    Polycam's own figure sits above what the mesh measures -- four rooms of one
    real level came out 4 to 12 cm over. Writing only `ceiling_high_cm` left
    `ceiling_low_cm` ABOVE it, which is not a ceiling range but two unrelated
    numbers. And since `elevation_for` reads the low one first, those were
    exactly the rooms where this whole stage would have run, reported success,
    and changed nothing.
    """
    room = room_at("bedroom", 0, 0, 3, 3,
                   ceiling_low_cm=270.0, ceiling_high_cm=270.0)
    up, down = faces(0, 0, 3, 3, 0.0), faces(0, 0, 3, 3, 2.66)

    changes = write_back([(None, room, measure_room(room, down, up, mesh_top=5.0))])
    assert room.ceiling_high_cm is not None
    assert room.ceiling_low_cm is None, "a stale low still overrides the measurement"
    assert changes[0].cleared_low_cm == 270.0, "the clearing has to be reported"


def test_a_low_that_still_fits_under_the_measurement_is_kept():
    """A genuine range is not damage. Nothing here measures the bottom of a
    raked ceiling, so a low that sits below the measured high is information
    this stage has no grounds to discard."""
    room = room_at("attic", 0, 0, 3, 3,
                   ceiling_low_cm=150.0, ceiling_high_cm=400.0, sloped=True)
    up, down = faces(0, 0, 3, 3, 0.0), faces(0, 0, 3, 3, 2.80)

    changes = write_back([(None, room, measure_room(room, down, up, mesh_top=5.0))])
    assert room.ceiling_low_cm == 150.0
    assert changes[0].cleared_low_cm is None
