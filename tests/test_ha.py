"""Reading the Home Assistant registries.

The failure this file mostly guards is silent and geographic: a light placed in
the room its switch is on rather than the room it lights. Nothing downstream can
detect that — the model is valid, the render succeeds, and one room is simply
lit by the wrong entity.
"""

from __future__ import annotations

from scan2ha.ha import (
    LightEntity,
    classify,
    duplicate_names,
    light_entities,
    redundant_groups,
    resolve_area,
    websocket_url,
)


def registry(entities=None, devices=None, states=None) -> dict:
    return {"entities": entities or [], "devices": devices or [], "states": states or []}


def entity(entity_id, area_id=None, device_id=None, **kw) -> dict:
    return {"entity_id": entity_id, "area_id": area_id, "device_id": device_id, **kw}


# --------------------------------------------------------------------------- #
# area resolution
# --------------------------------------------------------------------------- #


def test_a_multi_gang_switch_puts_its_lights_in_their_own_rooms():
    """The case that makes entity-before-device non-negotiable.

    One switch on one wall, driving three bulbs in three rooms. Two entities
    carry an area override; the third inherits the device's. Device-first would
    put all three where the switch is, so two of the three would be wrong.
    """
    devices = {"switch1": {"id": "switch1", "area_id": "kitchen"}}
    entities = [
        entity("light.pantry", area_id="pantry", device_id="switch1"),
        entity("light.sink", area_id=None, device_id="switch1"),
        entity("light.deck", area_id="deck", device_id="switch1"),
    ]
    resolved = [resolve_area(e, devices) for e in entities]

    assert resolved == [("pantry", "entity"), ("kitchen", "device"), ("deck", "entity")]
    assert [a for a, _ in resolved] != ["kitchen"] * 3, "device-first would give this"


def test_an_entity_with_no_area_anywhere_resolves_to_nothing():
    """Reported, never guessed — an unassigned light has no room to go in."""
    assert resolve_area(entity("light.x"), {}) == (None, "none")
    assert resolve_area(entity("light.x", device_id="missing"), {}) == (None, "none")


def test_a_device_with_no_area_does_not_rescue_the_entity():
    devices = {"d": {"id": "d", "area_id": None}}
    assert resolve_area(entity("light.x", device_id="d"), devices) == (None, "none")


# --------------------------------------------------------------------------- #
# reading the registry
# --------------------------------------------------------------------------- #


def test_only_light_entities_are_returned():
    reg = registry(entities=[
        entity("light.one", area_id="hall"),
        entity("switch.two", area_id="hall"),
        entity("sensor.three", area_id="hall"),
    ])
    assert [e.entity_id for e in light_entities(reg)] == ["light.one"]


def test_disabled_and_hidden_entities_are_flagged_not_dropped():
    """They should not be placed, but the report has to be able to say why."""
    reg = registry(entities=[
        entity("light.off", area_id="hall", disabled_by="user"),
        entity("light.shy", area_id="hall", hidden_by="integration"),
    ])
    by_id = {e.entity_id: e for e in light_entities(reg)}

    assert by_id["light.off"].disabled is True
    assert by_id["light.shy"].hidden is True
    assert len(by_id) == 2, "both are still reported"


def test_the_friendly_name_falls_back_through_the_registry_then_the_state():
    reg = registry(
        entities=[
            entity("light.named", area_id="a", name="Custom"),
            entity("light.original", area_id="a", original_name="From Integration"),
            entity("light.bare", area_id="a"),
        ],
        states=[{"entity_id": "light.bare", "attributes": {"friendly_name": "From State"}}],
    )
    names = {e.entity_id: e.name for e in light_entities(reg)}
    assert names == {"light.named": "Custom",
                     "light.original": "From Integration",
                     "light.bare": "From State"}


def test_group_members_come_from_the_state_attribute():
    reg = registry(
        entities=[entity("light.group", area_id="a")],
        states=[{"entity_id": "light.group",
                 "attributes": {"entity_id": ["light.a", "light.b"]}}],
    )
    found = light_entities(reg)[0]
    assert found.is_group
    assert found.members == ["light.a", "light.b"]


def test_a_malformed_member_list_does_not_crash_the_read():
    """One integration writing a bare string should not stop the whole run."""
    reg = registry(
        entities=[entity("light.group", area_id="a")],
        states=[{"entity_id": "light.group", "attributes": {"entity_id": "light.solo"}}],
    )
    assert light_entities(reg)[0].members == ["light.solo"]


def test_an_ordinary_light_is_not_a_group():
    reg = registry(
        entities=[entity("light.plain", area_id="a")],
        states=[{"entity_id": "light.plain", "attributes": {"friendly_name": "Plain"}}],
    )
    assert light_entities(reg)[0].is_group is False


# --------------------------------------------------------------------------- #
# groups
# --------------------------------------------------------------------------- #


def test_a_group_whose_members_are_present_is_redundant():
    """Placing both is the same bulbs twice, and the raytracer cannot tell."""
    entities = [
        LightEntity("light.group", "Group", "den", members=["light.a", "light.b"]),
        LightEntity("light.a", "A", "den"),
        LightEntity("light.b", "B", "den"),
    ]
    assert redundant_groups(entities) == {"light.group": ["light.a", "light.b"]}


def test_a_group_whose_members_are_absent_is_kept():
    """It may be the only handle on those bulbs."""
    entities = [LightEntity("light.group", "Group", "den", members=["light.a"])]
    assert redundant_groups(entities) == {}


# --------------------------------------------------------------------------- #
# classification — hints for a human, never a filter
# --------------------------------------------------------------------------- #


def test_indicators_are_flagged_for_review():
    for entity_id, name in [("light.router_led", "Router LED"),
                            ("light.speaker_led_ring", "Voice - LED Ring"),
                            ("light.thing_status", "Thing")]:
        kind, reason = classify(LightEntity(entity_id, name, "hall"))
        assert kind == "check", entity_id
        assert reason


def test_a_group_by_name_is_flagged_since_its_members_are_invisible():
    """Integration-native groups carry no member list, so the name is all there is."""
    kind, reason = classify(LightEntity("light.cabinets", "Den - Cabinets Group", "den"))
    assert kind == "check"
    assert "group" in reason


def test_an_ordinary_fitting_is_not_flagged():
    kind, reason = classify(LightEntity("light.kitchen_ceiling", "Kitchen - Ceiling", "kitchen"))
    assert (kind, reason) == ("fitting", "")


def test_shared_friendly_names_are_surfaced():
    """Usually one physical fitting exposed several ways — placing them all
    lights that spot two or three times over."""
    entities = [
        LightEntity("light.a", "String Lights", "deck"),
        LightEntity("light.b", "string lights", "deck"),
        LightEntity("light.c", "Ceiling", "deck"),
    ]
    assert duplicate_names(entities) == {"string lights": ["light.a", "light.b"]}


# --------------------------------------------------------------------------- #
# connection
# --------------------------------------------------------------------------- #


def test_websocket_url_is_derived_from_the_http_one():
    assert websocket_url("http://homeassistant.local:8123") == \
        "ws://homeassistant.local:8123/api/websocket"
    assert websocket_url("https://ha.example.com/") == \
        "wss://ha.example.com/api/websocket"
    # Already a websocket URL: left alone rather than doubled.
    assert websocket_url("ws://h:8123/api/websocket") == "ws://h:8123/api/websocket"


def test_wall_lights_are_not_mistaken_for_a_light_group():
    """Found against a real registry: "wall lights" CONTAINS "all lights",
    so bare substring matching reported an ordinary pair of wall lights as a
    group whose members were being double-counted."""
    kind, _ = classify(LightEntity("light.wall_1_and_2", "Interior Wall Lights", "den"))
    assert kind == "fitting"


def test_the_name_and_entity_id_are_not_searched_as_one_string():
    """Also found against a real registry. Joining them invents phrases that
    exist in neither: "...effect status" followed by "light.den..." reads as
    "status light" across the join, and the owner cannot see why it flagged."""
    entity = LightEntity("light.den_effect_thing", "Den - Effect Status Report", "den")
    _kind, reason = classify(entity)
    assert "status light" not in reason


def test_non_illumination_channels_are_flagged():
    """RGB controllers file things in the light domain that are not lamps."""
    for name in ("Den - Effect Sound", "Alarm Buzzer", "Shed Siren"):
        kind, _ = classify(LightEntity("light.x", name, "den"))
        assert kind == "check", name


def test_a_genuine_group_is_still_flagged_by_name():
    kind, reason = classify(LightEntity("light.cab", "Den - Light - Cabinets Group", "den"))
    assert kind == "check"
    assert "group" in reason
