#!/usr/bin/env python3
"""Combine several captures of one level into a single model, and say what to re-scan.

WHY THIS EXISTS
---------------
`schema.Capture` describes the doctrine -- geometry SELECTED, textures COMPOSITE,
detected features UNION -- and every field it needs already existed. Nothing
constructed one. Every stage ran on exactly one capture, and the cost was
concrete: a mid-level bathroom present in the fixture pass and absent from the
geometry pass vanished from the model, and nothing noticed.

THE ORDER IS THE DESIGN
-----------------------
Perfect overlap first, THEN decide which capture wins each area. Before every
capture shares a frame you cannot tell which rooms correspond, so "which is
better" is not yet a question. The five stages below are a sequence, not a menu.

ANY SCAN CAN CONTRIBUTE ANYTHING
--------------------------------
A fixture pass is not banned from supplying geometry. It is merely likely to be
worse at it, having been shot with the lights on and the phone aimed at
fittings. `role` is a scoring prior, never a veto -- the one room this pipeline
was missing is a bathroom only a fixture pass ever saw, and a veto would have
thrown it away for the second time.

THE PRIMARY OUTPUT IS A WORK LIST
---------------------------------
Not just a model. "The best source I have for the mid-level bathroom is a
fixture pass -- go re-scan it" is the thing that was impossible to say before.

WHAT THIS DELIBERATELY DOES NOT DO
----------------------------------
* No averaging, ever. Two plans of one room disagree by ~17 cm median; blending
  them matches neither wall. Selection is per room and takes the room whole.
* No handedness search. Mirroring is a property of the DXF export, so two plans
  from one scanner share it -- letting the fit choose invents a reflected
  building to paper over a poor overlap. `compare` refuses this too.
* No compositing of textures. The doctrine says they should composite; that is
  per-wall work in `textures_project`, and it is not this stage.
* No silent degradation. A capture that fits badly is named and excluded; a room
  that cannot be used is named; a room used against better judgement is flagged.

Usage:
    python -m lidar2ha.combine midlevel.json midlevel_fixtures.json -o combined.json
"""

from __future__ import annotations

import argparse
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import numpy as np
from scipy.spatial import cKDTree
from shapely.geometry import Polygon
from shapely.ops import unary_union

from .compare import Fit, plan_fit
from .registration import sample_along_walls, sample_segments, transform
from .rooms import polygon_of
from .schema import Capture, Level, Model, Room, Wall, load_model, save_model

CM_TO_M = 0.01
M_TO_CM = 100.0
CM2_TO_M2 = 1e-4

# --------------------------------------------------------------------------- #
# Constants are guesses. Each carries the measurement it came from and what
# would change it. They are tuned to one house and one scanning app.
# --------------------------------------------------------------------------- #

# ACCEPTANCE IS ON FIT QUALITY, NEVER ON COVERAGE, and the distinction is the
# whole point of this stage. `coverage` is the fraction of the SOURCE's wall
# points that found a match in the reference, so a capture that sees a room the
# reference does not will ALWAYS score lower -- and that capture is the entire
# reason to combine. A 90% coverage threshold rejected the mid-level fixture
# pass at 88%: the one capture containing the bathroom.
#
# What decides whether a fit can be trusted is how well the MATCHED walls agree.
# Two plans of the same space fit to ~17 cm median. Change these if a capture
# you know to be of the same rooms is being refused.
MAX_MEDIAN_CM = 35.0
MAX_P90_CM = 120.0

# Below this coverage, say loudly that the capture covers ground the reference
# does not. A signal to act on, never a reason to reject.
NEW_GROUND_COVERAGE = 0.95

# Two rooms are the same room when the smaller sits inside the larger.
# `intersection / min(area)` and NOT IoU: measured on this house, the fixture
# pass's fused 46 m2 living room scores 93/98/82/98 by containment against the
# four reference rooms it covers, but only 45/17/14/17 by IoU. No IoU threshold
# links the last three, so the disagreement would be missed and the fused room
# would look one-to-one with the reference's living room. Containment asks "is
# the smaller room inside the bigger one", which is the question being asked.
#
# Measured containments here are 3%, 13%, then 82-99%. Any threshold in 0.2-0.8
# gives the same grouping, so this is robust rather than tuned. It would need
# changing for a house with genuinely half-overlapping rooms.
EDGE_CONTAINMENT = 0.60
# Containment alone reports 1.00 for a 0.3 m2 sliver wholly inside a 40 m2 room.
# The smallest real correspondence here is 2.6 m2. Raise it if slivers still get
# through; lower it for a house with real cupboard-sized areas.
MIN_EDGE_M2 = 0.5
# Pairs below the edge threshold but above this are printed as near-edges. A
# wrong EDGE_CONTAINMENT then shows up as a near-edge at 0.58 instead of as
# nothing at all.
REPORT_CONTAINMENT = 0.05

# A component this large is not a correspondence, it is the whole floor chained
# together through one weak link. Never resolved without saying so.
TANGLE_SIZE = 6
# Two rooms of ONE capture overlapping by more than this is the scanner
# contradicting itself, which `seams` or a `merge:` entry repairs.
SELF_OVERLAP_M2 = 0.5

# When one already-named place covers this much of an unnamed room, say what it
# looks like. Measured on a fresh 10-room capture: 7 rooms sat above 71% inside
# exactly one named place, and the 3 that did not were three genuinely different
# questions. Nothing is ever named automatically -- see `name_suggestions`.
NAME_CONFIDENT_FRAC = 0.70
# ...and when named places cover this much of a room between them without any
# one of them dominating, the capture fused rooms rather than found a new one.
NAME_COVERED_FRAC = 0.90

# Below this share of the reference's footprint, a capture is small enough that
# it fits inside the reference wherever it is put, so coverage says nothing about
# whether the placement is right. Measured: a 5-wall single room against a
# 10-room reference reported 100% coverage and 18.9 cm median while sitting 65
# degrees and one room away from the truth. The value is a guess -- the real
# question is whether the rotation minimum is sharp, and nothing measures that
# yet -- so this reports doubt rather than refusing anything.
SMALL_CAPTURE_RATIO = 0.35

# The smallest piece of unclaimed floor worth calling a discovery. Differencing
# two polygons that nearly coincide leaves residue along every shared wall line:
# of six fragments measured on one level, two were 0.3 and 0.6 m2 slivers of
# exactly that kind and four were real. Below this a fragment is counted and its
# area totalled, never listed as somewhere to go and re-scan.
MIN_FRAGMENT_M2 = 1.0

# A wall point this close to an accepted wall counts as matched. Sized above the
# 7.4 cm median inter-capture agreement and below its 47.6 cm p90, because a
# wall the fit placed slightly worse is still the same wall.
WALL_MATCH_CM = 50.0
# ...and a wall this much of which matched is the same physical wall seen twice.
# Measured, the matched fraction is BIMODAL: 0, 11, 31, 31, 35, 42%, then 23
# walls at 100%. Any value in 0.5-0.99 drops the same 23 and keeps the same 6.
# The obvious alternative -- requiring EVERY sampled point to match -- is wrong:
# it keeps 16-21 of 29, mostly duplicates the fit placed slightly worse.
WALL_DUPLICATE_FRAC = 0.80

# How close a wall has to be to count as supporting a room's outline. The
# polygon runs the inner face and walls are centrelines, so half a 10-20 cm wall
# plus the inter-capture disagreement. Raise it for a house with thick walls.
WALL_GAP_CM = 25.0
# The disagreement at which a room's agreement signal reaches zero. Deliberately
# the same number MAX_MEDIAN_CM accepts a whole capture at: one constant, not
# two that can drift apart.
AGREE_SCALE_CM = MAX_MEDIAN_CM
# Below this matched fraction the other capture clipped a corner rather than
# witnessing the room, so agreement is UNMEASURED rather than bad.
MIN_AGREE_FRAC = 0.30

# Ceiling plausibility, judged on ceiling_high_cm ALONE. The lowest tallest-point
# measured anywhere in this house is 210 cm, so a room reporting less than about
# 2 m at its highest was not seen properly. Judging ceiling_low_cm instead marks
# most of a real sloped storey implausible: a good geometry capture of the
# upstairs here reports lows of 100, 110, 140, 160 and 170 cm, which are eaves.
CEIL_MIN_HIGH_CM = 200.0
# Two captures of one room differ by centimetres for reasons that are not
# evidence -- 270 against 280 -- so the relative comparison starts after this.
CEIL_SLACK_CM = 25.0
# The upper bound is high because a double-height space here measures 3.2-4.7 m
# and `ceilings` reported a truncated 5.92 m lower bound for a stairwell.
CEIL_MAX_CM = 700.0
CEIL_RAMP_CM = 30.0

# What a fixture pass costs itself. Measured: it fits at 7.4 cm where geometry
# captures fit at 1.4-1.6 cm, and reads a 220 cm room as 185. Sized to decide
# between near-equals and to be irrelevant when nothing opposes it -- raise it
# if a fixture pass wins a room it should not, lower it if it loses one it
# should. It is a prior; it must never reach a size that acts as a veto.
ROLE_PENALTY = 0.15

# Below this the winning geometry is flagged for re-scanning. The measured band
# 0.60-0.94 is empty on this house, so the exact value is not load-bearing --
# and it is only ONE of the reasons a room gets flagged. See `provisional_for`.
PROVISIONAL_SCORE = 0.70
# Two captures this close are a toss-up, not a decision, however it came out.
DISAGREE_MARGIN = 0.10
# Below this fraction of a disagreement group covered by the winner, the hole is
# reported. It is never filled from the loser: that is blending under another
# name, and it produces a T-junction sliver along every shared edge.
FOOTPRINT_FRAC = 0.85

WEIGHTS: dict[str, float] = {
    # Agreement with the co-registered consensus does most of the work: it is
    # the only signal measured against evidence from OUTSIDE the candidate's own
    # capture, so it is the only one a bad capture cannot flatter itself on.
    "agreement": 0.30,
    "ceiling": 0.25,
    "capture": 0.15,
    # Wall support and enclosure are weighted LOW on purpose, against the
    # obvious intuition and against the shape of the original request. Measured
    # on this house, outline points within 30 cm of a wall of the room's own
    # capture run 74-100% for the geometry capture and 88-100% for the fixture
    # pass -- the deliberately-bad scan scores HIGHER on every room. Polycam
    # derives room polygons from the walls it detected, so a polygon almost
    # always follows its own capture's walls and the signal is near-constant by
    # construction. Its real job is the pathological case: a polygon whose edges
    # follow no wall anywhere is a scan-limit artefact rather than a room.
    "wall": 0.10,
    "enclosure": 0.10,
    "consensus": 0.10,
}

Kind = Literal["one_to_one", "unopposed", "disagreement", "tangled"]


# --------------------------------------------------------------------------- #
# what a candidate room is, once every capture shares a frame
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Candidate:
    """One capture's claim on one piece of floor, in the reference frame."""

    index: int
    capture: str
    role: str
    room: Room                # already carried into the reference frame
    poly: Polygon             # centimetres, reference frame
    area_m2: float

    @property
    def label(self) -> str:
        """What to call this room, preferring the identity over the guess.

        The Home Assistant area id first, always. A scanner name is not
        identity -- one capture labelled a single entrance hall "Living Room"
        AND "Dining Room" -- and printing one where the project already records
        an area id has, in practice, ended with a person being asked to identify
        a room their own project.yaml had already named.
        """
        return str(self.room.ha_area or self.room.name or self.room.scanner_name or "?")

    @property
    def named(self) -> bool:
        """Whether anything has given this room its Home Assistant identity yet."""
        return self.room.ha_area is not None


@dataclass
class Group:
    """Rooms that cannot be decided independently, because they share floor."""

    members: list[int]
    per_capture: dict[str, list[int]]
    kind: Kind
    edges: dict[tuple[int, int], float] = field(default_factory=dict)
    near_edges: dict[tuple[int, int], float] = field(default_factory=dict)
    self_overlaps: list[tuple[int, int, float]] = field(default_factory=list)


@dataclass
class Score:
    """A candidate's score, and every signal that went into it.

    `missing` is not a defect. A signal nobody could measure has its weight
    redistributed over the ones that were measured, rather than scoring zero --
    "nobody looked" is not evidence against a room, which is the same
    three-answer rule `daylight.verdict_at` follows.
    """

    total: float
    signals: dict[str, float | None]
    missing: list[str]

    @property
    def weakest(self) -> str | None:
        """The signal that cost this candidate most.

        Named in the report because a 0.17 ceiling and a 0.0 wall support read
        completely differently and need completely different fixes.
        """
        measured = {k: v for k, v in self.signals.items() if v is not None}
        return min(measured, key=lambda k: measured[k]) if measured else None


@dataclass
class Decision:
    """Who won a group, and every reason the result is not to be trusted."""

    group: Group
    winner: str | None
    winner_rooms: list[int]
    runner_up: str | None
    margin: float | None
    provisional: bool
    reasons: list[str]
    hole_m2: float


# --------------------------------------------------------------------------- #
# stage 1 -- put every capture of the level in one frame
# --------------------------------------------------------------------------- #


def one_level(model: Model, name: str | None) -> Level:
    """The single level this capture contributes, or refuse and say why.

    A multi-floor capture flattened into one plan is how a mirror artefact wins:
    two storeys of walls become one point cloud, and a fit that reflects the
    building explains as much of it as the correct one does. `entranceway` here
    is exactly that capture, and the prototype folded it in without a word.
    """
    if name is not None:
        found = [lv for lv in model.levels if lv.name == name]
        if not found:
            raise ValueError(f"has no level named {name!r}; it has "
                             f"{[lv.name for lv in model.levels]}")
        return found[0]
    if len(model.levels) != 1:
        raise ValueError(
            f"contributes {len(model.levels)} levels "
            f"{[lv.name for lv in model.levels]}, and combining is per level. Pass "
            f"--level NAME to choose one -- flattening two storeys into a single "
            f"plan lets a mirrored fit explain as much of it as the correct one")
    return model.levels[0]


def spread_m2(points: np.ndarray) -> float:
    """Convex-hull area of a point cloud, in m2. How much ground it spans."""
    if len(points) < 3:
        return 0.0
    from shapely.geometry import MultiPoint
    return float(MultiPoint([tuple(p) for p in points]).convex_hull.area)


def coverage_is_uninformative(source: np.ndarray, reference: np.ndarray, *,
                              ratio: float = SMALL_CAPTURE_RATIO) -> float | None:
    """How small this capture is against the reference, when that makes coverage
    a number to ignore. None when the capture is big enough for it to mean
    something.

    A SMALL CAPTURE LANDS ENTIRELY INSIDE A LARGE ONE WHEREVER YOU PUT IT, so it
    reports 100% coverage for every wrong answer as readily as for the right
    one. Worse, a handful of wall segments in a mostly-rectangular room is
    nearly rotationally symmetric against a big floorplan: there are several
    near-equal minima and the fitter picks one. Measured, a five-wall 9.8 m2
    bedroom fitted a ten-room reference at 100% coverage and 18.9 cm median --
    every number the pipeline reports looked fine -- while sitting 88% on top of
    the hallway, 65 degrees from where it belongs.

    Nothing currently measured separates that from a correct fit, so this does
    not try to. It says the numbers cannot be read, which is the honest answer
    and stops a confidently wrong placement being a silent one.
    """
    ref = spread_m2(reference)
    if ref <= 0:
        return None
    share = spread_m2(source) / ref
    return share if share < ratio else None


def pick_reference(levels: dict[str, Level], roles: Mapping[str, str]) -> str:
    """The capture every other one is moved onto.

    Widest coverage first, because the anchor has to be able to see the others:
    a small capture as reference leaves most of the level with nothing to fit
    against. Wall count is the proxy, and its own registration error breaks
    ties. `role` orders before both -- a fixture pass CAN anchor a level, but it
    should be the last capture asked to.
    """
    def key(item: tuple[str, Level]) -> tuple[int, int, float]:
        name, level = item
        reg = level.registration
        err = reg.median_error_m if reg is not None else 9.99
        return (0 if roles[name] == "geometry" else 1, -len(level.walls), err)

    return sorted(levels.items(), key=key)[0][0]


def place_cm(points_cm: Any, fit: Fit | None) -> np.ndarray:
    """Carry plan centimetres into the reference frame's centimetres.

    Expressed through `registration.transform` -- the function that put the plan
    onto the mesh in the first place -- rather than by writing the rotation out
    again. Two hand-written copies of one transform is how a sign error
    survives: both look right, and a reflected plan is still a plan.
    """
    pts = np.asarray(points_cm, dtype=float).reshape(-1, 2)
    if fit is None:
        return pts
    return transform(pts * CM_TO_M, fit["theta_rad"], fit["tx"], fit["ty"], False) * M_TO_CM


def place_wall(wall: Wall, fit: Fit | None, source: str) -> Wall:
    """One wall, in the reference frame, carrying where it came from."""
    (xs, ys), (xe, ye) = place_cm(
        [(wall.x_start, wall.y_start), (wall.x_end, wall.y_end)], fit)
    return wall.model_copy(update={"x_start": float(xs), "y_start": float(ys),
                                   "x_end": float(xe), "y_end": float(ye),
                                   "source": source})


def outline_m(poly: Polygon, step_m: float = 0.05) -> np.ndarray:
    """Points around a room polygon's edges, in metres, without doubled corners.

    `include_end=False` matters: on a closed ring the last point of one edge is
    the first point of the next, so including it counts every corner twice and
    weights corners twice in every statistic taken over the result.
    """
    ring = np.asarray(poly.exterior.coords[:-1], dtype=float) * CM_TO_M
    if len(ring) < 3:
        return np.empty((0, 2))
    return sample_segments(list(zip(ring, np.roll(ring, -1, axis=0), strict=True)),
                           step_m, include_end=False)


def nn_stats(pts: np.ndarray, tree: cKDTree, cap_m: float
             ) -> tuple[float, float | None, float | None]:
    """(matched fraction, median, p90) of nearest-neighbour distance, in metres.

    `registration.score` cannot be used here, and the difference is not
    cosmetic: it returns inf below 20% matched, which is right when CHOOSING a
    fit and exactly wrong per room. A genuinely new room matches almost nothing
    by definition -- the bathroom matches 55% -- and inf would erase it.
    """
    if len(pts) == 0:
        return 0.0, None, None
    d, _ = tree.query(pts, k=1, distance_upper_bound=cap_m)
    ok = np.isfinite(d)
    if not ok.any():
        return 0.0, None, None
    return float(ok.mean()), float(np.median(d[ok])), float(np.percentile(d[ok], 90))


# --------------------------------------------------------------------------- #
# stage 2 -- correspond rooms by polygon overlap, never by name
# --------------------------------------------------------------------------- #


def containment(a: Polygon, b: Polygon) -> tuple[float, float]:
    """(intersection / smaller area, intersection in m2).

    NOT IoU, and the difference decides whether a fused room is noticed at all.
    Measured here, a fixture pass's single 46 m2 living room covers four
    reference rooms at 93/98/82/98 by containment and at 45/17/14/17 by IoU. An
    IoU threshold low enough to catch the 14 would chain half the floor into one
    group. Containment asks "is the smaller room inside the bigger one", which
    is the question actually being asked.
    """
    inter = a.intersection(b).area
    smaller = min(a.area, b.area)
    if smaller <= 0:
        return 0.0, 0.0
    return inter / smaller, inter * CM2_TO_M2


def classify(members: list[int], per_capture: dict[str, list[int]], tangle: int,
             self_overlaps: list[tuple[int, int, float]]) -> Kind:
    """Which of the four outcomes this group is. They all matter.

    `unopposed` is the one this stage exists for: exactly one capture has ever
    seen this floor. Union takes it, and it is still scored -- which is what
    turns it into a re-scan request rather than a silent inclusion.
    """
    if len(members) > tangle or self_overlaps:
        return "tangled"
    if len(per_capture) == 1 and len(members) == 1:
        return "unopposed"
    if all(len(rooms) == 1 for rooms in per_capture.values()):
        return "one_to_one"
    return "disagreement"


def group_rooms(cands: list[Candidate], *, edge: float = EDGE_CONTAINMENT,
                min_edge_m2: float = MIN_EDGE_M2, tangle: int = TANGLE_SIZE,
                self_overlap_m2: float = SELF_OVERLAP_M2) -> list[Group]:
    """Rooms that share floor, grouped so they are decided together.

    Connected components, and the chaining that implies is deliberate. If B1
    overlaps both A1 and A2 while A1 and A2 are disjoint, B1 covers the ground
    of two of A's rooms -- accepting B1 and A1 both would lay 22 m2 of floor
    twice over. The decision is physically coupled, so one component is the
    truthful unit even though it holds rooms that do not touch each other.

    The alternative, one-to-one matching per capture pair, fails in exactly the
    way this stage exists to prevent: it pairs the fused living room with the
    reference's living room and silently orphans the dining room.
    """
    n = len(cands)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    edges: dict[tuple[int, int], float] = {}
    near: dict[tuple[int, int], float] = {}
    selves: list[tuple[int, int, float]] = []

    for i in range(n):
        for j in range(i + 1, n):
            frac, inter_m2 = containment(cands[i].poly, cands[j].poly)
            contradiction = (cands[i].capture == cands[j].capture
                             and inter_m2 > self_overlap_m2)
            if contradiction:
                selves.append((i, j, inter_m2))
            # A capture overlapping itself joins the group whatever the
            # containment, because emitting both rooms lays that floor twice
            # either way -- so the decision is coupled and the group is the unit
            # that has to be reported. Two 16 m2 rooms of one capture sharing
            # 9 m2 sit at 0.56 containment, under the edge threshold, and would
            # otherwise land in separate groups with the contradiction dropped.
            if contradiction or (frac >= edge and inter_m2 >= min_edge_m2):
                edges[(i, j)] = frac
                parent[find(i)] = find(j)
            elif frac >= REPORT_CONTAINMENT:
                # Never used, always printed. A threshold set wrong then shows
                # up here as a near-edge at 0.58 rather than as silence.
                near[(i, j)] = frac

    buckets: dict[int, list[int]] = {}
    for i in range(n):
        buckets.setdefault(find(i), []).append(i)

    groups = []
    for members in buckets.values():
        per_capture: dict[str, list[int]] = {}
        for i in sorted(members):
            per_capture.setdefault(cands[i].capture, []).append(i)
        inside = set(members)
        mine = [s for s in selves if s[0] in inside and s[1] in inside]
        groups.append(Group(
            members=sorted(members),
            per_capture=per_capture,
            kind=classify(sorted(members), per_capture, tangle, mine),
            edges={k: v for k, v in edges.items() if k[0] in inside},
            near_edges={k: v for k, v in near.items()
                        if k[0] in inside and k[1] in inside},
            self_overlaps=mine,
        ))
    return sorted(groups, key=lambda g: g.members[0])


# --------------------------------------------------------------------------- #
# stage 3 -- score each candidate room on its own evidence
# --------------------------------------------------------------------------- #


def wall_support(poly: Polygon, own_tree: cKDTree | None, gap_m: float
                 ) -> tuple[float, float]:
    """(fraction of the outline with a wall behind it, enclosure).

    Enclosure is 1 minus the longest UNBROKEN unsupported run, as a fraction of
    the perimeter. It separates a room from a scan-limit artefact in the way the
    plain fraction cannot: a real room missing a doorway's worth of wall in four
    places and a polygon whose entire south side is just where the scan stopped
    both report 80%, and only one of them is a room.
    """
    pts = outline_m(poly)
    if own_tree is None or len(pts) == 0:
        return 0.0, 0.0
    d, _ = own_tree.query(pts, k=1, distance_upper_bound=gap_m)
    ok = np.isfinite(d)
    if bool(ok.all()):
        return 1.0, 1.0

    longest = run = 0
    # Twice around the ring, so a gap straddling the start is one run, not two.
    for hit in list(~ok) * 2:
        run = run + 1 if hit else 0
        longest = max(longest, run)
    longest = min(longest, len(pts))
    return float(ok.mean()), float(1.0 - longest / len(pts))


def agreement(poly: Polygon, others_tree: cKDTree | None, *,
              scale_cm: float = AGREE_SCALE_CM, min_frac: float = MIN_AGREE_FRAC,
              match_cm: float = WALL_MATCH_CM) -> float | None:
    """How well the OTHER captures' walls corroborate this room, or None.

    The only signal measured against evidence from outside the candidate's own
    capture, which is why it carries the most weight: it is the one a bad scan
    cannot flatter itself on.

    None below `min_frac`, and returning three answers rather than two is the
    point. A room no other capture reached is UNCORROBORATED, which is not the
    same as contradicted -- the bathroom is the first kind, and scoring it as
    the second is how it disappeared.
    """
    pts = outline_m(poly)
    if others_tree is None or len(pts) == 0:
        return None
    frac, median_m, _p90 = nn_stats(pts, others_tree, match_cm * CM_TO_M)
    if frac < min_frac or median_m is None:
        return None
    return float(frac * max(0.0, 1.0 - median_m / (scale_cm * CM_TO_M)))


def ceiling_plausibility(room: Room, siblings: list[Room], *,
                         lo_cm: float = CEIL_MIN_HIGH_CM, hi_cm: float = CEIL_MAX_CM,
                         slack_cm: float = CEIL_SLACK_CM,
                         ramp_cm: float = CEIL_RAMP_CM) -> float | None:
    """Whether the reported ceiling could be this room's; None if unmeasured.

    A CEILING MEASUREMENT CAN BE TOO LOW BUT NOT TOO HIGH. The scanner reports
    the highest surface it actually saw, so failing to look up costs height and
    nothing adds it. That asymmetry is the whole signal: across captures of the
    same floor the TALLEST reading is the best estimate, and a capture falling
    materially short of it did not look. A fixture pass, walked pointing at
    fittings, falls short systematically -- it reads 180-250 cm for a space the
    geometry capture measures at 230-470.

    Judged on `ceiling_high_cm`, never on `ceiling_low_cm`. Measured here, a
    perfectly good geometry capture of a sloped upstairs reports lows of 100,
    110, 140, 160 and 170 cm -- those are real eaves, not bad scanning, and an
    absolute floor under the low reading marks most of a storey implausible.

    `slack_cm` before the ramp starts, because two captures of one room differ
    by centimetres for reasons that are not evidence: one reads 270 where
    another reads 280.
    """
    high = room.ceiling_high_cm
    if high is None:
        high = room.ceiling_low_cm
    if high is None:
        return None

    # A room whose tallest point is under about 2 m was not seen properly. The
    # lowest ceiling_high measured anywhere in this house is 210 cm.
    sane = max(0.0, 1.0 - max(0.0, lo_cm - high) / ramp_cm)
    sane = min(sane, max(0.0, 1.0 - max(0.0, high - hi_cm) / ramp_cm))

    tallest = max((s.ceiling_high_cm for s in siblings
                   if s.ceiling_high_cm is not None), default=None)
    if tallest is None:
        return float(sane)
    shortfall = max(0.0, tallest - high - slack_cm)
    return float(min(sane, max(0.0, 1.0 - shortfall / ramp_cm)))


def capture_quality(capture: Capture) -> float | None:
    """The capture-level metrics as a weak tiebreak; None if none were measured.

    `coverage` is deliberately absent. It is the fraction of the source's walls
    the reference explains, so it is ANTI-correlated with the value of a
    capture: the fixture pass scores 88% precisely because it contains a room
    nothing else has. Scoring on it is the trap this whole stage was built
    around, and the way to keep out of it is not to have the term.
    """
    terms = []
    if capture.median_error_m is not None:
        terms.append(max(0.0, 1.0 - capture.median_error_m / (MAX_MEDIAN_CM * CM_TO_M)))
    if capture.wall_area_ratio is not None:
        terms.append(max(0.0, 1.0 - abs(capture.wall_area_ratio - 1.0) / 0.30))
    return float(sum(terms) / len(terms)) if terms else None


def consensus(cand: Candidate, group: Group, cands: list[Candidate]) -> float | None:
    """Mean IoU with the group's other candidates; None when nothing opposes it.

    An outlier polygon in a crowd is usually the wrong one. Alone it says
    nothing at all, which is a third answer rather than a bad score.
    """
    others = [cands[i] for i in group.members if i != cand.index]
    if not others:
        return None
    ious = []
    for other in others:
        union = cand.poly.union(other.poly).area
        if union > 0:
            ious.append(cand.poly.intersection(other.poly).area / union)
    return float(sum(ious) / len(ious)) if ious else None


def score_room(cand: Candidate, group: Group, cands: list[Candidate], *,
               own_tree: cKDTree | None, others_tree: cKDTree | None,
               capture: Capture, weights: dict[str, float] | None = None,
               role_penalty: float = ROLE_PENALTY,
               wall_gap_cm: float = WALL_GAP_CM) -> Score:
    """One candidate's score, and every signal behind it.

    An unmeasured signal has its weight REDISTRIBUTED over the measured ones,
    never scored as zero. Scoring it zero punishes a room for the scan never
    having looked, which is the same mistake as calling a ceiling nobody
    measured 0 cm tall.
    """
    weights = weights or WEIGHTS
    wall, enclosure = wall_support(cand.poly, own_tree, wall_gap_cm * CM_TO_M)
    # Siblings are the OTHER captures' rooms over this same floor. Only they can
    # say a ceiling reading is short, because only they measured the same space.
    siblings = [cands[i].room for i in group.members
                if cands[i].capture != cand.capture]
    signals: dict[str, float | None] = {
        "agreement": agreement(cand.poly, others_tree),
        "ceiling": ceiling_plausibility(cand.room, siblings),
        "capture": capture_quality(capture),
        "wall": wall,
        "enclosure": enclosure,
        "consensus": consensus(cand, group, cands),
    }
    missing = sorted(k for k, v in signals.items() if v is None)
    live = sum(weights[k] for k, v in signals.items() if v is not None)
    if live <= 0:
        return Score(0.0, signals, missing)

    total = sum(weights[k] * v for k, v in signals.items() if v is not None) / live
    if cand.role != "geometry":
        total -= role_penalty
    return Score(max(0.0, min(1.0, total)), signals, missing)


# --------------------------------------------------------------------------- #
# stage 4 -- the winner takes the group whole
# --------------------------------------------------------------------------- #


def partition_score(indices: list[int], cands: list[Candidate],
                    scores: dict[int, Score]) -> tuple[float, float]:
    """(area-weighted score, footprint m2) of one capture's view of a group.

    The unit of decision is a capture's ENTIRE view of the group, not one room.
    Picking room by room inside a disagreement either lays the same floor twice
    -- the reference's living room plus the fixture pass's fused one -- or
    leaves a gap where the two partitions do not agree on a boundary.

    Area-weighted, so four well-scanned small rooms beat one mediocre big one
    rather than losing to it on a plain mean. The footprint is a UNION and not a
    sum: a capture whose own rooms overlap each other would otherwise count that
    floor twice and win on the strength of its own contradiction.
    """
    total_area = sum(cands[i].area_m2 for i in indices)
    if total_area <= 0:
        return 0.0, 0.0
    weighted = sum(cands[i].area_m2 * scores[i].total for i in indices) / total_area
    footprint = unary_union([cands[i].poly for i in indices]).area * CM2_TO_M2
    return float(weighted), float(footprint)


def decide(group: Group, cands: list[Candidate], scores: dict[int, Score], *,
           margin_needed: float = DISAGREE_MARGIN,
           provisional_score: float = PROVISIONAL_SCORE,
           footprint_frac: float = FOOTPRINT_FRAC) -> Decision:
    """Who takes this group, and every reason not to trust the answer.

    THERE IS NO MINIMUM SCORE. The best candidate takes the floor however badly
    it scores, because refusing a group is precisely how the bathroom vanished
    the first time. The score decides WHO wins and WHETHER it is flagged; it
    never decides whether the floor exists.
    """
    ranked = sorted(
        ((cap, *partition_score(rooms, cands, scores))
         for cap, rooms in group.per_capture.items()),
        key=lambda t: (-t[1], -t[2], t[0]),
    )
    best_cap, best_score, best_footprint = ranked[0]
    runner_up = ranked[1][0] if len(ranked) > 1 else None
    margin = best_score - ranked[1][1] if len(ranked) > 1 else None

    whole = unary_union([cands[i].poly for i in group.members]).area * CM2_TO_M2
    hole = max(0.0, whole - best_footprint)

    reasons = list(provisional_for(group, best_cap, best_score, margin, cands,
                                   margin_needed, provisional_score))
    if hole > 0 and whole > 0 and best_footprint / whole < footprint_frac:
        # Reported, never filled from the loser. Filling is blending under
        # another name, and it leaves a T-junction sliver along every edge the
        # two partitions did not agree on.
        reasons.append(
            f"{hole:.1f} m2 of this group is floor only the losing capture saw; "
            f"it is reported, not patched in")

    return Decision(
        group=group, winner=best_cap, winner_rooms=group.per_capture[best_cap],
        runner_up=runner_up, margin=margin, provisional=bool(reasons),
        reasons=reasons, hole_m2=hole,
    )


def provisional_for(group: Group, winner: str, score: float, margin: float | None,
                    cands: list[Candidate], margin_needed: float,
                    provisional_score: float) -> list[str]:
    """Every reason this geometry needs a better scan. All of them, not the first.

    A DISJUNCTION, never a threshold on the score alone. The bathroom scores
    around 0.6 today, but it would still have to be flagged if it scored 0.95:
    what makes it provisional is that a fixture pass is the only thing that has
    ever seen it, and no cutoff on a number can express that.
    """
    role = next(c.role for c in cands if c.capture == winner)
    reasons = []
    if role != "geometry":
        reasons.append(f"the best source is a {role} pass, whose geometry was "
                       f"sacrificed on purpose -- re-scan this room as geometry")
        if group.kind == "unopposed":
            # The two together are the bathroom, and the pair is what makes it a
            # re-scan request. Unopposed ALONE is not a defect: one good capture
            # of a room is the ordinary state of every project before this stage
            # existed, so flagging it would flag most of a house and mean
            # nothing. The group kind is reported either way.
            reasons.append("and no other capture has ever seen this ground, so "
                           "there is nothing to check it against")
    if score < provisional_score:
        reasons.append(f"best available score {score:.2f}, below {provisional_score:.2f}")
    if group.kind == "disagreement" and margin is not None and margin < margin_needed:
        # Only a CLOSE disagreement flags the rooms. A decisive one is still
        # reported and still reaches the work list -- it is never picked
        # silently -- but "go re-scan this" is wrong advice when a good geometry
        # capture beat a fixture pass by 0.27. Flagging every room of every
        # disagreement flagged 9 rooms in 10 on the first real level, and a flag
        # that fires on nearly everything tells you nothing.
        reasons.append(f"the captures disagree about this layout and the winner is "
                       f"only {margin:.2f} ahead -- a toss-up, not a decision")
    if group.kind == "tangled":
        reasons.append(f"{len(group.members)} rooms are chained into one group; "
                       f"see the containment matrix before believing this")
    return reasons


def capture_order(decisions: list[Decision], cands: list[Candidate],
                  scores: dict[int, Score], every: list[str]) -> list[str]:
    """Captures best-first, which is the order walls are offered in.

    A capture that won no room still contributes walls -- it may be the only one
    that saw a corridor -- so it is ordered last rather than dropped.
    """
    won: dict[str, list[int]] = {}
    for decision in decisions:
        if decision.winner is not None:
            won.setdefault(decision.winner, []).extend(decision.winner_rooms)

    def key(name: str) -> tuple[int, float]:
        rooms = won.get(name)
        if not rooms:
            return (1, -0.0)
        return (0, -partition_score(rooms, cands, scores)[0])

    return sorted(every, key=key)


def select_walls(offered: list[tuple[str, Wall]], *, match_cm: float = WALL_MATCH_CM,
                 duplicate_frac: float = WALL_DUPLICATE_FRAC
                 ) -> tuple[list[Wall], list[tuple[str, Wall, float]]]:
    """Union the captures' walls, dropping each one seen twice.

    A wall is dropped when this fraction of its sampled points already has a
    wall within `match_cm`. Measured on this house that fraction is BIMODAL --
    0, 11, 31, 31, 35, 42%, then twenty-three walls at 100% -- so the threshold
    is robust rather than tuned.

    Requiring EVERY point to match instead, which is the obvious reading, is
    wrong: p90 disagreement between two captures is 47.6 cm, so a duplicate the
    fit placed slightly worse keeps one stray point and survives. That predicate
    kept 16-21 of 29 offered walls where this one keeps 6.

    A partly-matched wall is kept WHOLE and reported. Trimming it to its
    unmatched part is averaging by another name, and it leaves a stub.

    A capture is NEVER deduplicated against itself, and that is not a detail.
    Its own wall list is the answer it already arrived at, so two of its walls
    lying close together are two walls, not one wall seen twice. Comparing a
    capture with itself destroyed nine of the reference's own walls here --
    short collinear runs like (-20,466)-(80,466) and (80,466)-(150,466), each
    reading 100% matched against its neighbour.

    `offered` must therefore arrive grouped by capture, best capture first.
    """
    kept: list[Wall] = []
    dropped: list[tuple[str, Wall, float]] = []
    settled: list[np.ndarray] = []      # captures already finished with
    tree: cKDTree | None = None
    current: str | None = None
    pending: list[np.ndarray] = []

    def close_capture() -> None:
        nonlocal tree
        if pending:
            settled.extend(pending)
            pending.clear()
            tree = cKDTree(np.vstack(settled))

    for source, wall in offered:
        if source != current:
            close_capture()
            current = source
        pts = sample_along_walls([wall])
        frac = 0.0
        if tree is not None and len(pts):
            d, _ = tree.query(pts, k=1, distance_upper_bound=match_cm * CM_TO_M)
            frac = float(np.isfinite(d).mean())
        if frac >= duplicate_frac:
            dropped.append((source, wall, frac))
            continue
        kept.append(wall)
        if len(pts):
            pending.append(pts)
    close_capture()
    return kept, dropped


@dataclass
class Fragment:
    """A piece of floor some capture saw that the combined model does not have."""

    area_m2: float
    at_cm: tuple[float, float]
    capture: str
    room: str
    also_seen_by: list[str]


def uncovered_floor(cands: list[Candidate], chosen: list[int],
                    scores: dict[int, Score], *,
                    min_piece_m2: float = MIN_FRAGMENT_M2
                    ) -> tuple[list[Fragment], float, int]:
    """Floor some capture saw that the output does not contain, and whose it was.

    NEW GROUND IS NOT A PROPERTY OF A ROOM. A room overlapping the reference by
    80% is reported as known, and the fifth of it nobody else ever saw is
    dropped without a word. Measured on a three-capture level, the per-room
    framing reported 2 new rooms while 21.2 m2 of new floor went missing in 6
    pieces -- one a 5.0 m2 strip inside a room scoring 73-87% overlapping.

    So AREA comes from the geometric difference over the whole level. But
    IDENTITY cannot: splitting that difference into connected components fuses
    adjacent new rooms into one blob, and a 2.8 m2 toilet touching a larger new
    block stopped being a room at all. Identity therefore comes from
    intersecting the difference back with each capture's ROOMS. Neither half
    works alone -- per-room misses the strip, components lose the toilet.

    Fragments are attributed best-scoring claimant first and subtracted as they
    go, so the areas sum to the difference instead of double-counting the floor
    two captures both saw. The other claimants are still named: on this house
    the toilet fragment is claimed by the capture with the better geometry and
    by the one whose room `project.yaml` actually maps to an area.

    Reported, NEVER patched in. A set difference is not a room -- it has no
    ceiling, no name, and its edges are two captures disagreeing rather than
    walls. What it is, is the list of places to point a phone at next.
    """
    if not cands:
        return [], 0.0, 0
    everything = unary_union([c.poly for c in cands])
    taken = unary_union([cands[i].poly for i in chosen]) if chosen else Polygon()
    left = everything.difference(taken)
    if left.is_empty:
        return [], 0.0, 0

    fragments: list[Fragment] = []
    sliver_area = 0.0
    slivers = 0
    for cand in sorted(cands, key=lambda c: -scores[c.index].total):
        if left.is_empty:
            break
        piece = cand.poly.intersection(left)
        if piece.is_empty:
            continue
        area = piece.area * CM2_TO_M2
        left = left.difference(piece)
        if area < min_piece_m2:
            # Differencing two polygons that nearly coincide leaves a long tail
            # of wall-line residue. Counted, not listed, and never silent.
            sliver_area += area
            slivers += 1
            continue
        point = piece.representative_point()
        others = sorted({c.capture for c in cands
                         if c.capture != cand.capture
                         and c.poly.intersection(piece).area * CM2_TO_M2 >= min_piece_m2})
        fragments.append(Fragment(float(area), (float(point.x), float(point.y)),
                                  cand.capture, cand.label, others))
    return sorted(fragments, key=lambda f: -f.area_m2), sliver_area, slivers


# --------------------------------------------------------------------------- #
# putting it together
# --------------------------------------------------------------------------- #


# Four different questions, and lumping them together as "unmapped" is what
# makes naming cost one hand-written line per room per capture.
NAMING_ADVICE = {
    "looks_like": "this room stands almost entirely on one place the project has "
                  "already named. Confirm it and add it to `rooms.<capture>` in "
                  "project.yaml -- it is not named for you, because a room named by "
                  "overlap and then believed is how a light ends up in the wrong room",
    "split": "this room is covered by several already-named places at once, so the "
             "capture fused rooms the project keeps apart. The question is which of "
             "them it is, not what it is -- see `python -m lidar2ha.seams`",
    "ambiguous": "half of this room stands on a named place and half on ground "
                 "nothing has named. Neither a confirmation nor a fresh name will "
                 "cover it; look at where the boundary actually falls",
    "ask": "nothing named stands under this room, so it needs a name -- once, for "
           "the place. Every later capture that sees it can then inherit that name "
           "by overlap instead of being mapped again by hand",
}


@dataclass
class Naming:
    """What an unnamed room looks like it is, and how sure that is.

    Four answers, not two, because they are four different questions to a
    person. `ask` is the only one that needs a name invented.
    """

    capture: str
    room: str
    area_m2: float
    verdict: Literal["looks_like", "split", "ambiguous", "ask"]
    places: list[tuple[str, float]]


def name_suggestions(cands: list[Candidate], chosen: list[int], *,
                     confident: float = NAME_CONFIDENT_FRAC,
                     covered: float = NAME_COVERED_FRAC) -> list[Naming]:
    """For each unnamed room, which named place it is sitting on.

    A SUGGESTION AND NEVER A DECISION. Nothing here writes `ha_area`: identity
    is not inferred in this project, and a room named by overlap and then
    believed is how a light ends up in the wrong room. What this removes is the
    other failure -- being asked to name a room from a scanner label, when the
    project already has a name for the place it stands on.

    Scanner names cannot do this. They are assigned per capture and carry no
    identity across them: in this house "Office" means the kitchen, an alcove,
    or the actual office depending on which capture said it. Once captures share
    a frame, PLACE is the thing that persists, and the mapping keyed on place
    survives every capture that arrives afterwards.
    """
    named = [c for c in cands if c.named]
    if not named:
        return []

    out = []
    for index in chosen:
        cand = cands[index]
        if cand.named or cand.poly.area <= 0:
            continue
        places = []
        for other in named:
            if other.capture == cand.capture:
                continue
            share = cand.poly.intersection(other.poly).area / cand.poly.area
            if share >= REPORT_CONTAINMENT:
                places.append((str(other.room.ha_area), float(share)))
        places.sort(key=lambda t: -t[1])
        total = sum(share for _, share in places)

        if places and places[0][1] >= confident:
            verdict: Literal["looks_like", "split", "ambiguous", "ask"] = "looks_like"
        elif total >= covered:
            # Covered, but by several places at once: the capture fused rooms,
            # so the question is WHICH of these it is, not what it is.
            verdict = "split"
        elif total < REPORT_CONTAINMENT:
            # Genuinely new ground. This needs a name once ever, for the place,
            # not once for every capture that later sees it.
            verdict = "ask"
        else:
            # Half on a named place and half on ground nobody has named.
            verdict = "ambiguous"
        out.append(Naming(cand.capture, cand.label, cand.area_m2, verdict, places[:4]))
    return sorted(out, key=lambda n: -n.area_m2)


@dataclass
class Combined:
    """Everything `combine` worked out, so the report and the tests share it."""

    model: Model
    reference: str
    candidates: list[Candidate]
    groups: list[Group]
    scores: dict[int, Score]
    decisions: list[Decision]
    fits: dict[str, Fit | None]
    rejected: dict[str, str]
    malformed: list[dict[str, Any]]
    dropped_walls: list[tuple[str, Wall, float]]
    fragments: list[Fragment]
    sliver_m2: float
    slivers: int
    cautions: dict[str, str]
    naming: list[Naming]
    worklist: list[dict[str, Any]]


def candidates_of(capture: str, level: Level, role: str, fit: Fit | None,
                  start: int, malformed: list[dict[str, Any]]) -> list[Candidate]:
    """This capture's rooms, in the reference frame, with the unusable named.

    A room that cannot become a polygon is not emitted, so it has to be loud:
    silently returning fewer rooms than the capture holds is the exact failure
    this module was written to end.
    """
    out: list[Candidate] = []
    for room in level.rooms:
        note = None
        if len(room.points) < 3:
            note = f"{len(room.points)} points, which cannot be a polygon"
        else:
            moved = room.model_copy(update={
                "points": [(float(x), float(y)) for x, y in place_cm(room.points, fit)]})
            poly = polygon_of(moved)
            if not poly.is_valid:
                poly = poly.buffer(0)
            if poly.geom_type == "MultiPolygon":
                parts = sorted(poly.geoms, key=lambda g: g.area, reverse=True)
                lost = sum(g.area for g in parts[1:]) * CM2_TO_M2
                note = (f"self-intersecting; buffer(0) left {len(parts)} pieces, "
                        f"keeping {parts[0].area * CM2_TO_M2:.1f} m2 and dropping "
                        f"{lost:.2f} m2")
                poly = parts[0]
                moved = moved.model_copy(update={
                    "points": [(float(x), float(y)) for x, y in poly.exterior.coords[:-1]]})
            if poly.is_empty or poly.area <= 0:
                note = note or "zero area after repair"
                malformed.append({"capture": capture, "room": str(room.name),
                                  "reason": note})
                continue
            if note:
                malformed.append({"capture": capture, "room": str(room.name),
                                  "reason": note, "kept": True})
            out.append(Candidate(index=start + len(out), capture=capture, role=role,
                                 room=moved, poly=poly,
                                 area_m2=float(poly.area * CM2_TO_M2)))
            continue
        malformed.append({"capture": capture, "room": str(room.name), "reason": note})
    return out


def capture_record(name: str, role: str, fit: Fit | None,
                   is_reference: bool) -> Capture:
    """One Capture row, including the transform that put it in the frame.

    Without that transform nothing downstream can re-derive where a room came
    from, and the selection becomes an assertion rather than a record.
    """
    if fit is None:
        # The reference fits itself exactly, and that is a measurement rather
        # than a convenience: the combined frame IS its plan frame. Leaving
        # these None instead made `capture_quality` unmeasurable for the one
        # capture whose placement is certain, so every reference room silently
        # lost that signal's weight to the others.
        return Capture(id=name, role=role,  # type: ignore[arg-type]
                       median_error_m=0.0, coverage=1.0, p90_error_m=0.0,
                       theta_deg=0.0, tx_m=0.0, ty_m=0.0, is_reference=is_reference)
    return Capture(
        id=name, role=role,  # type: ignore[arg-type]
        median_error_m=fit["median_error_m"], coverage=fit["coverage"],
        p90_error_m=fit["p90_m"],
        theta_deg=round(math.degrees(fit["theta_rad"]) % 360, 2),
        tx_m=round(fit["tx"], 4), ty_m=round(fit["ty"], 4),
        is_reference=is_reference,
    )


def worklist(decisions: list[Decision], cands: list[Candidate],
             scores: dict[int, Score], fragments: list[Fragment],
             naming: list[Naming],
             expected_areas: set[str] | None) -> list[dict[str, Any]]:
    """What to go and do about the house, which is the point of the exercise.

    Per area: who won it, how well, and every reason to distrust that. Plus the
    floor nobody's model contains, and the areas `project.yaml` names that no
    capture ever won -- an area mapped but never seen is a gap, and saying
    nothing about it is the silent drop this repo refuses.
    """
    items: list[dict[str, Any]] = []
    won_areas: set[str] = set()

    for decision in decisions:
        group = decision.group
        if group.kind == "disagreement":
            # Every disagreement reaches the work list, decisive or not. The
            # rooms are only FLAGGED when the margin is thin, but "these two
            # captures do not agree about the layout here" is a fact about the
            # house and never gets picked silently.
            losers = [i for i in group.members if i not in decision.winner_rooms]
            items.append({
                "kind": "captures_disagree",
                "winner": decision.winner,
                "won": [f"{cands[i].label} {cands[i].area_m2:.1f} m2"
                        for i in decision.winner_rooms],
                "lost": [f"{cands[i].capture}/{cands[i].label} "
                         f"{cands[i].area_m2:.1f} m2" for i in losers],
                "margin": None if decision.margin is None else round(decision.margin, 3),
                "reasons": ["the captures split this floor differently. The winning "
                            "capture took it whole; nothing was averaged and nothing "
                            "was picked room by room"],
            })
        for i in decision.winner_rooms:
            cand = cands[i]
            score = scores[i]
            if cand.room.ha_area:
                won_areas.add(cand.room.ha_area)
            if not decision.provisional:
                continue
            items.append({
                "kind": "provisional_room",
                "area": cand.room.ha_area,
                "room": cand.label,
                "capture": cand.capture,
                "role": cand.role,
                "area_m2": round(cand.area_m2, 2),
                "score": round(score.total, 3),
                "weakest_signal": score.weakest,
                "unmeasured": score.missing,
                "reasons": decision.reasons,
            })

    for fragment in fragments:
        items.append({
            "kind": "floor_not_in_the_model",
            "area_m2": round(fragment.area_m2, 2),
            "at_cm": [round(v, 1) for v in fragment.at_cm],
            "capture": fragment.capture,
            "room": fragment.room,
            "also_seen_by": fragment.also_seen_by,
            "reasons": ["a capture saw this floor and the combined model does not "
                        "contain it. It is not patched in: a set difference has no "
                        "ceiling and no name, and its edges are two captures "
                        "disagreeing rather than walls"],
        })

    for suggestion in naming:
        items.append({
            "kind": f"name_{suggestion.verdict}",
            "capture": suggestion.capture,
            "room": suggestion.room,
            "area_m2": round(suggestion.area_m2, 2),
            "sits_on": [{"area": a, "fraction": round(f, 3)}
                        for a, f in suggestion.places],
            "reasons": [NAMING_ADVICE[suggestion.verdict]],
        })

    for area in sorted((expected_areas or set()) - won_areas):
        items.append({
            "kind": "area_with_no_source",
            "area": area,
            "reasons": ["project.yaml maps this area on this level and no capture "
                        "won it. Either it was never scanned, or its room was "
                        "refused above"],
        })
    return items


def combine(models: dict[str, Model], *, level_name: str | None = None,
            reference: str | None = None, max_median_cm: float = MAX_MEDIAN_CM,
            max_p90_cm: float = MAX_P90_CM, edge: float = EDGE_CONTAINMENT,
            expected_areas: set[str] | None = None) -> Combined:
    """The five stages, with no printing. `main` reports what this returns."""
    if len(models) < 2:
        raise ValueError("give at least two captures; one capture needs no combining")

    levels: dict[str, Level] = {}
    rejected: dict[str, str] = {}
    for name, model in models.items():
        try:
            levels[name] = one_level(model, level_name)
        except ValueError as exc:
            rejected[name] = str(exc)
    if not levels:
        raise ValueError("no capture contributed a usable level")

    roles = {name: models[name].role for name in levels}
    ref = reference or pick_reference(levels, roles)
    if ref not in levels:
        raise ValueError(f"reference {ref!r} is not among {sorted(levels)}")
    if not levels[ref].walls:
        raise ValueError(f"reference {ref!r} has no walls to fit against")

    # --- stage 1: one frame ------------------------------------------------
    fits: dict[str, Fit | None] = {ref: None}
    cautions: dict[str, str] = {}
    for name, level in levels.items():
        if name == ref:
            continue
        if not level.walls:
            rooms = [str(r.name) for r in level.rooms]
            area = sum(polygon_of(r).area for r in level.rooms if len(r.points) >= 3)
            rejected[name] = (
                f"has no walls, so there is nothing to fit and its "
                f"{len(rooms)} room(s) ({area * CM2_TO_M2:.1f} m2: "
                f"{', '.join(rooms) or 'none'}) cannot enter the shared frame")
            continue
        fit = plan_fit(models[name].model_copy(update={"levels": [level]}),
                       models[ref].model_copy(update={"levels": [levels[ref]]}))
        p90_cm = None if fit["p90_m"] is None else fit["p90_m"] * M_TO_CM
        why = []
        if fit["median_error_m"] * M_TO_CM > max_median_cm:
            why.append(f"median {fit['median_error_m'] * M_TO_CM:.0f} cm")
        if p90_cm is not None and p90_cm > max_p90_cm:
            why.append(f"p90 {p90_cm:.0f} cm")
        if why:
            # Never on coverage. See MAX_MEDIAN_CM.
            rejected[name] = ("fits too badly to be the same rooms: "
                              + ", ".join(why))
            continue
        fits[name] = fit
        share = coverage_is_uninformative(sample_along_walls(level.walls),
                                          sample_along_walls(levels[ref].walls))
        if share is not None:
            cautions[name] = (
                f"spans {share * 100:.0f}% of the reference's footprint, so it fits "
                f"inside it wherever it is placed and its {fit['coverage'] * 100:.0f}% "
                f"coverage says nothing about whether the placement is right. Check "
                f"this one against the house before believing the rooms below it")

    accepted = [n for n in levels if n in fits]

    # --- stage 2: correspond ------------------------------------------------
    cands: list[Candidate] = []
    malformed: list[dict[str, Any]] = []
    for name in accepted:
        cands += candidates_of(name, levels[name], roles[name], fits[name],
                               len(cands), malformed)
    groups = group_rooms(cands, edge=edge)

    # --- stage 3: score -----------------------------------------------------
    trees: dict[str, cKDTree | None] = {}
    points: dict[str, np.ndarray] = {}
    for name in accepted:
        walls_m = sample_along_walls(
            [place_wall(w, fits[name], name) for w in levels[name].walls])
        points[name] = walls_m
        trees[name] = cKDTree(walls_m) if len(walls_m) else None

    others: dict[str, cKDTree | None] = {}
    for name in accepted:
        stack = [points[o] for o in accepted if o != name and len(points[o])]
        others[name] = cKDTree(np.vstack(stack)) if stack else None

    captures = {name: capture_record(name, roles[name], fits[name], name == ref)
                for name in accepted}

    scores = {
        c.index: score_room(c, group, cands, own_tree=trees[c.capture],
                            others_tree=others[c.capture], capture=captures[c.capture])
        for group in groups for c in (cands[i] for i in group.members)
    }

    # --- stage 4: select ----------------------------------------------------
    decisions = [decide(g, cands, scores) for g in groups]
    chosen: list[int] = []
    rooms_out: list[Room] = []
    for decision in decisions:
        for i in decision.winner_rooms:
            cand = cands[i]
            chosen.append(i)
            rooms_out.append(cand.room.model_copy(update={
                "source": cand.capture,
                "score": round(scores[i].total, 3),
                "provisional": decision.provisional,
                "provisional_reason": list(decision.reasons),
            }))

    order = capture_order(decisions, cands, scores, accepted)
    offered = [(name, place_wall(w, fits[name], name))
               for name in order for w in levels[name].walls]
    walls_out, dropped = select_walls(offered)

    fragments, sliver_m2, slivers = uncovered_floor(cands, chosen, scores)
    naming = name_suggestions(cands, chosen)

    # --- the model ----------------------------------------------------------
    base = levels[ref]
    model = Model(
        source=models[ref].source,
        # The combined model is a survey of the building even where a fixture
        # pass supplied a room, so `build` accepts it. Which rooms came from
        # where is on the rooms, and why to distrust them is on them too.
        role="geometry",
        levels=[Level(name=base.name, ceiling_height_cm=base.ceiling_height_cm,
                      elevation_cm=base.elevation_cm, walls=walls_out,
                      rooms=rooms_out, doors=list(base.doors),
                      # The reference's own plan-to-mesh fit, because the
                      # combined frame IS the reference's plan frame -- so
                      # `textures_project` still indexes into its mesh.
                      registration=base.registration)],
        captures=[captures[n] for n in accepted],
    )
    return Combined(
        model=model, reference=ref, candidates=cands, groups=groups, scores=scores,
        decisions=decisions, fits=fits, rejected=rejected, malformed=malformed,
        dropped_walls=dropped, fragments=fragments, sliver_m2=sliver_m2,
        slivers=slivers, cautions=cautions, naming=naming,
        worklist=worklist(decisions, cands, scores, fragments, naming, expected_areas),
    )


# --------------------------------------------------------------------------- #
# the report, which is the deliverable as much as the model is
# --------------------------------------------------------------------------- #


def report(result: Combined) -> None:
    """Print everything that was decided and everything that was refused."""
    cands, scores = result.candidates, result.scores
    print(f"reference: {result.reference}\n")
    print("accepting on FIT QUALITY. Coverage is reported and never used to reject:")
    print("  a capture that sees a room the reference does not will always score")
    print("  lower on coverage, and that capture is the reason to combine at all.\n")

    print(f"{'capture':22s} {'role':9s} {'rot':>8s} {'median':>8s} {'cover':>7s} "
          f"{'p90':>8s}  verdict")
    print("-" * 88)
    for name, fit in result.fits.items():
        role = next((c.role for c in cands if c.capture == name), "?")
        if fit is None:
            print(f"{name:22s} {role:9s} {'--':>8s} {'--':>8s} {'--':>7s} "
                  f"{'--':>8s}  REFERENCE")
            continue
        verdict = "ok"
        if fit["coverage"] < NEW_GROUND_COVERAGE:
            verdict = f"ok + {(1 - fit['coverage']) * 100:.0f}% NEW ground"
        p90 = "--" if fit["p90_m"] is None else f"{fit['p90_m'] * M_TO_CM:.1f}cm"
        print(f"{name:22s} {role:9s} "
              f"{math.degrees(fit['theta_rad']) % 360:7.2f}° "
              f"{fit['median_error_m'] * M_TO_CM:7.1f}cm "
              f"{fit['coverage'] * 100:6.0f}% {p90:>8s}  {verdict}")

    for name, why in result.cautions.items():
        print(f"    ** {name}: {why}")

    for name, why in result.rejected.items():
        print(f"{name:22s} {'':9s} {'':>8s} {'':>8s} {'':>7s} {'':>8s}  REFUSED")
        print(f"    {why}")
    if result.rejected:
        print("\n  A refused capture is not folded in. Combining on a bad fit makes a")
        print("  plausible model that is wrong, which is worse than a known gap.")

    if result.malformed:
        print("\nMALFORMED ROOMS")
        for entry in result.malformed:
            kept = "kept, repaired" if entry.get("kept") else "NOT EMITTED"
            print(f"  {entry['capture']:<20} {entry['room']:<16} {kept}: {entry['reason']}")

    print(f"\n{'group':>5} {'kind':<13} {'winner':<20} {'m2':>7} {'score':>6} "
          f"{'margin':>7}  rooms")
    print("-" * 96)
    for decision in result.decisions:
        group = decision.group
        rooms = ", ".join(f"{cands[i].capture}/{cands[i].label}" for i in group.members)
        best = max((scores[i].total for i in decision.winner_rooms), default=0.0)
        area = sum(cands[i].area_m2 for i in decision.winner_rooms)
        margin = "--" if decision.margin is None else f"{decision.margin:.2f}"
        flag = " *" if decision.provisional else ""
        print(f"{group.members[0]:>5} {group.kind:<13} {str(decision.winner):<20} "
              f"{area:7.1f} {best:6.2f} {margin:>7}{flag}  {rooms}")

    flagged = [d for d in result.decisions if d.provisional]
    if flagged:
        print("\nFLAGGED -- the geometry is the best available and still not enough")
        for decision in flagged:
            for i in decision.winner_rooms:
                score = scores[i]
                print(f"  {cands[i].label:<20} {cands[i].area_m2:5.1f} m2  "
                      f"score {score.total:.2f}  weakest={score.weakest}"
                      + (f"  unmeasured={','.join(score.missing)}" if score.missing else ""))
                for reason in decision.reasons:
                    print(f"      - {reason}")

    near = [(a, b, v) for d in result.decisions
            for (a, b), v in d.group.near_edges.items()]
    if near:
        print(f"\nNEAR-EDGES (below {EDGE_CONTAINMENT:.2f}, never used -- printed so a")
        print("  wrong threshold shows up as a number rather than as silence)")
        for a, b, value in sorted(near, key=lambda t: -t[2])[:12]:
            print(f"  {value * 100:3.0f}%  {cands[a].capture}/{cands[a].label}"
                  f"  vs  {cands[b].capture}/{cands[b].label}")

    selves = [s for d in result.decisions for s in d.group.self_overlaps]
    if selves:
        print("\nSELF-OVERLAPPING ROOMS IN ONE CAPTURE -- the scanner contradicting itself")
        for a, b, area in selves:
            print(f"  {cands[a].capture}: {cands[a].label} and {cands[b].label} "
                  f"share {area:.1f} m2")
        print("  Repair it with `python -m lidar2ha.seams`, or a `merge:` entry in "
              "project.yaml.")

    if result.fragments:
        total = sum(f.area_m2 for f in result.fragments)
        print(f"\nFLOOR NOT IN THE MODEL -- {total:.1f} m2 in "
              f"{len(result.fragments)} piece(s)")
        print("  A capture saw this floor and the combined model does not contain it.")
        print("  New ground is NOT a property of a room: a room 80% overlapping reads")
        print("  as known, and the fifth of it nobody else saw goes missing silently.")
        for fragment in result.fragments:
            also = f"  also seen by {', '.join(fragment.also_seen_by)}" \
                if fragment.also_seen_by else ""
            print(f"  {fragment.area_m2:5.1f} m2 at ({fragment.at_cm[0]:.0f}, "
                  f"{fragment.at_cm[1]:.0f}) cm  in {fragment.capture}/"
                  f"{fragment.room}{also}")
    if result.slivers:
        print(f"\n{result.slivers} sliver(s) totalling {result.sliver_m2:.2f} m2 fell "
              f"below {MIN_FRAGMENT_M2} m2 and are not listed above: differencing two "
              f"polygons\n  that nearly coincide leaves residue along every shared "
              f"wall line, which is\n  registration noise rather than a discovery.")

    unnamed = sorted({f"{cands[i].capture}/{cands[i].label}"
                      for d in result.decisions for i in d.winner_rooms
                      if not cands[i].named})
    if unnamed:
        print("\nSTILL CARRYING SCANNER NAMES, so every label above for them is a guess")
        print(f"  {', '.join(unnamed)}")
        print("  A scanner name is not identity, and it is not even stable across")
        print("  captures: in this house \"Office\" has meant the kitchen, an alcove and")
        print("  the actual office depending on which capture said it. Run")
        print("  `python -m lidar2ha.rooms` per capture BEFORE combining.")

    if result.naming:
        print("\nWHAT THE UNNAMED ROOMS ARE STANDING ON")
        print("  Suggestions, never decisions -- nothing below is written into the")
        print("  model. Once captures share a frame, identity belongs to the PLACE, so")
        print("  a room can be recognised by what it overlaps rather than by its label.")
        for suggestion in result.naming:
            places = "  ".join(f"{a} {f * 100:.0f}%" for a, f in suggestion.places)
            print(f"  {suggestion.verdict.upper():<11} {suggestion.capture}/"
                  f"{suggestion.room:<14} {suggestion.area_m2:5.1f} m2  "
                  f"{places or 'nothing named under it'}")

    built = result.model.levels[0]
    print(f"\nwalls: {len(built.walls)} kept, {len(result.dropped_walls)} dropped as "
          f"the same wall seen twice")
    print(f"rooms: {len(built.rooms)} accepted, "
          f"{sum(1 for r in built.rooms if r.provisional)} of them flagged")


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("models", nargs="+",
                    help="one model json per capture of the SAME level. Optionally "
                         "id=path, to name a capture something other than its "
                         "filename")
    ap.add_argument("-o", "--out", default="combined.json")
    ap.add_argument("--worklist", default=None,
                    help="where to write the work list json (default: alongside --out)")
    ap.add_argument("--reference", help="capture id to anchor on; default is chosen")
    ap.add_argument("--level", help="which level to take from each capture, when a "
                                    "capture holds more than one")
    ap.add_argument("--max-median-cm", type=float, default=MAX_MEDIAN_CM,
                    help="a fit worse than this is not the same rooms. NOT a coverage "
                         "threshold -- see the module docstring")
    ap.add_argument("--max-p90-cm", type=float, default=MAX_P90_CM,
                    help="the tail matters more than the median: two captures can "
                         "agree at 6 cm median and differ by 44 cm at p90, which on a "
                         "small room is a large fraction of the room")
    ap.add_argument("--edge-containment", type=float, default=EDGE_CONTAINMENT,
                    help="intersection/min(area) at which two rooms are one room")
    args = ap.parse_args()

    models: dict[str, Model] = {}
    for item in args.models:
        name, _, path = item.rpartition("=")
        path = path or item
        if not name:
            name = Path(path).stem
            for suffix in ("_registered", "_named", "_split"):
                name = name.removesuffix(suffix)
        models[name] = load_model(path)

    try:
        result = combine(models, level_name=args.level, reference=args.reference,
                         max_median_cm=args.max_median_cm, max_p90_cm=args.max_p90_cm,
                         edge=args.edge_containment)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    report(result)
    save_model(result.model, args.out)
    work = Path(args.worklist) if args.worklist \
        else Path(args.out).with_name(Path(args.out).stem + "_worklist.json")
    work.write_text(json.dumps(result.worklist, indent=2), encoding="utf-8")
    print(f"\nwrote {args.out}")
    print(f"wrote {work}  ({len(result.worklist)} thing(s) to do about the house)")


if __name__ == "__main__":
    main()
