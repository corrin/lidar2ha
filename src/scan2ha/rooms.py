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

Run from PowerShell (see CLAUDE.md). Usage:
    python rooms.py model.json project.yaml -o named.json --capture midlevel
"""

from __future__ import annotations

import argparse
from pathlib import Path

import yaml
from shapely.geometry import Polygon
from shapely.ops import unary_union

from .schema import Model, Room, load_model, save_model


def polygon_of(room: Room) -> Polygon:
    return Polygon([(p[0], p[1]) for p in room.points])


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
                    help="which capture's mapping to apply, e.g. midlevel")
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
