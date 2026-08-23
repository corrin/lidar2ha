#!/usr/bin/env python3
"""Simplify a scanned wall chain down to the room a human pictures.

A LiDAR scan traces everything it sees: stair treads become diagonal wall
segments, a bay becomes five short walls, noise becomes geometry. That is more
detail than a room-level model wants -- and for a Home Assistant floorplan the
useful abstraction is the room as the occupant thinks of it, not as the sensor
measured it.

Two passes:

1. **Douglas-Peucker** on the ordered perimeter, which drops vertices that sit
   close to the line between their neighbours. This is what collapses a flight
   of stairs into the single wall a person would draw.
2. **Axis snapping** (optional), rotating near-orthogonal runs onto the
   building's dominant grid, since scanners rarely produce exactly square
   corners and a person pictures square rooms.

Both are deliberately reversible: the original chain is untouched, and the
tolerance is a knob.
"""

from __future__ import annotations

import math


def douglas_peucker(points: list[tuple[float, float]], tolerance: float):
    """Classic polyline simplification; keeps endpoints."""
    if len(points) < 3:
        return list(points)

    def perpendicular_distance(p, a, b):
        (px, py), (ax, ay), (bx, by) = p, a, b
        dx, dy = bx - ax, by - ay
        if dx == 0 and dy == 0:
            return math.hypot(px - ax, py - ay)
        t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
        t = max(0.0, min(1.0, t))
        return math.hypot(px - (ax + t * dx), py - (ay + t * dy))

    first, last = points[0], points[-1]
    worst_i, worst_d = 0, 0.0
    for i in range(1, len(points) - 1):
        d = perpendicular_distance(points[i], first, last)
        if d > worst_d:
            worst_i, worst_d = i, d

    if worst_d <= tolerance:
        return [first, last]
    left = douglas_peucker(points[: worst_i + 1], tolerance)
    right = douglas_peucker(points[worst_i:], tolerance)
    return left[:-1] + right


def dominant_angle(points: list[tuple[float, float]]) -> float:
    """Length-weighted modal bearing, mod 90 degrees -- the building's grid."""
    buckets: dict[int, float] = {}
    for (x0, y0), (x1, y1) in zip(points, points[1:], strict=False):
        length = math.hypot(x1 - x0, y1 - y0)
        if length < 1e-6:
            continue
        bearing = math.degrees(math.atan2(y1 - y0, x1 - x0)) % 90.0
        buckets[round(bearing)] = buckets.get(round(bearing), 0.0) + length
    if not buckets:
        return 0.0
    return max(buckets.items(), key=lambda kv: kv[1])[0]


def snap_to_axes(points, grid_deg: float, snap_units: float = 0.1):
    """Regularise near-orthogonal corners by snapping coordinates in grid space.

    Walking the chain and re-emitting each segment at a snapped bearing looks
    tempting, but every rounding error is carried into the next point, so the
    outline slowly rotates and the room shrinks. Instead: rotate into the
    building's own grid, round the coordinates there, and rotate back. Errors
    stay local and the overall shape is preserved.

    Only near-axis edges are affected -- a genuinely diagonal wall keeps its
    endpoints because both of its ends snap independently.
    """
    if len(points) < 2:
        return list(points)

    theta = math.radians(grid_deg)
    cos_t, sin_t = math.cos(-theta), math.sin(-theta)

    rotated = [(x * cos_t - y * sin_t, x * sin_t + y * cos_t) for x, y in points]
    snapped = [(round(x / snap_units) * snap_units, round(y / snap_units) * snap_units)
               for x, y in rotated]

    cos_b, sin_b = math.cos(theta), math.sin(theta)
    return [(x * cos_b - y * sin_b, x * sin_b + y * cos_b) for x, y in snapped]


def close_loop(points, tolerance: float):
    """If the chain nearly returns to its start, close it exactly."""
    if len(points) > 2 and math.dist(points[0], points[-1]) <= tolerance:
        return points[:-1] + [points[0]]
    return points


def simplify(points, tolerance: float, snap: bool = True, close_tol: float | None = None):
    """Full simplification, returning (points, stats).

    Everything is expressed as a fraction of `tolerance` rather than in fixed
    units. An earlier version hardcoded a 10 "cm" snap, which silently destroyed
    the outline when fed metres -- it snapped the room onto a 10-metre grid. The
    function is unit-agnostic now: pass points and tolerance in the same unit,
    whatever it is.
    """
    before = len(points)
    pts = douglas_peucker(points, tolerance)
    grid = dominant_angle(pts)
    if snap:
        pts = snap_to_axes(pts, grid, snap_units=tolerance / 5.0)
    pts = close_loop(pts, close_tol if close_tol is not None else tolerance * 2)
    return pts, {"before": before, "after": len(pts), "grid_deg": grid}


def polygon_area(points) -> float:
    """Shoelace area; useful as a sanity check that simplification kept the room."""
    if len(points) < 3:
        return 0.0
    s = 0.0
    for (x0, y0), (x1, y1) in zip(points, points[1:] + [points[0]], strict=False):
        s += x0 * y1 - x1 * y0
    return abs(s) / 2.0
