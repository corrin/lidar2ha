"""Saying which storeys of a capture belong to which level.

`combine --storey` is global, and on one real project no value of it works.
`--storey "Floor 1"` combines four single-storey captures and DISCARDS the
whole-house walk, whose storeys are named `Floor 1 (210cm)` and so on;
`--storey "Floor 1 (210cm)"` aborts, because the other four have no such level.
So a 148 m capture -- more wall than any other in that project -- was split
correctly, identified correctly, and could not reach `combine` at all.

The fact is per capture, so it goes where every other per-capture fact in
project.yaml already lives.
"""

from __future__ import annotations

import pytest

from lidar2ha.projectlevels import (
    Wanted,
    entry_key,
    expand,
    origin_of,
    parse_entries,
)
from lidar2ha.schema import Level, Model, Room, Wall


def wall(x0: float, y0: float, x1: float, y1: float) -> Wall:
    return Wall(x_start=x0, y_start=y0, x_end=x1, y_end=y1,
                thickness=10.0, height=250.0)


def model_with(*level_names: str) -> Model:
    return Model(source="x.dxf", levels=[
        Level(name=n, ceiling_height_cm=250.0, walls=[wall(0, 0, 100, 0)],
              rooms=[Room(name=f"r-{n}", points=[(0, 0), (100, 0), (100, 100)])])
        for n in level_names])


# --------------------------------------------------------------------------- #
# reading the entry
# --------------------------------------------------------------------------- #


def test_a_bare_capture_id_still_works():
    """Every project.yaml in existence lists bare strings, and every one of
    them has to keep combining exactly as it did."""
    assert parse_entries(["a", "b"]) == [Wanted("a"), Wanted("b")]


def test_a_mapping_names_the_storeys_this_level_wants():
    """The declaration itself. Without it there is no way to say which storey
    of a multi-storey capture belongs to this floor, and the capture reaches
    `combine` whole and is discarded."""
    got = parse_entries([{"id": "walk", "storeys": ["Floor 1 (210cm)"]}])
    assert got == [Wanted("walk", ("Floor 1 (210cm)",))]


def test_a_single_storey_written_without_brackets_is_taken_as_one():
    """What a person writes first. Refusing a clear intention over a bracket
    helps nobody."""
    assert parse_entries([{"id": "walk", "storeys": "Floor 3"}]) == [
        Wanted("walk", ("Floor 3",))]


def test_a_mapping_with_no_id_is_refused_by_name():
    """A mapping naming storeys but no capture applies to nothing. Skipping it
    would leave a level quietly short of a capture somebody had written down."""
    with pytest.raises(ValueError, match="needs an `id:`"):
        parse_entries([{"storeys": ["Floor 1"]}])


def test_an_empty_storeys_list_is_refused_rather_than_meaning_all():
    """It could plausibly mean "every storey" or "none", and guessing either
    puts geometry into a level nobody declared or leaves it out in silence."""
    with pytest.raises(ValueError, match="empty `storeys:` list"):
        parse_entries([{"id": "walk", "storeys": []}])


def test_an_unreadable_entry_is_refused_rather_than_skipped():
    """A level entry nobody can parse is a capture that quietly does not reach
    the union -- which is the failure this whole file exists to end."""
    with pytest.raises(ValueError, match="capture id or a mapping"):
        parse_entries([42])


def test_a_level_written_without_its_dashes_is_refused_rather_than_spelt_out():
    """`"Ground Level": my_capture` -- the `- ` forgotten -- iterated as one
    capture per CHARACTER. Four one-letter ids match no export, so the level
    combined from nothing while the file plainly named a capture. A mapping
    written there is worse: it iterates as its keys, which look plausible."""
    assert list("walk") == ["w", "a", "l", "k"], (
        "if a string does not iterate as letters this test proves nothing")
    for entry in ("walk", {"walk": ["Floor 1"]}):
        with pytest.raises(ValueError, match="takes a LIST of captures"):
            parse_entries(entry)


def test_a_storeys_that_is_not_a_list_of_names_is_refused():
    """A number raised `TypeError` from inside a comprehension, naming neither
    the capture nor the key; a mapping was read as its keys and became storey
    names nobody wrote."""
    with pytest.raises(ValueError, match="not a list of storey names"):
        parse_entries([{"id": "walk", "storeys": 42}])
    with pytest.raises(ValueError, match="not a list of storey names"):
        parse_entries([{"id": "walk", "storeys": {"Floor 1": 1}}])


def test_a_storey_name_that_is_blank_is_refused_while_it_can_still_be_named():
    """`expand` matches these against `Level.name` exactly, so a blank refuses
    anyway -- three stages later, quoting `''`, which tells the reader nothing
    about which of their entries is wrong."""
    with pytest.raises(ValueError, match="not a storey name"):
        parse_entries([{"id": "walk", "storeys": ["", "Floor 3"]}])


def test_an_id_that_is_not_a_string_is_refused():
    """`id: 2006` reads as an int from an unquoted capture id, and stringifies
    to something that matches no export -- so the level would combine one
    capture short with nothing saying so."""
    with pytest.raises(ValueError, match="must be the capture id as a string"):
        parse_entries([{"id": 2006}])


# --------------------------------------------------------------------------- #
# expanding it against the capture
# --------------------------------------------------------------------------- #


def test_one_capture_can_contribute_two_storeys_to_one_level():
    """The case that shaped the design. Polycam laid one walk of an upstairs
    across two sheet clusters, and after the ceiling-band split two of its
    levels both belong to that floor while holding DIFFERENT rooms -- 10.6 m2
    and 23.1 m2. Naming one would have discarded the other."""
    model = model_with("Floor 1 (710cm)", "Floor 2", "Floor 3")
    got = expand(Wanted("walk", ("Floor 1 (710cm)", "Floor 3")), model)

    assert [key for key, _ in got] == ["walk [Floor 1 (710cm)]", "walk [Floor 3]"]
    for _, one in got:
        assert len(one.levels) == 1, "each entry is one storey, not the whole capture"
    assert [one.levels[0].name for _, one in got] == ["Floor 1 (710cm)", "Floor 3"]


def test_an_ordinary_capture_keeps_its_plain_id():
    """So a project that never needed any of this combines to byte-identical
    output -- `Room.source` included."""
    got = expand(Wanted("plain"), model_with("Floor 1"))
    assert [key for key, _ in got] == ["plain"]
    assert entry_key("plain", None) == "plain"


def test_a_declared_storey_the_capture_lacks_is_refused_and_names_what_it_has():
    """A typo in the declaration. Carrying on would place nothing while looking
    like it had worked, and the reader would have no idea which name was
    wrong."""
    with pytest.raises(ValueError, match="has no level named 'Floor 9'"):
        expand(Wanted("walk", ("Floor 9",)), model_with("Floor 1", "Floor 2"))


def test_an_undeclared_multi_level_capture_goes_through_whole():
    """NOT an error. `combine` discards it by its own rule, naming the levels
    it has, and the other captures of that floor still combine -- which is what
    happens today. Refusing here would decline to build anything until an
    unrelated capture was sorted out, and the complaint was never the missing
    part: the remedy was.
    """
    model = model_with("Floor 1", "Floor 2")
    got = expand(Wanted("entrance"), model)
    assert [key for key, _ in got] == ["entrance"]
    assert len(got[0][1].levels) == 2, "it must reach combine intact to be judged"


def test_the_global_storey_still_picks_a_level_when_nothing_is_declared():
    """It is what makes one real ground floor work today: every single-storey
    capture there is called `Floor 1`, so one flag takes the two-storey
    capture's ground level and everybody else's only level at once."""
    got = expand(Wanted("entrance"), model_with("Floor 1", "Floor 2"), "Floor 1")
    assert [key for key, _ in got] == ["entrance"]
    assert [one.levels[0].name for _, one in got] == ["Floor 1"]


def test_project_yaml_beats_the_flag_where_both_speak():
    """One is a recorded decision and the other is what somebody typed once."""
    got = expand(Wanted("walk", ("Floor 3",)),
                 model_with("Floor 1", "Floor 3"), fallback="Floor 1")
    assert [one.levels[0].name for _, one in got] == ["Floor 3"]


# --------------------------------------------------------------------------- #
# reading the key back
# --------------------------------------------------------------------------- #


def test_a_key_says_which_export_it_came_from():
    """`Room.source` points at these keys, so a room that cannot name the
    export it came from cannot be checked, re-scanned or argued with. It is
    also what stops two storeys of one walk vouching for each other in the
    consensus."""
    assert origin_of(entry_key("walk", "Floor 3")) == "walk"
    assert origin_of(entry_key("plain", None)) == "plain"


@pytest.mark.parametrize("entry", ["odd [name]",
                                   {"id": "odd [name]", "storeys": ["Floor 1"]}])
def test_a_capture_id_that_would_confuse_the_key_is_refused(entry):
    """`entry_key` marks a storey with ` [name]` and `origin_of` reads it back,
    so an id carrying the same marker splits in the wrong place and two
    unrelated captures can look like one export. Silently: the only symptom is
    a consensus that counted wrong.

    Refused at the boundary, which makes the format unambiguous by
    construction rather than by hoping nobody names a capture that way.

    BOTH SHAPES. The first version of this guard sat in the mapping branch
    alone, so a bare string walked straight past it -- and this test only
    exercised the mapping, so it passed while the hole was open.
    """
    assert origin_of("odd [name]") != "odd [name]", (
        "if this parses correctly the refusal below is unnecessary")
    with pytest.raises(ValueError, match=r"contains ' \['"):
        parse_entries([entry])


def test_a_key_this_does_not_read_is_refused_rather_than_ignored():
    """`storey:` singular is what a person writes first, and ignoring it handed
    them the UNDECLARED path: a declaration that did nothing, said nothing, and
    left the capture out of the very union it was written to put it in.

    Every key is read, the way `schema.py` forbids an extra field.
    """
    with pytest.raises(ValueError, match="did you mean `storeys:`"):
        parse_entries([{"id": "walk", "storey": "Floor 3"}])

    with pytest.raises(ValueError, match="does not read"):
        parse_entries([{"id": "walk", "stories": ["Floor 3"]}])
