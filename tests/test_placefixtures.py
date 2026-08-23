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
from shapely.geometry import Polygon

from lidar2ha.contactsheet import order_for_review, pair_crops, shorten
from lidar2ha.placefixtures import (
    M_TO_CM,
    assign,
    mesh_to_plan_cm,
    plan_cm_to_mesh_m,
)
from lidar2ha.registration import transform
from lidar2ha.schema import Level, Registration, Room


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


@pytest.mark.parametrize("mirror", [False, True])
@pytest.mark.parametrize("theta_deg", [0.0, 37.0, 216.5])
def test_plan_to_mesh_is_the_exact_inverse_of_mesh_to_plan(mirror, theta_deg):
    """Differencing against an ordinary capture needs the hop back INTO its
    mesh, and a sign error there samples the atlas somewhere else entirely —
    which reports a fitting as a window, or the reverse, with a number next to
    it that looks like a measurement."""
    reg = registration(theta_deg, 12.5, -3.25, mirror)
    assert np.allclose(
        mesh_to_plan_cm(plan_cm_to_mesh_m(PLAN_CM, reg), reg), PLAN_CM, atol=1e-6)


# --------------------------------------------------------------------------- #
# one fixture pass across several geometry captures
# --------------------------------------------------------------------------- #

SQUARE = [(0.0, 0.0), (400.0, 0.0), (400.0, 400.0), (0.0, 400.0)]


def capture(area: str, offset: float = 0.0) -> list[tuple]:
    """One capture's room set, in its own plan frame."""
    room = Room(name=area, ha_area=area,
                points=[(x + offset, y) for x, y in SQUARE])
    level = Level(name="Ground", ceiling_height_cm=250.0, rooms=[room])
    return [(level, room, Polygon(room.points))]


def test_a_fitting_goes_to_the_capture_that_actually_contains_it():
    """A fixture pass is walked in one go and does not stop at the boundary
    between two geometry captures. Requiring one model per pass means splitting
    the fittings by hand along a line that exists only in the scanning history.
    """
    kitchen, hall = capture("kitchen"), capture("hall")
    # In the kitchen capture's frame the fitting is far outside; in the hall's
    # it is in the middle of the room. Each capture has its own frame, so the
    # two coordinates are different numbers for the same physical point.
    which, room, pair = assign([np.array([9000.0, 9000.0]), np.array([200.0, 200.0])],
                               [kitchen, hall])
    assert (which, room) == (1, "hall")
    assert pair[1].ha_area == "hall"


def test_containment_beats_a_near_miss_in_an_earlier_capture():
    """Checking each capture in turn would let a fitting that merely sits near
    a wall in the first one claim it, and never consult the capture the fitting
    is genuinely inside — so the answer would depend on the order the models
    were listed on the command line."""
    kitchen, hall = capture("kitchen"), capture("hall")
    just_outside = np.array([-30.0, 200.0])      # 30 cm past the kitchen wall
    inside_hall = np.array([200.0, 200.0])

    which, room, _pair = assign([just_outside, inside_hall], [kitchen, hall])
    assert (which, room) == (1, "hall")


def test_a_near_miss_is_flagged_rather_than_discarded():
    """Two registrations compose their errors, so just over a wall line is
    common and the fitting is still useful."""
    which, room, _pair = assign([np.array([-30.0, 200.0])], [capture("kitchen")])
    assert which == 0
    assert room.startswith("~kitchen")


def test_a_fitting_in_no_capture_at_all_belongs_to_none_of_them():
    """OUTSIDE has to stay distinguishable from a room: `lights` drops it, and
    a near-miss label would place it in a room it is nowhere near."""
    which, room, pair = assign([np.array([9000.0, 9000.0])], [capture("kitchen")])
    assert (which, room, pair) == (None, "OUTSIDE", None)


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


# --------------------------------------------------------------------------- #
# the review sheet's ordering, and the pairing that ordering would have broken
# --------------------------------------------------------------------------- #


def record(crop: str, room: str, verdict: str | None = None) -> dict:
    out = {"crop": crop, "room": room}
    if verdict is not None:
        out["verdict"] = verdict
    return out


def crops_dir(tmp_path, names):
    from PIL import Image
    for name in names:
        Image.new("RGB", (8, 8)).save(tmp_path / name)
    return tmp_path


def test_windows_are_demoted_to_the_end_and_fittings_keep_their_order():
    """Review runs top-down and can stop when the cells stop looking like
    lamps. Within a group, detection order — brightest and biggest first — is
    itself a confidence ordering and has to survive."""
    placed = [record("00.png", "a", "window"), record("01.png", "b", "fitting"),
              record("02.png", "c", "unseen"), record("03.png", "d", "fitting")]
    assert [r["crop"] for r in order_for_review(placed)] == [
        "01.png", "03.png", "02.png", "00.png"]


def test_a_sheet_with_no_verdicts_keeps_exactly_the_order_it_had():
    """A run without --daylight-mesh must behave as it always did."""
    placed = [record(f"{i:02d}.png", "a") for i in range(4)]
    assert order_for_review(placed) == placed


def test_crops_follow_their_record_through_a_reorder(tmp_path):
    """The regression the sort would otherwise have introduced. Pairing crops
    with records by position puts the right pictures under the wrong rooms and
    looks entirely normal — every cell is a real crop and every label is a real
    room, just not each other's."""
    crops = crops_dir(tmp_path, ["00.png", "01.png"])
    placed = [record("00.png", "kitchen", "window"), record("01.png", "den", "fitting")]

    pairs, problems = pair_crops(crops, order_for_review(placed))
    assert [(r["room"], p.name) for r, p in pairs] == [("den", "01.png"),
                                                       ("kitchen", "00.png")]
    assert problems == []


def test_a_record_whose_crop_is_missing_is_reported_not_skipped(tmp_path):
    """One cell fewer looks completely normal, and the missing one is exactly
    the candidate nobody then reviews."""
    crops = crops_dir(tmp_path, ["00.png"])
    pairs, problems = pair_crops(crops, [record("00.png", "den"),
                                         record("07.png", "kitchen")])
    assert len(pairs) == 1
    assert any("07.png" in p for p in problems)


def test_a_crop_with_no_record_is_reported_too(tmp_path):
    crops = crops_dir(tmp_path, ["00.png", "01.png"])
    _pairs, problems = pair_crops(crops, [record("00.png", "den")])
    assert any("01.png" in p for p in problems)


def test_an_old_fixtures_file_still_pairs_by_position_and_says_so(tmp_path):
    """Records written before crops were named have to keep working; the point
    is that the fragile behaviour announces itself instead of being the
    default."""
    crops = crops_dir(tmp_path, ["00.png", "01.png"])
    pairs, problems = pair_crops(crops, [{"room": "den"}, {"room": "kitchen"}])
    assert len(pairs) == 2
    assert any("by position" in p for p in problems)
