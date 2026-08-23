"""Getting a fixture capture's coordinates into the geometry capture's frame.

Every hop here is a rigid transform, and a sign error in any of them produces
coordinates that are perfectly plausible and in the wrong room — a mirrored
plan is still a plan. So the inverse is tested against the forward transform
the rest of the pipeline actually uses, rather than against itself.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from lidar2ha.contactsheet import shorten
from lidar2ha.placefixtures import M_TO_CM, mesh_to_plan_cm
from lidar2ha.registration import transform
from lidar2ha.schema import Registration


def registration(theta_deg=0.0, tx_m=0.0, ty_m=0.0, mirror=False) -> Registration:
    return Registration(theta_deg=theta_deg, tx_m=tx_m, ty_m=ty_m, mirror=mirror,
                        median_error_m=0.02, coverage=1.0, floor_z_m=0.0)


PLAN_CM = np.array([[0.0, 0.0], [400.0, 0.0], [400.0, 300.0], [-120.0, 55.5]])


@pytest.mark.parametrize("mirror", [False, True])
@pytest.mark.parametrize("theta_deg", [0.0, 37.0, 90.0, 216.5, 359.0])
@pytest.mark.parametrize("tx_m,ty_m", [(0.0, 0.0), (12.5, -3.25)])
def test_the_inverse_undoes_registrations_own_forward_transform(
        mirror, theta_deg, tx_m, ty_m):
    """The property that matters: whatever `registration` did, this undoes.

    Testing the inverse against its own algebra would pass with the mirror
    applied at the wrong end, which is the mistake worth catching — the result
    is a reflected plan, and every room lands in its opposite number.
    """
    reg = registration(theta_deg, tx_m, ty_m, mirror)

    forward_m = transform(PLAN_CM / M_TO_CM, math.radians(theta_deg), tx_m, ty_m, mirror)
    back_cm = mesh_to_plan_cm(forward_m, reg)

    assert np.allclose(back_cm, PLAN_CM, atol=1e-6)


def test_a_mirrored_fit_is_not_silently_treated_as_unmirrored():
    """The two differ by a reflection, so a wrong answer is still a valid plan."""
    reg_plain = registration(30.0, 1.0, 2.0, mirror=False)
    reg_mirror = registration(30.0, 1.0, 2.0, mirror=True)
    mesh = transform(PLAN_CM / M_TO_CM, math.radians(30.0), 1.0, 2.0, True)

    assert np.allclose(mesh_to_plan_cm(mesh, reg_mirror), PLAN_CM, atol=1e-6)
    assert not np.allclose(mesh_to_plan_cm(mesh, reg_plain), PLAN_CM, atol=1.0)


def test_the_result_is_centimetres_not_metres():
    """Everything downstream is plan centimetres; a metres answer would place
    every fitting within a metre of the origin and inside no room at all."""
    reg = registration()
    out = mesh_to_plan_cm(np.array([[1.0, 2.0]]), reg)
    assert np.allclose(out, [[100.0, 200.0]])


def test_the_input_array_is_not_modified_in_place():
    """placefixtures reuses the detected positions after this call."""
    reg = registration(mirror=True)
    mesh = np.array([[1.0, 2.0], [3.0, 4.0]])
    before = mesh.copy()
    mesh_to_plan_cm(mesh, reg)
    assert np.array_equal(mesh, before)


# --------------------------------------------------------------------------- #
# the review sheet's labels
# --------------------------------------------------------------------------- #


def test_a_short_area_id_is_left_alone():
    assert shorten("kitchen") == "kitchen"


def test_a_long_area_id_keeps_both_ends():
    """Truncating the tail would render two areas identical on the sheet, which
    is precisely the confusion the label exists to prevent."""
    a = shorten("upstairs_north_bathroom", width=16)
    b = shorten("upstairs_north_bedroom", width=16)
    assert a != b
    assert len(a) <= 16 and len(b) <= 16
