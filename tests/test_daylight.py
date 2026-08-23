"""Telling a lit fitting from a window by differencing two captures.

The failure this guards is a window rendered as a lamp: it looks fine, it
responds to nothing, and the only way to notice is to click the entity and see
the wrong part of the room change. The second failure is worse and quieter — a
real fitting called a window and dropped, leaving a room dark for a reason
buried three stages upstream.

So the tests here are mostly about the third answer. "The ordinary capture has
nothing at that point" is extremely common, because nobody points a phone at a
dark ceiling, and it must never collapse into either verdict.
"""

from __future__ import annotations

import numpy as np

from lidar2ha.daylight import WINDOW_LUMA, build_reference, summarise, verdict_at

WHITE = [255, 255, 255]
BLACK = [8, 8, 8]


def reference(points, colours):
    return build_reference(np.array(points, dtype=float), np.array(colours))


def test_bright_in_the_ordinary_capture_too_is_a_window():
    """Sunlight is there whether or not the lights are on."""
    ref = reference([[0, 0, 2.4], [0.05, 0, 2.4], [0.1, 0, 2.4]], [WHITE] * 3)
    verdict, luma, faces = verdict_at(ref, [0, 0, 2.4])
    assert verdict == "window"
    assert luma >= WINDOW_LUMA
    assert faces == 3


def test_dark_in_the_ordinary_capture_is_a_fitting():
    """It was bright in the fixture pass and dark here, so it was the bulb."""
    ref = reference([[0, 0, 2.4], [0.05, 0, 2.4], [0.1, 0, 2.4]], [BLACK] * 3)
    verdict, luma, _faces = verdict_at(ref, [0, 0, 2.4])
    assert verdict == "fitting"
    assert luma < WINDOW_LUMA


def test_nothing_nearby_is_unseen_rather_than_a_verdict():
    """The ordinary capture photographs ceilings badly — the camera meters for
    the room. "Never looked" is not evidence, and folding it into either answer
    would either invent windows or place them."""
    ref = reference([[10, 10, 2.4]], [WHITE])
    verdict, luma, faces = verdict_at(ref, [0, 0, 2.4])
    assert verdict == "unseen"
    assert luma is None
    assert faces == 0


def test_the_radius_is_what_decides_whether_anything_was_seen():
    ref = reference([[0.5, 0, 2.4]], [WHITE])
    assert verdict_at(ref, [0, 0, 2.4], radius_m=0.30)[0] == "unseen"
    assert verdict_at(ref, [0, 0, 2.4], radius_m=0.60)[0] == "window"


def test_one_bright_face_among_many_dark_ones_does_not_convict():
    """A p90, not a maximum. Specular glints and atlas seams put single bright
    faces on ordinary ceilings, and a maximum would call every fitting glass."""
    points = [[i * 0.01, 0, 2.4] for i in range(20)]
    ref = reference(points, [WHITE] + [BLACK] * 19)
    assert verdict_at(ref, [0.1, 0, 2.4])[0] == "fitting"


def test_a_mostly_bright_region_does_convict():
    points = [[i * 0.01, 0, 2.4] for i in range(20)]
    ref = reference(points, [WHITE] * 18 + [BLACK] * 2)
    assert verdict_at(ref, [0.1, 0, 2.4])[0] == "window"


def test_the_cutoff_is_adjustable_because_it_is_a_guess():
    """Every constant here is tuned to one house; the cutoff has to be readable
    against a real contact sheet rather than fixed in the abstract."""
    ref = reference([[0, 0, 2.4]], [[120, 120, 120]])
    assert verdict_at(ref, [0, 0, 2.4], cutoff=200.0)[0] == "fitting"
    assert verdict_at(ref, [0, 0, 2.4], cutoff=100.0)[0] == "window"


def test_the_summary_names_every_verdict_even_at_zero():
    """A missing key reads as "none of those" when it usually means nobody
    asked, and `unseen: 0` is itself worth seeing."""
    assert summarise(["window", "window", "fitting"]) == {
        "fitting": 1, "window": 2, "unseen": 0}
    assert summarise([]) == {"fitting": 0, "window": 0, "unseen": 0}
