"""Finding light fittings, and not counting one of them four times.

Fragmentation is a review-cost bug rather than a correctness one, which is why
it survived: every fragment is a real bright thing in a real place, and the
model built from them is fine. What it does is turn twelve fittings into
thirty-eight cells on a contact sheet, and the sheet is the step a human has to
finish. Detection that nobody reviews places nothing.
"""

from __future__ import annotations

import numpy as np

from lidar2ha.fixtures import MAX_EXTENT_M, extent_of, merge_fragments


def line(start: float, count: int = 4, step: float = 0.02) -> np.ndarray:
    """`count` points along x from `start`, tight enough to be one cluster."""
    return np.array([[start + i * step, 0.0, 2.4] for i in range(count)])


def selections_of(*groups: np.ndarray) -> tuple[list[np.ndarray], np.ndarray]:
    centers = np.vstack(groups)
    sels, offset = [], 0
    for group in groups:
        sels.append(np.arange(offset, offset + len(group)))
        offset += len(group)
    return sels, centers


def test_two_pieces_of_one_fitting_are_rejoined():
    """A downlight ring or a shade with a dark band comes back in pieces, and
    each piece then costs a cell on the sheet."""
    sels, centers = selections_of(line(0.0), line(0.25))
    merged, count = merge_fragments(sels, centers)

    assert count == 1
    assert [len(s) for s in merged] == [8]


def test_two_separate_downlights_are_left_alone():
    """The reason this is a post-merge rather than a bigger CLUSTER_M: adjacent
    downlights really are 30-40 cm apart, and merging them would lose a fitting
    rather than merely inflate a count."""
    sels, centers = selections_of(line(0.0), line(0.9))
    merged, count = merge_fragments(sels, centers)

    assert count == 0
    assert [len(s) for s in merged] == [4, 4]


def test_a_merge_that_would_break_compactness_is_refused():
    """The cap is what stops a chain of merges walking along a lit wall and
    ending as one enormous "fitting" spanning the room.

    Two parallel strips, each just compact enough to have survived the single
    cluster filter, whose centres are well within the merge radius. Their union
    is not compact, so the merge has to be refused even though everything about
    the pair says join.
    """
    long_a = np.array([[x, 0.0, 2.4] for x in np.linspace(0.0, 1.2, 12)])
    long_b = np.array([[x, 0.3, 2.4] for x in np.linspace(0.0, 1.2, 12)])
    sels, centers = selections_of(long_a, long_b)

    assert extent_of(long_a) <= MAX_EXTENT_M, "each must pass the single-cluster filter"
    assert extent_of(np.vstack([long_a, long_b])) > MAX_EXTENT_M
    merged, count = merge_fragments(sels, centers)
    assert count == 0
    assert len(merged) == 2


def test_a_chain_of_fragments_collapses_to_one():
    """Closest pair first, repeated. Three pieces of one fitting must end as
    one candidate, not two."""
    sels, centers = selections_of(line(0.0), line(0.2), line(0.4))
    merged, count = merge_fragments(sels, centers)

    assert count == 2
    assert [len(s) for s in merged] == [12]


def test_merging_never_loses_a_face():
    """Every bright face has to end up in exactly one candidate; a face dropped
    here is a fitting that quietly shrinks or disappears."""
    sels, centers = selections_of(line(0.0), line(0.25), line(2.0))
    merged, _count = merge_fragments(sels, centers)

    assert sorted(int(i) for s in merged for i in s) == list(range(len(centers)))


def test_a_single_candidate_survives_untouched():
    sels, centers = selections_of(line(0.0))
    merged, count = merge_fragments(sels, centers)
    assert count == 0
    assert np.array_equal(merged[0], sels[0])
