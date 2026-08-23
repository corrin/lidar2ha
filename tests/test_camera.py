"""Solving the camera framing.

The code this replaces was a bounding sphere times 1.3. It failed silently in
both directions — wasting half the frame on a long house, and clipping the model
at a wide aspect ratio — because nothing checked whether the building was
actually inside the picture. So these tests check containment against the
frustum definition itself, and check that the answer is the *closest* such
camera, which is what separates a solve from a fudge factor.
"""

from __future__ import annotations

import math

import pytest

from scan2ha.camera import (
    CameraConfig,
    View,
    basis,
    bounds_of,
    contains,
    corners_of,
    frame,
)

WIDE = 16 / 9
TALL = 9 / 16

CUBE = [(0, 0, 0), (400, 400, 400)]
CORRIDOR = [(0, 0, 0), (2000, 150, 250)]        # long and thin
STAIRWELL = [(0, 0, 0), (150, 150, 900)]        # tall and narrow
HOUSE = [(0, 0, 0), (1200, 800, 520)]           # two storeys


def box(low_high):
    return corners_of(tuple(low_high[0]), tuple(low_high[1]))


# --------------------------------------------------------------------------- #
# the basis
# --------------------------------------------------------------------------- #


def test_the_basis_is_orthonormal():
    for yaw in (0, 37, 90, 180, 271):
        for pitch in (0, 30, 50, 90):
            f, r, u = basis(yaw, pitch)
            for v in (f, r, u):
                assert math.isclose(math.dist(v, (0, 0, 0)), 1.0, abs_tol=1e-9)
            assert abs(sum(a * b for a, b in zip(f, r, strict=True))) < 1e-9
            assert abs(sum(a * b for a, b in zip(f, u, strict=True))) < 1e-9
            assert abs(sum(a * b for a, b in zip(r, u, strict=True))) < 1e-9


def test_yaw_zero_faces_increasing_y():
    """The convention that made every render a blank white frame: the camera sat
    on the far side of the plan and yaw=0 pointed it at the sky."""
    f, _, _ = basis(0, 0)
    assert f[1] > 0.99


def test_looking_straight_down_is_not_degenerate():
    """A cross product against world up would collapse at pitch 90."""
    f, r, u = basis(30, 90)
    assert math.isclose(f[2], -1.0, abs_tol=1e-9)
    assert math.isclose(math.dist(r, (0, 0, 0)), 1.0, abs_tol=1e-9)
    assert math.isclose(math.dist(u, (0, 0, 0)), 1.0, abs_tol=1e-9)


# --------------------------------------------------------------------------- #
# the solve
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("shape", [CUBE, CORRIDOR, STAIRWELL, HOUSE])
@pytest.mark.parametrize("aspect", [WIDE, TALL, 1.0])
@pytest.mark.parametrize("pitch", [20, 50, 90])
def test_every_corner_is_inside_the_frame(shape, aspect, pitch):
    corners = box(shape)
    view = frame(corners, pitch=pitch, aspect=aspect)
    for corner in corners:
        assert contains(view, corner, aspect), f"{corner} is outside the frame"


@pytest.mark.parametrize("shape", [CUBE, CORRIDOR, STAIRWELL, HOUSE])
@pytest.mark.parametrize("aspect", [WIDE, TALL])
def test_the_camera_is_as_close_as_it_can_be(shape, aspect):
    """The test a fudge factor cannot pass.

    Move the solved camera 3% closer along its own view direction and at least
    one corner must leave the frame. A bounding sphere with a safety multiplier
    is comfortably further back than this, and would survive the nudge.
    """
    corners = box(shape)
    view = frame(corners, aspect=aspect, margin=0.0)
    f, _, _ = basis(view.yaw, view.pitch)

    target = tuple((min(c[i] for c in corners) + max(c[i] for c in corners)) / 2
                   for i in range(3))
    distance = math.dist((view.x, view.y, view.z), target)
    closer = View(view.name,
                  target[0] - 0.97 * distance * f[0],
                  target[1] - 0.97 * distance * f[1],
                  target[2] - 0.97 * distance * f[2],
                  view.yaw, view.pitch, view.fov)

    assert not all(contains(closer, c, aspect) for c in corners), \
        "the camera was further back than it needed to be"


def test_a_long_thin_house_is_framed_closer_than_a_sphere_would_allow():
    """The concrete win. A bounding sphere sizes the shot by the diagonal, so a
    20 m corridor is framed as if it were 20 m tall as well."""
    corners = box(CORRIDOR)
    view = frame(corners, aspect=WIDE, margin=0.0)

    low, high = bounds_of(corners)
    target = tuple((low[i] + high[i]) / 2 for i in range(3))
    solved = math.dist((view.x, view.y, view.z), target)

    radius = math.dist(low, high) / 2
    sphere = radius / math.sin(math.radians(63) / 2) * 1.3      # the old formula
    assert solved < sphere * 0.9


def test_a_wider_frame_pushes_the_camera_back():
    """Because the field of view is horizontal, a wide frame has a narrow
    vertical angle — which is the axis that was clipping."""
    corners = box(HOUSE)
    low, high = bounds_of(corners)
    target = tuple((low[i] + high[i]) / 2 for i in range(3))

    def distance(aspect):
        v = frame(corners, aspect=aspect)
        return math.dist((v.x, v.y, v.z), target)

    assert distance(21 / 9) > distance(16 / 9) > distance(4 / 3)


def test_margin_moves_the_camera_back_monotonically():
    corners = box(HOUSE)
    low, high = bounds_of(corners)
    target = tuple((low[i] + high[i]) / 2 for i in range(3))

    distances = [math.dist(
        (lambda v: (v.x, v.y, v.z))(frame(corners, margin=m)), target)
        for m in (0.0, 0.05, 0.15)]
    assert distances[0] < distances[1] < distances[2]


def test_with_no_margin_a_corner_sits_on_the_frame_edge():
    corners = box(HOUSE)
    view = frame(corners, margin=0.0, aspect=WIDE)
    # Inside with the tolerance the frustum test allows, but not with any slack.
    assert all(contains(view, c, WIDE) for c in corners)
    assert not all(contains(view, c, WIDE, margin=0.02) for c in corners)


@pytest.mark.parametrize("yaw", [0, 90, 180, 270])
def test_the_frame_is_equally_tight_from_any_side(yaw):
    """A square-plan building should be framed from the same distance whichever
    side you look from; the answer must not depend on the coordinate axes."""
    corners = box([(0, 0, 0), (500, 500, 300)])
    low, high = bounds_of(corners)
    target = tuple((low[i] + high[i]) / 2 for i in range(3))
    view = frame(corners, yaw=yaw)
    assert math.isclose(math.dist((view.x, view.y, view.z), target),
                        math.dist((frame(corners, yaw=0).x,
                                   frame(corners, yaw=0).y,
                                   frame(corners, yaw=0).z), target),
                        rel_tol=1e-9)


def test_a_degenerate_model_still_produces_a_usable_camera():
    """One point, or a model with no extent, must not put the camera on top of
    it or divide by zero."""
    view = frame([(100, 100, 100)])
    assert math.dist((view.x, view.y, view.z), (100, 100, 100)) >= 50


def test_nothing_to_frame_is_an_error_not_a_guess():
    with pytest.raises(ValueError, match="nothing to frame"):
        frame([])


def test_all_eight_corners_are_considered():
    """Checking a subset is checking nothing: a rotated view can put any corner
    at the edge of frame."""
    assert len(corners_of((0, 0, 0), (1, 2, 3))) == 8
    assert len(set(corners_of((0, 0, 0), (1, 2, 3)))) == 8


# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #


def test_the_config_defaults_to_a_whole_house_view():
    config = CameraConfig.from_project({})
    assert config.scope == "house"
    assert config.yaw == 180.0
    assert config.pitch == 50.0


def test_the_config_reads_project_yaml():
    config = CameraConfig.from_project({"camera": {"yaw": 0, "pitch": 90, "scope": "level"}})
    assert (config.yaw, config.pitch, config.scope) == (0.0, 90.0, "level")
