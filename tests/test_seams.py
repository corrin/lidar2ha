"""Cutting an over-merged room in two.

The counterpart to rooms.merge. A scanner sometimes fuses spaces a person keeps
separate, and reports one ceiling height for both — so the split has to be
geometric, and the pieces have to come back in a predictable order or the
caller's names and measured ceilings attach to the wrong halves.
"""

from __future__ import annotations

import pytest
from shapely.geometry import Polygon

from lidar2ha.seams import split_room


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
