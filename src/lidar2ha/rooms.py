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


class Dissolved(NamedTuple):
    """A band whose last room merged away, and where its geometry went."""

    band: str
    onto: str
    walls: int
    doors: int


class Applied(NamedTuple):
    """What `apply` did, including everything it declined to do."""

    renamed: int
    merged: int
    unmapped: list[str]
    crossed: list[Crossed]
    dissolved: list[Dissolved]
    unapplied: list[list[str]]


def band_groups(model: Model) -> list[list[Level]]:
    """The capture's levels grouped by the frame they share, lowest band first.

    `polycam` cuts one Polycam level into ceiling bands, and those come out of
    ONE sheet cluster -- so a room the split separated can be unioned back.
    Polycam's own levels cannot: they are separate clusters with separate
    origins, and on one house two of them fit the same reference 17.36 m apart.

    A level with no `from_level` is its own group, which is every capture
    written before the field existed.

    Order inside a group is file order, which `split_into_storeys` emits lowest
    band first. That is what makes "the lowest band" readable without parsing
    a level name back apart.
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


def _refuse_across_frames(group: list[str], groups: list[list[Level]],
                          spans: list[int]) -> None:
    where = []
    for gi in spans:
        names = [lv.name for lv in groups[gi] for r in lv.rooms if r.name in group]
        where.append(f"{sorted(set(names))} (from {groups[gi][0].from_level or 'no split'})")
    raise ValueError(
        f"merge {group} names rooms on levels that do not share a frame: "
        f"{'; '.join(where)}. Ceiling bands of one Polycam level can be merged "
        f"because they were cut from one sheet, but Polycam's own levels are "
        f"separate clusters with separate origins -- unioning across them makes "
        f"one polygon spanning the gap between them. That case is decided in "
        f"`combine`, between captures, not here")


def _fuse(members: list[tuple[Level, Room]], group: list[str],
          order: list[Level]) -> tuple[Level, Room, bool]:
    """Union the members onto the lowest band. (level, survivor, crossed)."""
    union = unary_union([polygon_of(r) for _, r in members])
    if union.geom_type == "MultiPolygon":
        # Imperfectly abutting polygons: keep the real room, report the rest.
        parts = sorted(union.geoms, key=lambda g: g.area, reverse=True)
        dropped = sum(g.area for g in parts[1:]) / 10_000
        print(f"  note: merging {group} left {len(parts)} disjoint pieces; "
              f"keeping the largest, dropping {dropped:.2f} m2")
        union = parts[0]

    survivor = members[0][1]
    survivor.points = [(x, y) for x, y in union.exterior.coords[:-1]]
    # A merged open volume is as tall as its tallest part. Heights are
    # optional, so a group where nobody knows its ceiling stays unknown
    # rather than becoming zero.
    highs = [r.ceiling_high_cm for _, r in members if r.ceiling_high_cm is not None]
    lows = [r.ceiling_low_cm for _, r in members if r.ceiling_low_cm is not None]
    high = max(highs) if highs else None
    low = min(lows) if lows else None
    survivor.ceiling_high_cm = high
    survivor.ceiling_low_cm = low
    survivor.sloped = high is not None and low is not None and high - low > 15
    survivor.merged_from = list(group)

    # THE LOWEST BAND, which is what `polycam` already does with a shaft: a
    # volume spanning bands belongs at the bottom of it, not in whichever band
    # the declaration happened to name first.
    target = min((lv for lv, _ in members), key=order.index)
    crossed = len({id(lv) for lv, _ in members}) > 1
    if high is not None and target.ceiling_height_cm < high:
        # `polycam` sized the level from the rooms that band held. A room merged
        # up from a taller band is taller than that, and a level shorter than
        # its own room caps the double-height space the merge just restored.
        target.ceiling_height_cm = high
    return target, survivor, crossed


def apply(model: Model, mapping: dict, merges: list) -> Applied:
    """Rename and merge in place, saying what was refused as well as done."""
    renamed, merged = 0, 0
    unmapped: list[str] = []
    crossed: list[Crossed] = []
    dissolved: list[Dissolved] = []
    unapplied: list[list[str]] = []

    groups = band_groups(model)
    # Where each scanner name lives across the WHOLE capture. A merge is
    # answered against this rather than against one group, so naming rooms in
    # two frames is refused instead of quietly applying to neither.
    home: dict[str, set[int]] = {}
    for gi, levels in enumerate(groups):
        for lv in levels:
            for r in lv.rooms:
                if r.name:
                    home.setdefault(r.name, set()).add(gi)

    for group in merges:
        spans = sorted({gi for n in group for gi in home.get(n, set())})
        if len(spans) > 1:
            _refuse_across_frames(group, groups, spans)
        if sum(1 for n in group if n in home) < 2:
            # A typo in `merge:` used to do nothing and say nothing: the rooms
            # stay separate, the open plan stays split, and the only symptom is
            # a lighting group that binds to half a room.
            unapplied.append(list(group))

    for levels in groups:
        by_scanner: dict[str, tuple[Level, Room]] = {}
        for lv in levels:
            for r in lv.rooms:
                if r.name:
                    by_scanner.setdefault(r.name, (lv, r))

        consumed: set[str] = set()
        emptied: dict[int, Level] = {}
        for group in merges:
            members = [by_scanner[n] for n in group if n in by_scanner]
            if len(members) < 2:
                continue
            target, survivor, spanned = _fuse(members, group, levels)
            was = next(lv for lv, r in members if r is survivor)
            if was is not target:
                was.rooms = [r for r in was.rooms if r is not survivor]
                target.rooms.append(survivor)
                emptied.setdefault(id(was), target)
            for lv, room in members:
                if room is not survivor:
                    emptied.setdefault(id(lv), target)
            if spanned:
                crossed.append(Crossed(str(survivor.name),
                                       tuple(dict.fromkeys(lv.name for lv, _ in members)),
                                       target.name))
            consumed.update(n for n in group if n != survivor.name)
            merged += 1

        for lv in levels:
            lv.rooms = [r for r in lv.rooms if r.name not in consumed]

        # A band whose last room merged away still holds the walls `polycam`
        # gave it, and those run along the part of the room that moved.
        # Dropping the level with them loses geometry nothing else has.
        for lv in list(levels):
            onto = emptied.get(id(lv))
            if onto is None or lv is onto or lv.rooms:
                continue
            # A line the target already has keeps the TALLER of the two, for
            # `_build_level`'s own reason: too tall merely hides behind the
            # ceiling, too short leaves a gap light leaks through.
            standing = {_wall_key(w): w for w in onto.walls}
            moved = []
            for w in lv.walls:
                already = standing.get(_wall_key(w))
                if already is None:
                    standing[_wall_key(w)] = w
                    moved.append(w)
                elif w.height > already.height:
                    already.height = w.height
            onto.walls.extend(moved)
            doors = list(lv.doors)
            onto.doors.extend(doors)
            dissolved.append(Dissolved(lv.name, onto.name, len(moved), len(doors)))
            model.levels.remove(lv)

    for lv in model.levels:
        for r in lv.rooms:
            area = mapping.get(r.name)
            if not area:
                # A room with no name at all cannot be mapped either, and it is
                # still a room the plan will carry -- named here so the report
                # can sort them together rather than raising on a None.
                unmapped.append(r.name or "<unnamed>")
                continue
            r.scanner_name = r.name
            r.ha_area = area
            r.name = area
            renamed += 1

    return Applied(renamed, merged, unmapped, crossed, dissolved, unapplied)


def _wall_key(w) -> tuple:
    """A wall by its LINE, endpoint order included, and by nothing else.

    Wall assignment across bands is deliberately NOT exclusive -- the building's
    envelope belongs to every storey -- so the same wall really does arrive
    twice, and on the real capture 6 of one band's 11 walls were a line the
    target already had, differing only in height.

    Height is not part of identity here. Two walls on one line are one wall in
    plan, and keeping both puts two sampled points on every centimetre of it --
    which is the trap `combine` already records for its averaged walls: a denser
    line shrinks every distance measured to it, so the fit reads better than the
    capture is.
    """
    a, b = (w.x_start, w.y_start), (w.x_end, w.y_end)
    return (a, b) if a <= b else (b, a)


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

    done = apply(model, mapping, merges)
    save_model(model, args.out)

    print(f"\nwrote {args.out}")
    print(f"  merged {done.merged} group(s), renamed {done.renamed} room(s)")
    for one in done.crossed:
        print(f"  {one.survivor!r} spanned {list(one.bands)}, filed on "
              f"{one.onto!r} -- the lowest band it touches")
    for band in done.dissolved:
        print(f"  band {band.band!r} has no floor left; its {band.walls} wall(s) "
              f"and {band.doors} door(s) moved to {band.onto!r}")
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
