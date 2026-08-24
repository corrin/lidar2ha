"""Shared fixtures.

The Java tests need a real Sweet Home 3D and a real JDK, which most machines
running the unit tests will not have. They skip rather than fail there -- but
they must actually run on a developer's machine, because they are the only
thing that can catch a mistake in code that only Sweet Home 3D's own classes
can execute.
"""

from __future__ import annotations

import numpy as np
import pytest

from lidar2ha import javabridge
from lidar2ha.javabridge import ToolchainError
from lidar2ha.thresholds import FloorSample

WOOD = (150, 110, 70)
CARPET = (60, 70, 110)


def synthetic_floor(*, step_at=None, colour_change=False, span=4.0,
                    spacing=0.05, feature_at=2.0) -> FloorSample:
    """A flat floor in mesh metres, optionally with a feature across it.

    Shared by the tests of the measurement itself and of the stage that consumes
    it, so that a change to what a corroborated boundary looks like cannot leave
    one of the two agreeing with an older idea of it.

    Sampled at 5 cm -- the same order as `registration.sample_along_walls`, and
    dense enough that a 20 cm band holds far more than the 40 faces a band needs
    before it is believed.
    """
    grid = np.arange(0, span, spacing)
    xs, ys = np.meshgrid(grid, grid)
    xs, ys = xs.ravel(), ys.ravel()
    zs = np.zeros_like(xs)
    cols = np.tile(np.array(WOOD, dtype=float), (len(xs), 1))

    beyond = ys > feature_at
    if step_at is not None:
        zs = np.where(beyond, step_at, 0.0)
    if colour_change:
        cols[beyond] = CARPET

    return FloorSample(np.column_stack([xs, ys, zs]), cols)


@pytest.fixture(scope="session")
def toolchain():
    """A detected toolchain, or skip the test."""
    try:
        return javabridge.detect()
    except ToolchainError as exc:
        pytest.skip(f"no Sweet Home 3D toolchain: {exc}")


@pytest.fixture(scope="session")
def java_classes(toolchain):
    """Our Java, compiled against the local install. Cached across the session."""
    try:
        return javabridge.compile_java(toolchain)
    except ToolchainError as exc:
        pytest.skip(f"Java would not compile: {exc}")
