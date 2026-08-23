#!/usr/bin/env python3
"""Put each Home Assistant light entity somewhere sensible in its room.

The join is the easy part and it already exists: `rooms` writes an `area_id` onto
every Room as `ha_area`, so an entity's area resolves to a polygon and thence to
a level. Everything hard here is about being honest when it does not resolve.

THE POLE, NOT THE CENTROID. A room's centroid can lie outside the room -- for an
L-shaped living space it usually does, which would hang the light in the garden.
The pole of inaccessibility is the point furthest from any wall, which is both
inside the polygon by construction and roughly where a ceiling rose goes.

This is a GUESS, and the model says so. A real fitting's position is in the scan:
the mesh holds a small cluster below the ceiling plane and the atlas holds it
much brighter than the ceiling around it. When a fittings file supplies real
positions they are used in order and the guess only covers the remainder.

ONE ENTITY CAN NEED SEVERAL PLACEMENTS. Home Assistant gives an area exactly one
floor, and a real space need not have one -- a stairwell filed under the ground
floor may physically span three, with a fitting on each landing. The plugin sums
sources sharing a name, so N placements carrying one entity_id is not a
workaround, it is the correct representation of one switch driving N bulbs.

Nothing is silently dropped. An entity whose area has no room, a room nobody
lights, an entity excluded by hand -- each is counted and named, because a dark
room in the final render is otherwise a mystery with no thread to pull.

Usage:
    python -m lidar2ha.lights model.json registry.json -o lights.json \\
        --project project.yaml --report
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass, field
from pathlib import Path

from shapely.geometry import Point, Polygon
from shapely.ops import polylabel

from .ha import (
    LightEntity,
    classify,
    coordinator_groups,
    light_entities,
    load_registry,
    redundant_groups,
)
from .rooms import polygon_of
from .schema import Level, Light, Model, Room, load_model

# How far under the ceiling a fitting hangs. 20 cm matches examples/minimal.tsv
# (230 under a 250 ceiling) and is about right for a flush fitting or a short rose.
DROP_CM = 20.0
# Ring radius as a fraction of the room's clearance, when spreading several
# lights. Well inside the walls, so a spread light does not end up in a doorway.
SPREAD_FRACTION = 0.55
# polylabel's precision, in centimetres. Finer than this is invisible in a plan.
POLE_TOLERANCE_CM = 5.0
MIN_ELEVATION_CM = 10.0


@dataclass
class LightsConfig:
    """The human's corrections, from project.yaml."""

    exclude: set[str] = field(default_factory=set)
    include: set[str] = field(default_factory=set)
    # entity_id -> additional area ids to place it in as well.
    extra: dict[str, list[str]] = field(default_factory=dict)
    power: dict[str, float] = field(default_factory=dict)
    default_power: float = 0.5

    @classmethod
    def from_project(cls, project: dict) -> LightsConfig:
        section = (project or {}).get("lights") or {}
        return cls(
            exclude=set(section.get("exclude") or []),
            include=set(section.get("include") or []),
            extra={k: list(v) for k, v in (section.get("extra") or {}).items()},
            power={k: float(v) for k, v in (section.get("power") or {}).items()},
            default_power=float(section.get("default_power", 0.5)),
        )


@dataclass
class Report:
    """Everything the human needs to judge the placement, including failures."""

    placed: list[tuple[str, str, str]] = field(default_factory=list)   # entity, area, why
    skipped: list[tuple[str, str]] = field(default_factory=list)       # entity, why
    check: list[tuple[str, str]] = field(default_factory=list)         # entity, why
    areas_without_rooms: set[str] = field(default_factory=set)
    rooms_without_lights: list[str] = field(default_factory=list)
    duplicate_names: dict[str, list[str]] = field(default_factory=dict)
    # (area, fittings, entities) where measured positions were used.
    measured: list[tuple[str, int, int]] = field(default_factory=list)
    # (area, fittings, entities) where several of each made the pairing a guess.
    ambiguous: list[tuple[str, int, int]] = field(default_factory=list)


def room_index(model: Model) -> dict[str, tuple[int, Level, Room]]:
    """ha_area -> (level index, level, room).

    Rooms with no `ha_area` are not an error here; they mean `rooms` has not
    been run, which the caller reports once rather than per room.
    """
    index = {}
    for li, level in enumerate(model.levels):
        for room in level.rooms:
            if room.ha_area:
                index[room.ha_area] = (li, level, room)
    return index


def pole_of(poly: Polygon) -> Point:
    """The point furthest from any wall. Inside the polygon by construction."""
    return polylabel(poly, tolerance=POLE_TOLERANCE_CM)


def place(poly: Polygon, count: int,
          fittings: list[tuple[float, float]] | None = None) -> list[tuple[float, float]]:
    """`count` positions inside `poly`, preferring real fittings where known.

    Real positions are used first and in order. Whatever is left over is spread
    on a ring around the pole -- evenly, deterministically, and clamped back to
    the pole if a ring point would fall outside the room, which happens in a
    narrow or strongly concave space.
    """
    if count <= 0:
        return []
    known = list(fittings or [])[:count]
    remaining = count - len(known)
    if remaining == 0:
        return known

    pole = pole_of(poly)
    centre = (pole.x, pole.y)
    if remaining == 1 and not known:
        return [centre]

    # Clearance is the pole's distance to the nearest wall, so a fraction of it
    # is guaranteed to stay inside a convex neighbourhood of the pole.
    radius = poly.exterior.distance(pole) * SPREAD_FRACTION
    spread = []
    for i in range(remaining):
        angle = 2 * math.pi * i / remaining
        candidate = (centre[0] + radius * math.cos(angle),
                     centre[1] + radius * math.sin(angle))
        spread.append(candidate if poly.contains(Point(candidate)) else centre)
    return known + spread


def elevation_for(room: Room, level: Level, drop_cm: float = DROP_CM) -> float:
    """How high a fitting hangs, above this level's floor.

    The room's own ceiling wins over the level's, because ceiling height is a
    property of the room -- a 2.2 m laundry and a 4.7 m void share a level, and
    hanging the laundry light at 4.5 m would light it from outside the building.
    The LOW end of a sloped ceiling is used, so a fitting under a rake stays
    under it.
    """
    ceiling = room.ceiling_low_cm or room.ceiling_high_cm or level.ceiling_height_cm
    return max(MIN_ELEVATION_CM, ceiling - drop_cm)


def valid_entity_id(entity_id: str) -> bool:
    """The scene file is tab-separated and the entity_id goes in verbatim.

    A tab or newline would silently shift every later field on that line, so a
    light would land at a nonsensical coordinate rather than fail.
    """
    return bool(entity_id) and not any(c in entity_id for c in "\t\r\n")


def build_lights(
    model: Model,
    entities: list[LightEntity],
    config: LightsConfig | None = None,
    fittings: dict[str, list[Fitting]] | None = None,
) -> tuple[list[Light], Report]:
    """Place every placeable entity, and account for every one that is not."""
    config = config or LightsConfig()
    fittings = fittings or {}
    rooms = room_index(model)
    report = Report()
    groups = redundant_groups(entities)
    coordinated = coordinator_groups(entities)

    # Group by area first: a room's lights have to be spread against each other,
    # so they cannot be placed one at a time.
    wanted: dict[str, list[LightEntity]] = {}
    for entity in entities:
        if entity.entity_id in config.exclude:
            report.skipped.append((entity.entity_id, "excluded in project.yaml"))
            continue
        forced = entity.entity_id in config.include
        if not valid_entity_id(entity.entity_id):
            report.skipped.append((entity.entity_id, "entity_id contains a tab or newline"))
            continue
        if entity.disabled and not forced:
            report.skipped.append((entity.entity_id, "disabled in Home Assistant"))
            continue
        if entity.hidden and not forced:
            report.skipped.append((entity.entity_id, "hidden in Home Assistant"))
            continue
        if entity.entity_id in groups and not forced:
            member_ids = ", ".join(groups[entity.entity_id])
            report.skipped.append(
                (entity.entity_id,
                 f"light group; its members are placed instead ({member_ids})"))
            continue
        if entity.entity_id in coordinated and not forced:
            # Found by its device rather than by a member list, so the reason
            # names the mechanism -- this one cannot check that the members are
            # present, and the reader should know which kind of finding it is.
            report.skipped.append((entity.entity_id, coordinated[entity.entity_id]))
            continue

        areas = [entity.area] if entity.area else []
        areas += config.extra.get(entity.entity_id, [])
        if not areas:
            report.skipped.append(
                (entity.entity_id, "no area in Home Assistant -- assign one, or use lights.extra"))
            continue

        kind, reason = classify(entity)
        if kind == "check" and not forced:
            report.check.append((entity.entity_id, reason))

        for area in areas:
            if area not in rooms:
                report.areas_without_rooms.add(area)
                report.skipped.append(
                    (entity.entity_id, f"area {area!r} has no room in the model"))
                continue
            wanted.setdefault(area, []).append(entity)

    lights: list[Light] = []
    for area, in_area in sorted(wanted.items()):
        level_index, level, room = rooms[area]
        poly = polygon_of(room)
        measured = fittings.get(area) or []
        default_elevation = elevation_for(room, level)

        # How measured fittings pair with entities. Most rooms in a real house
        # have more fittings than entities -- one upstairs had roughly 18
        # fittings and 5 light.* entities, the rest on dumb switches -- so the
        # one-entity-many-fittings case is the common one, not the exception.
        pairs: list[tuple[LightEntity, tuple[float, float], float | None]] = []
        if measured and len(in_area) == 1:
            # The plugin sums sources sharing a name, so N placements carrying
            # one entity_id is the correct representation of one switch driving
            # N bulbs -- not a workaround for having too few entities.
            entity = in_area[0]
            pairs = [(entity, (f.x, f.y), f.elevation) for f in measured]
            report.measured.append((area, len(measured), 1))
        elif measured and len(in_area) > 1:
            # Which entity drives which fitting is not knowable from geometry,
            # and pairing by proximity would be a confident guess that is wrong
            # as often as it is right. Fall back to spreading and say so.
            report.ambiguous.append((area, len(measured), len(in_area)))
            positions = place(poly, len(in_area))
            pairs = [(e, p, None) for e, p in zip(in_area, positions, strict=True)]
        else:
            positions = place(poly, len(in_area))
            pairs = [(e, p, None) for e, p in zip(in_area, positions, strict=True)]

        for entity, (x, y), measured_elevation in pairs:
            power = config.power.get(entity.entity_id, config.default_power)
            if power <= 0:
                # The plugin ignores a source with no power, so it would vanish
                # from the render with nothing to explain why.
                report.skipped.append((entity.entity_id, f"power {power} is not > 0"))
                continue
            elevation = (measured_elevation if measured_elevation is not None
                         else default_elevation)
            lights.append(Light(entity_id=entity.entity_id, level=level_index,
                                x=x, y=y, elevation=max(MIN_ELEVATION_CM, elevation),
                                power=power))
            report.placed.append((entity.entity_id, area, f"level {level_index}"))

    lit = set(wanted)
    report.rooms_without_lights = sorted(a for a in rooms if a not in lit)
    return lights, report


@dataclass
class Fitting:
    """One measured light fitting, in the model's own frame."""

    x: float
    y: float
    # Height above its level's floor. None when the fixture capture had no floor
    # to measure from, in which case the room's ceiling is used as before.
    elevation: float | None = None


def load_fittings(path: str | Path) -> dict[str, list[Fitting]]:
    """Measured fitting positions per area, from `placefixtures`.

    Records whose room is `OUTSIDE` are dropped: they landed in no room and
    there is nothing to attach them to. A room prefixed `~` is a flagged
    near-miss -- the fitting fell just outside a wall line, which is common and
    still useful -- so the marker is stripped and the room used.
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    out: dict[str, list[Fitting]] = {}
    for entry in raw:
        room = str(entry.get("room") or entry.get("area") or "")
        if not room or room.startswith("OUTSIDE"):
            continue
        room = room.lstrip("~").split(" (")[0]
        x = entry.get("plan_x_cm", entry.get("x"))
        y = entry.get("plan_y_cm", entry.get("y"))
        if x is None or y is None:
            continue
        elevation = entry.get("elevation_cm")
        out.setdefault(room, []).append(
            Fitting(float(x), float(y),
                    float(elevation) if elevation is not None else None))
    return out


def save_lights(lights: list[Light], path: str | Path) -> None:
    Path(path).write_text(
        json.dumps([light.model_dump(mode="json") for light in lights], indent=2),
        encoding="utf-8")


def print_report(report: Report, lights: list[Light]) -> None:
    print(f"\nplaced {len(lights)} light(s) in "
          f"{len({p[1] for p in report.placed})} room(s)")
    for entity_id, area, where in report.placed:
        print(f"    {entity_id:<52} {area:<22} {where}")

    if report.check:
        print(f"\n  CHECK THESE {len(report.check)} -- placed, but they may not be fittings:")
        for entity_id, reason in report.check:
            print(f"    {entity_id:<52} {reason}")

    if report.skipped:
        print(f"\n  NOT PLACED ({len(report.skipped)}):")
        for entity_id, reason in report.skipped:
            print(f"    {entity_id:<52} {reason}")

    if report.areas_without_rooms:
        print(f"\n  AREAS WITH NO ROOM: {sorted(report.areas_without_rooms)}")
        print("    Either that space was not scanned, or its room needs the area id")
        print("    in project.yaml under rooms.<capture>.")

    if report.rooms_without_lights:
        print(f"\n  ROOMS WITH NO LIGHTS: {report.rooms_without_lights}")
        print("    These will render dark. Check the area assignment in Home Assistant.")

    if report.measured:
        print("\nMEASURED from a fixture pass (position and height, not guessed):")
        for area, found, entities in report.measured:
            print(f"    {area:<22} {found} fitting(s) on {entities} entity")

    if report.ambiguous:
        print("\nAMBIGUOUS -- measured fittings ignored, placed at the pole instead:")
        for area, found, entities in report.ambiguous:
            print(f"    {area:<22} {found} fitting(s) and {entities} entities")
        print("    Which entity drives which fitting is not in the geometry. Split the")
        print("    room with `seams`, or name the pairing in project.yaml.")

    if report.duplicate_names:
        print("\n  SHARED FRIENDLY NAMES (often one fitting exposed several ways):")
        for name, ids in sorted(report.duplicate_names.items()):
            print(f"    {name:<40} {ids}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("model")
    ap.add_argument("registry", help="registry.json, from `lidar2ha lights --refresh`")
    ap.add_argument("-o", "--out", default="lights.json")
    ap.add_argument("--project", help="project.yaml, for lights.exclude / extra / power")
    ap.add_argument("--fittings", help="real fitting positions, if you have them")
    ap.add_argument("--report", action="store_true", help="print the review table")
    args = ap.parse_args()

    model = load_model(args.model)
    entities = light_entities(load_registry(args.registry))

    config = LightsConfig()
    if args.project:
        import yaml
        config = LightsConfig.from_project(
            yaml.safe_load(Path(args.project).read_text(encoding="utf-8")) or {})

    rooms = room_index(model)
    if not rooms:
        raise SystemExit(
            "No room in the model carries an ha_area. Run `python -m lidar2ha.rooms` "
            "first -- lights are placed by Home Assistant area, not by scanner name.")

    fittings = load_fittings(args.fittings) if args.fittings else None
    lights, report = build_lights(model, entities, config, fittings)
    save_lights(lights, args.out)

    print(f"wrote {args.out}")
    if args.report:
        print_report(report, lights)


if __name__ == "__main__":
    main()
