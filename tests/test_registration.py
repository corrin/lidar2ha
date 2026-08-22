"""Fitting a plan onto the mesh, and in particular choosing handedness.

Whether the DXF is mirrored is the most consequential thing the fitter decides:
get it wrong and the plan is a plausible-looking reflection of the house, with
every room on the wrong side. It is also the decision most easily made badly,
because a wall-poor level can fit a mirrored corner almost anywhere.
"""

from __future__ import annotations

import numpy as np
import pytest
from scipy.spatial import cKDTree

from scan2ha.registration import register, transform


def plan_points(step: float = 0.05) -> np.ndarray:
    """An L-shaped wall chain, densely sampled.

    Asymmetric on purpose: a rectangle reads the same mirrored, so it could not
    distinguish handedness at all and would make these tests vacuous.
    """
    corners = [(0, 0), (6, 0), (6, 4), (2, 4), (2, 2), (0, 2), (0, 0)]
    pts = []
    # Deliberately ragged: a chain paired with its own tail.
    for a, b in zip(corners, corners[1:], strict=False):
        length = float(np.hypot(b[0] - a[0], b[1] - a[1]))
        for t in np.linspace(0, 1, max(2, int(length / step))):
            pts.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
    return np.array(pts)


def target_for(plan, theta, tx, ty, mirror, noise=0.01, seed=0):
    """The mesh points a correctly-placed plan would land on."""
    placed = transform(plan, theta, tx, ty, mirror)
    rng = np.random.default_rng(seed)
    return placed + rng.normal(0, noise, placed.shape)


@pytest.mark.parametrize("mirror", [False, True])
def test_recovers_a_known_placement(mirror):
    plan = plan_points()
    target = target_for(plan, 0.65, 12.0, -3.0, mirror)
    fit = register(plan, target, cKDTree(target))

    assert fit["mirror"] is mirror
    assert fit["median_error_m"] < 0.05
    assert fit["coverage"] > 0.9


@pytest.mark.parametrize("mirror", [False, True])
@pytest.mark.parametrize("theta", [0.0, 0.65, 2.4, 5.1])
def test_free_choice_is_never_worse_than_the_better_forced_one(mirror, theta):
    """The property the old implementation violated.

    It kept one global best across both handedness options at coarse
    resolution and refined only that, so a coarse winner that refined badly
    beat a coarse loser that would have refined well -- and the free run could
    come back worse than simply forcing the right answer. Refining each
    handedness before comparing them is what makes this hold.
    """
    plan = plan_points()
    target = target_for(plan, theta, 12.0, -3.0, mirror, noise=0.03, seed=1)
    tree = cKDTree(target)

    free = register(plan, target, tree)
    forced = [register(plan, target, tree, force_mirror=m) for m in (False, True)]
    best_forced = min(f["median_error_m"] for f in forced)

    assert free["median_error_m"] <= best_forced + 1e-9


def test_force_mirror_is_honoured_even_when_it_fits_worse():
    """Once the best-constrained level has chosen, the others must agree --
    a wall-poor level is not allowed to overrule it on score."""
    plan = plan_points()
    target = target_for(plan, 0.65, 12.0, -3.0, mirror=False)
    tree = cKDTree(target)

    wrong = register(plan, target, tree, force_mirror=True)
    assert wrong["mirror"] is True
    assert wrong["median_error_m"] > register(plan, target, tree)["median_error_m"]


def test_a_plan_that_matches_nothing_reports_infinite_error():
    """Better an obvious refusal than a confident transform onto noise."""
    plan = plan_points()
    target = np.array([[500.0, 500.0], [500.5, 500.5], [501.0, 500.0]])
    fit = register(plan, target, cKDTree(target))
    assert not np.isfinite(fit["median_error_m"])
