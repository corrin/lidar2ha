"""The prior knowledge `plan_fit` hands the fitter, and why each part exists.

The fitter searches rotation blindly and derives translation by aligning the two
point clouds' centroids. That is correct only when both captures cover the same
ground, and silently wrong when they do not: at the correct rotation the
centroid guess is metres out, so the correct basin scores worse than noise and
never reaches refinement.

These two functions supply what the fitter cannot work out for itself -- the
rotations a shared wall grid admits, and points believed to be the same place in
both captures.
"""

from __future__ import annotations

import math

from lidar2ha.compare import grid_rotations, room_anchors
from lidar2ha.schema import Level, Model, Room, Wall

CM = 100.0


def wall(x1: float, y1: float, x2: float, y2: float) -> Wall:
    return Wall(x_start=x1, y_start=y1, x_end=x2, y_end=y2,
                thickness=10.0, height=250.0)


def box(x0: float, y0: float, w: float, h: float) -> list[tuple[float, float]]:
    return [(x0, y0), (x0 + w, y0), (x0 + w, y0 + h), (x0, y0 + h)]


def capture(rooms: list[Room], walls: list[Wall]) -> Model:
    return Model(source="t.dxf", units="cm",
                 levels=[Level(name="Floor 1", ceiling_height_cm=250,
                               walls=walls, rooms=rooms)])


def square_walls(x0: float, y0: float, w: float, h: float) -> list[Wall]:
    return [wall(x0, y0, x0 + w, y0), wall(x0 + w, y0, x0 + w, y0 + h),
            wall(x0 + w, y0 + h, x0, y0 + h), wall(x0, y0 + h, x0, y0)]


# --------------------------------------------------------------------------- #
# rotations
# --------------------------------------------------------------------------- #


def test_only_four_rotations_can_relate_two_captures_of_one_building():
    """Both saw the same walls, so the answer is a quarter turn and nothing
    else. 180 candidates from a blind two-degree sweep collapse to four, and by
    construction none of them can land off the grid -- which is what the blind
    sweep did on four of this house's captures, by 14 to 39 degrees."""
    square = capture([], square_walls(0, 0, 400, 300))
    rots = grid_rotations([w for lv in square.levels for w in lv.walls],
                          [w for lv in square.levels for w in lv.walls])
    assert len(rots) == 4
    degrees = sorted(math.degrees(r) % 360 for r in rots)
    for got, want in zip(degrees, (0.0, 90.0, 180.0, 270.0), strict=True):
        assert abs(got - want) < 1e-6


def test_a_capture_with_no_walls_offers_no_rotations():
    """It falls back to the blind sweep rather than to a guess. A capture too
    wall-poor to have a grid has nothing to say about which rotations are
    admissible, and inventing four would place it on the strength of nothing."""
    walls = square_walls(0, 0, 400, 300)
    assert grid_rotations([], walls) == []
    assert grid_rotations(walls, []) == []


# --------------------------------------------------------------------------- #
# correspondences
# --------------------------------------------------------------------------- #


def test_rooms_of_similar_area_are_paired():
    """The ordinary case. Area is nearly rotation-invariant and nearly
    capture-invariant, which is what makes it a usable key before anything has
    been placed."""
    a = capture([Room(name="a", points=box(0, 0, 400, 300))], square_walls(0, 0, 400, 300))
    b = capture([Room(name="b", points=box(9000, 9000, 400, 300))],
                square_walls(9000, 9000, 400, 300))
    pairs = room_anchors(a, b)
    assert ((2.0, 1.5), (92.0, 91.5)) in [
        ((round(s[0], 3), round(s[1], 3)), (round(t[0], 3), round(t[1], 3)))
        for s, t in pairs]


def test_rooms_of_very_different_area_are_not_paired():
    """The filter that keeps the pairing from being every room against every
    room. A 12 m2 bedroom and a 2 m2 cupboard are not the same place, and
    scoring that correspondence is work spent to learn nothing."""
    a = capture([Room(name="a", points=box(0, 0, 400, 300))],
                square_walls(0, 0, 400, 300))
    b = capture([Room(name="b", points=box(0, 0, 250, 200))],
                square_walls(0, 0, 250, 200))
    assert not 0.6 < 5.0 / 12.0 < 1.6, "if these pair on area the test is vacuous"
    # Union against union is offered unconditionally and is the only pairing
    # here, one room each. So the test is that nothing ELSE was offered.
    assert len(room_anchors(a, b)) == 1


def test_a_sliver_is_not_a_correspondence():
    """Scanners emit 1 m2 artefacts, and one artefact pairs with another as
    readily as a room pairs with its own reflection. A centroid needs a room
    behind it to locate anything."""
    a = capture([Room(name="chip", points=box(0, 0, 100, 100))],
                square_walls(0, 0, 100, 100))
    b = capture([Room(name="chip", points=box(0, 0, 100, 100))],
                square_walls(0, 0, 100, 100))
    assert room_anchors(a, b) == []


def test_a_capture_that_over_segments_is_still_given_a_correspondence():
    """The case that recovered the fourth of four discarded captures.

    SEGMENTATION IS NOT SHARED BETWEEN CAPTURES. One real capture cut a space
    into a 15.6 m2 room and a 13.4 m2 room; another saw it whole, as one
    29.9 m2 room. Neither half pairs with the whole on area, so a strict
    room-to-room generator offers nothing at all and that capture is discarded
    -- as it was, at 27.6 cm and 32.9 degrees off grid, where the correct fit
    is 3.0 cm across 100% of its walls.
    """
    split = capture(
        [Room(name="left", points=box(0, 0, 300, 400)),
         Room(name="right", points=box(300, 0, 300, 400))],
        square_walls(0, 0, 600, 400))
    whole = capture([Room(name="all", points=box(0, 0, 600, 400))],
                    square_walls(0, 0, 600, 400))

    # The trap, stated: neither half pairs with the whole.
    halves = [12.0, 12.0]
    assert all(not (0.6 < h / 24.0 < 1.6) for h in halves), \
        "if a half pairs with the whole on area this test proves nothing"

    pairs = room_anchors(split, whole)
    assert pairs, "an over-segmented capture was offered no correspondence at all"
    centres = [(round(s[0], 3), round(s[1], 3)) for s, _ in pairs]
    assert (3.0, 2.0) in centres, (
        f"the union of the two halves is not among the correspondences: {centres}")


def test_a_capture_with_no_rooms_offers_no_correspondences():
    """It falls back to centroid alignment, which is exactly what it gets today.
    Nothing here may make a capture worse off than before it existed."""
    walls = square_walls(0, 0, 400, 300)
    assert room_anchors(capture([], walls), capture([], walls)) == []
