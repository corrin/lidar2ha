"""Which of the house's areas have geometry, across every level at once.

The question the pipeline cannot currently answer, and the one an owner asks
first: *is my house in there?* Three stages each answer a piece of it and none
answers this:

* `combine` reports an area with no room -- for ONE level, and only for areas
  `project.yaml` maps. An area Home Assistant knows and the project never
  mentions is invisible to it.
* `lights` reports areas with no room -- for ONE model, and only while walking
  light entities, so an area with no light is never even considered.
* Neither crosses levels, and a house is not one level.

Measured on the house this was built for, that gap hid a garage, a den and a
downstairs hallway from a model that looked complete.

REPORTS, NEVER REPAIRS. Every remedy here is a line the owner writes in
`project.yaml`: which area a scanner room is depends on knowing the house, and
a tool that guessed would be inventing the one fact it cannot have.
"""

from __future__ import annotations

from dataclasses import dataclass

from .lights import rooms_sharing_an_area
from .rooms import polygon_of
from .schema import Model

CM2_PER_M2 = 10_000


@dataclass(frozen=True)
class LevelCoverage:
    """One level: what Home Assistant expects of it, and what the model holds."""

    level: str
    # None when no HA floor has this name. `levels:` keys are floor names by
    # convention and nothing enforces it, so a typo has to be visible -- it
    # otherwise reads as a level whose every area is missing.
    floor_id: str | None
    # None when no model has been built yet. A level not yet combined and a
    # level covering nothing look identical in a count, and only one of them
    # means go and scan something.
    source: str | None
    floor_areas: tuple[str, ...]
    covered: tuple[str, ...]
    missing: tuple[str, ...]
    # Rooms carrying an `ha_area` that is not an area of this floor AND is not
    # on any level's floor either. Counting these as covering the level is how a
    # level reports more areas covered than it has; the mismatch is a finding in
    # itself, since either the area is on the wrong floor in Home Assistant or
    # the room is.
    foreign: tuple[str, ...]
    # Areas whose rooms are here and whose HA floor is some other level's. A
    # stairwell is one area and three storeys, and that is not a mistake: Home
    # Assistant's area belongs to exactly one floor while the volume consumes
    # space on every storey it passes through, which is why `polycam` files a
    # shaft on the lowest band it spans. Counted as covered on its own floor and
    # only there, so the denominator still means something.
    spans: tuple[str, ...]
    # (label, m2), largest first. Each needs a name, a `split:`, or to be
    # recorded as not-an-area.
    unnamed: tuple[tuple[str, float], ...]
    # area -> how many rooms carry it here, where that is more than one. Only
    # one of them can ever take a light: `lights.room_index` is keyed by area.
    shared: dict[str, int]


def _floors_by_name(registry: dict) -> dict[str, str]:
    return {f["name"]: f["floor_id"] for f in registry.get("floors") or []
            if f.get("name") and f.get("floor_id")}


def _areas_of(registry: dict, floor_id: str | None) -> tuple[str, ...]:
    return tuple(sorted(a["area_id"] for a in registry.get("areas") or []
                        if a.get("floor_id") == floor_id and a.get("area_id")))


def uncovered_floors(settings: dict, registry: dict) -> tuple[str, ...]:
    """Floors Home Assistant knows that no level claims.

    Reported apart from the missing areas rather than counted with them. This
    house has an `msm` floor that is not the building being modelled, and
    folding its areas into the gap would overstate the total forever -- which
    is how a report teaches people to ignore it.
    """
    claimed = set(settings.get("levels") or {})
    return tuple(sorted(name for name in _floors_by_name(registry)
                        if name not in claimed))


def measure(settings: dict, registry: dict,
            models: dict[str, Model],
            sources: dict[str, str] | None = None) -> list[LevelCoverage]:
    """Coverage per declared level. `models` is keyed by the `levels:` key."""
    floors = _floors_by_name(registry)
    declared = list(settings.get("levels") or {})

    # Every area any declared level's own floor owns. An area found on a level
    # that is not its floor is only a mistake if NO declared level owns it --
    # otherwise it is a volume spanning storeys, and saying so per level would
    # fire forever on correct data.
    ours: set[str] = set()
    for level in declared:
        if floors.get(level):
            ours.update(_areas_of(registry, floors[level]))

    rows: list[LevelCoverage] = []
    for level in declared:
        floor_id = floors.get(level)
        floor_areas = _areas_of(registry, floor_id) if floor_id else ()
        model = models.get(level)
        if model is None:
            rows.append(LevelCoverage(level, floor_id, None, floor_areas,
                                      (), (), (), (), (), {}))
            continue

        held: set[str] = set()
        unnamed: list[tuple[str, float]] = []
        for lv in model.levels:
            for room in lv.rooms:
                if room.ha_area:
                    held.add(room.ha_area)
                    continue
                label = room.name or room.scanner_name or "?"
                unnamed.append((label, polygon_of(room).area / CM2_PER_M2))

        known = set(floor_areas)
        elsewhere = held - known
        rows.append(LevelCoverage(
            level=level,
            floor_id=floor_id,
            source=(sources or {}).get(level, "the model"),
            floor_areas=floor_areas,
            covered=tuple(sorted(held & known)),
            missing=tuple(sorted(known - held)),
            foreign=tuple(sorted(elsewhere - ours)),
            spans=tuple(sorted(elsewhere & ours)),
            unnamed=tuple(sorted(unnamed, key=lambda t: -t[1])),
            shared=rooms_sharing_an_area(model),
        ))
    return rows


def report(rows: list[LevelCoverage], uncovered: tuple[str, ...] = ()) -> None:
    """Print the roll-up, then each level, then what to write where."""
    have = sum(len(r.covered) for r in rows)
    want = sum(len(r.floor_areas) for r in rows)
    print(f"\n{have} of {want} area(s) across {len(rows)} level(s) have geometry.")

    for row in rows:
        print()
        if row.floor_id is None:
            print(f"{row.level}: no Home Assistant floor has this name.")
            print("  `levels:` is keyed by floor name. Check it against "
                  "`python -m lidar2ha.ha --refresh`.")
            continue
        if row.source is None:
            print(f"{row.level}: no model yet -- run `lidar2ha combine "
                  f"{row.level!r}`.")
            continue

        print(f"{row.level}: {len(row.covered)} of {len(row.floor_areas)} "
              f"area(s), from {row.source}")
        if row.missing:
            print("  NO GEOMETRY -- not scanned, or the room needs this area "
                  "id under `rooms.<capture>`:")
            for area in row.missing:
                print(f"    {area}")
        if row.spans:
            print("  SPANS STOREYS -- this area's floor is another level's, and "
                  "that is not a\n  mistake. Counted there, not here:")
            for area in row.spans:
                print(f"    {area}")
        if row.foreign:
            print("  CARRIED, BUT NOT AN AREA OF ANY LEVEL'S FLOOR -- either "
                  "the area is on the wrong\n  floor in Home Assistant, or the "
                  "room is:")
            for area in row.foreign:
                print(f"    {area}")
        if row.shared:
            print("  ONE AREA, SEVERAL ROOMS -- only one of each can take a "
                  "light, because\n  lights bind by area:")
            for area, n in row.shared.items():
                print(f"    {area:<24} {n} rooms")
        if row.unnamed:
            print("  NO AREA -- each needs a name in `rooms:`, a `split:`, or "
                  "to be left as not-an-area:")
            for label, m2 in row.unnamed:
                print(f"    {label:<28} {m2:6.1f} m2")

    if uncovered:
        print(f"\nFloors no level claims: {', '.join(uncovered)}")
        print("  Not counted above. A floor you are not modelling is not a gap.")
