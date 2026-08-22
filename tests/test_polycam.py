"""Reading the Polycam floor plan, and in particular naming rooms.

A room's name is a key, not a caption: `rooms` builds `{r.name: r}` to apply the
HA area mapping, so two rooms sharing a name silently drops one of them, and a
room that never got its label is invisible in the mapping the human writes.
"""

from __future__ import annotations

import math

import pytest

from scan2ha.polycam import (
    assign_room_labels,
    centreline_and_thickness,
    split_into_floors,
    strip_mtext,
)


def room(cx, cy):
    return {"cx": cx, "cy": cy, "points": []}


def label(name, x, y):
    return {"name": name, "x": x, "y": y}


def test_two_rooms_cannot_claim_the_same_label():
    """The reported failure: small rooms off a hallway produced two
    "Bathroom 1" and no Hallway at all."""
    rooms = [room(0, 0), room(1, 0)]
    labels = [label("Bathroom 1", 0.4, 0), label("Hallway", 5, 0)]

    unnamed, unused = assign_room_labels(rooms, labels)

    assert sorted(r["name"] for r in rooms) == ["Bathroom 1", "Hallway"]
    assert (unnamed, unused) == ([], [])


def test_the_closest_pairing_overall_wins_not_the_first_room_to_ask():
    """Independent nearest-neighbour is order-dependent; assignment is not."""
    rooms = [room(0, 0), room(10, 0)]
    labels = [label("A", 1, 0), label("B", 11, 0)]

    assign_room_labels(rooms, labels)
    assert [r["name"] for r in rooms] == ["A", "B"]

    # Same problem, rooms presented in the other order.
    rooms = [room(10, 0), room(0, 0)]
    assign_room_labels(rooms, labels)
    assert [r["name"] for r in rooms] == ["B", "A"]


def test_more_rooms_than_labels_reports_the_unnamed_ones():
    rooms = [room(0, 0), room(1, 0), room(2, 0)]
    unnamed, unused = assign_room_labels(rooms, [label("Only", 0, 0)])

    assert len(unnamed) == 2
    assert unused == []
    assert sum(r["name"] is None for r in rooms) == 2


def test_more_labels_than_rooms_reports_the_unused_ones():
    rooms = [room(0, 0)]
    unnamed, unused = assign_room_labels(rooms, [label("Used", 0, 0), label("Spare", 9, 9)])

    assert unnamed == []
    assert unused == ["Spare"]


def test_no_labels_at_all_leaves_every_room_unnamed():
    """A DXF with no room labels is a real export, not a crash."""
    rooms = [room(0, 0), room(1, 1)]
    unnamed, unused = assign_room_labels(rooms, [])

    assert len(unnamed) == 2
    assert unused == []
    assert all(r["name"] is None for r in rooms)


def test_a_label_is_not_spent_on_a_room_a_floor_away():
    """Floors sit side by side on one sheet, so distance alone keeps labels
    local -- but only because the match is made across all floors at once."""
    ground = [room(0, 0), room(1, 0)]
    upper = [room(100, 0), room(101, 0)]
    labels = [label("Hall", 0.1, 0), label("Kitchen", 1.1, 0),
              label("Landing", 100.1, 0), label("Bedroom", 101.1, 0)]

    assign_room_labels(ground + upper, labels)

    assert [r["name"] for r in ground] == ["Hall", "Kitchen"]
    assert [r["name"] for r in upper] == ["Landing", "Bedroom"]


def test_mtext_formatting_codes_are_stripped():
    assert strip_mtext(r"\A1;Living Room") == "Living Room"
    assert strip_mtext("{Kitchen}") == "Kitchen"


def test_a_wall_outline_reduces_to_its_centreline():
    """Polycam gives a 7-point outline; p0 and p3 are the end-cap midpoints,
    so they ARE the centreline. A bounding box would not work on a diagonal."""
    pts = [(0, 0), (0, 0.05), (4, 0.05), (4, 0), (4, -0.05), (0, -0.05), (0, 0)]
    a, b, thickness = centreline_and_thickness(pts)

    assert a == (0, 0)
    assert b == (4, 0)
    assert thickness == pytest.approx(0.1)


def test_a_diagonal_wall_keeps_its_true_thickness():
    d = 0.05 * math.sqrt(0.5)
    pts = [(0, 0), (-d, d), (3 - d, 3 + d), (3, 3), (3 + d, 3 - d), (d, -d), (0, 0)]
    _a, _b, thickness = centreline_and_thickness(pts)
    assert thickness == pytest.approx(0.1)


def test_floors_split_at_the_largest_gaps():
    items = [{"cx": x} for x in (0, 1, 2, 50, 51, 52)]
    groups = split_into_floors(items, 2)
    assert [len(g) for g in groups] == [3, 3]
