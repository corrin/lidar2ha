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
from typing import Any

import yaml
from shapely.geometry import Polygon
from shapely.ops import unary_union

from .schema import Model, Room, load_model, save_model

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


def apply(model: Model, mapping: dict, merges: list) -> tuple[int, int, list]:
    """Rename and merge in place. Returns (renamed, merged, unmapped)."""
    renamed, merged, unmapped = 0, 0, []

    for lv in model.levels:
        by_scanner = {r.name: r for r in lv.rooms if r.name}

        # --- merge first, so the survivor carries one area name ---------------
        consumed: set[str] = set()
        for group in merges:
            members = [by_scanner[n] for n in group if n in by_scanner]
            if len(members) < 2:
                continue
            union = unary_union([polygon_of(m) for m in members])
            if union.geom_type == "MultiPolygon":
                # Imperfectly abutting polygons: keep the real room, report the rest.
                parts = sorted(union.geoms, key=lambda g: g.area, reverse=True)
                dropped = sum(g.area for g in parts[1:]) / 10_000
                print(f"  note: merging {group} left {len(parts)} disjoint pieces; "
                      f"keeping the largest, dropping {dropped:.2f} m2")
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
            survivor.merged_from = list(group)
            consumed.update(n for n in group if n != survivor.name)
            merged += 1

        lv.rooms = [r for r in lv.rooms if r.name not in consumed]

        # --- then rename ------------------------------------------------------
        for r in lv.rooms:
            area = mapping.get(r.name)
            if not area:
                unmapped.append(r.name)
                continue
            r.scanner_name = r.name
            r.ha_area = area
            r.name = area
            renamed += 1

    return renamed, merged, unmapped


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

    renamed, merged, unmapped = apply(model, mapping, merges)
    save_model(model, args.out)

    print(f"\nwrote {args.out}")
    print(f"  merged {merged} group(s), renamed {renamed} room(s)")
    for lv in model.levels:
        for r in lv.rooms:
            src = r.merged_from or r.scanner_name
            area = polygon_of(r).area / 10_000
            print(f"    {str(r.name):<16} {area:6.1f} m2   <- {src}")
    if unmapped:
        print(f"\n  UNMAPPED (still carrying scanner names): {sorted(set(unmapped))}")
        print("  Add them to rooms." + args.capture + " in project.yaml.")


if __name__ == "__main__":
    main()
