"""Room identity: HA areas in, scanner guesses out.

The scanner's names are guesses and its splits are artefacts. Both failure
modes are quiet -- a plan with a room called "Office 1" looks fine until you
try to bind it to an area that does not exist, and an open kitchen split in two
looks fine until lights group by the wrong half.
"""

from __future__ import annotations

from scan2ha.rooms import apply, polygon_of
from scan2ha.schema import Level, Model, Room


def model_with(*rooms: Room) -> Model:
    return Model(source="x.dxf",
                 levels=[Level(name="Ground", ceiling_height_cm=250, rooms=list(rooms))])


def square(name, x0, x1, y0=0, y1=300, low=240.0, high=240.0) -> Room:
    return Room(name=name, points=[(x0, y0), (x1, y0), (x1, y1), (x0, y1)],
                ceiling_low_cm=low, ceiling_high_cm=high)


def test_rename_records_the_scanner_name_it_replaced():
    model = model_with(square("Living Room", 0, 400))
    renamed, merged, unmapped = apply(model, {"Living Room": "entrance_hall"}, [])

    room = model.levels[0].rooms[0]
    assert (renamed, merged, unmapped) == (1, 0, [])
    assert room.name == "entrance_hall"
    assert room.ha_area == "entrance_hall"
    # Kept, because the scan's own label is what a later capture will match on.
    assert room.scanner_name == "Living Room"


def test_merge_unions_the_polygons_rather_than_boxing_them():
    """The real case: one open kitchen came back as Kitchen plus Office 1."""
    model = model_with(square("Kitchen", 0, 400), square("Office 1", 400, 700))
    renamed, merged, unmapped = apply(
        model, {"Kitchen": "kitchen"}, [["Kitchen", "Office 1"]])

    rooms = model.levels[0].rooms
    assert (renamed, merged) == (1, 1)
    assert len(rooms) == 1, "the consumed room should be gone, not left behind"
    assert rooms[0].name == "kitchen"
    assert rooms[0].merged_from == ["Kitchen", "Office 1"]
    # 7 m x 3 m -- the shared edge dissolved instead of both areas being summed
    # or a bounding box being taken.
    assert polygon_of(rooms[0]).area == 700 * 300


def test_a_merged_volume_takes_the_extremes_of_its_parts():
    model = model_with(square("Dining", 0, 400, low=320, high=470),
                       square("Hall", 400, 700, low=240, high=240))
    apply(model, {"Dining": "dining"}, [["Dining", "Hall"]])

    room = model.levels[0].rooms[0]
    assert (room.ceiling_low_cm, room.ceiling_high_cm) == (240, 470)
    # A range this wide is a double-height space: a candidate for a void.
    assert room.sloped is True


def test_ceiling_stays_unknown_rather_than_becoming_zero():
    """Merging rooms nobody measured must not invent a 0 cm ceiling."""
    model = model_with(square("A", 0, 400, low=None, high=None),
                       square("B", 400, 700, low=None, high=None))
    apply(model, {"A": "a"}, [["A", "B"]])

    room = model.levels[0].rooms[0]
    assert room.ceiling_high_cm is None
    assert room.sloped is False


def test_unmapped_rooms_keep_their_scanner_name_and_are_reported():
    """Silently dropping them would lose the room from the plan entirely."""
    model = model_with(square("Kitchen", 0, 400), square("Bedroom 2", 400, 700))
    renamed, _merged, unmapped = apply(model, {"Kitchen": "kitchen"}, [])

    assert renamed == 1
    assert unmapped == ["Bedroom 2"]
    assert {r.name for r in model.levels[0].rooms} == {"kitchen", "Bedroom 2"}


def test_a_merge_group_naming_one_present_room_is_left_alone():
    model = model_with(square("Kitchen", 0, 400))
    _renamed, merged, _unmapped = apply(
        model, {"Kitchen": "kitchen"}, [["Kitchen", "Office 1"]])

    assert merged == 0, "a group with nothing to merge into should be a no-op"
    assert model.levels[0].rooms[0].merged_from == []
