"""Room identity: HA areas in, scanner guesses out.

The scanner's names are guesses and its splits are artefacts. Both failure
modes are quiet -- a plan with a room called "Office 1" looks fine until you
try to bind it to an area that does not exist, and an open kitchen split in two
looks fine until lights group by the wrong half.
"""

from __future__ import annotations

import pytest

from lidar2ha.rooms import (
    CannotMerge,
    Crossed,
    apply,
    band_groups,
    polygon_of,
)
from lidar2ha.schema import Level, Model, Room, Wall


def model_with(*rooms: Room) -> Model:
    return Model(source="x.dxf",
                 levels=[Level(name="Ground", ceiling_height_cm=250, rooms=list(rooms))])


def square(name, x0, x1, y0=0, y1=300, low=240.0, high=240.0) -> Room:
    return Room(name=name, points=[(x0, y0), (x1, y0), (x1, y1), (x0, y1)],
                ceiling_low_cm=low, ceiling_high_cm=high)


def banded(*bands) -> Model:
    """A capture `polycam` split into ceiling bands of ONE Polycam level.

    `bands` is (name, from_level, ceiling_height_cm, rooms, walls[, doors]).
    """
    levels = []
    for band in bands:
        name, origin, height, rooms, walls = band[:5]
        doors = band[5] if len(band) > 5 else []
        levels.append(Level(name=name, from_level=origin, ceiling_height_cm=height,
                            rooms=list(rooms), walls=list(walls), doors=list(doors)))
    return Model(source="x.dxf", levels=levels)


def wall(x0, y0, x1, y1, height=250.0) -> Wall:
    return Wall(x_start=x0, y_start=y0, x_end=x1, y_end=y1,
                thickness=10.0, height=height)


def two_halves(order=("Living Room 1", "Living Room 2")):
    """The real case: a double-height room Polycam returned as two.

    25.6 m2 at ceiling 380-800 and 5.7 m2 at 480, abutting exactly -- no
    overlap, no gap, 31.3 m2 together against the 29.8 m2 that four other
    captures each see as ONE room.
    """
    low = square("Living Room 1", 0, 400, low=380, high=800)
    high = square("Living Room 2", 400, 700, low=480, high=480)
    model = banded(("Floor 1 (210cm)", "0:Floor 1", 800, [low], [wall(0, 0, 400, 0)]),
                   ("Floor 1 (480cm)", "0:Floor 1", 480, [high], [wall(400, 0, 700, 0)]))
    return model, [list(order)]


def test_two_bands_of_one_level_merge_onto_the_lower_band():
    """The real case. A double-height living room came back from Polycam as two
    rooms and the ceiling-band split filed them on different bands. `merge:`
    could not reach across, and the 5.7 m2 band held nothing else -- so
    `whichlevel` refused to place it and a piece of the living room became an
    unplaceable storey.

    Bands of ONE Polycam level are cut from one sheet, so they share a frame
    and the union is valid.
    """
    model, merges = two_halves()
    done = apply(model, {"Living Room 1": "living_room"}, merges)

    assert done.merged == 1, "the halves are in one capture and one Polycam level"
    assert [(lv.name, [r.name for r in lv.rooms]) for lv in model.levels] == [
        ("Floor 1 (210cm)", ["living_room"]), ("Floor 1 (480cm)", [])]
    assert polygon_of(model.levels[0].rooms[0]).area == 700 * 300


def test_the_lowest_band_wins_however_the_declaration_is_ordered():
    """`_fuse` files the survivor on the lowest band and not on whichever the
    reader happened to type first. Every other test here names the low band
    first, so the two rules are indistinguishable in them and a survivor left
    on the HIGH band would pass the lot -- taking a ground-floor room up a
    storey with nothing said."""
    model, merges = two_halves(order=("Living Room 2", "Living Room 1"))
    apply(model, {"Living Room 2": "living_room"}, merges)

    assert [(lv.name, [r.name for r in lv.rooms]) for lv in model.levels] == [
        ("Floor 1 (210cm)", ["living_room"]), ("Floor 1 (480cm)", [])], (
        "declared high-band-first, and it still belongs at the bottom")


def test_the_band_that_gave_its_floor_away_keeps_its_place_and_its_walls():
    """DELETING IT BREAKS A STAGE TWO STEPS UPSTREAM. `textures_project` runs
    before this and `scene.py` looks its manifest up by POSITION -- (index of
    level, index of wall). Removing a level renumbers every level after it, so
    a rectified photo either paints the wrong wall or vanishes from scene.tsv
    with no warning at all.

    So the band stays, holding the walls `polycam` gave it, and is reported.
    """
    model, merges = two_halves()
    done = apply(model, {"Living Room 1": "living_room"}, merges)

    assert len(model.levels) == 2, "the level count is a contract with the manifest"
    assert [len(lv.walls) for lv in model.levels] == [1, 1], "no wall moved"
    assert done.emptied == ["Floor 1 (480cm)"], "and it is not left unsaid"


def test_crossed_names_the_bands_it_spanned_and_where_it_landed():
    """The only report of a merge that crossed a band. Unasserted, the whole
    record could stop being written and every band test here would still
    pass."""
    model, merges = two_halves()
    done = apply(model, {"Living Room 1": "living_room"}, merges)

    assert done.crossed == [Crossed("Living Room 1",
                                    ("Floor 1 (210cm)", "Floor 1 (480cm)"),
                                    "Floor 1 (210cm)")]


def test_the_level_ceiling_rises_to_the_taller_half():
    """`polycam` sets a level's height from the rooms that band held. A room
    merged up from another band is taller than that, and a level shorter than
    its own room caps a double-height space -- which is the whole thing the
    merge was declared to restore."""
    model = banded(
        ("Floor 1 (210cm)", "0:F1", 260, [square("A", 0, 400, low=240, high=260)], []),
        ("Floor 1 (480cm)", "0:F1", 480, [square("B", 400, 700, low=470, high=480)], []))

    apply(model, {"A": "living_room"}, [["A", "B"]])

    assert model.levels[0].rooms[0].ceiling_high_cm == 480
    assert model.levels[0].ceiling_height_cm == 480, (
        "the level still caps its own room at the height its band had")


def test_a_merge_across_two_polycam_levels_is_refused():
    """Polycam's OWN levels are separate sheet clusters and do NOT share a
    frame: measured on one house, two of them fit the same reference 17.36 m
    apart. Unioning across them makes a polygon spanning the gap -- a room the
    size of the street.

    BOTH SIDES ARE BANDS HERE, with different `from_level` values, so this
    exercises the grouping key. Written with two unsplit levels instead, it
    took an early return and the key was never read -- a version that grouped
    every band together, merging Floor 1 with Floor 3, passed it.
    """
    model = banded(("Floor 1 (210cm)", "0:Floor 1", 250, [square("A", 0, 400)], []),
                   ("Floor 3 (250cm)", "2:Floor 3", 250, [square("B", 5000, 5400)], []))

    assert polygon_of(model.levels[0].rooms[0]).distance(
        polygon_of(model.levels[1].rooms[0])) > 1000, (
        "if these overlap the union is harmless and the test proves nothing")

    with pytest.raises(ValueError, match="not on the level its other rooms are on"):
        apply(model, {"A": "living_room"}, [["A", "B"]])


def test_a_name_repeated_on_an_unrelated_level_does_not_refuse():
    """Polycam repeats labels across storeys as a matter of course. Refusing
    whenever a merged name appeared on more than one level refused 155 of 500
    ordinary captures WITH NO BANDS AT ALL, every one of which merged correctly
    before -- a regression on every project in existence.

    The declaration is only impossible when no level holds two of its rooms.
    """
    model = Model(source="x.dxf", levels=[
        Level(name="Floor 1", ceiling_height_cm=250,
              rooms=[square("Kitchen", 0, 400), square("Hallway", 400, 700)]),
        Level(name="Floor 2", ceiling_height_cm=250,
              rooms=[square("Hallway", 0, 400)])])

    done = apply(model, {"Kitchen": "kitchen", "Hallway": "hall"},
                 [["Kitchen", "Hallway"]])

    assert done.merged == 1
    assert [r.name for r in model.levels[0].rooms] == ["kitchen"]
    assert [r.name for r in model.levels[1].rooms] == ["hall"], (
        "the unrelated room of the same name is untouched")


def test_two_declarations_naming_one_room_between_them_are_refused():
    """Every attempt at this stage has mangled overlapping groups a different
    way -- the room placed twice, the second group silently skipped, and once
    every room in the level deleted while the report read `merged 2 group(s)`.
    They are refused now, by an invariant rather than by a rule for each shape:
    a room that leaves must be named in some survivor's `merged_from`.

    `[[Kitchen, Dining], [Kitchen, Pantry]]` is how a reader writes what they
    mean by `[Kitchen, Dining, Pantry]`, so the message says that.
    """
    model = model_with(square("Kitchen", 0, 300), square("Dining", 300, 600),
                       square("Pantry", 600, 900))

    with pytest.raises(CannotMerge, match="named by two `merge:` groups"):
        apply(model, {"Kitchen": "kitchen"},
              [["Kitchen", "Dining"], ["Kitchen", "Pantry"]])


def test_a_room_eaten_by_a_merge_is_always_named_by_its_survivor():
    """The invariant's other half: an ordinary merge must not trip it. If it
    did, every merge in every project would refuse and the guard would be
    turned off within the day."""
    model = model_with(square("Kitchen", 0, 400), square("Office 1", 400, 700))

    done = apply(model, {"Kitchen": "kitchen"}, [["Kitchen", "Office 1"]])

    assert done.merged == 1
    assert model.levels[0].rooms[0].merged_from == ["Kitchen", "Office 1"]


def test_every_impossible_merge_is_named_and_not_only_the_first():
    """Raising on the first leaves the reader to find the rest one run at a
    time, and each run costs a re-scan-and-recombine to reach."""
    model = banded(("F1 (210cm)", "0:F1", 250, [square("A", 0, 400),
                                                square("C", 400, 700)], []),
                   ("F3 (250cm)", "2:F3", 250, [square("B", 5000, 5400),
                                                square("D", 5400, 5800)], []))

    with pytest.raises(ValueError) as raised:
        apply(model, {"A": "a"}, [["A", "B"], ["C", "D"]])
    assert "['A', 'B']" in str(raised.value) and "['C', 'D']" in str(raised.value)


def test_a_merge_group_that_matched_nothing_is_reported_rather_than_skipped():
    """A typo in `merge:` did nothing and said nothing. The rooms stay
    separate, the open plan stays split, and the only symptom is a lighting
    group that binds to half a room."""
    model = model_with(square("Kitchen", 0, 400), square("Office 1", 400, 700))

    done = apply(model, {"Kitchen": "kitchen"}, [["Kitchen", "Ofice 1"]])

    assert done.merged == 0
    assert done.unapplied == [["Kitchen", "Ofice 1"]], (
        "a declaration that matched fewer than two rooms has to be named")


def test_a_room_with_no_name_is_reported_rather_than_crashing_the_report():
    """`unmapped` is printed through `sorted()`, and a bare None in it raises
    TypeError there -- so the stage would die while writing its own report,
    after it had already written the model."""
    model = model_with(square("Kitchen", 0, 400), Room(name=None, points=[
        (400, 0), (700, 0), (700, 300), (400, 300)]))

    done = apply(model, {"Kitchen": "kitchen"}, [])

    assert sorted(set(done.unmapped)) == ["<unnamed>"]


def test_a_typo_beside_a_repeated_label_does_not_refuse():
    """Polycam gives every unlabelled room its floor's label, so one name on
    two storeys is routine. Counting that as "one room in each of two frames"
    refused 376 of 3000 ordinary captures -- 268 of them for a declaration
    naming a room that is not in the model at all, where the answer is "check
    the spelling" and not a hard exit.

    A band has to be involved before frames mean anything.
    """
    model = Model(source="x.dxf", levels=[
        Level(name="Floor 1", ceiling_height_cm=250,
              rooms=[square("Hallway", 0, 400), square("Kitchen", 400, 700)]),
        Level(name="Floor 2", ceiling_height_cm=250,
              rooms=[square("Hallway", 0, 400)])])

    done = apply(model, {"Hallway": "hall"}, [["Hallway", "Kitcen"]])

    assert done.merged == 0
    assert done.unapplied == [["Hallway", "Kitcen"]], "the typo is what to say"


def test_a_merge_naming_a_label_two_rooms_share_is_refused():
    """Which of two same-named rooms a merge takes was decided by file
    position, and every rule tried deleted the other one's floor on some real
    capture: `main` takes the first, the pre-band code the last, and the two
    disagree on 21 of 3000 ordinary captures. Polycam gives every unlabelled
    room its floor's label, so two rooms sharing one is its ordinary output.

    Neither pick is defensible, so neither is made.
    """
    model = model_with(square("Bedroom", 0, 300),
                       square("Hallway", 300, 600),
                       square("Bedroom", 600, 900))

    with pytest.raises(CannotMerge, match="names 2 rooms"):
        apply(model, {"Bedroom": "bedroom"}, [["Bedroom", "Hallway"]])

    assert len(model.levels[0].rooms) == 3, "and nothing is touched on the way out"


def test_a_room_on_another_band_is_not_deleted_with_the_one_it_shares_no_name_with():
    """Rooms were removed by NAME across the whole band group, so a room on
    another band that merely shared a scanner name with a consumed one was
    deleted -- 18 m2 of floor gone while the report read `merged=1` and named
    nothing at all."""
    model = banded(
        ("F1 (380cm)", "0:F1", 400, [square("Living Room", 0, 400),
                                     square("Office 1", 400, 600)], []),
        ("F1 (480cm)", "0:F1", 480, [square("Snug", 900, 1500),
                                     square("Study", 1500, 1800)], []))

    apply(model, {"Living Room": "lounge", "Study": "study"},
          [["Living Room", "Office 1"]])

    assert len(model.levels[1].rooms) == 2, (
        f"the upper band keeps its own rooms: "
        f"{[r.name for r in model.levels[1].rooms]}")


def test_a_declaration_spanning_a_band_and_another_cluster_is_refused_whole():
    """Refusing only when NO level holds two names let a three-room
    declaration be HALF applied: two rooms merged across bands while the third,
    in another Polycam cluster, stayed where it was -- and `merged_from` named
    it anyway, so the provenance asserted a union that never happened."""
    model = banded(("F1 (240cm)", "0:F1", 400, [square("Kitchen", 0, 300)], []),
                   ("F1 (480cm)", "0:F1", 480, [square("Dining", 300, 600)], []),
                   ("Floor 2", None, 250, [square("Snug", 9000, 9300)], []))

    with pytest.raises(CannotMerge, match="not on the level its other rooms are on"):
        apply(model, {"Kitchen": "open_plan"}, [["Kitchen", "Dining", "Snug"]])

    assert [r.name for lv in model.levels for r in lv.rooms] == [
        "Kitchen", "Dining", "Snug"], "a refused run leaves the model alone"


def test_the_emptied_band_is_reported_whichever_way_it_was_declared():
    """The report was written from a set the move had already emptied, so the
    same model, merged the same way, said it or not depending on which room the
    reader happened to type first."""
    for order in (["Kitchen", "Dining"], ["Dining", "Kitchen"]):
        model = banded(("F1 (240cm)", "0:F1", 400, [square("Kitchen", 0, 300)], []),
                       ("F1 (480cm)", "0:F1", 480, [square("Dining", 300, 600)], []))
        done = apply(model, {order[0]: "open_plan"}, [order])
        assert done.emptied == ["F1 (480cm)"], f"declared {order}: {done.emptied}"


def test_a_broken_room_polygon_is_not_blamed_on_the_declaration():
    """`main` catches the refusal to give a sentence instead of a traceback.
    Catching bare `ValueError` also caught shapely's complaint about a
    degenerate polygon and reported a broken ROOM as a bad `merge:` line, with
    the traceback suppressed so nothing said which room."""
    model = model_with(square("Kitchen", 0, 400))
    model.levels[0].rooms.append(Room(name="Sliver", points=[(0, 0), (1, 1)]))

    with pytest.raises(ValueError) as raised:
        apply(model, {"Kitchen": "kitchen"}, [["Kitchen", "Sliver"]])
    assert not isinstance(raised.value, CannotMerge), (
        "a geometry error is not a declaration this stage refuses")


def test_a_group_half_glued_in_one_band_is_finished_across_the_others():
    """Pass 1 glues what it can inside a level and pass 2 glues the rest across
    the bands. Guarded on "did pass 1 touch this group" instead of "is anything
    of it still unglued", pass 2 was switched off for the whole cluster the
    moment pass 1 fired -- so the remaining room sat on a sibling band,
    unjoined, with `unapplied` quiet because the group had applied and nothing
    missing for the invariant to count. 189 of 6000 banded models, silent.
    """
    model = banded(
        ("F1 (240cm)", "0:F1", 400, [square("Kitchen", 0, 300),
                                     square("Dining", 300, 600)], []),
        ("F1 (480cm)", "0:F1", 480, [square("Snug", 600, 900)], []))

    done = apply(model, {"Kitchen": "open_plan"}, [["Kitchen", "Dining", "Snug"]])

    assert [[r.name for r in lv.rooms] for lv in model.levels] == [["open_plan"], []]
    assert model.levels[0].rooms[0].merged_from == ["Kitchen", "Dining", "Snug"], (
        "glued twice, and the first pass's members stay in the record")
    assert polygon_of(model.levels[0].rooms[0]).area == 900 * 300
    assert done.unapplied == [] and done.emptied == ["F1 (480cm)"]


def test_rooms_that_share_no_edge_are_refused_rather_than_half_kept():
    """Keeping the largest piece and dropping the rest was the largest single
    source of floor going quietly: two rooms that share no edge are not one
    room, so approximating the union throws a whole room away."""
    model = model_with(square("Kitchen", 0, 300), square("Far", 4000, 4300))

    with pytest.raises(CannotMerge, match="separate pieces"):
        apply(model, {"Kitchen": "kitchen"}, [["Kitchen", "Far"]])


def test_an_offcut_smaller_than_a_sliver_is_dropped_with_a_note(capsys):
    """Two scanned polygons meeting at a wall leave scanner residue, and
    refusing on that would refuse ordinary captures. The line between the two
    is a guess, which is why it is a flag."""
    model = model_with(square("Kitchen", 0, 300), square("Nearly", 300, 600))
    model.levels[0].rooms.append(square("Speck", 5000, 5010, y1=10))

    apply(model, {"Kitchen": "kitchen"}, [["Kitchen", "Nearly", "Speck"]],
          sliver_m2=1.0)

    assert "dropping" in capsys.readouterr().out, "floor going is never silent"


def test_two_levels_of_one_capture_that_was_never_split_cannot_be_glued():
    """`band_groups` gives a level with no `from_level` a group of its own, and
    that is what makes pass 2 unreachable for the 17 of 19 real captures
    `polycam` never split. Grouped together, a merge would union rooms across
    two unrelated storeys."""
    model = Model(source="x.dxf", levels=[
        Level(name="Floor 1", ceiling_height_cm=250, rooms=[square("Lounge", 0, 300)]),
        Level(name="Floor 2", ceiling_height_cm=250, rooms=[square("Attic", 300, 600)])])

    assert [len(g) for g in band_groups(model)] == [1, 1], (
        "if these share a group the refusal below is not what is being tested")
    with pytest.raises(CannotMerge, match="not on the level its other rooms"):
        apply(model, {"Lounge": "lounge"}, [["Lounge", "Attic"]])


def test_a_merge_written_without_its_dashes_is_refused():
    """`cap: ["Kitchen", "Office 1"]` -- the dash forgotten -- iterated the
    strings and produced groups of single CHARACTERS, which merged nothing and
    exited 0. `projectlevels.parse_entries` refuses the same shape."""
    model = model_with(square("Kitchen", 0, 400))

    with pytest.raises(CannotMerge, match="list of scanner room names"):
        apply(model, {"Kitchen": "kitchen"}, ["Kitchen", "Office 1"])
    with pytest.raises(CannotMerge, match="LIST of groups"):
        apply(model, {"Kitchen": "kitchen"}, "Kitchen")


def test_a_refusal_halfway_through_leaves_the_model_untouched():
    """The first group merges, the second is impossible. Working in place, the
    caller was handed a half-merged model along with the error -- and the
    invariant cannot help, because it can only fire after the damage."""
    model = model_with(square("Kitchen", 0, 300), square("Office 1", 300, 600),
                       square("Snug", 1000, 1300), square("Far", 5000, 5300))
    before = [(r.name, polygon_of(r).area) for r in model.levels[0].rooms]

    with pytest.raises(CannotMerge):
        apply(model, {"Kitchen": "kitchen"},
              [["Kitchen", "Office 1"], ["Snug", "Far"]])

    assert [(r.name, polygon_of(r).area) for r in model.levels[0].rooms] == before


def test_a_capture_with_no_bands_merges_exactly_as_before():
    """Every capture in every project so far has `from_level` unset, and none
    of them may change behaviour by a millimetre."""
    model = model_with(square("Kitchen", 0, 400), square("Office 1", 400, 700))
    done = apply(model, {"Kitchen": "kitchen"}, [["Kitchen", "Office 1"]])

    assert (done.merged, done.crossed, done.emptied) == (1, [], [])
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
