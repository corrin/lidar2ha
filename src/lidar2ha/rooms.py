#!/usr/bin/env python3
"""Give rooms their Home Assistant identity, and merge open-plan splits.

The scanner names rooms by guessing, and splits them wherever its own
segmentation happened to land. Neither is identity. A capture of one kitchen
came back as "Kitchen" plus "Office 1"; an entrance hall came back as "Living
Room" and "Dining Room". So room names are discarded and replaced by Home
Assistant area ids, which the human confirms once.

Two operations, and they are all that open plan needs here:

  rename  scanner room -> HA area. Many scanner rooms may map to one area.
  merge   named groups of scanner rooms are unioned into a single polygon,
          because an open volume the scanner split in two is one room.

Merging is a real union, not a bounding box: the shared edge dissolves, so the
result is the actual outline of the combined space. Where the union leaves a
sliver or a hole -- which happens when two scanner polygons abut imperfectly --
the largest resulting piece wins and the rest is reported rather than silently
dropped.

Usage:
    python -m lidar2ha.rooms model.json project.yaml -o named.json --capture upstairs
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

import yaml
from shapely.geometry import Polygon
from shapely.ops import unary_union

from .schema import Level, Model, Room, load_model, save_model

CM2_TO_M2 = 1e-4

# How much of the smaller room has to be buried before `covered_rooms` says so.
# Adjacent rooms share a wall and, once two captures have been fitted onto one
# another, share a centimetre or two of floor with it -- so a report on any
# intersection at all would fire on every neighbouring pair and mean nothing.
#
# Measured over three storeys of one house: the pairs a person picked out by
# eye read 22% and 49% of the smaller room, and every pair nobody minded read
# 5.6% or less. Both halves of the test have to pass, because share alone fires
# on a sliver -- a 0.3 m2 scanner artefact is 40% covered by whatever it sits
# on, and there is no room there to lose.
COVERED_SHARE = 0.10
COVERED_MIN_M2 = 0.10


def polygon_of(room: Room) -> Polygon:
    return Polygon([(p[0], p[1]) for p in room.points])


@dataclass(frozen=True)
class Placed:
    """One room polygon that has reached a model, and what to call it."""

    label: str
    where: str      # the capture it came from, or the room it was cut out of
    poly: Polygon

    @property
    def area_m2(self) -> float:
        return float(self.poly.area) * CM2_TO_M2


def covered_rooms(placed: list[Placed], *,
                  min_share: float = COVERED_SHARE,
                  min_area_m2: float = COVERED_MIN_M2) -> list[dict[str, Any]]:
    """Rooms in one model that lie on top of each other, worst first.

    A ROOM THAT IS PRESENT BUT COVERED IS WORSE THAN ONE THAT IS MISSING. It
    renders underneath, it keeps its Home Assistant area, and every audit that
    counts named areas reports the level as complete -- so all the usual
    signals agree that nothing is wrong. Found four times on one real house in
    a day, every one of them by a person looking at a picture:

        a 1.4 m2 toilet sitting entirely inside a hallway polygon
        a 1.0 m2 cellar swallowed as a lobe of the stairwell
        3.6 m2 of kitchen standing on hallway and stairwell
        4.0 m2 of a hallway that was really the stairwell opening

    Two of those four are one polygon covering ground that is not its floor,
    with no second room to overlap -- this cannot see those and is not the
    check for them. That is a room-against-FLOOR question, and the measurement
    that separates it is whether the mesh has geometry BELOW the room, not how
    much floor the room has: an ordinary bedroom scanned from above reads 45%
    floor and a stairwell reads 15%.

    Reported and never resolved. Which room is right is a question about the
    house -- an island, a mezzanine and a mis-drawn wall look identical from
    here -- so preferring one would be the silent pick these stages exist to
    refuse.
    """
    items: list[dict[str, Any]] = []
    for i, a in enumerate(placed):
        for b in placed[i + 1:]:
            if not a.poly.intersects(b.poly):
                continue
            shared_m2 = float(a.poly.intersection(b.poly).area) * CM2_TO_M2
            if shared_m2 <= 0:
                continue
            # Share of the SMALLER room, because that is the one at risk of
            # disappearing. Against the larger, a swallowed cupboard is a
            # rounding error -- a 1.4 m2 toilet is 8% of its hallway and 100%
            # of itself, and only the second number says a room is going.
            smaller, larger = ((a, b) if a.area_m2 <= b.area_m2 else (b, a))
            share = shared_m2 / smaller.area_m2 if smaller.area_m2 else 0.0
            if share < min_share or shared_m2 < min_area_m2:
                continue
            items.append({
                "kind": "rooms_overlap",
                "covered": smaller.label,
                "covered_capture": smaller.where,
                "covered_area_m2": round(smaller.area_m2, 2),
                "covering": larger.label,
                "covering_capture": larger.where,
                "overlap_m2": round(shared_m2, 2),
                "share_of_covered": round(share, 3),
                "reasons": [
                    f"{shared_m2:.2f} m2 of {smaller.label} lies inside "
                    f"{larger.label} -- {share * 100:.0f}% of it. Both are in the "
                    f"model, so the smaller one draws underneath and an audit "
                    f"counting named areas cannot see that it is buried. Decide "
                    f"which is right: one may be an island or an opening the other "
                    f"should have a hole for, or a wall may be in the wrong place"],
            })
    items.sort(key=lambda it: -float(it["share_of_covered"]))
    return items


class Crossed(NamedTuple):
    """A merge that spanned more than one of a capture's ceiling bands."""

    survivor: str
    bands: tuple[str, ...]
    onto: str


class Applied(NamedTuple):
    """What `apply` did, including everything it declined to do."""

    renamed: int
    merged: int
    unmapped: list[str]
    crossed: list[Crossed]
    emptied: list[str]
    unapplied: list[list[str]]


class CannotMerge(ValueError):
    """A `merge:` declaration this stage will not carry out.

    Its own type, so `main` can turn it into a sentence without also catching
    a shapely `ValueError` from a degenerate polygon and reporting a broken
    room as a bad declaration.
    """


def band_groups(model: Model) -> list[list[Level]]:
    """The capture's levels grouped by the frame they share, lowest band first.

    `polycam` cuts one Polycam level into ceiling bands, and those come out of
    ONE sheet cluster -- so a room the split separated can be unioned back.
    Polycam's own levels cannot: they are separate clusters with separate
    origins, and on one house two of them fit the same reference 17.36 m apart.

    A level with no `from_level` is its own group. That is every capture
    written before the field existed and every capture whose storeys each sit
    at one ceiling height -- 17 of 19 on the real project -- and it is what
    makes the second pass below unreachable for them.

    Order inside a group is file order, which `split_into_storeys` emits lowest
    band first, so "the lowest band" needs no name parsed back apart.
    """
    groups: list[list[Level]] = []
    by_origin: dict[str, list[Level]] = {}
    for lv in model.levels:
        if lv.from_level is None:
            groups.append([lv])
            continue
        if lv.from_level not in by_origin:
            by_origin[lv.from_level] = []
            groups.append(by_origin[lv.from_level])
        by_origin[lv.from_level].append(lv)
    return groups


# A merged union that comes out in pieces is two rooms that share no edge, and
# `merge:` exists to dissolve a shared edge. Below this, the pieces are the
# couple of centimetres two scanned polygons leave along a wall they both drew;
# above it, they are separate floor and the declaration is refused rather than
# approximated. Measured: no merge declared on the real house produces pieces at
# all. What would move it is a capture whose abutting rooms routinely leave a
# larger gap -- which would mean the scan, not the declaration, is the problem.
SLIVER_M2 = 0.5

_OWN_LEVEL = "nothing -- a Polycam level of its own"


def _union_of(members: list[Room], names: list[str], *,
              sliver_m2: float = SLIVER_M2) -> Room:
    """Union the members onto the first; the survivor takes the extremes."""
    union = unary_union([polygon_of(m) for m in members])
    if union.geom_type == "MultiPolygon":
        parts = sorted(union.geoms, key=lambda g: g.area, reverse=True)
        dropped = sum(g.area for g in parts[1:]) * CM2_TO_M2
        if dropped > sliver_m2:
            raise CannotMerge(
                f"merge {names} leaves {len(parts)} separate pieces -- "
                f"{', '.join(f'{g.area * CM2_TO_M2:.1f} m2' for g in parts)} -- "
                f"so those rooms share no edge and are not one room. Keeping "
                f"the largest would throw {dropped:.1f} m2 of floor away. Check "
                f"the names: where a label repeats on this level, the one that "
                f"merges is the last of them")
        # A sliver along a shared wall, which is registration noise rather than
        # a discovery. Named, because it is still floor going.
        print(f"  note: merging {names} left {len(parts)} pieces; keeping the "
              f"largest, dropping {dropped:.2f} m2")
        union = parts[0]

    survivor = members[0]
    survivor.points = [(x, y) for x, y in union.exterior.coords[:-1]]
    # A merged open volume is as tall as its tallest part. Heights are
    # optional, so a group where nobody knows its ceiling stays unknown
    # rather than becoming zero.
    highs = [m.ceiling_high_cm for m in members if m.ceiling_high_cm is not None]
    lows = [m.ceiling_low_cm for m in members if m.ceiling_low_cm is not None]
    high = max(highs) if highs else None
    low = min(lows) if lows else None
    survivor.ceiling_high_cm = high
    survivor.ceiling_low_cm = low
    survivor.sloped = high is not None and low is not None and high - low > 15
    # THE ROOMS ACTUALLY UNIONED, not the declaration. Recording the group made
    # provenance assert a union that never happened.
    survivor.merged_from = list(names)
    return survivor


def parse_merges(raw) -> list[list[str]]:
    """The `merge:` entry for one capture, validated at the boundary.

    Unvalidated, a forgotten dash -- `cap: ["Kitchen", "Office 1"]` -- iterated
    the strings and produced groups of single CHARACTERS, which merged nothing
    and exited 0. `projectlevels.parse_entries` refuses the same shape for the
    same reason.
    """
    if raw is None:
        return []
    if isinstance(raw, (str, bytes)) or not isinstance(raw, (list, tuple)):
        raise CannotMerge(
            f"`merge:` for a capture takes a LIST of groups, got {raw!r}. Each "
            f"group is itself a list of the scanner room names to join")
    out = []
    for group in raw:
        if isinstance(group, (str, bytes)) or not isinstance(group, (list, tuple)):
            raise CannotMerge(
                f"a `merge:` group takes a list of scanner room names, got "
                f"{group!r}. Write it as - [\"Kitchen\", \"Office 1\"]")
        bad = [n for n in group if not isinstance(n, str) or not n.strip()]
        if bad:
            raise CannotMerge(
                f"a `merge:` group names something that is not a room name: "
                f"{bad[0]!r}")
        out.append([str(n) for n in group])
    return out


def _held_per_group(names: list[str], groups: list[list[Level]]) -> list[int]:
    """How many of the declaration's rooms each band group holds."""
    return [sum(1 for n in names
                if any(r.name == n for lv in levels for r in lv.rooms))
            for levels in groups]


def _refusals(merges: list[list[str]], groups: list[list[Level]]) -> list[str]:
    """Every declaration that cannot be carried out, decided before anything moves.

    TWO KINDS, and both are answered against the model as it arrived, so the
    answer cannot depend on the order the lines were written or on what an
    earlier line already consumed.
    """
    out = []
    seen: dict[str, int] = {}
    for gi, group in enumerate(merges):
        for name in group:
            if name in seen and seen[name] != gi:
                out.append(
                    f"{name!r} is named by two `merge:` groups, {merges[seen[name]]} "
                    f"and {group}. One scanner room is one piece of floor and can "
                    f"only be glued into one place -- if all of them are one space, "
                    f"write a single group naming them all")
            seen.setdefault(name, gi)

        # THE FRAME THAT HOLDS MOST OF THEM does the gluing. A name that
        # exists somewhere else but NOT there cannot be joined to the rest --
        # separate clusters have separate origins, so the union would span the
        # gap between them. A name that exists nowhere is a misspelling, and
        # the answer to that is the `unapplied` report, not a refusal: guarding
        # on the bare collision refused 376 of 3000 ordinary captures, because
        # Polycam repeats room labels across storeys as a matter of course.
        held = _held_per_group(group, groups)
        best = max(range(len(groups)), key=lambda j: held[j], default=None)
        if best is None or not held[best]:
            continue
        home = {r.name for lv in groups[best] for r in lv.rooms}
        elsewhere = [n for n in group if n not in home
                     and any(r.name == n for g in groups for lv in g for r in lv.rooms)]
        if not elsewhere:
            continue
        where = "; ".join(
            f"{n!r} on "
            f"{[lv.name for g in groups for lv in g for r in lv.rooms if r.name == n][0]!r}"
            for n in elsewhere)
        out.append(
            f"merge {group} names {where}, which is not on the level its other "
            f"rooms are on. Ceiling bands of one Polycam level can be glued "
            f"because they were cut from one sheet, but Polycam's own levels "
            f"are separate clusters with separate origins -- unioning across "
            f"them makes one polygon spanning the gap between them. That case "
            f"is decided in `combine`, between captures, not here")
    return out


def apply(model: Model, mapping: dict, merges: list, *,
          sliver_m2: float = SLIVER_M2) -> Applied:
    """Rename and merge in place, saying what was refused as well as done.

    TWO PASSES, AND THE SECOND CANNOT REACH AN ORDINARY CAPTURE. The first is
    the per-level merge as it was before ceiling bands existed; the second joins
    rooms the band split separated, and runs only where a band GROUP holds more
    than one level -- which happens only when `polycam` split a cluster. A
    capture it did not split has one level per group, so nothing it does can
    change. That is a property of the shape rather than of the tests, and three
    regressions on ordinary captures came from rewriting the shared path.

    ATOMIC. Everything happens on a copy and is committed at the end, so a
    declaration refused halfway leaves the caller's model as it found it.

    THE BAND A ROOM LEAVES IS NOT DELETED. Its walls and doors stay where
    `polycam` put them and the level keeps its place, because `textures_*` runs
    BEFORE this stage and `scene.py` reads its manifest by POSITION -- (index of
    level, index of wall). Removing a level renumbers every level after it, so a
    rectified photo either paints the wrong wall or vanishes from `scene.tsv`
    without a word.
    """
    merges = parse_merges(merges)
    work = model.model_copy(deep=True)
    groups = band_groups(work)

    refusals = _refusals(merges, groups)
    if refusals:
        raise CannotMerge("\n".join(refusals))

    renamed, merged = 0, 0
    unmapped: list[str] = []
    crossed: list[Crossed] = []
    emptied: list[str] = []
    applied: set[int] = set()
    fused: set[int] = set()
    populated = {id(lv) for lv in work.levels if lv.rooms}
    started = Counter(str(r.name) for lv in work.levels for r in lv.rooms)

    for levels in groups:
        here: set[int] = set()

        # --- pass 1: inside one level ---------------------------------------
        for lv in levels:
            # Last wins, which is what this map has always done.
            by_scanner = {str(r.name): r for r in lv.rooms if r.name}
            # BY IDENTITY, and the map follows the model. Consumed by NAME, a
            # room on another band sharing a scanner name was deleted with it.
            consumed: set[int] = set()
            for gi, group in enumerate(merges):
                names = [n for n in group if n in by_scanner]
                members = [by_scanner[n] for n in names]
                if len(members) < 2:
                    continue
                survivor = _union_of(members, names, sliver_m2=sliver_m2)
                for name, room in zip(names, members, strict=True):
                    if room is not survivor:
                        consumed.add(id(room))
                        if by_scanner.get(name) is room:
                            del by_scanner[name]
                by_scanner[str(survivor.name)] = survivor
                fused.add(id(survivor))
                here.add(gi)
                applied.add(gi)
                merged += 1
            lv.rooms = [r for r in lv.rooms if id(r) not in consumed]

        # --- pass 2: across the bands of THIS cluster ------------------------
        # Reached only where one cluster produced several bands. Tried for every
        # cluster, because a declaration glued inside one level may still have
        # to be glued across the bands of another -- keyed globally it fired
        # once and left every other cluster split, silently.
        if len(levels) < 2:
            continue
        for gi, group in enumerate(merges):
            if gi in here:
                continue
            placed = {str(r.name): (lv, r) for lv in levels for r in lv.rooms if r.name}
            names = [n for n in group if n in placed]
            if len(names) < 2:
                continue
            paired = [placed[n] for n in names]

            survivor = _union_of([r for _, r in paired], names, sliver_m2=sliver_m2)
            was = next(lv for lv, r in paired if r is survivor)
            # THE LOWEST BAND, which is what `polycam` does with a shaft: a
            # volume spanning bands belongs at the bottom of it.
            target = min((lv for lv, _ in paired), key=levels.index)
            if survivor.ceiling_high_cm is not None and \
                    target.ceiling_height_cm < survivor.ceiling_high_cm:
                target.ceiling_height_cm = survivor.ceiling_high_cm
            if was is not target:
                # Off the old band BEFORE it goes on the new one, or the same
                # Room sits on two levels: saved twice, drawn twice, and -- the
                # plugin sums sources sharing a name -- lit twice.
                was.rooms = [r for r in was.rooms if r is not survivor]
                target.rooms.append(survivor)
            gone = {id(r) for _, r in paired if r is not survivor}
            for lv in levels:
                lv.rooms = [r for r in lv.rooms if id(r) not in gone]

            crossed.append(Crossed(
                str(survivor.name),
                tuple(dict.fromkeys(lv.name for lv in sorted(
                    (lv for lv, _ in paired), key=levels.index))),
                target.name))
            fused.add(id(survivor))
            applied.add(gi)
            merged += 1

    for lv in work.levels:
        if id(lv) in populated and not lv.rooms:
            emptied.append(lv.name)
    unapplied = [list(g) for gi, g in enumerate(merges) if gi not in applied]

    # NO ROOM LEAVES WITHOUT SAYING SO. A survivor accounts for the rooms in its
    # `merged_from`, which includes itself; every other room accounts for
    # itself. Counted, because a set of names cannot see the second `Bedroom`
    # go. Three separate bugs here deleted floor in silence and every one was
    # found afterwards by a reader counting square metres; this is the backstop
    # that catches the class rather than each shape of it.
    accounted: Counter[str] = Counter()
    for lv in work.levels:
        for r in lv.rooms:
            accounted.update(r.merged_from if id(r) in fused else [str(r.name)])
    vanished = sorted(n for n, count in started.items() if count > accounted[n])
    if vanished:
        raise CannotMerge(
            f"merging removed {vanished} from the model and nothing surviving "
            f"accounts for {'them' if len(vanished) > 1 else 'it'}. That is "
            f"floor disappearing rather than floor being joined, and it is a "
            f"bug in this stage rather than in your declaration -- please "
            f"report it with the capture")

    for lv in work.levels:
        for r in lv.rooms:
            area = mapping.get(r.name)
            if not area:
                # A room with no name cannot be mapped either, and it is still a
                # room the plan carries -- named so the report can sort them.
                unmapped.append(r.name or "<unnamed>")
                continue
            r.scanner_name = r.name
            r.ha_area = area
            r.name = area
            renamed += 1

    model.levels = work.levels
    return Applied(renamed, merged, unmapped, crossed, emptied, unapplied)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("model")
    ap.add_argument("project")
    ap.add_argument("-o", "--out", default="named.json")
    ap.add_argument("--capture", required=True,
                    help="which capture's mapping to apply, e.g. upstairs")
    args = ap.parse_args()

    model = load_model(args.model)
    project = yaml.safe_load(Path(args.project).read_text(encoding="utf-8")) or {}

    mapping = (project.get("rooms") or {}).get(args.capture) or {}
    merges = (project.get("merge") or {}).get(args.capture) or []
    if not mapping:
        raise SystemExit(f"No rooms mapping for capture {args.capture!r} in {args.project}")

    try:
        done = apply(model, mapping, merges)
    except CannotMerge as bad:
        # A declaration this cannot carry out is the reader's to fix, and a
        # traceback tells them nothing about which line of project.yaml it was.
        raise SystemExit(f"\n{bad}") from None
    save_model(model, args.out)

    print(f"\nwrote {args.out}")
    print(f"  merged {done.merged} group(s), renamed {done.renamed} room(s)")
    for one in done.crossed:
        print(f"  {one.survivor!r} spanned {list(one.bands)}, filed on "
              f"{one.onto!r} -- the lowest band it touches")
    for band in done.emptied:
        print(f"  band {band!r} gave its floor away and now holds walls only; "
              f"it keeps its place in the file")
    for lv in model.levels:
        for r in lv.rooms:
            src = r.merged_from or r.scanner_name
            area = polygon_of(r).area / 10_000
            print(f"    {str(r.name):<16} {area:6.1f} m2   <- {src}")
    if done.unapplied:
        print("\n  MERGES THAT DID NOTHING (fewer than two of their rooms exist "
              "in this capture):")
        for group in done.unapplied:
            print(f"    {group}")
        print("  Check the spelling against the scanner names above. A merge "
              "that matches nothing")
        print("  leaves the open plan split, and the only symptom is a light "
              "bound to half a room.")
    if done.unmapped:
        print(f"\n  UNMAPPED (still carrying scanner names): "
              f"{sorted(set(done.unmapped))}")
        print("  Add them to rooms." + args.capture + " in project.yaml.")


if __name__ == "__main__":
    main()
