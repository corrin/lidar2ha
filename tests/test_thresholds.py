"""Asking the mesh whether a declared room boundary is really there.

An open-plan boundary is often a matter of use — the table end against the sofa
end — and the floor under it does not change at all. So this measurement can
only ever corroborate a declaration, never authorise one, and the interesting
property is that it distinguishes "measured, and the floor is continuous" from
"never photographed that strip". Folding those together would invent a verdict
about a boundary nobody looked at.
"""

from __future__ import annotations

import numpy as np

from conftest import synthetic_floor as floor
from lidar2ha.thresholds import FloorSample, boundary_support

# The boundary under test runs along y = 2, from x = 1 to x = 3.
A, B = (1.0, 2.0), (3.0, 2.0)


def test_a_step_under_the_declared_line_corroborates_it():
    support = boundary_support(A, B, floor(step_at=0.19))
    assert support is not None
    assert support.corroborated
    assert support.step_cm > 15


def test_a_flooring_change_alone_corroborates_it():
    """No step at all — wood giving way to carpet is a boundary by itself."""
    support = boundary_support(A, B, floor(colour_change=True))
    assert support is not None
    assert support.corroborated
    assert support.step_cm < 1


def test_an_unbroken_floor_reads_as_measured_and_unsupported():
    """The sofa-end boundary: really there, and the mesh really cannot see it.

    This must come back as a Support that is not corroborated, NOT as None —
    the difference is whether the report can say "we looked".
    """
    support = boundary_support(A, B, floor())
    assert support is not None
    assert not support.corroborated


def test_floor_nobody_photographed_is_not_looked_at():
    """Distinct from unsupported, and the whole reason this returns None.

    Reporting "no evidence" for a strip the capture never walked would be a
    verdict invented out of an absence.
    """
    empty = FloorSample(np.zeros((0, 3)), np.zeros((0, 3)))
    assert boundary_support(A, B, empty) is None

    # Sparse enough that no band reaches the sample floor: still not a verdict.
    assert boundary_support(A, B, floor(spacing=0.9)) is None


def test_the_offset_says_how_far_the_real_edge_sits_from_the_declared_one():
    """A trace half a metre out is corroborated by the wrong edge otherwise."""
    shifted = boundary_support((1.0, 1.6), (3.0, 1.6), floor(step_at=0.19))
    assert shifted is not None
    assert shifted.corroborated
    # The step is at y = 2.0 and the line was declared at y = 1.6.
    assert 30 < shifted.offset_cm < 50


def test_a_degenerate_boundary_is_not_measured():
    assert boundary_support(A, A, floor(step_at=0.19)) is None
