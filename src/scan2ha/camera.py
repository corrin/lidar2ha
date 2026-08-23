#!/usr/bin/env python3
"""Work out where to stand so the whole house is in shot.

Framing a model is pure geometry with an exact answer, and the code this
replaces did not compute it: it took a sphere around the building and multiplied
by 1.3. That is roughly right for a roughly cubic house and wrong for everything
else -- a long thin house wastes most of the frame, and at a wide aspect ratio
the model is clipped anyway. Measured on a two-level example at 480x270, content
occupied 252 of 480 columns and all 270 rows: half the width thrown away while
the height overflowed.

The exact version: take the eight corners of the bounding box, and for each one
ask how far back the camera must be for it to fall inside the frustum. The
answer is the largest of those distances. No fudge factor, and nothing to tune.

TWO CONVENTIONS, BOTH ESTABLISHED BY EXPERIMENT rather than from documentation,
because both are easy to get backwards and neither fails loudly:

* Sweet Home 3D's field of view is HORIZONTAL. Rendering one camera at 480x270
  and again at 270x480, the portrait frame shows more vertically while the
  landscape frame fills its height completely. A vertical field of view would
  have covered the same fraction of height in both. So the vertical half-angle
  is derived from the horizontal one and the aspect ratio, and on a wide frame
  it is the vertical axis that binds.

* Yaw looks along (sin yaw, cos yaw). yaw=0 faces increasing y, which is why an
  earlier camera placed on the far side of the plan and left at yaw=0 rendered a
  uniform white frame -- it was pointed at the sky.

Everything here works in the scene file's coordinate frame, which is Sweet Home
3D's: y already flipped and re-origined. Solving in the model's own frame and
transforming afterwards would be wrong, because the flip reverses handedness and
the yaw with it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# Sweet Home 3D's own default, and what getFieldOfView() returns on a new Home.
DEFAULT_FOV_DEG = 63.0
# Looking from the far side of the plan, toward decreasing y.
DEFAULT_YAW_DEG = 180.0
# Steep enough to see into rooms, shallow enough to read as a 3D view. Near
# walls do occlude far rooms at this angle; raise it toward 90 for a plan view.
DEFAULT_PITCH_DEG = 50.0
# Fraction of the frame left as a border, so the building does not touch the edge.
DEFAULT_MARGIN = 0.06
MIN_DISTANCE_CM = 50.0

Vec3 = tuple[float, float, float]


@dataclass(frozen=True)
class View:
    """A solved camera, in scene-file centimetres and degrees."""

    name: str
    x: float
    y: float
    z: float
    yaw: float
    pitch: float
    fov: float


@dataclass(frozen=True)
class CameraConfig:
    """The viewing choices, which belong to the user rather than the tool.

    `fov` and `margin` are deliberately not here. The field of view IS the
    frustum being solved against, and the margin exists so the model does not
    touch the frame edge -- neither is a matter of taste.
    """

    yaw: float = DEFAULT_YAW_DEG
    pitch: float = DEFAULT_PITCH_DEG
    # "house" frames the whole building in one shot; "level" frames each storey
    # separately. House is the default and usually right: the plugin renders
    # every light into one image, and cross-floor spill is the reason this
    # pipeline uses a raytracer at all.
    scope: str = "house"

    @classmethod
    def from_project(cls, project: dict) -> CameraConfig:
        section = (project or {}).get("camera") or {}
        return cls(
            yaw=float(section.get("yaw", DEFAULT_YAW_DEG)),
            pitch=float(section.get("pitch", DEFAULT_PITCH_DEG)),
            scope=str(section.get("scope", "house")),
        )


def basis(yaw_deg: float, pitch_deg: float) -> tuple[Vec3, Vec3, Vec3]:
    """(forward, right, up) unit vectors for a camera at this orientation.

    Written as closed forms rather than cross products so that looking straight
    down still works: at pitch 90 the forward vector is parallel to world up and
    a cross product would be degenerate.
    """
    yaw, pitch = math.radians(yaw_deg), math.radians(pitch_deg)
    sy, cy = math.sin(yaw), math.cos(yaw)
    sp, cp = math.sin(pitch), math.cos(pitch)

    forward = (sy * cp, cy * cp, -sp)
    right = (cy, -sy, 0.0)
    up = (sy * sp, cy * sp, cp)
    return forward, right, up


def _dot(a: Vec3, b: Vec3) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def bounds_of(points) -> tuple[Vec3, Vec3]:
    """Axis-aligned bounds of an iterable of (x, y, z)."""
    pts = list(points)
    if not pts:
        raise ValueError("nothing to frame")
    xs, ys, zs = zip(*pts, strict=True)
    return (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))


def corners_of(low: Vec3, high: Vec3) -> list[Vec3]:
    """The eight corners of a box. All of them: a rotated view can put any one
    of them at the edge of frame, so checking a subset is checking nothing."""
    return [(x, y, z) for x in (low[0], high[0])
            for y in (low[1], high[1])
            for z in (low[2], high[2])]


def frame(
    points,
    *,
    name: str = "view",
    yaw: float = DEFAULT_YAW_DEG,
    pitch: float = DEFAULT_PITCH_DEG,
    fov: float = DEFAULT_FOV_DEG,
    aspect: float = 16 / 9,
    margin: float = DEFAULT_MARGIN,
) -> View:
    """The closest camera at this orientation that still contains `points`.

    `aspect` is width / height of the render. It matters: the field of view is
    horizontal, so a wide frame has a narrow vertical angle and the vertical
    axis is what limits how close the camera can be.

    The solve. With the camera at `P = T - D*f` and a corner offset `v = c - T`,
    that corner's depth in front of the camera is `v.f + D`, so it is inside the
    frustum when `|v.r| <= (v.f + D) * th` and `|v.u| <= (v.f + D) * tv`.
    Rearranged, each corner and each axis gives a lower bound on D, and the
    answer is the largest of the sixteen.
    """
    low, high = bounds_of(points)
    target = tuple((low[i] + high[i]) / 2 for i in range(3))

    forward, right, up = basis(yaw, pitch)
    th = math.tan(math.radians(fov) / 2) * (1.0 - margin)
    tv = th / aspect
    if th <= 0 or tv <= 0:
        raise ValueError(f"field of view {fov} and margin {margin} leave no frame")

    distance = MIN_DISTANCE_CM
    for corner in corners_of(low, high):
        v = (corner[0] - target[0], corner[1] - target[1], corner[2] - target[2])
        along = _dot(v, forward)
        distance = max(distance,
                       abs(_dot(v, right)) / th - along,
                       abs(_dot(v, up)) / tv - along)

    return View(
        name=name,
        x=target[0] - distance * forward[0],
        y=target[1] - distance * forward[1],
        z=target[2] - distance * forward[2],
        yaw=yaw,
        pitch=pitch,
        fov=fov,
    )


def contains(view: View, point: Vec3, aspect: float, margin: float = 0.0) -> bool:
    """Whether a point falls inside this camera's frustum.

    Exists so the solver can be checked against its own definition rather than
    against a restatement of its algebra -- and so a test can assert the answer
    is *minimal* by moving the camera closer and watching a corner leave.
    """
    forward, right, up = basis(view.yaw, view.pitch)
    v = (point[0] - view.x, point[1] - view.y, point[2] - view.z)
    depth = _dot(v, forward)
    if depth <= 0:
        return False
    th = math.tan(math.radians(view.fov) / 2) * (1.0 - margin)
    tv = th / aspect
    return abs(_dot(v, right)) <= depth * th + 1e-9 and \
        abs(_dot(v, up)) <= depth * tv + 1e-9
