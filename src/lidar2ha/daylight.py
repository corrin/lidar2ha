#!/usr/bin/env python3
"""Tell a lit fitting from a window, by differencing two captures.

Brightness alone cannot do it, and that is a fact about the sensor rather than
about the threshold: a bulb and a sunlit pane both saturate it. Detection
therefore finds windows, rooflights, mirrors reflecting a lit fitting, and on
one real run a candle burning on a desk. A ground-level pass returned 38
candidates for perhaps 12-14 real fittings, 25 of them against walls where a
fixture pass should be ceiling-heavy. Reviewing that by eye costs more than the
detection saves.

THE DISCRIMINATOR IS A SECOND CAPTURE, not a cleverer threshold. A window is
bright in EVERY capture; a fitting is bright only in the one taken with the
lights on. So sampling the ordinary capture at the same physical point answers
the question mechanically -- and both inputs already exist for every level that
has a fixture pass, because the ordinary capture is the one the geometry came
from.

THERE ARE THREE ANSWERS, NOT TWO, and collapsing them would be the whole
mistake. An ordinary capture photographs ceilings badly: the camera meters for
the room, and nobody points a phone at a dark ceiling for long. So "no faces
near that point" is extremely common and means the scan never looked, which is
not evidence about the fitting either way. It is reported as `unseen` and
nothing downstream may treat it as a verdict.

    window   faces are there, and they are bright with the lights off too
    fitting  faces are there, and they are dark -- so it was the bulb
    unseen   the ordinary capture has nothing at that point to compare with

Usage is through `placefixtures --daylight-mesh`, which already holds both
registrations needed to put a candidate at a point in this mesh's frame.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from .fixtures import LUMA, load_faces

# A face has to be at least this bright, with the lights off, to convict a
# candidate of being a window. The same floor `fixtures --min-luma` uses, and
# for the same reason: it must not be possible for a dim capture to promote
# grey to bright, because here that would discard a real fitting.
WINDOW_LUMA = 200.0
# How far from the candidate to look, in metres. Comparable to fixtures'
# CLUSTER_M, so it covers about one fitting's worth of surface -- wider starts
# picking up the window next to the lamp and convicting the lamp.
RADIUS_M = 0.30
# Which face decides. The brightest single face is noise; the mean is dragged
# down by the dark surround a window is set into. The p90 asks "is a
# substantial part of what is here bright", which is the actual question.
PERCENTILE = 90.0


@dataclass
class Reference:
    """An ordinary capture, indexed for point queries.

    Holds the mesh's face centroids and their luma, plus a KD-tree over the
    centroids. Built once and reused for every candidate.
    """

    centers: np.ndarray
    luma: np.ndarray
    # scipy ships no stubs worth the noise; the boundary to it is untyped here
    # exactly as it is in registration and placefixtures.
    tree: Any

    @property
    def faces(self) -> int:
        return len(self.luma)


def build_reference(centers: np.ndarray, colours: np.ndarray) -> Reference:
    """Index face centroids and their colours. Separate from `load_reference`
    so the verdict logic can be tested without a mesh file on disk."""
    from scipy.spatial import cKDTree

    luma = colours.astype(float) @ LUMA
    return Reference(centers=np.asarray(centers, dtype=float), luma=luma,
                     tree=cKDTree(centers))


def load_reference(mesh_path: str) -> Reference:
    """Read an ordinary capture's mesh and index it.

    Reuses `fixtures.load_faces`, so the atlas sampling and the luma weights
    are identical on both sides of the difference. Sampling them two different
    ways would make the comparison meaningless in a way nothing would report.
    """
    centers, _normals, colours, _ids, _xy, _atlases = load_faces(mesh_path)
    return build_reference(centers, colours)


def verdict_at(reference: Reference, point_m: np.ndarray,
               radius_m: float = RADIUS_M,
               cutoff: float = WINDOW_LUMA) -> tuple[str, float | None, int]:
    """("window" | "fitting" | "unseen", the p90 luma, how many faces were near).

    The luma is returned even for a "fitting" so a human can see how close the
    call was, and `None` only when there was nothing to measure.
    """
    near = reference.tree.query_ball_point(np.asarray(point_m, dtype=float), radius_m)
    if not near:
        return "unseen", None, 0
    bright = float(np.percentile(reference.luma[near], PERCENTILE))
    return ("window" if bright >= cutoff else "fitting"), bright, len(near)


def summarise(verdicts: list[str]) -> dict[str, int]:
    """Counts per verdict, with every key present even at zero.

    A missing key reads as "none of those" when it usually means the caller
    forgot to ask, and `unseen` at zero is itself worth seeing -- it says the
    ordinary capture covered every candidate, which is unusual.
    """
    return {kind: verdicts.count(kind) for kind in ("fitting", "window", "unseen")}
