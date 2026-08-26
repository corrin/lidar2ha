"""Room identity: HA areas in, scanner guesses out.

The scanner's names are guesses and its splits are artefacts. Both failure
modes are quiet -- a plan with a room called "Office 1" looks fine until you
try to bind it to an area that does not exist, and an open kitchen split in two
looks fine until lights group by the wrong half.
"""

from __future__ import annotations

import pytest

from lidar2ha.rooms import apply, polygon_of
from lidar2ha.schema import Level, Model, Room, Wall


def model_with(*rooms: Room) -> Model:
    return Model(source="x.dxf",
                 levels=[Level(name="Ground", ceiling_height_cm=250, rooms=list(rooms))])


def square(name, x0, x1, y0=0, y1=300, low=240.0, high=240.0) -> Room:
    return Room(name=name, points=[(x0, y0), (x1, y0), (x1, y1), (x0, y1)],
                ceiling_low_cm=low, ceiling_high_cm=high)


def banded(*bands) -> Model:
    """A capture `polycam` split into ceiling bands of ONE Polycam level.

    `bands` is (name, from_level, ceiling_height_cm, rooms, walls).
    """
    return Model(source="x.dxf", levels=[
        Level(name=n, from_level=f, ceiling_height_cm=h, rooms=list(rs),
              walls=list(ws))
        for n, f, h, rs, ws in bands])


def wall(x0, y0, x1, y1) -> Wall:
    return Wall(x_start=x0, y_start=y0, x_end=x1, y_end=y1,
                thickness=10.0, height=250.0)


def test_two_bands_of_one_level_merge_onto_the_lower_band():
    """The real case. A double-height living room came back from Polycam as two
    rooms, 25.6 m2 at ceiling 380-800 and 5.7 m2 at 480, so the ceiling-band
    split filed them on different bands. `merge:` could not reach across, and
    the 5.7 m2 band held nothing else -- `whichlevel` then refused to place it
    at all, so a piece of the living room became an unplaceable storey.

    Bands of ONE Polycam level are cut from one sheet, so they share a frame
    and the union is valid.
    """
    low = square("Living Room 1", 0, 400, low=380, high=800)
    high = square("Living Room 2", 400, 700, low=480, high=480)
    model = banded(("Floor 1 (210cm)", "Floor 1", 800, [low], []),
                   ("Floor 1 (480cm)", "Floor 1", 480, [high], []))

    done = apply(model, {"Living Room 1": "living_room"},
                 [["Living Room 1", "Living Room 2"]])

    assert done.merged == 1, "the halves are in one capture and one Polycam level"
    survivors = [(lv.name, [r.name for r in lv.rooms]) for lv in model.levels]
    assert survivors == [("Floor 1 (210cm)", ["living_room"])], (
        f"one room on the lower band, and no band left holding a fragment: {survivors}")
    assert polygon_of(model.levels[0].rooms[0]).area == 700 * 300


def test_the_emptied_band_is_dissolved_and_its_walls_survive():
    """A band whose only room merged away has no floor left, but it still holds
    the walls `polycam` gave it -- and those are the walls along the part of the
    room that moved. Dropping the level with them loses geometry nothing else
    has."""
    keep, gone = wall(0, 0, 400, 0), wall(400, 0, 700, 0)
    model = banded(
        ("Floor 1 (210cm)", "Floor 1", 800, [square("A", 0, 400)], [keep]),
        ("Floor 1 (480cm)", "Floor 1", 480, [square("B", 400, 700)], [gone]))

    apply(model, {"A": "living_room"}, [["A", "B"]])

    assert len(model.levels) == 1, "an empty band is not a level"
    surviving = {(w.x_start, w.y_start, w.x_end, w.y_end) for w in model.levels[0].walls}
    assert (400.0, 0.0, 700.0, 0.0) in surviving, "the dissolved band's wall was lost"
    assert len(surviving) == 2, f"and not duplicated: {surviving}"


def test_a_moved_wall_on_a_line_the_target_has_is_not_laid_twice():
    """Measured on the real capture: 6 of the dissolved band's 11 walls were a
    line the target already had, at a different height. Height is not identity
    -- two walls on one line are one wall in plan, and keeping both puts two
    sampled points on every centimetre of it, which is how a fit reads better
    than the capture is.

    The taller survives, for `_build_level`'s reason: too tall hides behind the
    ceiling, too short leaks light.
    """
    shared_low = wall(0, 0, 700, 0)
    shared_high = wall(0, 0, 700, 0)
    shared_high.height = 800.0
    model = banded(
        ("Floor 1 (210cm)", "Floor 1", 260, [square("A", 0, 400)], [shared_low]),
        ("Floor 1 (480cm)", "Floor 1", 480, [square("B", 400, 700)], [shared_high]))

    apply(model, {"A": "living_room"}, [["A", "B"]])

    walls = model.levels[0].walls
    assert len(walls) == 1, f"one line, one wall: {[(w.x_start, w.height) for w in walls]}"
    assert walls[0].height == 800.0, "the taller of the two is the one that stands"


def test_the_level_ceiling_rises_to_the_taller_half():
    """`polycam` sets a level's height from the rooms that band held. A room
    merged up from another band is taller than that, and a level shorter than
    its own room caps a double-height space -- which is the whole thing the
    merge was declared to restore."""
    model = banded(
        ("Floor 1 (210cm)", "Floor 1", 260, [square("A", 0, 400, low=240, high=260)], []),
        ("Floor 1 (480cm)", "Floor 1", 480, [square("B", 400, 700, low=470, high=480)], []))

    apply(model, {"A": "living_room"}, [["A", "B"]])

    assert model.levels[0].rooms[0].ceiling_high_cm == 480
    assert model.levels[0].ceiling_height_cm == 480, (
        "the level still caps its own room at the height its band had")


def test_a_merge_across_two_polycam_levels_is_refused():
    """Polycam's OWN levels are separate sheet clusters and do NOT share a
    frame: measured on one house, two of them fit the same reference 17.36 m
    apart. Unioning across them makes a polygon spanning the gap -- a room the
    size of the street. It is the upstairs hallway's case, and the answer is a
    refusal rather than a silent 17 m room."""
    model = Model(source="x.dxf", levels=[
        Level(name="Floor 1", ceiling_height_cm=250, rooms=[square("A", 0, 400)]),
        Level(name="Floor 3", ceiling_height_cm=250, rooms=[square("B", 5000, 5400)])])

    assert polygon_of(model.levels[0].rooms[0]).distance(
        polygon_of(model.levels[1].rooms[0])) > 1000, (
        "if these overlap the union is harmless and the test proves nothing")

    with pytest.raises(ValueError, match="do not share a frame"):
        apply(model, {"A": "living_room"}, [["A", "B"]])


def test_a_merge_group_that_matched_nothing_is_reported_rather_than_skipped():
    """A typo in `merge:` did nothing and said nothing. The rooms stay
    separate, the open plan stays split, and the only symptom is a lighting
    group that binds to half a room."""
    model = model_with(square("Kitchen", 0, 400), square("Office 1", 400, 700))

    done = apply(model, {"Kitchen": "kitchen"}, [["Kitchen", "Ofice 1"]])

    assert done.merged == 0
    assert done.unapplied == [["Kitchen", "Ofice 1"]], (
        "a declaration that matched fewer than two rooms has to be named")


def test_a_capture_with_no_bands_merges_exactly_as_before():
    """Every capture in every project so far has `from_level` unset, and none
    of them may change behaviour by a millimetre."""
    model = model_with(square("Kitchen", 0, 400), square("Office 1", 400, 700))
    done = apply(model, {"Kitchen": "kitchen"}, [["Kitchen", "Office 1"]])

    assert (done.merged, done.crossed, done.dissolved) == (1, [], [])
    assert polygon_of(model.levels[0].rooms[0]).area == 700 * 300


def test_rename_records_the_scanner_name_it_replaced():
    model = model_with(square("Living Room", 0, 400))
    done = apply(model, {"Living Room": "entrance_hall"}, [])

    room = model.levels[0].rooms[0]
    assert (done.renamed, done.merged, done.unmapped) == (1, 0, [])
    assert room.name == "entrance_hall"
    assert room.ha_area == "entrance_hall"
    # Kept, because the scan's own label is what a later capture will match on.
    assert room.scanner_name == "Living Room"


def test_merge_unions_the_polygons_rather_than_boxing_them():
    """The real case: one open kitchen came back as Kitchen plus Office 1."""
    model = model_with(square("Kitchen", 0, 400), square("Office 1", 400, 700))
    done = apply(model, {"Kitchen": "kitchen"}, [["Kitchen", "Office 1"]])

    rooms = model.levels[0].rooms
    assert (done.renamed, done.merged) == (1, 1)
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
    done = apply(model, {"Kitchen": "kitchen"}, [])

    assert done.renamed == 1
    assert done.unmapped == ["Bedroom 2"]
    assert {r.name for r in model.levels[0].rooms} == {"kitchen", "Bedroom 2"}


def test_a_merge_group_naming_one_present_room_is_left_alone_but_reported():
    """The room itself must not move -- a capture that simply did not see the
    other half still has a correct half. What used to be missing is any word
    that the declaration did nothing, which is how a typo survives."""
    model = model_with(square("Kitchen", 0, 400))
    done = apply(model, {"Kitchen": "kitchen"}, [["Kitchen", "Office 1"]])

    assert done.merged == 0, "a group with nothing to merge into should be a no-op"
    assert model.levels[0].rooms[0].merged_from == []
    assert done.unapplied == [["Kitchen", "Office 1"]]
