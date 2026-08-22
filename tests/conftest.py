"""Shared fixtures.

The Java tests need a real Sweet Home 3D and a real JDK, which most machines
running the unit tests will not have. They skip rather than fail there -- but
they must actually run on a developer's machine, because they are the only
thing that can catch a mistake in code that only Sweet Home 3D's own classes
can execute.
"""

from __future__ import annotations

import pytest

from scan2ha import javabridge
from scan2ha.javabridge import ToolchainError


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
