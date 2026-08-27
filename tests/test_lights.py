"""Placing light entities in rooms.

Two classes of failure here, both quiet. A light placed outside its own room
still renders — it just lights the space next door. And a light that never gets
placed leaves a room dark in the finished dashboard with nothing to say why, so
every refusal has to be counted and named.
"""

from __future__ import annotations

import json

import pytest
from shapely.geometry import Point, Polygon

from lidar2ha.ha import LightEntity
from lidar2ha.lights import (
    Fitting,
    LightsConfig,
    build_lights,
    elevation_for,
    load_fittings,
    place,
    pole_of,
    print_report,
    room_index,
    valid_entity_id,
)
from lidar2ha.schema import Level, Model, Room

# An L. Its centroid is outside it, which is the whole reason for the pole.
L_SHAPE = [(0, 0), (600, 0), (600, 100), (100, 100), (100, 600), (0, 600)]
SQUARE = [(0, 0), (400, 0), (400, 400), (0, 400)]


def model_with(*rooms: Room, ceiling=250.0) -> Model:
    return Model(source="x.dxf",
                 levels=[Level(name="Ground", ceiling_height_cm=ceiling, rooms=list(rooms))])


def room(area, points=None, **kw) -> Room:
    return Room(name=area, ha_area=area, points=points or SQUARE, **kw)


def light(entity_id, area, **kw) -> LightEntity:
    return LightEntity(entity_id, entity_id, area, **kw)


# --------------------------------------------------------------------------- #
# geometry
# --------------------------------------------------------------------------- #


def test_the_pole_is_inside_a_room_whose_centroid_is_not():
    """An L-shaped living space is the ordinary case, not a pathological one."""
    poly = Polygon(L_SHAPE)
    assert not poly.contains(poly.centroid), "if this passes the test proves nothing"
    assert poly.contains(pole_of(poly))


def test_one_light_goes_to_the_pole():
    poly = Polygon(SQUARE)
    assert place(poly, 1) == [(pole_of(poly).x, pole_of(poly).y)]


@pytest.mark.parametrize("count", [2, 3, 5, 8])
@pytest.mark.parametrize("shape", [SQUARE, L_SHAPE])
def test_every_spread_light_lands_inside_its_room(count, shape):
    poly = Polygon(shape)
    positions = place(poly, count)
    assert len(positions) == count
    for x, y in positions:
        assert poly.contains(Point(x, y)), f"({x}, {y}) is outside the room"


def test_spread_lights_are_not_all_stacked_on_one_point():
    positions = place(Polygon(SQUARE), 4)
    assert len(set(positions)) > 1


def test_placement_is_deterministic():
    """The review loop compares runs; jitter would make every diff noise."""
    assert place(Polygon(L_SHAPE), 3) == place(Polygon(L_SHAPE), 3)


def test_real_fittings_are_used_before_any_guess():
    poly = Polygon(SQUARE)
    known = [(10.0, 10.0), (20.0, 20.0)]
    positions = place(poly, 3, fittings=known)
    assert positions[:2] == known
    assert len(positions) == 3


def test_more_fittings_than_lights_uses_only_what_is_needed():
    positions = place(Polygon(SQUARE), 1, fittings=[(10.0, 10.0), (20.0, 20.0)])
    assert positions == [(10.0, 10.0)]


# --------------------------------------------------------------------------- #
# elevation
# --------------------------------------------------------------------------- #


def test_the_rooms_own_ceiling_beats_the_levels():
    """A 2.2 m laundry beside a 4.7 m void shares a level; using the level's
    height would hang the laundry light above its own ceiling."""
    level = Level(name="Ground", ceiling_height_cm=470)
    laundry = room("laundry", ceiling_low_cm=220, ceiling_high_cm=220)
    assert elevation_for(laundry, level) == 200


def test_a_sloped_ceiling_uses_its_high_end():
    """The eave is not where anything hangs.

    This used the LOW end, reasoning that a fitting under a rake should stay
    under it. The reasoning holds and the number does not: the low end of a
    sloping ceiling is where it meets the WALL. Measured over one storey of
    raked rooms, none of whose lows was wrong, it put a master-bedroom
    downlight at 90 cm and a sewing-room light at 130.
    """
    level = Level(name="Ground", ceiling_height_cm=250)
    raked = room("attic", ceiling_low_cm=110, ceiling_high_cm=400, sloped=True)
    assert elevation_for(raked, level) == 380


def test_a_room_with_only_a_low_reading_still_uses_it():
    """Preferring the high end is not distrusting the low one. Where a capture
    supplied only the low figure it is the sole evidence there is, and falling
    through to the level's height would discard a real measurement for a
    building-wide guess."""
    level = Level(name="Ground", ceiling_height_cm=470)
    only_low = room("laundry", ceiling_low_cm=220)
    assert elevation_for(only_low, level) == 200


def test_a_room_with_no_measured_ceiling_falls_back_to_the_level():
    level = Level(name="Ground", ceiling_height_cm=250)
    assert elevation_for(room("plain"), level) == 230


def test_elevation_never_goes_below_the_floor():
    level = Level(name="Ground", ceiling_height_cm=10)
    assert elevation_for(room("tiny"), level) > 0


# --------------------------------------------------------------------------- #
# the join
# --------------------------------------------------------------------------- #


def test_rooms_without_an_ha_area_are_not_indexed():
    """Before `rooms` runs, a room carries a scanner guess, not an identity."""
    model = model_with(Room(name="Living Room", points=SQUARE), room("kitchen"))
    assert set(room_index(model)) == {"kitchen"}


def unnamed_house() -> Model:
    """One mapped room, and an unmapped one on each of two levels.

    The upper one is a `split:` piece of a parent `rooms:` never mapped, which
    is what carries a section name AND its parent's scanner label. The lower is
    a plain scanner room. Both are called `Bedroom` by something, because
    Polycam repeats a label across storeys as a matter of course.
    """
    return Model(source="x.dxf", levels=[
        Level(name="Ground", ceiling_height_cm=250, rooms=[
            room("kitchen"),
            Room(name="Bedroom", points=SQUARE, source="scan7")]),
        Level(name="Upper", ceiling_height_cm=240, rooms=[
            Room(name="wardrobe", scanner_name="Bedroom", split_from="Bedroom",
                 points=SQUARE, source="scan9")])])


def test_a_room_with_no_ha_area_is_reported_not_merely_skipped():
    """`room_index` drops such a room, and the only check is "are they all".

    A house where `rooms:` misses one room, or where `split` cuts a parent it
    missed, places every other light and says nothing -- and a room that renders
    correctly and can never be lit looks exactly like one that worked.
    """
    lights, report = build_lights(unnamed_house(), [light("light.a", "kitchen")])

    assert lights, "if this fails nothing was placed and the test proves nothing"
    assert report.rooms_without_areas == [
        ("Ground", "Bedroom", "scan7"), ("Upper", "wardrobe", "scan9")]


def test_an_unreachable_room_is_named_by_its_own_label_not_its_parent_s():
    """A `split:` piece carries the section name AND the parent's scanner label.

    Reported by the label, every piece of one cut collapses to the same line --
    `Bedroom, Bedroom` -- and the names a person wrote, which are the only way
    to tell the pieces apart, are the ones that vanish.
    """
    _, report = build_lights(unnamed_house(), [light("light.a", "kitchen")])

    labels = [label for _, label, _ in report.rooms_without_areas]
    assert "wardrobe" in labels and "Bedroom" in labels


def test_the_rooms_with_no_area_reach_the_printed_report(capsys):
    """The field is not the deliverable; the report a person reads is.

    Populating it and never printing it leaves the run looking exactly as silent
    as it was, which is the whole failure.
    """
    lights, report = build_lights(unnamed_house(), [light("light.a", "kitchen")])
    print_report(report, lights)
    out = capsys.readouterr().out

    assert "ROOMS WITH NO AREA" in out
    assert "wardrobe" in out and "Upper" in out and "scan9" in out


def test_a_room_that_has_an_area_is_not_reported_as_lacking_one(capsys):
    """A line printed on every ordinary house is a line nobody reads."""
    lights, report = build_lights(model_with(room("kitchen")),
                                  [light("light.a", "kitchen")])
    print_report(report, lights)

    assert report.rooms_without_areas == []
    assert "ROOMS WITH NO AREA" not in capsys.readouterr().out


def test_a_light_is_placed_in_its_own_area_on_the_right_level():
    model = Model(source="x.dxf", levels=[
        Level(name="Ground", ceiling_height_cm=250, rooms=[room("hall")]),
        Level(name="Upper", ceiling_height_cm=240, rooms=[room("landing")]),
    ])
    lights, report = build_lights(model, [light("light.a", "hall"), light("light.b", "landing")])

    by_id = {lt.entity_id: lt for lt in lights}
    assert by_id["light.a"].level == 0
    assert by_id["light.b"].level == 1
    assert report.skipped == []


def test_an_area_with_no_room_is_reported_not_dropped():
    """Usually a space that was not scanned — and the human needs to know."""
    model = model_with(room("hall"))
    lights, report = build_lights(model, [light("light.shed", "shed")])

    assert lights == []
    assert report.areas_without_rooms == {"shed"}
    assert any("shed" in why for _, why in report.skipped)


def test_a_room_nobody_lights_is_reported():
    """It renders dark, which is otherwise a mystery."""
    model = model_with(room("hall"), room("cupboard"))
    _lights, report = build_lights(model, [light("light.a", "hall")])
    assert report.rooms_without_lights == ["cupboard"]


def test_an_entity_with_no_area_is_reported():
    model = model_with(room("hall"))
    _lights, report = build_lights(model, [light("light.orphan", None)])
    assert any("no area" in why for _, why in report.skipped)


# --------------------------------------------------------------------------- #
# refusals, each of which would otherwise be silent
# --------------------------------------------------------------------------- #


def test_a_tab_in_an_entity_id_is_refused():
    """The scene file is tab-separated and the id goes in verbatim, so this
    would shift every later field and put the light at a nonsense coordinate."""
    assert valid_entity_id("light.ok")
    assert not valid_entity_id("light.\tbad")
    assert not valid_entity_id("light.bad\n")

    model = model_with(room("hall"))
    lights, report = build_lights(model, [light("light.\tbad", "hall")])
    assert lights == []
    assert any("tab or newline" in why for _, why in report.skipped)


def test_disabled_and_hidden_entities_are_not_placed():
    model = model_with(room("hall"))
    lights, _report = build_lights(model, [
        light("light.off", "hall", disabled=True),
        light("light.shy", "hall", hidden=True),
    ])
    assert lights == []


def test_a_redundant_group_is_skipped_but_its_members_are_placed():
    model = model_with(room("den"))
    lights, report = build_lights(model, [
        light("light.group", "den", members=["light.a", "light.b"]),
        light("light.a", "den"),
        light("light.b", "den"),
    ])
    assert {lt.entity_id for lt in lights} == {"light.a", "light.b"}
    assert any("light group" in why for _, why in report.skipped)


def on_coordinator(entity_id, area) -> LightEntity:
    """A ZHA group: its entity hangs off the radio, not off a lamp."""
    return LightEntity(entity_id, entity_id, area, device_id="radio",
                       device_model="Generic Zigbee Coordinator (EZSP)",
                       device_name="Sonoff Zigbee Coordinator (EZSP)")


def test_a_zha_group_is_skipped_even_though_its_members_are_unknowable():
    """A ZHA group publishes no member list, so unlike `redundant_groups` this
    cannot check them — and placing it anyway is the same bulbs twice. The
    plugin sums sources sharing a name, so the failure is a room that renders
    quietly too bright, with no error at any stage."""
    model = model_with(room("den"))
    lights, report = build_lights(model, [
        on_coordinator("light.den_wall_lights", "den"),
        light("light.den_wall_left", "den"),
    ])

    assert {lt.entity_id for lt in lights} == {"light.den_wall_left"}
    assert any("coordinator" in why for _, why in report.skipped)


def test_including_a_zha_group_by_hand_places_it_anyway():
    """The escape hatch, and the reason skipping is safe. If ZHA somehow does
    not expose the members individually, the group is the only handle on those
    bulbs and the human says so once, in project.yaml."""
    model = model_with(room("den"))
    config = LightsConfig(include={"light.den_wall_lights"})
    lights, _report = build_lights(
        model, [on_coordinator("light.den_wall_lights", "den")], config)

    assert [lt.entity_id for lt in lights] == ["light.den_wall_lights"]


def test_zero_power_is_refused_because_the_plugin_would_ignore_it():
    model = model_with(room("hall"))
    config = LightsConfig(power={"light.a": 0.0})
    lights, report = build_lights(model, [light("light.a", "hall")], config)

    assert lights == []
    assert any("not > 0" in why for _, why in report.skipped)


def test_every_placed_light_has_power_above_zero():
    model = model_with(room("hall"))
    lights, _ = build_lights(model, [light("light.a", "hall")])
    assert all(lt.power > 0 for lt in lights)


# --------------------------------------------------------------------------- #
# the human's corrections
# --------------------------------------------------------------------------- #


def test_an_excluded_entity_is_not_placed():
    model = model_with(room("hall"))
    config = LightsConfig(exclude={"light.router_led"})
    lights, report = build_lights(
        model, [light("light.router_led", "hall"), light("light.real", "hall")], config)

    assert {lt.entity_id for lt in lights} == {"light.real"}
    assert any("excluded" in why for _, why in report.skipped)


def test_include_overrides_a_skip():
    """The human has looked and decided; the tool's guess does not get a veto."""
    model = model_with(room("den"))
    config = LightsConfig(include={"light.group"})
    lights, _report = build_lights(model, [
        light("light.group", "den", members=["light.a"]),
        light("light.a", "den"),
    ], config)
    assert {lt.entity_id for lt in lights} == {"light.group", "light.a"}


def test_one_entity_can_be_placed_in_several_areas():
    """A stairwell spans three levels but Home Assistant files it under one.
    The plugin sums sources sharing a name, so this is the correct shape."""
    model = Model(source="x.dxf", levels=[
        Level(name="Ground", ceiling_height_cm=250, rooms=[room("stairwell")]),
        Level(name="Upper", ceiling_height_cm=250, rooms=[room("upper_stair")]),
    ])
    config = LightsConfig(extra={"light.stairwell": ["upper_stair"]})
    lights, _report = build_lights(model, [light("light.stairwell", "stairwell")], config)

    assert len(lights) == 2
    assert {lt.entity_id for lt in lights} == {"light.stairwell"}
    assert sorted(lt.level for lt in lights) == [0, 1]


def test_config_reads_the_project_yaml_shape():
    config = LightsConfig.from_project({"lights": {
        "exclude": ["light.a"],
        "extra": {"light.b": ["hall", "landing"]},
        "power": {"light.c": 0.25},
    }})
    assert config.exclude == {"light.a"}
    assert config.extra == {"light.b": ["hall", "landing"]}
    assert config.power == {"light.c": 0.25}


def test_a_project_with_no_lights_section_is_fine():
    assert LightsConfig.from_project({}).exclude == set()


# --------------------------------------------------------------------------- #
# measured fittings, which are the point of a fixture pass
# --------------------------------------------------------------------------- #


def test_one_entity_with_several_fittings_gets_a_placement_at_each():
    """The common case, not the exception.

    One real upstairs had roughly 18 fittings and 5 light.* entities; the rest
    were dumb switches. The plugin sums sources sharing a name, so N placements
    carrying one entity_id is the correct model of one switch driving N bulbs.
    """
    model = model_with(room("office"))
    found = {"office": [Fitting(50, 50, 240), Fitting(150, 150, 240),
                        Fitting(250, 250, 240)]}
    lights, report = build_lights(model, [light("light.office", "office")], None, found)

    assert len(lights) == 3
    assert {lt.entity_id for lt in lights} == {"light.office"}
    assert {(lt.x, lt.y) for lt in lights} == {(50, 50), (150, 150), (250, 250)}
    assert report.measured == [("office", 3, 1)]


def test_a_measured_height_replaces_the_ceiling_guess():
    """Measuring the fittings is the whole point; the guess is wrong for every
    pendant, wall light and lamp in the house."""
    model = model_with(room("hall", ceiling_low_cm=250, ceiling_high_cm=250))
    found = {"hall": [Fitting(100, 100, 172.5)]}
    lights, _ = build_lights(model, [light("light.hall", "hall")], None, found)

    assert lights[0].elevation == 172.5, "fell back to ceiling minus the drop"


def test_a_fitting_with_no_measured_height_falls_back_to_the_ceiling():
    model = model_with(room("hall", ceiling_low_cm=250, ceiling_high_cm=250))
    found = {"hall": [Fitting(100, 100, None)]}
    lights, _ = build_lights(model, [light("light.hall", "hall")], None, found)
    assert lights[0].elevation == 230


def test_several_entities_and_several_fittings_is_reported_not_guessed():
    """Which entity drives which fitting is not in the geometry, and pairing by
    proximity would be confidently wrong about as often as it is right."""
    model = model_with(room("lounge"))
    found = {"lounge": [Fitting(50, 50, 240), Fitting(250, 250, 240)]}
    lights, report = build_lights(
        model, [light("light.a", "lounge"), light("light.b", "lounge")], None, found)

    # Two entities on two devices -- these carry none, so each is its own.
    assert report.ambiguous == [("lounge", 2, 2, 2)]
    assert len(lights) == 2
    # Fell back to the pole rather than pairing them off by distance.
    assert {(lt.x, lt.y) for lt in lights} != {(50, 50), (250, 250)}


def test_load_fittings_drops_records_that_landed_in_no_room(tmp_path):
    path = tmp_path / "f.json"
    path.write_text(json.dumps([
        {"room": "kitchen", "plan_x_cm": 10, "plan_y_cm": 20, "elevation_cm": 240},
        {"room": "OUTSIDE", "plan_x_cm": 900, "plan_y_cm": 900},
    ]), encoding="utf-8")

    found = load_fittings(path)
    assert set(found) == {"kitchen"}
    assert found["kitchen"][0].elevation == 240


def test_load_fittings_accepts_a_flagged_near_miss(tmp_path):
    """A fitting just the wrong side of a wall line is common and still useful;
    placefixtures marks it rather than discarding it."""
    path = tmp_path / "f.json"
    path.write_text(json.dumps(
        [{"room": "~pantry (+42cm)", "plan_x_cm": 10, "plan_y_cm": 20}]), encoding="utf-8")

    found = load_fittings(path)
    assert set(found) == {"pantry"}
    assert found["pantry"][0].elevation is None


def test_a_daylight_verdict_survives_the_load(tmp_path):
    """The refusal happens in build_lights, where it can be counted, rather
    than at load, where it would be a silent drop."""
    path = tmp_path / "f.json"
    path.write_text(json.dumps([
        {"room": "kitchen", "plan_x_cm": 10, "plan_y_cm": 20,
         "verdict": "window", "crop": "03.png"},
    ]), encoding="utf-8")

    fitting = load_fittings(path)["kitchen"][0]
    assert fitting.verdict == "window"
    assert fitting.crop == "03.png"


def test_a_fitting_that_reads_as_a_window_is_not_placed_and_is_counted():
    """Differencing said it was bright in an ordinary capture too, so it is
    glass. Placing it would hang a lamp in a window; dropping it in silence
    would leave nobody able to say which step decided that."""
    model = model_with(room("kitchen"))
    fittings = {"kitchen": [Fitting(50.0, 50.0, 240.0, verdict="window"),
                            Fitting(300.0, 300.0, 240.0, verdict="fitting")]}
    lights, report = build_lights(
        model, [light("light.kitchen", "kitchen")], fittings=fittings)

    assert [(lt.x, lt.y) for lt in lights] == [(300.0, 300.0)]
    assert report.daylight == [("kitchen", 1)]


def test_an_unjudged_fitting_is_still_placed():
    """"unseen" means the ordinary capture never photographed that spot, which
    is not evidence about the fitting. Treating it as a window would discard
    the ceiling fittings an ordinary capture covers worst."""
    model = model_with(room("kitchen"))
    fittings = {"kitchen": [Fitting(50.0, 50.0, 240.0, verdict="unseen")]}
    lights, report = build_lights(
        model, [light("light.kitchen", "kitchen")], fittings=fittings)

    assert [(lt.x, lt.y) for lt in lights] == [(50.0, 50.0)]
    assert report.daylight == []
