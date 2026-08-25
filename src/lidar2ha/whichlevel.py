#!/usr/bin/env python3
"""Ask which storey a capture is of, instead of already knowing.

Every other stage requires the answer up front. `project.yaml` will not let
`combine` look at a capture until its level is declared, and `compare` only
answers the question you already aimed it at -- so identifying one capture
across three storeys means running it by hand, once per storey, and reading the
numbers side by side. One real table took nine invocations to assemble.

THE ERROR DECIDES. Measured on one house, a capture reads 0.0-0.9 cm against its
own storey and 20-35 cm against the others, so the separation is enormous and
nothing subtle is needed to see it.

COVERAGE IS REPORTED AND NEVER REFUSES. It is the fraction of the CAPTURE a
storey explains, so a capture that sees a room the storey does not always scores
lower -- `combine` learned that the expensive way, where a 90% threshold
rejected the one capture containing a mid-level bathroom, at 88%, which was the
very room worth having. A thin fit is said out loud beside the number that
decided, and left to the reader.

So this refuses on the ERROR. A ranking with nothing at the top of it is the
failure mode worth avoiding: it invites the reader to take the least bad row,
which for a capture of a building nobody has declared is a confident wrong
answer.

Usage:
    python -m lidar2ha.whichlevel capture.json --against ground.json mid.json
    python -m lidar2ha.whichlevel capture.json --project project.yaml
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from pathlib import Path

from .compare import plan_fit
from .schema import Model, load_model

# A fit at or under this is the same storey. `combine.MAX_MEDIAN_CM` draws the
# same line for the same reason and this deliberately matches it -- a capture
# this identifies is one `combine` will then accept.
SAME_LEVEL_CM = 5.0
# Coverage this low is worth SAYING and is never grounds for refusing. It is the
# fraction of the CAPTURE the storey explains, so a capture that sees a room the
# storey does not always scores lower -- `combine` learned that the expensive
# way, where a 90% threshold rejected the one capture containing a mid-level
# bathroom, at 88%, which was the very room worth having. Reported beside the
# error so a reader can weigh it; the error is what decides.
LOW_COVERAGE = 0.90
# How much better the best must be than the next, as a ratio AND in centimetres.
# Measured, a capture on its own storey reads 0.0-0.9 cm where the next reads
# 20-35, so a real answer clears both by a mile. The ratio alone is not enough:
# an exact fit reads 0.0 cm, and every multiple of zero is zero, so two
# indistinguishable storeys would both pass it and the first would be picked
# arbitrarily.
MARGIN = 2.0
MIN_GAP_CM = 2.0


@dataclass
class Candidate:
    """One storey this capture was tried against."""

    level: str
    median_cm: float
    coverage: float
    matched: int
    sampled: int


@dataclass
class Answer:
    """Which storey, or why the question could not be answered."""

    verdict: str            # "identified" | "ambiguous" | "none"
    level: str | None
    reason: str
    ranked: list[Candidate]
    # Levels that could not be fitted at all, and why. A level missing from the
    # ranking without explanation looks like one that was tried and lost.
    unfittable: list[str] = field(default_factory=list)


def rank(capture: Model, levels: dict[str, Model], *,
         same_level_cm: float = SAME_LEVEL_CM,
         low_coverage: float = LOW_COVERAGE,
         margin: float = MARGIN,
         min_gap_cm: float = MIN_GAP_CM) -> Answer:
    """Fit the capture onto every declared storey and say which one it is.

    THREE ANSWERS. `identified` is one storey clearly better than the rest;
    `ambiguous` is two the capture cannot separate, which is what a capture
    holding rooms from both looks like; `none` is a capture that fits nothing,
    which is a refusal and not a ranking with a weak winner at the top.

    THE ERROR DECIDES AND COVERAGE IS REPORTED. Coverage is the fraction of the
    CAPTURE a storey explains, so a capture seeing a room the storey does not
    always scores lower -- refusing on it discards exactly the captures worth
    having. It is said out loud instead, beside the number that did decide.
    """
    ranked: list[Candidate] = []
    unfittable: list[str] = []
    for name, level in levels.items():
        try:
            fit = plan_fit(capture, level)
        except ValueError as exc:
            # Named, not skipped. A level quietly absent from the ranking looks
            # exactly like one that was tried and lost.
            unfittable.append(f"{name} ({exc})")
            continue
        ranked.append(Candidate(
            level=name,
            median_cm=fit["median_error_m"] * 100,
            coverage=fit["coverage"],
            matched=fit["matched"],
            sampled=fit["sampled"],
        ))
    ranked.sort(key=lambda c: (c.median_cm, -c.coverage))
    aside = (f". Not comparable at all: {', '.join(unfittable)}"
             if unfittable else "")

    if not ranked:
        return Answer("none", None,
                      "nothing could be compared -- the capture or every level "
                      f"given has no walls to fit{aside}", ranked, unfittable)

    best = ranked[0]
    if best.median_cm > same_level_cm:
        return Answer("none", None,
                      f"the closest is {best.level!r} at {best.median_cm:.1f} cm, "
                      f"over the {same_level_cm:.0f} cm a capture of a storey "
                      f"reads against its own. This is a capture of somewhere "
                      f"not declared here, or one no fit can place{aside}",
                      ranked, unfittable)

    # SEPARATED ON BOTH, or the two storeys are one answer. A ratio alone fails
    # at an exact fit, where every multiple of 0.0 cm is 0.0 and the first row
    # would be picked arbitrarily.
    runner = next((c for c in ranked[1:] if c.level != best.level), None)
    if runner is not None and not (runner.median_cm >= best.median_cm * margin
                                   and runner.median_cm - best.median_cm >= min_gap_cm):
        return Answer("ambiguous", None,
                      f"{best.level!r} at {best.median_cm:.1f} cm and "
                      f"{runner.level!r} at {runner.median_cm:.1f} cm are too "
                      f"close to choose between. A capture holding rooms from "
                      f"two storeys reads exactly like this -- see `polycam`, "
                      f"which splits one on its ceiling bands{aside}",
                      ranked, unfittable)

    thin = (f", though over only {best.coverage * 100:.0f}% of the capture -- "
            f"the rest of it matched nothing there, which is what a capture "
            f"holding ground the storey does not have looks like"
            if best.coverage < low_coverage else
            f" over {best.coverage * 100:.0f}% of the capture")
    return Answer("identified", best.level,
                  f"{best.level!r} at {best.median_cm:.1f} cm{thin}{aside}",
                  ranked, unfittable)


def levels_from_project(project_path: Path) -> dict[str, Model]:
    """The combined model per level named in project.yaml, where one exists.

    A level with no combined model yet is skipped rather than guessed at -- the
    question is which of the storeys you have this capture matches, and a
    storey you have not built cannot answer it.
    """
    import yaml

    root = Path(project_path).parent
    project = yaml.safe_load(Path(project_path).read_text(encoding="utf-8")) or {}
    out: dict[str, Model] = {}
    for name in (project.get("levels") or {}):
        slug = str(name).lower().replace(" ", "_")
        for candidate in (root / "exports" / f"{slug}_combined.json",
                          root / "exports" / f"{slug}.json"):
            if candidate.is_file():
                out[str(name)] = load_model(candidate)
                break
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("capture")
    ap.add_argument("--against", nargs="*", default=[],
                    help="combined model per storey; the file name is the label")
    ap.add_argument("--project", help="project.yaml, to find them by level name")
    ap.add_argument("--same-level-cm", type=float, default=SAME_LEVEL_CM)
    ap.add_argument("--low-coverage", type=float, default=LOW_COVERAGE,
                    help="below this, coverage is reported as thin -- it is "
                         "never a reason to refuse")
    args = ap.parse_args()

    levels: dict[str, Model] = {}
    if args.project:
        levels.update(levels_from_project(Path(args.project)))
    for path in args.against:
        levels[Path(path).stem] = load_model(path)
    if not levels:
        raise SystemExit(
            "Nothing to compare against. Pass --against with a combined model "
            "per storey, or --project to find them by level name.")

    capture = load_model(args.capture)
    print(f"{Path(args.capture).name}  ({len(capture.levels)} level(s))\n")

    for level in capture.levels:
        one = capture.model_copy(update={"levels": [level]})
        answer = rank(one, levels, same_level_cm=args.same_level_cm,
                      low_coverage=args.low_coverage)
        print(f"  {level.name}  ({len(level.walls)} walls, {len(level.rooms)} rooms)")
        for c in answer.ranked:
            mark = "  <--" if c.level == answer.level else ""
            print(f"      {c.level:<28}{c.median_cm:7.1f} cm  "
                  f"{c.coverage * 100:3.0f}% of {c.sampled:,} points{mark}")
        print(f"      {answer.verdict.upper()}: {answer.reason}\n")


if __name__ == "__main__":
    main()
