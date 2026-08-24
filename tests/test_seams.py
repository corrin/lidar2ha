"""Cutting an over-merged room into the rooms a person actually uses.

The counterpart to rooms.merge. A scanner sometimes fuses spaces a person keeps
separate, and reports one ceiling height for both — so the split has to be
geometric, and the pieces have to come back in a predictable order or the
caller's names and measured ceilings attach to the wrong halves.

An open plan fuses them for a better reason: there is no wall to segment on, so
every capture agrees and no rescanning separates the kitchen end from the dining
end. The boundary is then a declaration rather than a measurement, and the tests
below are mostly about what happens when a hand-traced declaration does not
quite fit the scanned polygon it is cutting.
"""

from __future__ import annotations

import pytest
from shapely.geometry import Polygon

from conftest import synthetic_floor
from lidar2ha.schema import Level, Model, Registration, Room
from lidar2ha.seams import apply, sections_of, split_room


def rect(x0, y0, x1, y1) -> Polygon:
    return Polygon([(x0, y0), (x1, y0), (x1, y1), (x0, y1)])


def test_a_seam_cuts_the_room_in_two():
    pieces = split_room(rect(0, 0, 400, 300), (200, -50), (200, 350))
    assert len(pieces) == 2
    assert sum(p.area for p in pieces) == pytest.approx(400 * 300)
    assert [round(p.area) for p in pieces] == [60000, 60000]


def test_the_seam_line_is_extended_past_the_polygon():
    """A seam measured off a preview stops at the wall it was drawn to.

    Given verbatim it would not reach the far edge and shapely would return the
    room uncut — so the line is extended before cutting.
    """
    pieces = split_room(rect(0, 0, 400, 300), (200, 100), (200, 200))
    assert len(pieces) == 2


def test_pieces_come_back_in_a_stable_side_order():
    """Names and measured ceilings are attached by position, so shapely's own
    ordering is not good enough."""
    room = rect(0, 0, 400, 300)
    forward = split_room(room, (100, -50), (100, 350))
    assert [round(p.area) for p in forward] == [30000, 90000]

    # Reversing the seam's direction swaps which side is which, and must do so
    # consistently rather than arbitrarily.
    backward = split_room(room, (100, 350), (100, -50))
    assert [round(p.area) for p in backward] == [90000, 30000]


def test_a_seam_that_misses_the_room_leaves_it_whole():
    pieces = split_room(rect(0, 0, 400, 300), (900, -50), (900, 350))
    assert len(pieces) == 1
    assert pieces[0].area == pytest.approx(400 * 300)


def test_a_degenerate_seam_is_rejected():
    with pytest.raises(ValueError, match="same point"):
        split_room(rect(0, 0, 400, 300), (200, 100), (200, 100))


def test_slivers_are_discarded_rather_than_becoming_rooms():
    """Cutting along the very edge should not yield a zero-width second room."""
    pieces = split_room(rect(0, 0, 400, 300), (0, -50), (0, 350))
    assert len(pieces) == 1


# --- traced sections ------------------------------------------------------
#
# A line cannot say "the kitchen is the L-shaped corner", so a declaration may
# instead trace each section. Hand-traced outlines do not tile a scanned polygon
# exactly, and every way they fail to has to end up in the report.

ROOM = rect(0, 0, 400, 300)          # 12 m2


def sections_of_boxes(*named) -> list[tuple[str, Polygon]]:
    return [(name, rect(*box)) for name, box in named]


def test_the_pieces_tile_the_room_exactly():
    """The one invariant the whole tiling exists to keep.

    Every square centimetre of the fused room ends up in exactly one piece.
    A gap here is floor that silently stops existing, which is the failure this
    codebase is built around -- and it renders as a room that is simply smaller
    than the house.
    """
    tiling = sections_of(
        ROOM,
        sections_of_boxes(("kitchen", (0, 0, 180, 300)),
                          ("lounge", (220, 0, 400, 300))),
        parent_name="open_living")
    assert sum(p.poly.area for p in tiling.pieces) == pytest.approx(ROOM.area)


def test_a_section_traced_past_the_wall_does_not_grow_the_room():
    """A trace read off a preview overshoots; annexing the neighbour is silent."""
    tiling = sections_of(
        ROOM,
        sections_of_boxes(("kitchen", (-100, 0, 200, 300)),
                          ("lounge", (200, 0, 400, 300))),
        parent_name="open_living")

    kitchen = next(p for p in tiling.pieces if p.name == "kitchen")
    assert kitchen.poly.area == pytest.approx(200 * 300)
    assert tiling.spill_m2["kitchen"] == pytest.approx(3.0)


def test_overlapping_sections_beyond_the_slop_are_refused():
    """Two traces claiming the same real floor cannot be resolved by guessing.

    Whichever way it went, the wrong half of the room gets the light -- forever,
    and with nothing on screen to say so.
    """
    with pytest.raises(ValueError, match="overlap"):
        sections_of(
            ROOM,
            sections_of_boxes(("kitchen", (0, 0, 200, 300)),
                              ("lounge", (100, 0, 400, 300))),
            parent_name="open_living")


def test_tracing_slop_is_resolved_and_the_area_moved_is_reported():
    tiling = sections_of(
        ROOM,
        sections_of_boxes(("kitchen", (0, 0, 200, 300)),
                          ("lounge", (195, 0, 400, 300))),
        parent_name="open_living")

    assert sum(p.poly.area for p in tiling.pieces) == pytest.approx(ROOM.area)
    assert tiling.moved_m2 == pytest.approx(0.15)


def test_unclaimed_floor_becomes_a_flagged_piece_rather_than_vanishing():
    """`uncovered_floor`'s lesson at room scale: the strip nobody traced.

    Kept under the parent's name so it is visible on the preview and can be
    traced properly next time round, not folded into whichever neighbour
    happened to be nearest.
    """
    tiling = sections_of(
        ROOM,
        sections_of_boxes(("kitchen", (0, 0, 180, 300)),
                          ("lounge", (220, 0, 400, 300))),
        parent_name="open_living")

    remainder = [p for p in tiling.pieces if "unclaimed_remainder" in p.reasons]
    assert len(remainder) == 1
    assert remainder[0].name == "open_living"
    assert remainder[0].poly.area == pytest.approx(40 * 300)
    assert tiling.remainder_m2 == pytest.approx(1.2)


def test_a_sliver_of_unclaimed_floor_is_absorbed_and_counted():
    """Below the threshold it is tracing slop, not a room -- but still counted."""
    tiling = sections_of(
        ROOM,
        sections_of_boxes(("kitchen", (0, 0, 190, 300)),
                          ("lounge", (210, 0, 400, 300))),
        parent_name="open_living")

    assert not [p for p in tiling.pieces if "unclaimed_remainder" in p.reasons]
    assert tiling.absorbed_m2 == pytest.approx(0.6)
    assert sum(p.poly.area for p in tiling.pieces) == pytest.approx(ROOM.area)


def test_sections_that_abut_an_l_shaped_room_stay_polygons():
    """Differencing polygons that share an edge yields dangling lines too.

    Shapely returns a GeometryCollection there, and a room built from one dies
    on `.exterior`. Sections are MEANT to abut, so this is the ordinary case
    rather than a corner one — it fired on the first real L-shaped room.
    """
    ell = Polygon([(0, 0), (600, 0), (600, 400), (200, 400), (200, 200), (0, 200)])
    tiling = sections_of(
        ell,
        sections_of_boxes(("kitchen", (0, 0, 200, 400)),
                          ("lounge", (200, 0, 600, 400))),
        parent_name="open_living")

    assert [p.poly.geom_type for p in tiling.pieces] == ["Polygon", "Polygon"]
    assert sum(p.poly.area for p in tiling.pieces) == pytest.approx(ell.area)


def test_one_section_is_not_a_split():
    with pytest.raises(ValueError, match="two sections"):
        sections_of(ROOM, sections_of_boxes(("kitchen", (0, 0, 200, 300))),
                    parent_name="open_living")


def test_a_section_wholly_outside_the_room_is_refused():
    """Coordinates read off the wrong preview, or the wrong room named."""
    with pytest.raises(ValueError, match="outside"):
        sections_of(
            ROOM,
            sections_of_boxes(("kitchen", (900, 0, 1000, 300)),
                              ("lounge", (0, 0, 400, 300))),
            parent_name="open_living")


def test_a_seam_and_the_equivalent_sections_agree():
    """Two ways to say one thing is how a sign error survives in this repo.

    If these ever disagree, one of the two paths has drifted.
    """
    by_line = split_room(ROOM, (200, -50), (200, 350))
    by_trace = sections_of(
        ROOM,
        sections_of_boxes(("west", (0, 0, 200, 300)),
                          ("east", (200, 0, 400, 300))),
        parent_name="open_living")

    assert ([round(p.area) for p in by_line]
            == [round(p.poly.area) for p in by_trace.pieces])


# --- carrying a declaration onto the model --------------------------------


def fused(**over) -> Model:
    """One 12 m2 room standing in for an open plan, already through `combine`."""
    room = Room(name="open_living", ha_area="open_living",
                scanner_name="Living Room",
                points=[(0, 0), (400, 0), (400, 300), (0, 300)],
                ceiling_low_cm=180, ceiling_high_cm=250,
                source="scan7", score=0.81, **over)
    return Model(source="synthetic.dxf", levels=[
        Level(name="Mid Level", ceiling_height_cm=250, rooms=[room])])


DECLARATION = {
    "room": "open_living",
    "sections": [{"name": "kitchen", "box": [[0, 0], [200, 300]]},
                 {"name": "lounge", "box": [[200, 0], [400, 300]]}],
}


def test_pieces_do_not_inherit_the_fused_ceiling():
    """The fused 180-250 cm range is one number standing for two spaces.

    Inheriting it would carry forward the very error the split exists to remove,
    and would do it looking exactly like a measurement.
    """
    model = fused()
    apply(model, [DECLARATION], level_name="Mid Level")

    assert [r.name for r in model.levels[0].rooms] == ["kitchen", "lounge"]
    for room in model.levels[0].rooms:
        assert room.ceiling_high_cm is None
        assert room.ceiling_low_cm is None


def test_provenance_survives_the_cut():
    """The geometry is still the capture's, still won the same way.

    Losing `source` here would make a split room indistinguishable from one no
    capture ever saw.
    """
    model = fused()
    apply(model, [DECLARATION], level_name="Mid Level")

    kitchen = model.levels[0].rooms[0]
    assert kitchen.split_from == "open_living"
    assert kitchen.source == "scan7"
    assert kitchen.score == 0.81
    assert kitchen.scanner_name == "Living Room"
    assert kitchen.ha_area == "kitchen"


def test_a_provisional_parent_makes_provisional_pieces():
    """Cutting a room up does not improve the scan it came from."""
    model = fused(provisional=True, provisional_reason=["won by a fixture pass"])
    apply(model, [DECLARATION], level_name="Mid Level")

    for room in model.levels[0].rooms:
        assert room.provisional
        assert "won by a fixture pass" in room.provisional_reason


def test_a_declaration_naming_no_room_is_refused():
    """Silently doing nothing leaves an open plan looking correctly split."""
    with pytest.raises(ValueError, match="no room"):
        apply(fused(), [{"room": "conservatory", "sections": []}],
              level_name="Mid Level")


def test_a_declaration_cannot_be_both_forms():
    with pytest.raises(ValueError, match="exactly one"):
        apply(fused(), [dict(DECLARATION, seam=[[200, -50], [200, 350]],
                             names=["a", "b"])], level_name="Mid Level")


def test_a_box_is_two_corners_not_a_traced_ring():
    with pytest.raises(ValueError, match="two opposite corners"):
        apply(fused(), [{"room": "open_living", "sections": [
            {"name": "kitchen", "box": [[0, 0], [200, 300], [0, 300]]},
            {"name": "lounge", "box": [[200, 0], [400, 300]]}]}],
            level_name="Mid Level")


def test_the_declared_level_is_the_only_one_cut():
    """A room name repeats across storeys; a declaration is about one of them."""
    model = fused()
    model.levels.append(Level(name="Upper", ceiling_height_cm=240, rooms=[
        Room(name="open_living", ha_area="open_living",
             points=[(0, 0), (400, 0), (400, 300), (0, 300)])]))

    apply(model, [DECLARATION], level_name="Mid Level")

    assert [r.name for r in model.levels[1].rooms] == ["open_living"]


# --- asking the floor whether the declared boundary is really there --------
#
# The room is 400 x 300 cm and the registration is the identity, so plan
# centimetres divided by 100 are mesh metres and the synthetic floor's feature
# at y = 2 m sits under a boundary declared at y = 200 cm.

ACROSS = {
    "room": "open_living",
    "sections": [{"name": "kitchen", "box": [[0, 0], [400, 200]]},
                 {"name": "lounge", "box": [[0, 200], [400, 300]]}],
}


def registered(**over) -> Model:
    model = fused(**over)
    model.levels[0].registration = Registration(
        theta_deg=0, tx_m=0, ty_m=0, mirror=False,
        median_error_m=0.01, coverage=1.0)
    return model


def test_a_step_under_the_boundary_corroborates_the_declaration():
    model = registered()
    cuts = apply(model, [ACROSS], level_name="Mid Level",
                 floor=synthetic_floor(step_at=0.19))

    assert len(cuts[0].edges) == 1
    assert cuts[0].edges[0].support.corroborated


def test_an_unbroken_floor_leaves_the_declaration_standing():
    """The sofa-end boundary. It is real, and the mesh cannot see it.

    Refusing here, or moving the line, would make the one kind of boundary an
    open plan is actually made of impossible to state.
    """
    model = registered()
    cuts = apply(model, [ACROSS], level_name="Mid Level",
                 floor=synthetic_floor())

    assert [r.name for r in model.levels[0].rooms] == ["kitchen", "lounge"]
    assert not cuts[0].edges[0].support.corroborated


def test_an_uncorroborated_boundary_is_not_marked_provisional():
    """`provisional` means "go and re-scan", and no rescan finds a sofa end.

    Setting it on every open-plan room forever would fire on everything and so
    mean nothing -- the reason the redundancy report judges a level against
    itself rather than against an absolute.
    """
    model = registered()
    apply(model, [ACROSS], level_name="Mid Level", floor=synthetic_floor())

    for room in model.levels[0].rooms:
        assert not room.provisional
        assert room.provisional_reason == []


def test_no_mesh_is_not_looked_at_rather_than_unsupported():
    cuts = apply(registered(), [ACROSS], level_name="Mid Level")

    edge = cuts[0].edges[0]
    assert edge.support is None
    assert edge.unmeasured == "no mesh given"


def test_an_unregistered_level_cannot_be_asked():
    """Without a registration the plan frame and the mesh frame are unrelated.

    Measuring anyway would sample the floor somewhere else entirely and report
    whatever it happened to find there as evidence about this boundary.
    """
    cuts = apply(fused(), [ACROSS], level_name="Mid Level",
                 floor=synthetic_floor(step_at=0.19))

    assert cuts[0].edges[0].unmeasured == "level is unregistered"
