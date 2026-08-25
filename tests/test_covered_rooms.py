"""Rooms that reach the model lying on top of each other.

`combine` already reports two captures claiming one piece of floor with rooms of
DIFFERENT SHAPE, as `captures_disagree`. Two rooms simply overlapping is the
other way for that to happen and nothing saw it, so the smaller one drew
underneath, kept its Home Assistant area, and every audit that counts named
areas called the level complete.

Found four times on one real house in a day, every one by a person looking at a
picture. The worst of them was covered AND unnamed, so the coverage roll-up
reported it as never scanned -- which reached three commit messages and a
dashboard before the owner spotted its walls in the plan.
"""

from __future__ import annotations

from shapely.geometry import Polygon

from lidar2ha.combine import Candidate, covered_rooms
from lidar2ha.schema import Room

M = 100.0  # plan geometry is centimetres


def candidate(index: int, name: str, x0: float, y0: float, w: float, h: float,
              capture: str = "scan") -> Candidate:
    """A room of `w` x `h` metres with its corner at (x0, y0) metres."""
    pts = [(x0 * M, y0 * M), ((x0 + w) * M, y0 * M),
           ((x0 + w) * M, (y0 + h) * M), (x0 * M, (y0 + h) * M)]
    poly = Polygon(pts)
    return Candidate(index=index, capture=capture, role="geometry",
                     room=Room(name=name, points=pts), poly=poly,
                     area_m2=poly.area / 1e4)


def test_a_room_swallowed_by_another_is_named_in_the_work_list():
    """The toilet. 1.44 m2 sitting entirely inside a hallway polygon -- named,
    mapped, and invisible in the render because it draws underneath."""
    hallway = candidate(0, "hallway", 0, 0, 6.0, 2.0)
    toilet = candidate(1, "toilet", 1.0, 0.4, 1.2, 1.2)

    items = covered_rooms([hallway, toilet])
    assert len(items) == 1
    it = items[0]
    assert it["kind"] == "rooms_overlap"
    assert it["covered"] == "toilet", "the buried room is the one at risk"
    assert it["covering"] == "hallway"
    assert it["share_of_covered"] == 1.0
    assert "lies inside" in it["reasons"][0]


def test_the_share_is_of_the_smaller_room_not_the_larger():
    """Against the larger room a swallowed cupboard is a rounding error. This
    toilet is 8% of its hallway and 100% of itself, and only the second number
    says a room is about to disappear -- measured the other way round it falls
    under the threshold and is never reported at all."""
    hallway = candidate(0, "hallway", 0, 0, 6.0, 3.0)
    toilet = candidate(1, "toilet", 1.0, 0.4, 1.2, 1.2)

    share_of_larger = toilet.area_m2 / hallway.area_m2
    assert share_of_larger < 0.10, "if this passes the threshold the test is vacuous"
    assert covered_rooms([hallway, toilet])[0]["share_of_covered"] == 1.0


def test_rooms_that_merely_touch_are_not_reported():
    """Every room on a level shares a wall with a neighbour, and after two
    captures have been fitted onto one another they share a centimetre or two
    of floor with it. Reporting that would fire on every pair and mean
    nothing."""
    left = candidate(0, "left", 0, 0, 3.0, 3.0)
    right = candidate(1, "right", 3.0, 0, 3.0, 3.0)
    assert left.poly.intersects(right.poly), "if these do not touch, nothing is tested"
    assert covered_rooms([left, right]) == []


def test_a_seam_a_couple_of_centimetres_wide_is_not_reported():
    """The measured case: fitted captures leave neighbours overlapping by the
    registration error. On this house every pair nobody minded read 5.6% of the
    smaller room or less."""
    left = candidate(0, "left", 0, 0, 3.0, 3.0)
    right = candidate(1, "right", 2.97, 0, 3.0, 3.0)
    overlap = left.poly.intersection(right.poly).area / 1e4
    assert 0.05 < overlap < 0.12, f"a 3 cm seam should be ~0.09 m2, got {overlap}"
    assert covered_rooms([left, right]) == []


def test_a_sliver_is_not_reported_however_buried_it_is():
    """A 0.06 m2 scanner artefact is 100% covered by whatever it sits on, and
    there is no room there to lose. Share alone would fire on every one of
    them."""
    room = candidate(0, "room", 0, 0, 4.0, 4.0)
    chip = candidate(1, "chip", 1.0, 1.0, 0.3, 0.2)
    assert chip.area_m2 < 0.10, "if this is not a sliver the test proves nothing"
    assert covered_rooms([room, chip]) == []


def test_a_partial_overlap_is_reported_with_the_area_a_person_can_check():
    """3.6 m2 of kitchen standing on a hallway. The number in the report has to
    be the one somebody can go and measure, not a ratio."""
    hallway = candidate(0, "hallway", 0, 0, 6.0, 3.0)
    kitchen = candidate(1, "kitchen", 4.0, 0, 4.0, 3.0)

    items = covered_rooms([hallway, kitchen])
    assert len(items) == 1
    assert items[0]["overlap_m2"] == 6.0
    assert "6.00 m2" in items[0]["reasons"][0]


def test_the_worst_buried_room_is_reported_first():
    """A level with several of these is a level somebody has to work through,
    and the room that is 90% gone matters more than the one that is 15% gone."""
    hallway = candidate(0, "hallway", 0, 0, 10.0, 4.0)
    buried = candidate(1, "buried", 1.0, 1.0, 1.0, 1.0)
    clipped = candidate(2, "clipped", 8.0, 3.5, 3.0, 3.0)

    items = covered_rooms([hallway, buried, clipped])
    assert [it["covered"] for it in items] == ["buried", "clipped"]
    assert items[0]["share_of_covered"] > items[1]["share_of_covered"]


def test_nothing_is_resolved_here():
    """Which room is right is a question about the house -- an island, a
    mezzanine and a mis-drawn wall all look identical from here. Both rooms stay
    in the model and the report says to go and decide, because quietly
    preferring one is the silent pick this stage exists to refuse."""
    hallway = candidate(0, "hallway", 0, 0, 6.0, 2.0)
    toilet = candidate(1, "toilet", 1.0, 0.4, 1.2, 1.2)

    items = covered_rooms([hallway, toilet])
    assert "Decide" in items[0]["reasons"][0]
    # Both rooms are still named in the entry, so neither has been dropped.
    assert items[0]["covered"] and items[0]["covering"]
