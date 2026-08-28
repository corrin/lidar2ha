"""What `project.yaml` claims, checked before a stage acts on it.

Every case here is one that actually happened on the house this was built for,
and every one of them produced a plausible model rather than an error. The file
is read leniently everywhere except `levels:` -- six top-level keys are consumed
by nothing, a misspelled section does nothing and says nothing, and an area id
is whatever string was typed -- so a check that runs before the pipeline is the
only place these surface.

One property per test. The docstring says which failure it catches.
"""

from __future__ import annotations

from lidar2ha import validate


def registry(*area_ids: str) -> dict:
    return {"areas": [{"area_id": a, "name": a.replace("_", " ").title(),
                       "floor_id": None} for a in area_ids],
            "floors": [], "devices": [], "entities": [], "states": []}


def kinds(findings) -> list[str]:
    return [f.kind for f in findings]


def test_a_capture_in_a_level_with_no_rooms_mapping_is_named():
    """The failure that cost this house three rooms.

    A capture with no `rooms:` entry keeps its scanner names, and an unnamed
    room that wins a group takes the area name with it -- `master_bedroom`
    became `Other 1` and vanished from the model. `combine` only says something
    when NO capture in the level maps anything, so one unmapped capture among
    four is silent.
    """
    found = validate.check(
        {"levels": {"Upstairs": ["geom_a", "fixtures_b"]},
         "rooms": {"geom_a": {"Bedroom": "master_bedroom"}}},
        registry("master_bedroom"))
    assert "capture_not_named" in kinds(found)
    assert any("fixtures_b" in f.detail for f in found
               if f.kind == "capture_not_named")


def test_an_area_that_is_really_a_capture_id_is_caught():
    """`rooms.py` adopts any non-empty string, so a typo invents an area.

    A capture-rename migration put a capture id in the area column and it rode
    into the model for weeks: the phantom counted as mapped while the real area
    had no geometry. This is the specific shape that goes wrong, so it gets its
    own verdict rather than the generic one.
    """
    found = validate.check(
        {"captures": {"deck_geometry_0823-1304": {}},
         "levels": {"Ground": ["deck_geometry_0823-1304", "other"]},
         "rooms": {"deck_geometry_0823-1304":
                   {"Living Room": "deck_geometry_0823-1304"},
                   "other": {"Room": "den"}}},
        registry("den"))
    assert "area_is_a_capture_id" in kinds(found)


def test_an_area_no_registry_knows_is_reported():
    """A mapping to an area that does not exist is silent everywhere else.

    Nothing in the package reads `registry["areas"]` except to count them, so a
    misspelled area id reaches `Room.ha_area`, the built model, and the light
    index, and the only symptom is an entity landing in `AREAS WITH NO ROOM`
    from the other direction entirely.
    """
    found = validate.check(
        {"levels": {"Ground": ["a", "b"]},
         "rooms": {"a": {"Room": "lounge"}, "b": {"Room": "lounnge"}}},
        registry("lounge"))
    bad = [f for f in found if f.kind == "area_not_in_registry"]
    assert bad and "lounnge" in bad[0].detail
    assert all("lounge" != f.detail.split()[0] for f in bad), (
        "the correctly spelled area was reported too")


def test_a_null_mapping_is_not_an_error():
    """`null` means "ask me later" and is a legitimate, deliberate state.

    Reporting it would train the reader to ignore the report, which is the one
    thing a gate cannot afford.
    """
    found = validate.check(
        {"levels": {"Ground": ["a", "b"]},
         "rooms": {"a": {"Room": None}, "b": {"Room": "den"}}},
        registry("den"))
    assert "area_not_in_registry" not in kinds(found)


def test_a_capture_declared_and_used_in_no_level_is_reported():
    """Seven of this house's nineteen captures are in that state.

    They are imported, registered, named -- and contribute nothing, because no
    `levels:` entry lists them. That is how a garage and two decks stayed out of
    the model while looking fully set up.
    """
    found = validate.check(
        {"captures": {"used": {}, "orphan": {}},
         "levels": {"Ground": ["used", "other"]},
         "rooms": {"used": {"R": "den"}, "other": {"R": "den"}}},
        registry("den"))
    assert "capture_unused" in kinds(found)
    assert any("orphan" in f.detail for f in found if f.kind == "capture_unused")


def test_a_top_level_key_nothing_reads_is_reported():
    """The file is read with `.get` throughout, so a misspelled section is
    silent. Ten areas of `light_pairing:` sat beside the `lights.pairing:` the
    code reads, and had never once been applied."""
    found = validate.check(
        {"levels": {"G": ["a", "b"]},
         "rooms": {"a": {"R": "den"}, "b": {"R": "den"}},
         "light_pairing": {"den": {}}},
        registry("den"))
    unknown = [f for f in found if f.kind == "unknown_key"]
    assert unknown and "light_pairing" in unknown[0].detail


def test_a_clean_project_reports_nothing():
    """If this fails the checks are firing on correct input, and a gate that
    cries wolf is worse than no gate."""
    found = validate.check(
        {"captures": {"a": {}, "b": {}},
         "levels": {"Ground": ["a", "b"]},
         "rooms": {"a": {"Room": "den"}, "b": {"Room": "den"}},
         "camera": {"yaw": 180}, "render": {"width": 800}},
        registry("den"))
    assert found == [], [f.kind for f in found]


def test_without_a_registry_the_area_checks_abstain():
    """Three answers, not two: with no cached registry the tool cannot know
    whether an area exists, and saying nothing is different from saying it is
    fine. The structural checks still run."""
    found = validate.check(
        {"levels": {"Ground": ["a", "b"]},
         "rooms": {"a": {"Room": "whatever"}}}, None)
    assert "area_not_in_registry" not in kinds(found)
    assert "capture_not_named" in kinds(found), (
        "the checks that need no registry must still run")
