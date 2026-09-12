"""Polycam-shaped DXF fixtures, re-exported from the package.

These moved to `lidar2ha.demo` when `lidar2ha demo` was built on them: the
generator has to ship in the wheel for a reader with no repo to run the
tutorial, and two copies of a fixture is how one of them goes quietly stale.

Kept as a module because the tests import it by this name, and because where a
test fixture lives is not what any of those tests are about.
"""

from lidar2ha.demo import (
    Sheet,
    box,
    labelled_floor_with_no_rooms,
    one_storey,
    three_storeys_on_one_cluster,
    wall_outline,
)

__all__ = [
    "Sheet",
    "box",
    "labelled_floor_with_no_rooms",
    "one_storey",
    "three_storeys_on_one_cluster",
    "wall_outline",
]
