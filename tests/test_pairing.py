"""Saying which entity drives which fitting, since the geometry cannot.

`lights` refused to bind whenever a room held more than one entity, and told the
reader to "split the room with `seams`, or name the pairing in project.yaml".
Neither existed. `LightsConfig` had no pairing key at all, and splitting a room
so each piece holds one entity invents internal boundaries that are not in the
building -- one real den would have needed ten.

Across three storeys of the house this was built for, 11 of 37 entities reached
a measured position out of 96 detected candidates. Every refusal was correct.
The consequence was that most lights rendered at a room's centre and there was
nowhere to write down what the owner already knew.
"""

from __future__ import annotations

from lidar2ha.ha import LightEntity, device_groups
from lidar2ha.lights import (
    Fitting,
    LightsConfig,
    build_lights,
    match_fitting,
)
from lidar2ha.schema import Level, Model, Room

SQUARE = [(0, 0), (600, 0), (600, 600), (0, 600)]


def room(area: str) -> Room:
    return Room(name=area, ha_area=area, points=SQUARE)


def model_with(*rooms: Room) -> Model:
    return Model(source="x.dxf",
                 levels=[Level(name="Ground", ceiling_height_cm=250,
                               rooms=list(rooms))])


def light(entity_id: str, area: str, device: str | None = None) -> LightEntity:
    return LightEntity(entity_id, entity_id, area, device_id=device)


def config(pairing: dict) -> LightsConfig:
    return LightsConfig.from_project({"lights": {"pairing": pairing}})


# --------------------------------------------------------------------------- #
# counting devices rather than entities
# --------------------------------------------------------------------------- #


def test_four_entities_on_one_device_are_one_fitting():
    """One real Sonoff exposes a light, an effect light, an effect STATUS and an
    effect SOUND -- four `light.*` entities, one switch, and one of them does
    not emit light at all. Counted as entities that room looks unresolvable."""
    entities = [light(f"light.den_{n}", "den", device="sonoff-1")
                for n in ("main", "effect_light", "effect_sound", "effect_status")]
    groups = device_groups(entities)

    assert len(groups) == 1, "four entities on one device read as four fittings"
    assert len(groups[0]) == 4


def test_entities_with_no_device_are_not_pooled_together():
    """`device_id: None` is a template or a helper -- one house exposes a door
    light twice, once per adjoining room, so each can address it. Two such
    entities share nothing but the absence, and calling them one light would be
    a claim made on no evidence."""
    entities = [light("light.door_hall_side", "hall"),
                light("light.door_kitchen_side", "hall")]
    assert [e.device_id for e in entities] == [None, None], "guard: both lack one"
    assert len(device_groups(entities)) == 2


def test_the_report_says_how_many_devices_so_the_room_reads_as_resolvable():
    """"7 fittings and 2 entities" reads as hopeless. "7 fittings and 2 entities
    on 1 device" reads as one line of project.yaml, and the difference is the
    only thing telling a reader which it is."""
    model = model_with(room("kitchen"))
    entities = [light("light.k_a", "kitchen", device="dev-1"),
                light("light.k_b", "kitchen", device="dev-1")]
    found = {"kitchen": [Fitting(100, 100, 240), Fitting(400, 400, 240)]}

    _, report = build_lights(model, entities, None, found)
    assert report.ambiguous == [("kitchen", 2, 2, 1)]


# --------------------------------------------------------------------------- #
# matching a declared point to a fitting
# --------------------------------------------------------------------------- #


def test_a_declared_point_takes_the_fitting_it_names():
    fittings = [Fitting(100, 100, 240), Fitting(500, 500, 250)]
    got, why = match_fitting((498, 502), fittings)
    assert why == ""
    assert got is not None and (got.x, got.y) == (500, 500)


def test_a_point_with_nothing_near_it_is_refused_and_the_distance_reported():
    """That number is what separates "I mistyped a coordinate" from "the
    detector moved". Binding the nearest regardless would put a light in the
    wrong place, which looks exactly like a light in the right place."""
    fittings = [Fitting(100, 100, 240)]
    got, why = match_fitting((5000, 5000), fittings)
    assert got is None
    assert "nearest fitting is" in why and "cm away" in why


def test_a_point_between_two_close_fittings_names_neither():
    """Measured over three storeys, the closest pair of fittings inside one room
    is 10.1 cm. Taking the nearer by a millimetre would be exactly the proximity
    guess this refuses to make anywhere else."""
    fittings = [Fitting(100, 100, 240), Fitting(110, 100, 240)]
    gap = abs(fittings[0].x - fittings[1].x)
    assert gap < 20, f"if these are far apart the test proves nothing (gap {gap})"

    got, why = match_fitting((105, 100), fittings)
    assert got is None
    assert "no single fitting" in why


def test_a_point_clearly_on_one_of_two_close_fittings_is_accepted():
    """The refusal above must not make close pairs undeclarable -- the den has
    them, and they are the rooms that most need declaring."""
    fittings = [Fitting(100, 100, 240), Fitting(140, 100, 240)]
    got, why = match_fitting((100, 100), fittings)
    assert why == ""
    assert got is not None and got.x == 100


# --------------------------------------------------------------------------- #
# the declaration, end to end
# --------------------------------------------------------------------------- #


def test_a_declared_pairing_binds_the_fittings_it_names():
    """The whole point: two entities, four fittings, and the owner knows which
    is which even though nothing in the scan does."""
    model = model_with(room("den"))
    entities = [light("light.ceiling", "den", device="d1"),
                light("light.cabinet", "den", device="d2")]
    found = {"den": [Fitting(100, 100, 240), Fitting(200, 100, 240),
                     Fitting(500, 500, 90), Fitting(520, 520, 90)]}

    lights, report = build_lights(
        model, entities,
        config({"den": {"light.ceiling": [[100, 100], [200, 100]],
                        "light.cabinet": [[500, 500], [520, 520]]}}), found)

    assert report.pairing_failed == []
    assert report.ambiguous == [], "a fully declared room is not ambiguous"
    at = {(lt.entity_id, lt.x, lt.y) for lt in lights}
    assert at == {("light.ceiling", 100, 100), ("light.ceiling", 200, 100),
                  ("light.cabinet", 500, 500), ("light.cabinet", 520, 520)}


def test_one_entity_declared_against_three_fittings_is_three_placements():
    """One switch driving three bulbs is three placements carrying one
    entity_id -- the plugin sums sources sharing a name, so that IS the
    representation and not a workaround for having too few entities."""
    model = model_with(room("hall"))
    entities = [light("light.hall", "hall", device="d1"),
                light("light.other", "hall", device="d2")]
    found = {"hall": [Fitting(100, 100, 240), Fitting(200, 200, 240),
                      Fitting(300, 300, 240)]}

    lights, _ = build_lights(
        model, entities,
        config({"hall": {"light.hall": [[100, 100], [200, 200],
                                        [300, 300]]}}), found)

    placed = [lt for lt in lights if lt.entity_id == "light.hall"]
    assert len(placed) == 3
    assert {(lt.x, lt.y) for lt in placed} == {(100, 100), (200, 200), (300, 300)}


def test_a_pairing_that_cannot_be_honoured_still_places_the_entity_and_says_so():
    """Centring it silently would look exactly like the declaration having
    worked. The entity goes back in with the undeclared ones so it still
    appears, and the reason is the only sign anything went wrong."""
    model = model_with(room("den"))
    entities = [light("light.ceiling", "den", device="d1")]
    found = {"den": [Fitting(100, 100, 240)]}

    lights, report = build_lights(
        model, entities, config({"den": {"light.ceiling": [[5000, 5000]]}}), found)

    assert len(report.pairing_failed) == 1
    area, entity_id, why = report.pairing_failed[0]
    assert (area, entity_id) == ("den", "light.ceiling")
    assert "nearest fitting is" in why
    assert [lt.entity_id for lt in lights] == ["light.ceiling"], "the entity vanished"


def test_a_declaration_naming_an_entity_that_is_not_here_is_reported():
    """Almost always a rename, a moved area or a typo -- and the consequence of
    ignoring it is a fitting the owner believes is declared and nobody placed."""
    model = model_with(room("den"))
    entities = [light("light.ceiling", "den", device="d1")]
    found = {"den": [Fitting(100, 100, 240)]}

    _, report = build_lights(
        model, entities, config({"den": {"light.gone": [[100, 100]]}}), found)

    assert any(e == "light.gone" for _, e, _ in report.pairing_failed)


def test_a_partly_declared_room_reports_what_is_left():
    """Working through a house one room at a time is the normal state, so how
    much is still undeclared has to be visible rather than inferred from a
    silence."""
    model = model_with(room("den"))
    entities = [light("light.ceiling", "den", device="d1"),
                light("light.cabinet", "den", device="d2")]
    found = {"den": [Fitting(100, 100, 240), Fitting(500, 500, 90)]}

    _, report = build_lights(
        model, entities, config({"den": {"light.ceiling": [[100, 100]]}}), found)

    assert len(report.pairing_partial) == 1
    area, unnamed, left = report.pairing_partial[0]
    assert area == "den"
    assert unnamed == ["light.cabinet"]
    assert left == 1, "the fitting the declaration did not claim"


def test_a_room_with_no_pairing_behaves_exactly_as_before():
    """The declaration is opt-in per room. A house that has never heard of it
    must place lights the way it did yesterday."""
    model = model_with(room("lounge"))
    entities = [light("light.a", "lounge"), light("light.b", "lounge")]
    found = {"lounge": [Fitting(50, 50, 240), Fitting(250, 250, 240)]}

    plain, plain_report = build_lights(model, entities, None, found)
    with_empty, empty_report = build_lights(
        model, entities, config({}), found)

    assert [(lt.entity_id, lt.x, lt.y) for lt in plain] == \
           [(lt.entity_id, lt.x, lt.y) for lt in with_empty]
    assert plain_report.ambiguous == empty_report.ambiguous
