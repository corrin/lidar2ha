"""Which of the house's areas have geometry, across every level at once.

`combine` answers this per level and only for areas `project.yaml` maps;
`lights` answers it per model and only for areas some light entity names. An
area Home Assistant knows and the project never mentions is reported by nothing,
which is how a garage, a den and a downstairs hallway stayed out of a model that
looked complete.

One property per test. The docstring says which failure it catches.
"""

from __future__ import annotations

from lidar2ha import coverage
from lidar2ha.schema import Level, Model, Room


def registry(floors: dict[str, str], areas: dict[str, str]) -> dict:
    return {
        "floors": [{"floor_id": fid, "name": name} for fid, name in floors.items()],
        "areas": [{"area_id": a, "name": a.title(), "floor_id": fid}
                  for a, fid in areas.items()],
        "devices": [], "entities": [], "states": [],
    }


def model(*rooms: tuple[str | None, float]) -> Model:
    """One level whose rooms are squares of the given area, named or not."""
    made = []
    for i, (area, m2) in enumerate(rooms):
        side = (m2 * 10_000) ** 0.5           # m2 -> cm2 -> a side in cm
        x = i * 10_000
        made.append(Room(name=area or f"Other {i}", ha_area=area,
                         points=[(x, 0), (x + side, 0),
                                 (x + side, side), (x, side)]))
    return Model(source="t.dxf", units="cm", levels=[Level(
        name="Floor 1", elevation_cm=0, ceiling_height_cm=240,
        walls=[], rooms=made)])


REG = registry({"g": "Ground", "u": "Upstairs"},
               {"den": "g", "garage": "g", "office": "u"})


def test_an_area_the_project_never_mentions_is_still_reported_missing():
    """The gap nothing else can see.

    `combine`'s `area_with_no_source` only considers areas `project.yaml` maps,
    and `lights` only areas a light entity names. `garage` is in Home Assistant,
    in no `rooms:` mapping and in no model -- so it is invisible to both, and it
    is exactly the kind of room a person notices is missing from the render.
    """
    rows = coverage.measure({"levels": {"Ground": []}}, REG,
                            {"Ground": model(("den", 12.0))})
    ground = rows[0]
    assert "garage" in ground.missing
    assert "den" in ground.covered


def test_a_room_carrying_an_area_of_another_floor_is_named():
    """The bug behind "8 of 7 areas covered".

    Counting `ha_area` values without intersecting them against the floor's own
    areas lets a room named for another storey inflate the numerator, so a level
    can report more areas covered than it has. The mismatch is itself the
    finding: either the area is on the wrong floor in Home Assistant, or the
    room is.
    """
    rows = coverage.measure({"levels": {"Ground": []}}, REG,
                            {"Ground": model(("den", 12.0), ("office", 9.0))})
    ground = rows[0]
    assert "office" in ground.foreign
    assert "office" not in ground.covered, (
        "an area of another floor was counted as covering this one")
    assert len(ground.covered) <= len(ground.floor_areas)


def test_rooms_with_no_area_are_listed_largest_first():
    """Each one needs a name, a `split:`, or to be recorded as not-an-area, and
    the biggest is the one worth resolving first -- a 17 m2 unnamed room is a
    room, a 0.3 m2 one is a scanning artefact."""
    rows = coverage.measure({"levels": {"Ground": []}}, REG,
                            {"Ground": model(("den", 12.0), (None, 3.0),
                                             (None, 17.0))})
    sizes = [round(m2) for _, m2 in rows[0].unnamed]
    assert sizes == [17, 3], sizes


def test_a_level_with_no_model_says_so_rather_than_reading_as_empty():
    """A level that has not been combined yet and a level whose every area is
    missing look identical in a count. They are not the same thing, and only one
    of them means go and scan something."""
    rows = coverage.measure({"levels": {"Ground": [], "Upstairs": []}}, REG,
                            {"Ground": model(("den", 12.0))})
    upstairs = next(r for r in rows if r.level == "Upstairs")
    assert upstairs.source is None
    assert upstairs.covered == () and upstairs.missing == ()


def test_a_level_matching_no_ha_floor_is_reported_not_guessed():
    """`levels:` keys are floor NAMES by convention and nothing enforces it. A
    typo would otherwise silently produce a level with no areas to cover, which
    reads as a level that is entirely missing."""
    rows = coverage.measure({"levels": {"Grund": []}}, REG,
                            {"Grund": model(("den", 12.0))})
    assert rows[0].floor_id is None


def test_a_floor_no_level_covers_is_reported_separately():
    """This house has an `msm` floor that is not part of the building being
    modelled. Counting its areas as missing would overstate the gap forever and
    train the reader to ignore the total."""
    rows = coverage.measure({"levels": {"Ground": []}}, REG,
                            {"Ground": model(("den", 12.0))})
    assert coverage.uncovered_floors({"levels": {"Ground": []}}, REG) == ("Upstairs",)
    assert all(r.level != "Upstairs" for r in rows)


def test_an_area_on_several_levels_including_its_own_floor_spans():
    """A stairwell is one area and three storeys, and that is not a mistake.

    Home Assistant's area belongs to exactly one floor; a stairwell consumes
    space on every storey it passes through. The community's answer is "the
    floor it consumes", and `polycam` already files a shaft on the lowest band
    it spans. So the geometry legitimately puts one area on several levels, and
    flagging it per level fires forever on correct data -- which is how a report
    teaches people to skip it.
    """
    rows = coverage.measure({"levels": {"Ground": [], "Upstairs": []}}, REG,
                            {"Ground": model(("den", 12.0), ("garage", 20.0)),
                             "Upstairs": model(("office", 9.0), ("den", 4.0))})
    ground = next(r for r in rows if r.level == "Ground")
    upstairs = next(r for r in rows if r.level == "Upstairs")

    assert "den" not in upstairs.foreign, (
        "an area spanning levels was reported as misfiled")
    assert "den" in ground.covered, "it is still covered on its own floor"
    assert "den" in upstairs.spans
    # Counted once, or the denominator stops meaning anything.
    assert "den" not in upstairs.covered


def test_an_area_only_ever_off_its_own_floor_is_still_an_error():
    """The case the `foreign` verdict was actually for.

    An area that appears on no level but its HA floor is a real mis-mapping --
    a room named for another storey, or a floor assignment that is wrong. Losing
    that check while making room for the stairwell would trade a false positive
    for a false negative.
    """
    rows = coverage.measure({"levels": {"Ground": []}}, REG,
                            {"Ground": model(("den", 12.0), ("office", 9.0))})
    assert "office" in rows[0].foreign
    assert "office" not in rows[0].spans
