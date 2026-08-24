"""Finding the right basin when the two plans cover different ground.

The fitter's coarse stage sweeps rotation but derives translation by aligning
the two point clouds' centroids, which silently assumes both clouds cover the
same part of the building. A capture of one bedroom against a ten-room survey
breaks that assumption: at the CORRECT rotation the centroid guess is metres
out, the correct basin scores worse than the noise, and local refinement --
which travels 0.30 m in total -- can never walk back to it.

The symptom is not a bad number. It is a confident transform onto the wrong
walls, which `combine` then reports as a character reference for the capture:
"This is the wrong basin, not a poor fit". The capture was fine.
"""

from __future__ import annotations

import math

import numpy as np
from scipy.spatial import cKDTree

from lidar2ha.registration import register, sample_segments, transform

STEP = 0.05


def _chain(corners) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    return list(zip(corners, corners[1:], strict=False))


def one_room_segments():
    """An L-shaped room, 5 x 3.5 m with a bite out of one corner.

    L-shaped rather than rectangular because a rectangle reads the same under a
    half turn and under reflection, so it could match the building's other
    rooms as readily as its own place and the test would prove nothing.
    """
    return _chain([(7.0, 0.0), (12.0, 0.0), (12.0, 3.5), (9.5, 3.5),
                   (9.5, 2.0), (7.0, 2.0), (7.0, 0.0)])


def building_segments():
    """Three rooms in a row: two plain rectangles and the L-shaped one.

    Different sizes, so the L-shaped room has exactly one place it belongs.
    """
    return (_chain([(0.0, 0.0), (4.0, 0.0), (4.0, 3.0), (0.0, 3.0), (0.0, 0.0)])
            + _chain([(4.0, 0.0), (7.0, 0.0), (7.0, 5.0), (4.0, 5.0), (4.0, 0.0)])
            + one_room_segments())


ROOM_CENTRES = {
    "left": (2.0, 1.5),
    "middle": (5.5, 2.5),
    "L-shaped": (9.9, 1.2),
}

THETA, TX, TY = math.radians(30.0), 12.0, -3.0


def _placed_case():
    """One room, and the whole building carried off to a known transform."""
    room = sample_segments(one_room_segments(), STEP)
    building = sample_segments(building_segments(), STEP)
    target = transform(building, THETA, TX, TY, mirror=False)
    return room, building, target, cKDTree(target)


def _anchors_as_room_pairing_would_give_them():
    """Every candidate correspondence, not just the correct one.

    `compare.room_anchors` pairs on area and has no idea which rooms are the
    same room, so handing this test only the true pairing would test something
    easier than the thing that runs.
    """
    src_c = ROOM_CENTRES["L-shaped"]
    return [(src_c, tuple(transform(np.array([c]), THETA, TX, TY, False)[0]))
            for c in ROOM_CENTRES.values()]


def test_a_capture_of_one_room_finds_its_place_in_the_whole_building():
    """The failure that discards four of this house's captures.

    One room against a three-room survey. Their centroids are ~4 m apart in the
    room's own frame, so centroid-aligned translation puts the room in the
    middle of the building at every rotation it tries -- including the correct
    one, which therefore scores worse than noise and never reaches refinement.
    Correspondences between rooms are what put the correct basin on the table.
    """
    room, building, target, tree = _placed_case()
    truth = transform(room, THETA, TX, TY, mirror=False)

    # Two guards, without which this test would prove nothing.
    offset = np.linalg.norm(building.mean(axis=0) - room.mean(axis=0))
    assert offset > 3.0, "if the centroids coincide there is no defect to fix"
    blind = register(room, target, tree, force_mirror=False)
    blind_placed = transform(room, blind["theta_rad"], blind["tx"], blind["ty"], False)
    assert np.abs(blind_placed - truth).max() > 1.0, (
        "the blind sweep already found it, so the seeds are not what fixed this"
    )

    fit = register(room, target, tree, force_mirror=False,
                   rotations=[THETA + k * math.pi / 2 for k in range(4)],
                   anchors=_anchors_as_room_pairing_would_give_them())

    placed = transform(room, fit["theta_rad"], fit["tx"], fit["ty"], fit["mirror"])
    assert np.abs(placed - truth).max() < 0.10, (
        f"placed the room {np.abs(placed - truth).max():.2f} m from where it is; "
        f"rotation {math.degrees(fit['theta_rad']) % 360:.1f} deg against 30.0"
    )
    assert fit["coverage"] > 0.95


def test_seeding_cannot_return_a_worse_fit_than_not_seeding():
    """Seeds are ranked on one unrefined scoring, which is a weak ranking.

    So the blind sweep's winner is refined unconditionally alongside them. Were
    it merely thrown into the pool and ranked, a capture the old code placed
    perfectly could be displaced by a seed that scores better unrefined and
    worse refined -- a regression on the captures that already work, bought to
    fix the ones that do not.
    """
    room, _, target, tree = _placed_case()
    # Deliberately useless correspondences: all three point somewhere empty.
    junk = [((0.0, 0.0), (100.0, 100.0)), ((1.0, 1.0), (-80.0, 40.0))]

    blind = register(room, target, tree, force_mirror=False)
    seeded = register(room, target, tree, force_mirror=False,
                      rotations=[0.0, 1.0, 2.0, 3.0], anchors=junk)

    assert seeded["fit_cost_m"] <= blind["fit_cost_m"] + 1e-9
