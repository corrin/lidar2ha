"""Combining several captures of one level.

MOST OF THESE RUN ON THE REAL HOUSE. `tests/fixtures/` holds three actual
captures of one storey, trimmed to walls and room polygons. They are here because
the numbers in this module -- 270.84 degrees, 88% coverage, 3% overlap, 23
duplicate walls -- came off real scans, and invented geometry would agree with
whatever the code happened to do.

`scan7.json` is the third, and it earns its place by being the capture that
showed `midlevel` was the worst of the three rather than the yardstick. Two
captures cannot show that: it takes a third for "fits onto the others" to mean
anything.

The mid-level bathroom is the case the whole module exists for: it is in the
fixture pass, absent from the geometry pass, and before `combine` it vanished
from the model with nothing saying so.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest
from shapely.geometry import Polygon

from lidar2ha import combine as combining
from lidar2ha.combine import (
    Candidate,
    ceiling_plausibility,
    containment,
    group_rooms,
    select_walls,
)
from lidar2ha.schema import Capture, Level, Model, Room, Wall, load_model

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def house() -> dict[str, Model]:
    """The two real mid-level captures."""
    return {name: load_model(FIXTURES / f"{name}.json")
            for name in ("midlevel", "midlevel_fixtures")}


@pytest.fixture(scope="module")
def combined(house):
    return combining.combine(house)


def room_named(model: Model, name: str) -> Room:
    return next(r for lv in model.levels for r in lv.rooms if r.name == name)


# --------------------------------------------------------------------------- #
# the room that used to vanish
# --------------------------------------------------------------------------- #


def test_the_bathroom_survives_combining(combined):
    """The mid-level bathroom is in the fixture pass and not in the geometry
    pass. Before this stage it disappeared from the model and nothing noticed --
    which is the failure the whole module exists to end."""
    bathroom = room_named(combined.model, "Bathroom")
    assert bathroom.source == "midlevel_fixtures"
    assert bathroom.provisional, "a fixture pass is the only thing that has seen it"


def test_the_bathroom_says_why_it_is_provisional(combined):
    """A flag with no reason is a flag nobody can act on. The work list has to
    say `re-scan this as geometry`, not just `low confidence`."""
    bathroom = room_named(combined.model, "Bathroom")
    assert any("re-scan" in r for r in bathroom.provisional_reason)
    assert any("fixtures pass" in r for r in bathroom.provisional_reason)


def test_the_bathroom_reaches_the_work_list(combined):
    """The model is half the output. A room that is present but silently wrong
    is the same failure in a different place."""
    entries = [w for w in combined.worklist
               if w.get("room") == "Bathroom" and w["kind"] == "provisional_room"]
    assert len(entries) == 1
    assert entries[0]["capture"] == "midlevel_fixtures"
    assert entries[0]["role"] == "fixtures"


def test_provisional_does_not_depend_on_the_score_alone(combined):
    """The bathroom scores around 0.6, but it would still have to be flagged at
    0.95: what makes it provisional is that only a fixture pass has ever seen
    it. A bare threshold on the score would ship it unflagged the day the fit
    improved."""
    bathroom = room_named(combined.model, "Bathroom")
    reasons = [r for r in bathroom.provisional_reason if "below" not in r]
    assert reasons, "every reason was the score threshold; nothing else caught it"


# --------------------------------------------------------------------------- #
# stage 1 -- one frame, and what may refuse a capture
# --------------------------------------------------------------------------- #


def test_the_fixture_pass_lands_where_it_was_measured_to(combined):
    """Stage 1 is the hop everything downstream is meaningless without. These
    are measured off the real captures; a different rotation means the fit was
    rewritten wrong, however plausible the rooms look afterwards."""
    fit = combined.fits["midlevel_fixtures"]
    assert math.degrees(fit["theta_rad"]) % 360 == pytest.approx(270.84, abs=0.1)
    assert fit["median_error_m"] * 100 == pytest.approx(7.4, abs=0.3)
    assert fit["p90_m"] * 100 == pytest.approx(47.6, abs=1.0)


def test_a_capture_seeing_a_new_room_is_not_rejected_for_low_coverage(combined):
    """Coverage is the fraction of the SOURCE's walls the reference explains, so
    a capture that sees a room the reference does not always scores lower. A 90%
    threshold rejected this capture at 88% -- the one capture with the bathroom
    in it, which is precisely the information being sought."""
    fit = combined.fits["midlevel_fixtures"]
    assert fit["coverage"] == pytest.approx(0.88, abs=0.02)
    assert "midlevel_fixtures" not in combined.rejected


def test_a_multi_level_capture_is_refused_by_name(house):
    """Flattening two storeys into one point cloud lets a mirrored fit explain
    as much of it as the correct one. The prototype folded such a capture in
    without a word."""
    two_storey = house["midlevel"].model_copy(update={
        "levels": [house["midlevel"].levels[0],
                   house["midlevel"].levels[0].model_copy(update={"name": "Floor 2"})]})
    result = combining.combine({"midlevel_fixtures": house["midlevel_fixtures"],
                                "two_storey": two_storey},
                               reference="midlevel_fixtures")
    assert "two_storey" in result.rejected
    assert "2 levels" in result.rejected["two_storey"]


def test_a_capture_with_no_walls_names_the_rooms_lost_with_it(house):
    """With no walls there is nothing to fit, so the capture cannot enter the
    shared frame -- and its rooms go with it. Saying only `no walls` hides that
    a room count just dropped."""
    roomy = house["midlevel_fixtures"].model_copy(update={
        "levels": [house["midlevel_fixtures"].levels[0].model_copy(update={"walls": []})]})
    result = combining.combine({"midlevel": house["midlevel"], "roomy": roomy})
    assert "roomy" in result.rejected
    assert "5 room(s)" in result.rejected["roomy"]
    assert "Bathroom" in result.rejected["roomy"]


# --------------------------------------------------------------------------- #
# stage 2 -- correspondence by polygon, never by name
# --------------------------------------------------------------------------- #


def square(x0, y0, side, name="r", **kw) -> Room:
    return Room(name=name, points=[(x0, y0), (x0 + side, y0),
                                   (x0 + side, y0 + side), (x0, y0 + side)], **kw)


def cand(index, capture, room, role="geometry") -> Candidate:
    poly = Polygon(room.points)
    return Candidate(index=index, capture=capture, role=role, room=room, poly=poly,
                     area_m2=poly.area * 1e-4)


def test_iou_would_miss_the_fused_room(combined):
    """The predicate is `intersection / min(area)` and NOT IoU, and this is the
    measurement that decides it. The fixture pass fused four rooms into one; by
    IoU it barely touches three of them, so an IoU threshold low enough to catch
    them would chain half the floor into one group."""
    by = {(c.capture, c.room.name): c for c in combined.candidates}
    fused = by[("midlevel_fixtures", "Living Room")]
    dining = by[("midlevel", "Dining Room")]

    contained, _ = containment(fused.poly, dining.poly)
    iou = (fused.poly.intersection(dining.poly).area
           / fused.poly.union(dining.poly).area)
    assert contained > 0.9, "the dining room is almost wholly inside the fused room"
    assert iou < 0.2, "if this fails, IoU would have worked and the choice is moot"


def test_a_fused_room_is_reported_as_a_disagreement(combined):
    """One capture split this floor four ways and the other did not. Picking
    either silently is the forbidden outcome; the disagreement itself is the
    thing to surface."""
    fused = next(c for c in combined.candidates
                 if c.capture == "midlevel_fixtures" and c.room.name == "Living Room")
    group = next(g for g in combined.groups if fused.index in g.members)
    assert group.kind == "disagreement"
    assert len(group.per_capture["midlevel"]) == 4


def test_a_decisive_disagreement_still_reaches_the_work_list(combined):
    """The rooms are only FLAGGED when the margin is thin -- `go re-scan this` is
    wrong advice when a geometry capture beat a fixture pass decisively. But
    `these captures do not agree about the layout here` is a fact about the
    house, and it must never be dropped just because the winner was clear."""
    disagreements = [w for w in combined.worklist if w["kind"] == "captures_disagree"]
    assert disagreements, "a disagreement was resolved without being reported"
    assert all(d["lost"] for d in disagreements)


def test_correspondence_ignores_the_scanner_name(house):
    """Scanner names are not identity: one capture labelled a single entrance
    hall `Living Room` AND `Dining Room`. Renaming a room must change nothing."""
    renamed = house["midlevel_fixtures"].model_copy(update={
        "levels": [house["midlevel_fixtures"].levels[0].model_copy(update={
            "rooms": [r.model_copy(update={"name": f"Nonsense {i}"})
                      for i, r in enumerate(house["midlevel_fixtures"].levels[0].rooms)]})]})
    before = combining.combine(house)
    after = combining.combine({"midlevel": house["midlevel"], "midlevel_fixtures": renamed})
    assert ([g.kind for g in before.groups] == [g.kind for g in after.groups])


def test_a_sliver_inside_a_large_room_is_not_a_correspondence():
    """Containment alone reports 1.00 for a cupboard-sized offcut wholly inside a
    big room, which would marry the two and let one delete the other."""
    big = cand(0, "a", square(0, 0, 600))
    sliver = cand(1, "b", square(100, 100, 50))       # 0.25 m2, wholly inside
    contained, area = containment(sliver.poly, big.poly)
    assert contained == pytest.approx(1.0), "if this fails the test proves nothing"
    assert area < combining.MIN_EDGE_M2
    groups = group_rooms([big, sliver])
    assert len(groups) == 2, "the sliver was married to the room it sits in"


def test_two_rooms_of_one_capture_overlapping_is_reported_as_a_tangle():
    """The scanner contradicting itself. It has to be surfaced rather than
    resolved, because the repair is a seam or a merge, not a score."""
    groups = group_rooms([cand(0, "a", square(0, 0, 400, name="x")),
                          cand(1, "a", square(100, 100, 400, name="y"))])
    assert len(groups) == 1
    assert groups[0].kind == "tangled"
    assert groups[0].self_overlaps


# --------------------------------------------------------------------------- #
# stage 3 -- scoring
# --------------------------------------------------------------------------- #


def test_the_geometry_capture_keeps_the_room_it_scanned_better(combined):
    """Both captures saw the laundry and agree it is one room. The geometry pass
    fits it at 1.3 cm against the fixture pass's 6.3 cm, so it must win --
    otherwise `role` and fit quality are not doing anything at all."""
    laundry = [c for c in combined.candidates if c.room.name == "Laundry"]
    assert len(laundry) == 2, "if this fails the contest never happened"
    group = next(g for g in combined.groups if laundry[0].index in g.members)
    decision = next(d for d in combined.decisions if d.group is group)
    assert decision.winner == "midlevel"


def test_a_ceiling_is_judged_on_its_high_point_not_its_low_one():
    """A real sloped storey reports lows of 100-170 cm at the eaves. Judging
    `ceiling_low_cm` against a plausibility floor marks most of it implausible,
    which is a good geometry capture being told it is a bad one."""
    eaves = square(0, 0, 300, ceiling_low_cm=110.0, ceiling_high_cm=400.0)
    assert ceiling_plausibility(eaves, []) == pytest.approx(1.0)


def test_a_room_no_capture_saw_a_ceiling_above_two_metres_is_implausible():
    """The lowest tallest-point measured anywhere in this house is 210 cm. A
    room whose highest point is under about 2 m was not seen properly."""
    unseen = square(0, 0, 300, ceiling_low_cm=150.0, ceiling_high_cm=170.0)
    assert ceiling_plausibility(unseen, []) < 0.2


def test_a_short_ceiling_reading_loses_to_a_taller_one_of_the_same_room():
    """A scanner reports the highest surface it saw, so failing to look up costs
    height and nothing adds it. Across captures of one space the tallest reading
    is the best estimate, and falling short of it is evidence of not looking."""
    short = square(0, 0, 300, ceiling_low_cm=180.0, ceiling_high_cm=185.0)
    tall = square(0, 0, 300, ceiling_low_cm=220.0, ceiling_high_cm=270.0)
    assert ceiling_plausibility(short, [tall]) < ceiling_plausibility(tall, [short])


def test_slack_keeps_a_ten_centimetre_disagreement_from_deciding_anything():
    """Two captures of one room read 270 and 280 for reasons that are not
    evidence. Without slack the taller reading wins every such room."""
    a = square(0, 0, 300, ceiling_low_cm=270.0, ceiling_high_cm=270.0)
    b = square(0, 0, 300, ceiling_low_cm=280.0, ceiling_high_cm=280.0)
    assert ceiling_plausibility(a, [b]) == pytest.approx(1.0)


def test_an_unmeasured_signal_redistributes_its_weight(combined):
    """Scoring an unmeasured signal as zero punishes a room for the scan never
    having looked -- the same mistake as calling a ceiling nobody measured 0 cm
    tall. An unopposed room has no consensus to measure and must not be sunk
    by that alone."""
    bathroom = next(c for c in combined.candidates if c.room.name == "Bathroom")
    score = combined.scores[bathroom.index]
    assert "consensus" in score.missing
    assert score.total > 0.4, "an unmeasured signal was scored as zero"


def test_role_is_a_prior_and_never_a_veto(combined):
    """A fixture pass is likely to be worse at geometry, not barred from it. If
    `role` vetoed, the bathroom would have been thrown away a second time."""
    bathroom = room_named(combined.model, "Bathroom")
    assert bathroom.source == "midlevel_fixtures"
    assert combining.ROLE_PENALTY < 0.5, "a penalty this large would act as a veto"


# --------------------------------------------------------------------------- #
# stage 4 -- selection, and the walls
# --------------------------------------------------------------------------- #


def test_no_room_in_the_output_has_a_null_source(combined):
    """Provenance is the point. A room that cannot say which capture it came
    from cannot be checked, re-scanned, or trusted."""
    rooms = combined.model.levels[0].rooms
    assert rooms and all(r.source is not None for r in rooms)


def test_no_wall_in_the_output_has_a_null_source(combined):
    """Walls are selected across captures too, so they carry the same debt."""
    walls = combined.model.levels[0].walls
    assert walls and all(w.source is not None for w in walls)


def test_a_duplicate_wall_is_dropped_and_a_new_one_kept(combined):
    """Measured on these two captures: the reference's 32 walls, plus the 6 the
    fixture pass adds that nothing else has -- the bathroom. The other 23 are the
    same physical walls seen twice."""
    walls = combined.model.levels[0].walls
    by_source = {}
    for wall in walls:
        by_source[wall.source] = by_source.get(wall.source, 0) + 1
    assert by_source == {"midlevel": 32, "midlevel_fixtures": 6}


def test_a_capture_is_never_deduplicated_against_itself():
    """A capture's own wall list is the answer it already arrived at, so two of
    its walls lying close together are two walls. Comparing a capture with
    itself destroyed nine of the reference's own walls -- short collinear runs,
    each reading 100% matched against its neighbour."""
    a = Wall(x_start=0, y_start=0, x_end=100, y_end=0, thickness=10, height=250)
    b = Wall(x_start=100, y_start=0, x_end=200, y_end=0, thickness=10, height=250)
    kept, dropped = select_walls([("one", a), ("one", b)])
    assert len(kept) == 2 and not dropped

    kept, dropped = select_walls([("one", a), ("two", b.model_copy())])
    assert len(kept) == 2, "different captures, genuinely different walls"


def test_the_same_wall_from_two_captures_is_kept_once():
    """The other half of the previous test: across captures, one wall measured
    twice is one wall. Averaging the two would match neither."""
    a = Wall(x_start=0, y_start=0, x_end=500, y_end=0, thickness=10, height=250)
    nearly = Wall(x_start=2, y_start=6, x_end=502, y_end=6, thickness=10, height=250)
    kept, dropped = select_walls([("one", a), ("two", nearly)])
    assert len(kept) == 1 and len(dropped) == 1
    assert kept[0] is a, "the better capture's version survives, not a blend"


def test_the_winner_takes_the_group_whole_rather_than_room_by_room(combined):
    """Picking room by room inside a disagreement lays the same floor twice --
    the reference's living room plus the fixture pass's fused one."""
    fused = next(c for c in combined.candidates
                 if c.capture == "midlevel_fixtures" and c.room.name == "Living Room")
    group = next(g for g in combined.groups if fused.index in g.members)
    decision = next(d for d in combined.decisions if d.group is group)
    sources = {combined.candidates[i].capture for i in decision.winner_rooms}
    assert len(sources) == 1, "a group was split between captures"


def test_the_combined_model_is_buildable(combined):
    """`build` refuses anything that is not a survey of the building. A combined
    model IS one, even where a fixture pass supplied a room -- which capture
    supplied what is recorded on the rooms, not on the model."""
    assert combined.model.role == "geometry"
    assert {c.id for c in combined.model.captures} == {"midlevel", "midlevel_fixtures"}
    assert sum(1 for c in combined.model.captures if c.is_reference) == 1


def test_the_combined_frame_is_the_reference_frame(combined):
    """Textures are indexed in the reference's own mesh, so the level has to keep
    the reference's registration or every wall texture lands somewhere else."""
    reference = load_model(FIXTURES / "midlevel.json")
    assert combined.model.levels[0].registration == reference.levels[0].registration


# --------------------------------------------------------------------------- #
# stage 5 -- what is missing, which is the part nothing else reports
# --------------------------------------------------------------------------- #


def test_new_ground_is_decided_by_overlap_not_by_outline_matching(combined):
    """The bathroom finds walls along 55% of its outline, because it is carved
    out of existing space and shares its walls with neighbours the reference
    does have. An outline test reads that as `mostly known`. It overlaps the
    reference's rooms by 3%, which is what actually identifies it as new."""
    bathroom = next(c for c in combined.candidates if c.room.name == "Bathroom")
    reference = [c for c in combined.candidates if c.capture == "midlevel"]
    covered = sum(bathroom.poly.intersection(c.poly).area for c in reference)
    assert covered / bathroom.poly.area < 0.10


def test_floor_only_one_capture_saw_is_reported_even_inside_a_known_room(combined):
    """New ground is NOT a property of a room. A room overlapping the reference
    by 80% reads as known, and the fifth of it nobody else saw is dropped
    without a word -- on a three-capture level that lost 21.2 m2 while the
    per-room flag reported two rooms."""
    assert combined.fragments, "the geometric difference found nothing at all"
    assert all(f.capture and f.room for f in combined.fragments), \
        "a fragment with no room is a blob; identity comes from intersecting back"
    assert any(w["kind"] == "floor_not_in_the_model" for w in combined.worklist)


def test_registration_residue_is_counted_rather_than_listed(combined):
    """Differencing two polygons that nearly coincide leaves slivers along every
    shared wall line. Listing them as places to go and re-scan buries the real
    findings; dropping them silently is worse."""
    assert combined.slivers > 0, "if this fails the fixtures stopped producing residue"
    assert all(f.area_m2 >= combining.MIN_FRAGMENT_M2 for f in combined.fragments)


def test_an_area_the_project_maps_but_nobody_won_is_named(house):
    """An area mapped in project.yaml that no capture ever supplied is a gap.
    Saying nothing about it is exactly the silent drop this repo refuses."""
    result = combining.combine(house, expected_areas={"conservatory"})
    missing = [w for w in result.worklist if w["kind"] == "area_with_no_source"]
    assert [w["area"] for w in missing] == ["conservatory"]


def test_a_room_only_the_reference_saw_still_reaches_the_output(combined):
    """Union runs both ways. The reference's pantry stands on floor no other
    capture reached, and must survive exactly as the fixture pass's bathroom
    does.

    Selected by source AND area, not by name: both captures happen to contain a
    room called `Other 2` and they are different rooms 2.3 and 9.6 m2 apart, so
    matching on the name alone accepts either and proves nothing.
    """
    pantry = next(r for r in combined.model.levels[0].rooms
                  if r.name == "Other 2" and r.source == "midlevel")
    assert Polygon(pantry.points).area / 10_000 == pytest.approx(2.29, abs=0.05)

    group = next(g for g in combined.groups
                 if any(combined.candidates[i].capture == "midlevel"
                        and combined.candidates[i].area_m2 < 2.5
                        and combined.candidates[i].room.name == "Other 2"
                        for i in g.members))
    assert group.kind == "unopposed", "if this fails the room was not reference-only"


def test_a_malformed_polygon_is_named_rather_than_skipped(house):
    """A room that cannot become a polygon is not emitted, so it has to be loud.
    Silently returning fewer rooms than the capture holds is the failure this
    module was written to end."""
    broken = house["midlevel_fixtures"].levels[0].model_copy(update={
        "rooms": [*house["midlevel_fixtures"].levels[0].rooms,
                  Room(name="Impossible", points=[(0.0, 0.0), (1.0, 1.0)])]})
    result = combining.combine({
        "midlevel": house["midlevel"],
        "midlevel_fixtures": house["midlevel_fixtures"].model_copy(
            update={"levels": [broken]})})
    named = [m for m in result.malformed if m["room"] == "Impossible"]
    assert len(named) == 1
    assert "2 points" in named[0]["reason"]


# --------------------------------------------------------------------------- #
# a small capture, whose coverage means nothing
# --------------------------------------------------------------------------- #


def test_a_small_capture_is_told_its_coverage_proves_nothing(house):
    """A capture spanning a fraction of the reference lands inside it wherever
    it is put, so it reports 100% coverage for a wrong placement as readily as
    for a right one. Measured, a five-wall single room fitted a ten-room
    reference at 100% coverage and 18.9 cm median while sitting 65 degrees out
    and on top of the wrong room. Nothing reported distinguished it."""
    one_room = house["midlevel_fixtures"].levels[0]
    small = house["midlevel_fixtures"].model_copy(update={
        "levels": [one_room.model_copy(update={
            "walls": one_room.walls[:5], "rooms": one_room.rooms[:1]})]})
    result = combining.combine({"midlevel": house["midlevel"], "small": small})
    assert "small" in result.cautions
    assert "coverage says nothing" in result.cautions["small"]


def test_a_full_size_capture_is_not_cautioned(combined):
    """The caution has to stay rare enough to read. A capture that genuinely
    covers the level constrains its own rotation, and coverage means what it
    says."""
    assert combined.cautions == {}


# --------------------------------------------------------------------------- #
# what an unnamed room is standing on
# --------------------------------------------------------------------------- #


def named_house(house) -> dict[str, Model]:
    """The reference with Home Assistant areas on it, as `rooms` leaves it."""
    areas = {"Laundry": "laundry", "Kitchen": "kitchen", "Living Room": "living_room",
             "Dining Room": "dining", "Other 1": "hallway", "Other 2": "pantry",
             "Office 1": "kitchen", "Office 2": "boy_alcove"}
    level = house["midlevel"].levels[0]
    return {"midlevel": house["midlevel"].model_copy(update={
        "levels": [level.model_copy(update={"rooms": [
            r.model_copy(update={"ha_area": areas.get(str(r.name)),
                                 "scanner_name": r.name,
                                 "name": areas.get(str(r.name), r.name)})
            for r in level.rooms]})]}),
        "midlevel_fixtures": house["midlevel_fixtures"]}


def test_a_room_on_new_ground_is_asked_about_rather_than_guessed(house):
    """Nothing named stands under the bathroom, so it needs a name -- once, for
    the place. That is a different question from a room the project has already
    named under a different scanner label, and lumping the two together is what
    makes naming cost a line per room per capture."""
    result = combining.combine(named_house(house))
    bathroom = next(n for n in result.naming if n.room == "Bathroom")
    assert bathroom.verdict == "ask"
    assert bathroom.places == []


def test_a_name_is_suggested_and_never_written(house):
    """Identity is not inferred in this project. A room named by overlap and
    then believed is how a light ends up in the wrong room -- so the suggestion
    goes to the work list and `ha_area` stays empty."""
    result = combining.combine(named_house(house))
    assert result.naming, "if this fails the test proves nothing"
    for suggestion in result.naming:
        room = next(r for r in result.model.levels[0].rooms
                    if r.source == suggestion.capture and r.name == suggestion.room)
        assert room.ha_area is None, "a suggestion was written into the model"


def test_the_suggestion_reaches_the_work_list_with_what_to_do(house):
    """A verdict with no instruction is another thing to look up."""
    result = combining.combine(named_house(house))
    entries = [w for w in result.worklist if w["kind"].startswith("name_")]
    assert entries
    assert all(w["reasons"] and w["reasons"][0] for w in entries)


# --------------------------------------------------------------------------- #
# choosing the anchor, which decides what every other number means
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def trio() -> dict[str, Model]:
    """All three real captures of the mid level, including the poor one."""
    return {name: load_model(FIXTURES / f"{name}.json")
            for name in ("midlevel", "midlevel_fixtures", "scan7")}


def test_agreement_is_read_across_captures_not_against_the_anchor(trio):
    """The row of the pairwise matrix, not the column. How well others fit onto
    a capture partly measures how much it is being used as the yardstick; how
    well it fits onto ground others agree about does not.

    On this level the two differ completely: `midlevel` receives good fits and
    lands badly, which is what a bad anchor looks like from the inside."""
    onto = combining.fits_onto_others(trio)
    assert onto["midlevel"] > 2 * onto["scan7"]
    assert onto["midlevel"] > 2 * onto["midlevel_fixtures"]


def test_the_anchor_is_the_capture_that_resolves_the_most_rooms(trio):
    """The reference is the room set everything else is corresponded against, so
    a capture that fused three rooms into one makes that fusion the baseline and
    turns every capture that got it right into a disagreement.

    Wall count, which used to choose, cannot see this: `midlevel` and `scan7`
    have 32 walls each and 8 rooms against 10."""
    result = combining.combine(trio)
    assert result.reference == "scan7"
    assert len(trio["midlevel"].levels[0].walls) == len(trio["scan7"].levels[0].walls), \
        "if these differ the test proves nothing about wall count"


def test_a_capture_far_worse_than_the_rest_may_not_anchor(trio):
    """`midlevel` has more rooms than the fixture pass and would out-rank it on
    room count alone, but it lands 3.2x worse than the best on ground the others
    agree about. An anchor charges its own error to everything else."""
    onto = combining.fits_onto_others(trio)
    levels = {n: m.levels[0] for n, m in trio.items()}
    assert combining.pick_reference(levels, onto) == "scan7"
    # ...and with the outlier gone, room count decides between the rest.
    rest = {n: onto[n] for n in ("midlevel", "midlevel_fixtures")}
    assert combining.pick_reference(
        {n: levels[n] for n in rest}, rest) == "midlevel"


def test_two_captures_are_not_enough_to_disqualify_either(house):
    """The guard needs a consensus to be a consensus. With two captures the
    figure is one directed measurement, and the asymmetry between the two
    directions is the very thing in doubt -- excluding on it let a five-room
    fixture pass anchor over an eight-room survey on a tenth of a centimetre."""
    result = combining.combine(house)
    assert result.reference == "midlevel"


def test_the_poor_capture_is_named_as_the_outlier(trio):
    """A capture several times worse than the rest drags every correspondence it
    touches. Reported and not refused -- it still holds rooms nothing else has —
    but it must not be silent, because every other number moves when it is in."""
    result = combining.combine(trio)
    best = min(result.agreement.values())
    assert result.agreement["midlevel"] / best > combining.OUTLIER_RATIO


# --------------------------------------------------------------------------- #
# fusion, which is where a fixture pass actually loses
# --------------------------------------------------------------------------- #


def test_a_polygon_covering_several_rooms_is_penalised_for_it(combined):
    """A capture laying one polygon over four rooms another keeps apart is not
    slightly wrong about a boundary, it is missing three walls. That is
    measurable, and it is the real defect behind what `role` was guessing at."""
    fused = next(c for c in combined.candidates
                 if c.capture == "midlevel_fixtures" and c.room.name == "Living Room")
    group = next(g for g in combined.groups if fused.index in g.members)
    assert combining.partitioning(fused, group, combined.candidates) <= 0.25


def test_a_room_matching_one_room_is_not_penalised(combined):
    """One room to one room is the ordinary case and has to cost nothing, or
    every candidate in every correspondence pays for the fused ones."""
    laundry = next(c for c in combined.candidates
                   if c.capture == "midlevel_fixtures" and c.room.name == "Laundry")
    group = next(g for g in combined.groups if laundry.index in g.members)
    assert combining.partitioning(laundry, group, combined.candidates) == 1.0


def test_fusion_is_unmeasurable_when_nobody_else_saw_the_room(combined):
    """The third answer. A room no other capture has seen cannot be shown to
    fuse anything, which is not the same as being known not to."""
    bathroom = next(c for c in combined.candidates if c.room.name == "Bathroom")
    group = next(g for g in combined.groups if bathroom.index in g.members)
    assert combining.partitioning(bathroom, group, combined.candidates) is None
    assert "partitioning" in combined.scores[bathroom.index].missing


def test_the_role_prior_yields_to_the_measurement(combined):
    """`role` used to be charged to every fixture-pass room. Measured over two
    levels the fixture pass is the BEST-registered capture on both, so charging
    it for its name on top of a measurement of the thing the name stood for is
    charging it twice. The prior applies only where fusion is unmeasurable."""
    laundry = next(c for c in combined.candidates
                   if c.capture == "midlevel_fixtures" and c.room.name == "Laundry")
    group = next(g for g in combined.groups if laundry.index in g.members)
    score = combining.score_room(
        laundry, group, combined.candidates, own_tree=None, others_tree=None,
        capture=Capture(id="midlevel_fixtures", role="fixtures"))
    penalised = combining.score_room(
        laundry, group, combined.candidates, own_tree=None, others_tree=None,
        capture=Capture(id="midlevel_fixtures", role="fixtures"),
        role_penalty=0.9)
    assert score.total == penalised.total, \
        "the role penalty was charged despite fusion being measurable"


# --------------------------------------------------------------------------- #
# refusals at the boundary
# --------------------------------------------------------------------------- #


def test_one_capture_is_refused_rather_than_combined_with_itself(house):
    """Combining needs something to combine. Returning the single capture
    unchanged would make `combine` look like it had done something."""
    with pytest.raises(ValueError, match="at least two"):
        combining.combine({"midlevel": house["midlevel"]})


def test_a_named_reference_that_is_not_there_is_refused(house):
    """A typo in --reference would otherwise silently anchor on whichever
    capture the default happened to choose."""
    with pytest.raises(ValueError, match="not among"):
        combining.combine(house, reference="nosuch")


def test_the_reference_prefers_a_geometry_capture(combined):
    """A fixture pass CAN anchor a level -- it is a prior, not a veto -- but it
    should be the last capture asked to."""
    assert combined.reference == "midlevel"


def test_an_empty_level_is_refused_before_it_produces_a_confident_answer():
    """A model with no geometry at all reaching the fitter yields a transform
    describing nothing, which is worse than an error."""
    empty = Model(source="a.dxf", levels=[Level(name="L", ceiling_height_cm=250)])
    with pytest.raises(ValueError, match="no walls"):
        combining.combine({"a": empty, "b": empty})
