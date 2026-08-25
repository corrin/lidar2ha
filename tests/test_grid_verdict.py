"""Whether the wall grid can rule on a rotation, and when it must abstain.

The grid is the one channel an error figure cannot provide: a capture placed on
the wrong walls entirely reads a plausible median, because every point does find
a nearby point. So it is a gate rather than a report.

But it is computed from a MEAN over wall directions, and a mean of directions
that agree on nothing still returns an angle. Treating that angle as a fact
turned a capture overlaying at 3.0 cm across 100% of its walls into a
"wrong basin" refusal, on a phantom 4 degrees that came out of scatter.
"""

from __future__ import annotations

import math

from lidar2ha.combine import add_caution, grid_verdict, joined_cautions
from lidar2ha.registration import grid_concentration
from lidar2ha.schema import Level, Wall


def wall(x1: float, y1: float, x2: float, y2: float) -> Wall:
    return Wall(x_start=x1, y_start=y1, x_end=x2, y_end=y2,
                thickness=10.0, height=250.0)


def rectilinear(name: str = "L") -> Level:
    """A plain square room: four walls, two directions, one unmistakable grid."""
    return Level(name=name, ceiling_height_cm=250, walls=[
        wall(0, 0, 400, 0), wall(400, 0, 400, 300),
        wall(400, 300, 0, 300), wall(0, 300, 0, 0)])


def scattered(name: str = "S") -> Level:
    """The same four corners walked at angles that agree on nothing.

    Deliberately avoids multiples of 45: at 45 degrees a wall lands back on a
    grid rotated a half-quarter, which is coherent again at four times the
    angle and would give this level a perfectly good bearing.
    """
    angles = (0.0, 23.0, 51.0, 79.0, 12.0, 66.0)
    walls = []
    for i, deg in enumerate(angles):
        a = math.radians(deg)
        x, y = i * 500.0, 0.0
        walls.append(wall(x, y, x + 300 * math.cos(a), y + 300 * math.sin(a)))
    return Level(name=name, ceiling_height_cm=250, walls=walls)


def test_a_square_plan_states_its_grid_unambiguously():
    """The number the gate leans on. If a plain rectangle does not score near 1
    the measure is not reading a grid at all."""
    assert grid_concentration(rectilinear().walls) > 0.99


def test_walls_that_agree_on_nothing_say_so():
    """A bearing is still returned for these -- that is the trap. The
    concentration is the only thing that reports the difference."""
    assert grid_concentration(scattered().walls) < 0.5


def test_a_rotation_on_the_grid_is_admitted():
    """The ordinary case, and the guard for the two below: if a quarter turn
    between two square plans did not read as on-grid, nothing here would."""
    check = grid_verdict(90.0, rectilinear(), rectilinear())
    assert check.verdict == "on_grid"
    assert check.off_deg is not None and check.off_deg < 1e-6


def test_a_rotation_off_the_grid_is_refused_when_the_grid_is_readable():
    """The gate has to keep working. Both plans here are unmistakably square, so
    30 degrees really is inadmissible and there is nothing to abstain about."""
    check = grid_verdict(30.0, rectilinear(), rectilinear())
    assert check.verdict == "off_grid"
    assert check.off_deg is not None and check.off_deg > 5.0


def test_an_unreadable_grid_abstains_rather_than_refusing():
    """The failure this exists to end.

    Same 30 degree rotation, same verdict from `off_grid_deg` -- and it must
    NOT be a refusal, because the bearing it is measured against came out of
    walls pointing six different ways. Measured on the real capture this was
    written for: concentration 0.341 where every other capture of its storey
    scored 0.59 to 1.00, giving a phantom 4 degree bearing and a ~5 degree
    reading against all of them.
    """
    check = grid_verdict(30.0, scattered(), rectilinear())
    assert check.verdict == "no_grid"
    assert check.concentration < 0.5
    # The angle is still reported. Abstaining is not the same as having nothing
    # to say, and a human reading the report needs the number that prompted it.
    assert check.off_deg is not None


def test_abstention_is_not_a_pass():
    """`no_grid` says this channel could not rule, so the caller must not read
    it as corroboration. The distinct third name is the whole mechanism -- were
    it folded into `on_grid`, an uncheckable capture would be indistinguishable
    from a checked one."""
    assert grid_verdict(30.0, scattered(), rectilinear()).verdict != "on_grid"


def test_a_capture_with_no_walls_has_no_grid_to_be_off():
    """None from `off_grid_deg`, and not zero. Scoring a capture with nothing to
    take a bearing from as perfectly aligned would let it through on the
    strength of having no evidence at all."""
    empty = Level(name="E", ceiling_height_cm=250)
    check = grid_verdict(45.0, empty, rectilinear())
    assert check.verdict == "no_grid"
    assert check.off_deg is None


def test_a_capture_earning_two_cautions_keeps_both():
    """Cautions were assigned into a dict, not accumulated, so a capture that
    earned two kept whichever fired last.

    They are not alternatives. "its bearing is unreadable" and "it is too small
    for its coverage to mean anything" are different reasons to go and look at
    the house, and the overwritten one is the one nobody ever saw -- which is
    the single thing this stage may not do.
    """
    store: dict[str, list[str]] = {}
    add_caution(store, "scan", "sits 5.2 deg off the wall grid, and IS NOT REFUSED")
    add_caution(store, "scan", "spans 12% of the reference's footprint")
    add_caution(store, "other", "lands at 5.2 cm on the anchor")

    assert len(store["scan"]) == 2
    line = joined_cautions(store)["scan"]
    assert "off the wall grid" in line and "footprint" in line
    assert joined_cautions(store)["other"] == "lands at 5.2 cm on the anchor"
