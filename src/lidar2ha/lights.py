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

WHICH ENTITY DRIVES WHICH FITTING IS NOT IN THE GEOMETRY. A room with several
of each cannot be paired by anything here -- proximity would be a confident
guess wrong about as often as it is right, since a ceiling light, two cabinet
lights and a wall pair interleave. The only place that knowledge exists is in
the owner's head, so `lights.pairing` in project.yaml is where they write it
down, addressed by plan coordinate:

    lights:
      pairing:
        den:
          light.den_dimmer_switch_ceiling: [[-23.0, -421.9], [-129.2, -414.6]]

Rooms are counted by DEVICE, not by entity. Several entities are routinely one
fitting and Home Assistant says so by giving them one `device_id`: one real
Sonoff exposes four `light.*` entities of which one controls sound. Counting
entities made a room of ten look unresolvable where six devices, four of them
interior fittings, is a short declaration.

Nothing is silently dropped. An entity whose area has no room, a room nobody
lights, an entity excluded by hand, a declared pairing that names no fitting --
each is counted and named, because a dark room in the final render is otherwise
a mystery with no thread to pull.

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
    device_groups,
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

# How far a declared pairing point may sit from the fitting it names. A point is
# authored by copying a detected fitting's own coordinate, so a real one matches
# at about zero; this exists to catch a re-detection that MOVED the fitting, and
# to tell that apart from a typo. What would move it: a capture whose fittings
# routinely shift more than this between runs, which would mean the declaration
# is chasing the detector rather than describing the house.
PAIRING_MATCH_CM = 60.0
# ...and a point must pick ONE fitting out of the room. Measured over three
# storeys, the closest pair of fittings within a single room is 10.1 cm and
# several rooms sit under 40, so no fixed radius is both generous and
# unambiguous. The second-nearest must therefore be this many times further
# away than the nearest, or the point has not named anything and is refused.
PAIRING_AMBIGUOUS_RATIO = 2.0


@dataclass
class LightsConfig:
    """The human's corrections, from project.yaml."""

    exclude: set[str] = field(default_factory=set)
    include: set[str] = field(default_factory=set)
    # entity_id -> additional area ids to place it in as well.
    extra: dict[str, list[str]] = field(default_factory=dict)
    power: dict[str, float] = field(default_factory=dict)
    default_power: float = 0.5
    # area -> entity_id -> the plan-cm points of the fittings that entity drives.
    # WHICH ENTITY DRIVES WHICH FITTING IS NOT IN THE GEOMETRY and cannot be
    # inferred -- the only place it exists is in the owner's head, so this is
    # where they write it down.
    pairing: dict[str, dict[str, list[tuple[float, float]]]] = field(
        default_factory=dict)

    @classmethod
    def from_project(cls, project: dict) -> LightsConfig:
        section = (project or {}).get("lights") or {}
        pairing: dict[str, dict[str, list[tuple[float, float]]]] = {}
        for area, by_entity in (section.get("pairing") or {}).items():
            for entity_id, points in (by_entity or {}).items():
                pairing.setdefault(str(area), {})[str(entity_id)] = [
                    (float(p[0]), float(p[1])) for p in points]
        return cls(
            exclude=set(section.get("exclude") or []),
            include=set(section.get("include") or []),
            extra={k: list(v) for k, v in (section.get("extra") or {}).items()},
            power={k: float(v) for k, v in (section.get("power") or {}).items()},
            default_power=float(section.get("default_power", 0.5)),
            pairing=pairing,
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
    # (area, fittings, entities, devices) where several of each made the pairing
    # a guess. `devices` is carried because it is the number that says whether
    # the room is resolvable: "7 fittings, 2 entities" reads as hopeless where
    # "7 fittings, 2 entities on 1 device" reads as one line of project.yaml.
    ambiguous: list[tuple[str, int, int, int]] = field(default_factory=list)
    # (area, count) of measured fittings the daylight difference called windows.
    daylight: list[tuple[str, int]] = field(default_factory=list)
    # (area, entity, why) -- a declared pairing that could not be honoured. The
    # entity is still placed, by the ordinary spread, so this line is the only
    # thing between the declaration and looking like it worked.
    pairing_failed: list[tuple[str, str, str]] = field(default_factory=list)
    # (area, entities, fittings) left over where a room IS partly declared.
    # Working through a house one room at a time is the normal state, and how
    # much is still undeclared has to be visible.
    pairing_partial: list[tuple[str, list[str], int]] = field(default_factory=list)


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


def match_fitting(point: tuple[float, float], measured: list[Fitting], *,
                  match_cm: float = PAIRING_MATCH_CM,
                  ambiguous_ratio: float = PAIRING_AMBIGUOUS_RATIO,
                  ) -> tuple[Fitting | None, str]:
    """The one measured fitting a declared point names. (fitting, why not).

    TWO WAYS TO REFUSE, AND NEITHER IS SILENT, because a pairing that binds the
    wrong fitting is a light in the wrong place that looks exactly like a light
    in the right place:

    * nothing near it -- the distance to the nearest is reported, which is what
      separates "I mistyped a coordinate" from "the detector moved".
    * two fittings about equally near -- the point has not picked one. Real
      rooms have fittings 10 cm apart, so this is not hypothetical, and taking
      the nearer by a millimetre would be the proximity guess this refuses to
      make anywhere else.
    """
    if not measured:
        return None, "the room has no measured fittings at all"

    ranked = sorted(measured, key=lambda f: math.dist((f.x, f.y), point))
    nearest = ranked[0]
    away = math.dist((nearest.x, nearest.y), point)
    if away > match_cm:
        return None, (f"nothing measured within {match_cm:.0f} cm -- the nearest "
                      f"fitting is {away:.0f} cm away at ({nearest.x:.0f}, "
                      f"{nearest.y:.0f})")

    if len(ranked) > 1:
        second = math.dist((ranked[1].x, ranked[1].y), point)
        if second < away * ambiguous_ratio:
            return None, (f"names no single fitting -- two are about equally "
                          f"near, at {away:.0f} cm and {second:.0f} cm. Move the "
                          f"point onto the one you mean")
    return nearest, ""


def declared_pairs(area: str, in_area: list[LightEntity], measured: list[Fitting],
                   declaration: dict[str, list[tuple[float, float]]],
                   report: Report,
                   ) -> tuple[list[tuple[LightEntity, tuple[float, float],
                                          float | None]],
                              list[LightEntity], set[int]]:
    """Carry out this room's `lights.pairing`. (placements, left over, claimed).

    Returns the entities the declaration did NOT name alongside the placements,
    because those still have to be placed by the ordinary rules -- a house is
    declared one room at a time and a half-declared room must not lose the other
    half.

    AN ENTITY WHOSE DECLARATION CANNOT BE HONOURED IS NOT SILENTLY DROPPED, and
    is not silently placed either. It goes back in with the undeclared ones, so
    it still appears, and the reason lands in the report -- centring it quietly
    would look exactly like the declaration having worked.

    A declaration naming an entity that is not in this room is reported rather
    than ignored: it is almost always a typo or an area that moved, and the
    consequence of ignoring it is a fitting nobody ever placed.
    """
    by_id = {e.entity_id: e for e in in_area}
    for entity_id in declaration:
        if entity_id not in by_id:
            report.pairing_failed.append(
                (area, entity_id, "declared here but not a light of this area -- "
                                  "renamed, moved, or a typo"))

    pairs: list[tuple[LightEntity, tuple[float, float], float | None]] = []
    claimed: set[int] = set()
    named: set[str] = set()
    index = {id(f): i for i, f in enumerate(measured)}

    for entity in in_area:
        points = declaration.get(entity.entity_id)
        if not points:
            continue
        found: list[Fitting] = []
        for point in points:
            fitting, why = match_fitting(point, measured)
            if fitting is None:
                report.pairing_failed.append(
                    (area, entity.entity_id,
                     f"({point[0]:.0f}, {point[1]:.0f}) {why}"))
                continue
            found.append(fitting)
        if not found:
            continue
        named.add(entity.entity_id)
        for fitting in found:
            claimed.add(index[id(fitting)])
            pairs.append((entity, (fitting.x, fitting.y), fitting.elevation))

    return pairs, [e for e in in_area if e.entity_id not in named], claimed


def elevation_for(room: Room, level: Level, drop_cm: float = DROP_CM) -> float:
    """How high a fitting hangs, above this level's floor.

    The room's own ceiling wins over the level's, because ceiling height is a
    property of the room -- a 2.2 m laundry and a 4.7 m void share a level, and
    hanging the laundry light at 4.5 m would light it from outside the building.

    THE HIGH END OF A RAKE, NOT THE LOW ONE. This preferred `ceiling_low_cm`,
    reasoning that a fitting under a rake should stay under it. The reasoning
    holds and the number does not: the low end of a sloping ceiling is the
    EAVE, where it meets the wall, and nothing hangs there. Measured over one
    storey of raked rooms, where every low was correct and none was stale:

        master_bedroom   low 110  high 400  ->  placed at  90 cm
        sewing_room      low 150  high 400  ->  placed at 130 cm
        girl_bedroom     low 160  high 264  ->  placed at 140 cm
        office           low 170  high 268  ->  placed at 150 cm

    A master-bedroom downlight at 90 cm. Off the high end the same four come out
    at 380, 380, 244 and 248, and the double-height rooms stay right too -- a
    den measured at 396 and a stairwell shaft at 485.

    So this is not a case of distrusting `ceiling_low_cm`. It is true, and it is
    simply not the height anything hangs at. The only reading that misleads off
    the high end is a room the scan saw through, and `ceilings` refuses to write
    those at all rather than recording a lower bound as a measurement.
    """
    ceiling = room.ceiling_high_cm or room.ceiling_low_cm or level.ceiling_height_cm
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
        # A window is bright in every capture and a fitting only when it is
        # switched on, so `daylight` can say which is which -- see
        # lidar2ha.daylight. "unseen" means the ordinary capture never
        # photographed that spot, which is not evidence either way, so it is
        # kept. Only a positive "window" is refused, and it is counted.
        all_measured = fittings.get(area) or []
        measured = [f for f in all_measured if f.verdict != "window"]
        if len(measured) != len(all_measured):
            report.daylight.append((area, len(all_measured) - len(measured)))
        default_elevation = elevation_for(room, level)

        # How measured fittings pair with entities. Most rooms in a real house
        # have more fittings than entities -- one upstairs had roughly 18
        # fittings and 5 light.* entities, the rest on dumb switches -- so the
        # one-entity-many-fittings case is the common one, not the exception.
        pairs: list[tuple[LightEntity, tuple[float, float], float | None]] = []
        declared, undeclared, claimed = declared_pairs(
            area, in_area, measured, config.pairing.get(area) or {}, report)
        pairs += declared

        # Fittings the declaration did not claim are still the room's, and are
        # shared out by the rules below among the entities it did not name.
        left = [f for i, f in enumerate(measured) if i not in claimed]
        devices = len(device_groups(undeclared))

        if declared and undeclared:
            report.pairing_partial.append(
                (area, [e.entity_id for e in undeclared], len(left)))

        if left and len(undeclared) == 1:
            # The plugin sums sources sharing a name, so N placements carrying
            # one entity_id is the correct representation of one switch driving
            # N bulbs -- not a workaround for having too few entities.
            entity = undeclared[0]
            pairs += [(entity, (f.x, f.y), f.elevation) for f in left]
            report.measured.append((area, len(left), 1))
        elif left and len(undeclared) > 1:
            # Which entity drives which fitting is not knowable from geometry,
            # and pairing by proximity would be a confident guess that is wrong
            # as often as it is right. Fall back to spreading and say so --
            # counting DEVICES, because entities sharing one are one fitting and
            # a count that says otherwise makes a resolvable room look hopeless.
            report.ambiguous.append((area, len(left), len(undeclared), devices))
            positions = place(poly, len(undeclared))
            pairs += [(e, p, None)
                      for e, p in zip(undeclared, positions, strict=True)]
        elif undeclared:
            positions = place(poly, len(undeclared))
            pairs += [(e, p, None)
                      for e, p in zip(undeclared, positions, strict=True)]

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
    # "fitting" | "window" | "unseen" from `daylight`, or None when the fixture
    # pass was never differenced against an ordinary capture. Carried this far
    # rather than filtered at load, so the refusal can be counted in the report
    # instead of happening in silence.
    verdict: str | None = None
    # Which crop on the contact sheet this is, so a report line is answerable.
    crop: str | None = None


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
                    float(elevation) if elevation is not None else None,
                    verdict=entry.get("verdict"),
                    crop=entry.get("crop")))
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
        for area, found, entities, devices in report.ambiguous:
            on = (f"{entities} entities on {devices} device"
                  + ("s" if devices != 1 else "")
                  if devices != entities else f"{entities} entities")
            print(f"    {area:<22} {found} fitting(s) and {on}")
        print("    Which entity drives which fitting is not in the geometry and cannot")
        print("    be guessed from it. Name the pairing in project.yaml:")
        print("      lights:")
        print("        pairing:")
        print("          <area>:")
        print("            light.<entity>: [[x_cm, y_cm], ...]")
        print("    The coordinates are plan centimetres in this model's own frame --")
        print("    the same ones `split:` sections use. Each point takes the fitting")
        print("    nearest it, and says so rather than guessing if that is not one.")

    if report.pairing_failed:
        print(f"\n  PAIRING NOT HONOURED ({len(report.pairing_failed)}) -- the entity is "
              "still placed, by the\n  ordinary spread, so these lines are the only "
              "sign the declaration did nothing:")
        for area, entity_id, why in report.pairing_failed:
            print(f"    {area:<18} {entity_id:<44} {why}")

    if report.pairing_partial:
        print("\n  PARTLY DECLARED -- these rooms have a pairing that does not cover "
              "everything:")
        for area, entity_ids, left in report.pairing_partial:
            print(f"    {area:<18} {len(entity_ids)} entity(ies) unnamed, "
                  f"{left} fitting(s) unclaimed")
            for entity_id in entity_ids:
                print(f"        {entity_id}")

    if report.daylight:
        print("\nIGNORED AS DAYLIGHT -- bright in an ordinary capture too, so a window:")
        for area, count in report.daylight:
            print(f"    {area:<22} {count} candidate(s)")
        print("    Delete the \"verdict\" key from that record in fixtures_placed.json")
        print("    to place it anyway; check the contact sheet crop first.")

    if report.duplicate_names:
        print("\n  SHARED FRIENDLY NAMES (often one fitting exposed several ways):")
        for name, ids in sorted(report.duplicate_names.items()):
            print(f"    {name:<40} {ids}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("model")
    ap.add_argument("registry", help="registry.json, from `lidar2ha lights --refresh`")
    ap.add_argument("-o", "--out", default="lights.json")
    ap.add_argument("--project",
                    help="project.yaml, for lights.exclude / extra / power / pairing")
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
