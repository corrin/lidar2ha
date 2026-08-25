"""Separating the storeys inside one multi-storey capture.

Walking a whole house in one go is now the ordinary way to capture it, and
Polycam lays the storeys onto one sheet. `split_into_floors` separates that
layout by clustering on X, and on most captures the cluster IS the storey.

On one it was not. A cluster came back holding rooms whose ceilings sit at 210,
480 and 710 cm -- three storeys, stacked, with a `Bedroom` at cx 2.22 m and an
`Office` at 2.45 m: 23 cm apart in plan and a storey apart in the building.
Nothing downstream can use a level like that. `combine` fits it as one rigid
body, `compare` returns a fit averaging two storeys, and `ceilings` measures a
ceiling and a stairwell together.

Measured against three known storeys, that capture as imported read 5.8 cm
against the ground floor and 5.4 against the top one -- a coin flip, because it
genuinely contains both. Cut on the ceiling band, the low group reads 4.6 cm
against the ground floor with its next-best 20.8, and the high group 4.3 cm
against the top with its next-best 19.4.
"""

from __future__ import annotations

from lidar2ha.polycam import (
    STOREY_M,
    doors_touching,
    split_into_storeys,
    walls_touching,
)


def room(name: str, low: float, high: float, x: float = 0.0, y: float = 0.0,
         size: float = 3.0) -> dict:
    """A square room in metres, with the ceiling Polycam would report."""
    return {
        "name": name,
        "ceiling_low": low,
        "ceiling_high": high,
        "cx": x + size / 2,
        "cy": y + size / 2,
        "points": [(x, y), (x + size, y), (x + size, y + size), (x, y + size)],
    }


def wall(x0: float, y0: float, x1: float, y1: float) -> dict:
    return {"start": (x0, y0), "end": (x1, y1), "thickness": 0.1,
            "cx": (x0 + x1) / 2, "cy": (y0 + y1) / 2}


# --------------------------------------------------------------------------- #
# finding the storeys
# --------------------------------------------------------------------------- #


def test_three_ceiling_bands_become_three_storeys():
    """The failure this exists for. One sheet cluster, three storeys stacked --
    and Polycam reports a ceiling above the CAPTURE DATUM, so the height is the
    storey even though the plan positions overlap."""
    rooms = [room("Bedroom", 2.10, 2.10), room("Living Room 2", 4.80, 4.80),
             room("Office", 7.10, 7.10), room("Other 5", 2.10, 2.10)]
    storeys, shafts = split_into_storeys(rooms)

    assert shafts == []
    assert [len(rs) for _, rs in storeys] == [2, 1, 1]
    assert [round(c, 2) for c, _ in storeys] == [2.10, 4.80, 7.10]


def test_one_band_is_left_exactly_as_it_arrived():
    """Every capture that already worked has one band, so this is the path that
    must not change -- including the ORDER, which is what a consumer matching
    rooms positionally relies on."""
    rooms = [room("a", 2.2, 2.2), room("b", 2.4, 2.4), room("c", 2.1, 2.1)]
    storeys, shafts = split_into_storeys(rooms)

    assert shafts == []
    assert len(storeys) == 1
    assert [r["name"] for _, rs in storeys for r in rs] == ["a", "b", "c"]


def test_two_floors_closer_than_half_a_storey_are_not_split():
    """A 24 cm difference is two rooms with slightly different ceilings, not two
    floors. Splitting there would turn one storey into two levels that each hold
    part of a house."""
    rooms = [room("a", 2.18, 2.18), room("b", 2.42, 2.42)]
    gap = 2.42 - 2.18
    assert gap < STOREY_M / 2, f"if these are far apart the test proves nothing ({gap})"

    storeys, _ = split_into_storeys(rooms)
    assert len(storeys) == 1


def test_a_room_taller_than_a_storey_is_a_shaft_and_joins_no_band():
    """A stairwell is the one room that genuinely belongs to no floor, and its
    SPAN says so without needing a mesh. Averaged into whichever band its
    midpoint lands in, it would drag a storey's geometry with it."""
    rooms = [room("Bedroom", 2.10, 2.10), room("Living Room 1", 3.80, 8.00)]
    storeys, shafts = split_into_storeys(rooms)

    assert [r["name"] for r in shafts] == ["Living Room 1"]
    assert [r["name"] for _, rs in storeys for r in rs] == ["Bedroom"]


def test_a_room_with_no_ceiling_reading_does_not_become_its_own_storey():
    """Missing evidence is not evidence of a separate floor. It stays with the
    first band rather than inventing a level nothing else agrees exists."""
    rooms = [room("known", 2.1, 2.1), {**room("unknown", 0, 0),
                                       "ceiling_low": None, "ceiling_high": None}]
    storeys, shafts = split_into_storeys(rooms)

    assert shafts == []
    assert len(storeys) == 1
    assert {r["name"] for _, rs in storeys for r in rs} == {"known", "unknown"}


# --------------------------------------------------------------------------- #
# which walls belong to which storey
# --------------------------------------------------------------------------- #


def test_a_wall_can_belong_to_more_than_one_storey():
    """The envelope of a building runs its full height, so it is a wall of the
    ground floor AND of the top one. An exclusive split would give one storey
    the envelope and leave the others holding partitions only.

    There is also no rule that could split it: Polycam keys ceiling heights by
    ROOM, so a wall carries no height, and stacked storeys put their walls
    centimetres apart in plan.
    """
    from lidar2ha.polycam import band_footprint

    low = [room("ground", 2.1, 2.1)]
    high = [room("top", 7.1, 7.1)]      # same footprint, a storey up
    shared = wall(0.0, 0.0, 3.0, 0.0)   # the south wall of both

    assert band_footprint(low).equals(band_footprint(high)), (
        "if the footprints differ this is not the stacked case")
    assert shared in walls_touching([shared], band_footprint(low))
    assert shared in walls_touching([shared], band_footprint(high))


def test_a_wall_across_the_sheet_belongs_to_neither():
    """Non-exclusive is not unconditional. A wall nowhere near a band's rooms is
    not that band's, or every level would hold the whole capture and the split
    would achieve nothing."""
    here = [room("here", 2.1, 2.1, x=0.0, y=0.0)]
    far = wall(50.0, 50.0, 53.0, 50.0)

    from lidar2ha.polycam import band_footprint
    assert walls_touching([far], band_footprint(here)) == []


def test_a_door_goes_to_the_bands_whose_rooms_it_opens_into():
    """Doors are points rather than lines, so they are tested by containment --
    a door in the far corner of the sheet is not this storey's."""
    from lidar2ha.polycam import band_footprint

    rooms = [room("r", 2.1, 2.1, x=0.0, y=0.0, size=3.0)]
    inside = {"cx": 1.5, "cy": 1.5, "width": 0.8, "depth": 0.1}
    outside = {"cx": 50.0, "cy": 50.0, "width": 0.8, "depth": 0.1}

    got = doors_touching([inside, outside], band_footprint(rooms))
    assert got == [inside]
