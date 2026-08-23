"""The intermediate model, as types rather than as a convention.

Every stage between the DXF and the scene file reads and writes one JSON
document. Until now its shape existed only as string keys spread across four
modules -- `polycam` built it, `registration` added to it, `textures_project`
and `scenefile` indexed into it -- so a typo in any of them surfaced as a
`KeyError` three stages later, or worse, as a silently missing wall.

This module is that shape. There is no separate specification to drift from it:
`model.json` *is* these classes.

ON-DISK NAMES ARE PRESERVED. The geometry keys are camelCase (`xStart`,
`yStart`) because that is what Sweet Home 3D's own model calls them and what
already-written `home.json` files contain. Python code says `x_start`; the
alias keeps both true, so existing captures still load.

UNITS. Plan geometry is centimetres, because that is Sweet Home 3D's internal
unit and converting once at the DXF boundary is cheaper than converting at
every use. Registration is metres, because it lives in the mesh's coordinate
frame, which is metres. Field names carry the unit wherever it is not obvious.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Base(BaseModel):
    # populate_by_name lets Python construct with x_start= while JSON keeps
    # xStart; extra="forbid" turns a misspelled key into an error here rather
    # than into a missing wall several stages downstream.
    model_config = ConfigDict(populate_by_name=True, extra="forbid")


# A capture is scanned for one of two purposes and they are not interchangeable.
# Keeping the distinction in the type means a fixture pass cannot be mistaken
# for a survey by anything that reads a model.
Role = Literal["geometry", "fixtures"]


class Wall(_Base):
    """A wall centreline. Polycam gives outlines; `polycam` reduces them."""

    x_start: float = Field(alias="xStart")
    y_start: float = Field(alias="yStart")
    x_end: float = Field(alias="xEnd")
    y_end: float = Field(alias="yEnd")
    thickness: float
    height: float

    @property
    def length_cm(self) -> float:
        """Used to size a rectified texture so it covers the wall exactly once."""
        return ((self.x_end - self.x_start) ** 2 + (self.y_end - self.y_start) ** 2) ** 0.5


class Room(_Base):
    """A floor polygon.

    Not the same thing as four walls. The floor-plan plugin groups light
    entities by `Room`, and a room polygon needs no enclosing walls -- which is
    exactly what makes open-plan volumes representable.

    CEILING HEIGHT BELONGS HERE, not on the level. One capture has a 2.2 m
    laundry beside a 3.2-4.7 m double-height dining space; collapsing those to
    one number per level destroys precisely the geometry that makes cross-floor
    light spill worth rendering. `sloped` marks a room whose ceiling is a range
    rather than a plane -- those are the candidates for a void through the slab
    above. All three are optional: captures parsed before per-room ceilings
    existed simply do not carry them.
    """

    # `name` is the Home Assistant area id once `rooms` has run. Before that it
    # is whatever the scanner guessed, which is not identity: one capture
    # labelled a single entrance hall "Living Room" and "Dining Room".
    name: str | None = None
    points: list[tuple[float, float]]
    ceiling_low_cm: float | None = None
    ceiling_high_cm: float | None = None
    sloped: bool = False

    # --- identity, set by `rooms` -----------------------------------------
    scanner_name: str | None = None
    ha_area: str | None = None
    # Scanner rooms fused into this one where its split was an artefact -- an
    # open kitchen came back as "Kitchen" plus "Office 1".
    merged_from: list[str] = Field(default_factory=list)

    # --- provenance --------------------------------------------------------
    # Which capture this geometry came from. Several scans of the same space
    # each carry partial information, so every element records its source and
    # nothing is silently averaged. See Capture.
    source: str | None = None


class Door(_Base):
    """A door opening's centre and width. Not yet emitted to the scene file."""

    x: float
    y: float
    width: float


class Registration(_Base):
    """A level's 2D rigid fit onto the mesh, in mesh (metre) coordinates.

    `median_error_m` and `coverage` are the grading: a wall-poor level gives the
    fitter little to hold onto, and a confident-looking transform on a
    degenerate fit is the failure mode worth refusing to act on.
    """

    theta_deg: float
    tx_m: float
    ty_m: float
    mirror: bool
    median_error_m: float
    coverage: float
    # None when the mesh had no floor under this level's walls.
    floor_z_m: float | None = None


class Capture(_Base):
    """One scan, and how much it should be trusted.

    Scanning the same space repeatedly is normal and useful: each pass captures
    different walls, finds different windows, and registers differently well.
    So captures are immutable inputs that accumulate, never a thing to redo and
    overwrite, and every derived element records which one it came from.

    How several captures of one space combine differs by kind of data, and
    conflating the three is how a model ends up quietly wrong:

    * **Geometry is SELECTED, not averaged.** Two plans of one room disagree by
      centimetres; blending them blurs corners and squares nothing. The
      best-registered capture wins the room outright.
    * **Textures COMPOSITE.** Per-wall coverage runs 6-60%, and the gaps are in
      different places each pass, so pixels union across captures. This is the
      biggest reason to rescan, and coverage genuinely adds up.
    * **Detected features UNION, deduplicated.** A window found in any capture
      is a real window; one capture reported 0.0 m2 of glazing and another
      found 8.9 m2 in the same house.

    Compositing requires captures of the same space to be co-registered, which
    is tractable precisely because they overlap heavily -- unlike two captures
    of *adjacent* spaces, which share no frame and cannot be merged at all.
    """

    id: str
    # What this capture is FOR. A fixture pass is scanned differently on
    # purpose -- every light on, the phone aimed at each fitting, geometry
    # sacrificed -- so its walls and elevations are not evidence about the
    # building and must never be selected as geometry. See Model.role.
    role: Role = "geometry"
    # Quality, measured rather than asserted. Populated by `register` and
    # `inspect`, and used to decide which capture wins a contested room.
    median_error_m: float | None = None
    coverage: float | None = None
    # Mesh wall area over the CSV's figure. Near 1.0 is a good scan; one
    # capture managed 0.87 with a mirror wrecking it, another 1.04.
    wall_area_ratio: float | None = None
    windows_found: int | None = None
    note: str | None = None


class Level(_Base):
    name: str
    ceiling_height_cm: float
    # The DXF is 2D and carries no elevation; `registration` recovers it from
    # the mesh, and it stays None if the mesh was too incomplete.
    elevation_cm: float | None = None
    walls: list[Wall] = Field(default_factory=list)
    rooms: list[Room] = Field(default_factory=list)
    doors: list[Door] = Field(default_factory=list)
    registration: Registration | None = None


class Model(_Base):
    """One capture's geometry, or -- for a fixture pass -- one capture's lights.

    ROLE IS LOAD-BEARING. A fixture pass is a deliberately bad scan: lights on,
    phone aimed at fittings, geometry quality traded away. Its walls still
    register onto its own mesh, which is exactly what `placefixtures` needs,
    and that same registration recovers a floor elevation that is worthless.
    Nothing in the pipeline could previously tell the two kinds apart, so a
    fixture capture could be built into a house or contribute a level height
    with no complaint. Defaulted, because every model.json written before this
    existed came from a geometry pass.
    """

    source: str
    role: Role = "geometry"
    units: Literal["cm"] = "cm"
    levels: list[Level] = Field(default_factory=list)
    # The scans this model was built from. Empty for a single-capture project,
    # which is all the pipeline supports today -- but Room.source points here,
    # so carrying the list is what makes that provenance mean anything.
    captures: list[Capture] = Field(default_factory=list)

    def bounds(self) -> tuple[float, float, float, float]:
        """(min_x, min_y, max_x, max_y) over every level's geometry.

        Computed across all levels together, never per level: the levels are
        laid out on one sheet and share a frame, so re-origining them
        independently would slide the floors out of alignment with each other.
        """
        xs: list[float] = []
        ys: list[float] = []
        for level in self.levels:
            for wall in level.walls:
                xs += [wall.x_start, wall.x_end]
                ys += [wall.y_start, wall.y_end]
            for room in level.rooms:
                xs += [p[0] for p in room.points]
                ys += [p[1] for p in room.points]
        if not xs:
            raise ValueError("model has no geometry")
        return min(xs), min(ys), max(xs), max(ys)


class WallTexture(_Base):
    """One entry of `textures_project`'s manifest: a wall's rectified images.

    A side the scan did not cover well enough is simply absent, rather than
    present and black -- the caller falls back to the tiled patch.
    """

    level: int
    wall: int
    left: Path | None = None
    right: Path | None = None
    left_coverage: float | None = None
    right_coverage: float | None = None


class Light(_Base):
    """A light placement.

    `entity_id` is load-bearing: the floor-plan plugin matches objects by
    `name == entity_id` and sums multiple sources sharing a name, so a switch
    driving six bulbs is six placements carrying one entity_id.
    """

    entity_id: str
    level: int
    x: float
    y: float
    elevation: float
    power: float = 0.5


def load_model(path: str | Path) -> Model:
    return Model.model_validate_json(Path(path).read_text(encoding="utf-8"))


def save_model(model: Model, path: str | Path) -> None:
    """Write with the on-disk (camelCase) names, so captures stay loadable."""
    Path(path).write_text(
        model.model_dump_json(indent=2, by_alias=True), encoding="utf-8"
    )


def load_wall_textures(path: str | Path) -> list[WallTexture]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return [WallTexture.model_validate(entry) for entry in raw]
