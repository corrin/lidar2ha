"""Combining several captures of one level.

MOST OF THESE RUN ON THE REAL HOUSE. `tests/fixtures/midlevel.json` and
`midlevel_fixtures.json` are two actual captures of one storey, trimmed to walls
and room polygons. They are here because the numbers in this module -- 270.84
degrees, 88% coverage, 3% overlap, 23 duplicate walls -- came off real scans, and
invented geometry would agree with whatever the code happened to do.

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
from lidar2ha.schema import Level, Model, Room, Wall, load_model

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
    """Union runs both ways. The reference's pantry is in no other capture and
    must survive exactly as the fixture pass's bathroom does."""
    pantry = room_named(combined.model, "Other 2")
    assert pantry.source in {"midlevel", "midlevel_fixtures"}
    assert sum(1 for r in combined.model.levels[0].rooms if r.name == "Other 2") == 2


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
