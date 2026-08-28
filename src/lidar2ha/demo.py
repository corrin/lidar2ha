"""A demo project: a two-storey house that belongs to nobody.

`lidar2ha demo <dir>` writes what Polycam would hand you for eight captures of
one building, so the tutorial can be followed -- and tested -- without a phone,
a scan, or somebody's floor plan.

FAITHFUL WHERE IT MATTERS, and only there. The DXF carries the four layers
`polycam` reads and nothing else; the CSV carries the multi-floor column layout;
the mesh is Z-up, textured, and in metres. Everything a real export also holds --
dimensions, furniture, the compass, the logo -- is noise this pipeline ignores,
so none of it is here.

Two things are deliberately awkward, because they are awkward in life:

- **Each capture ships as TWO zips whose inner files are all named the same.**
  Polycam names a download by capture date, so `plan.dxf` inside one archive and
  `plan.dxf` inside the next collide the moment you unpack both into one
  directory. Staging is the first thing a reader does and the first thing that
  can silently destroy data, so the demo makes them do it.
- **Every capture has its own coordinate frame.** A plan is drawn wherever the
  scanner started, and the mesh sits somewhere else again. Registration and
  `combine` exist to undo that, and neither has anything to do if the demo hands
  them geometry already in one frame.

One capture is wrong on purpose, and wrong in the only way that counts: it puts
a room somewhere the others do not. A whole-capture rotation or offset is just
another frame and registration removes it exactly, so a capture skewed that way
agrees with everybody and teaches nothing. `wrong_room` moves ONE room instead.
The capture registers perfectly against its OWN mesh -- that is what makes it
dangerous -- and only shows up once `combine` measures it against the averaged
walls of the others.
"""

from __future__ import annotations

import csv
import json
import math
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import ezdxf

M_TO_CM = 100.0

# The inner filename every archive uses. Polycam names by capture date, so two
# captures shot on one day collide -- which is the trap Part 3 of the tutorial
# exists for. Keep this a constant: varying it would quietly remove the trap.
EXPORT_STEM = "12_04_2027"


# --------------------------------------------------------------------------- #
# the DXF a Polycam floor-plan export looks like
# --------------------------------------------------------------------------- #


def wall_outline(x0: float, y0: float, x1: float, y1: float,
                 thickness: float = 0.1) -> list[tuple[float, float]]:
    """The 7-point outline Polycam draws for a centreline from (x0,y0)-(x1,y1).

    p0 and p3 are the end-cap midpoints, which ARE the centreline, and the
    corners sit half a thickness off it. Built from the centreline rather than
    written out, so a diagonal wall is as correct as an axis-aligned one --
    which is the case `centreline_and_thickness` exists to get right.
    """
    dx, dy = x1 - x0, y1 - y0
    length = math.hypot(dx, dy)
    ux, uy = dx / length, dy / length
    nx, ny = -uy * thickness / 2, ux * thickness / 2
    return [
        (x0, y0),                       # p0: end-cap A midpoint
        (x0 + nx, y0 + ny),             # p1: one long side
        (x1 + nx, y1 + ny),             # p2
        (x1, y1),                       # p3: end-cap B midpoint
        (x1 - nx, y1 - ny),             # p4: the other long side
        (x0 - nx, y0 - ny),             # p5
        (x0, y0),                       # closes the ring
    ]


def box(x: float, y: float, w: float, h: float) -> list[tuple[float, float]]:
    return [(x, y), (x + w, y), (x + w, y + h), (x, y + h), (x, y)]


class Sheet:
    """One synthetic export: a DXF and the CSV of ceiling heights beside it."""

    def __init__(self) -> None:
        self.doc = ezdxf.new(setup=True)
        self.doc.header["$INSUNITS"] = 6          # metres, as Polycam writes
        self.msp = self.doc.modelspace()
        self.ceilings: dict[str, tuple[float, float]] = {}

    def floor_label(self, name: str, x: float, y: float = 0.0) -> Sheet:
        self.msp.add_mtext(name, dxfattribs={"layer": "Floor Label"}).set_location((x, y))
        return self

    def room(self, name: str, x: float, y: float, w: float, h: float,
             low: float, high: float | None = None) -> Sheet:
        """A room, its label, and the ceiling the CSV would report for it."""
        self.msp.add_lwpolyline(box(x, y, w, h), dxfattribs={"layer": "Poly-Rooms"})
        self.msp.add_mtext(name, dxfattribs={"layer": "Poly-RoomLabels"}).set_location(
            (x + w / 2, y + h / 2))
        self.ceilings[name] = (low, high if high is not None else low)
        return self

    def polygon_room(self, name: str, pts: list[tuple[float, float]],
                     low: float, high: float | None = None) -> Sheet:
        """A room whose outline is not a rectangle, for the L-shaped case."""
        ring = list(pts) + [pts[0]]
        self.msp.add_lwpolyline(ring, dxfattribs={"layer": "Poly-Rooms"})
        cx = sum(p[0] for p in pts) / len(pts)
        cy = sum(p[1] for p in pts) / len(pts)
        self.msp.add_mtext(name, dxfattribs={"layer": "Poly-RoomLabels"}).set_location(
            (cx, cy))
        self.ceilings[name] = (low, high if high is not None else low)
        return self

    def wall(self, x0: float, y0: float, x1: float, y1: float,
             thickness: float = 0.1) -> Sheet:
        """One wall, written TWICE -- Polycam does, and the reader deduplicates
        on the exact point sequence. A fixture that wrote it once would leave
        that path untested."""
        pts = wall_outline(x0, y0, x1, y1, thickness)
        for _ in range(2):
            self.msp.add_lwpolyline(pts, dxfattribs={"layer": "Poly-Walls"})
        return self

    def door(self, x: float, y: float, w: float = 0.8, d: float = 0.15) -> Sheet:
        self.msp.add_lwpolyline(box(x, y, w, d), dxfattribs={"layer": "Poly-Doors"})
        return self

    def swing_arc(self, x: float, y: float) -> Sheet:
        """A 2-point entity on the door layer. Polycam draws these and they are
        NOT openings, so a fixture without one cannot prove they are skipped."""
        self.msp.add_lwpolyline([(x, y), (x + 0.8, y)],
                                dxfattribs={"layer": "Poly-Doors"})
        return self

    def write(self, directory: Path, stem: str = "plan") -> tuple[Path, Path]:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        dxf_path = directory / f"{stem}.dxf"
        csv_path = directory / f"{stem}.csv"
        self.doc.saveas(dxf_path)

        # Shaped like a real export: `Floor,Room,Description,Value`, with the
        # ceiling rows found by their Description rather than their position,
        # and other measurements interleaved. The Latitude row is not padding
        # -- a reader that took every row would pass without it.
        with csv_path.open("w", newline="", encoding="utf-8") as fh:
            out = csv.writer(fh)
            out.writerow(["Floor", "Room", "Description", "Value"])
            out.writerow(["All", "Entire Roomplan", "Latitude", "-1.234567 m"])
            for name, (low, high) in self.ceilings.items():
                value = (f"{high:.1f}" if low == high
                         else f"{low:.1f} - {high:.1f}")
                out.writerow(["Floor 1", name,
                              "Ceiling height [m] (approx)", value])
                out.writerow(["Floor 1", name, "Area [m2]", "12.0"])
        return dxf_path, csv_path


# --------------------------------------------------------------------------- #
# the fixtures the tests already import -- kept here so there is one copy
# --------------------------------------------------------------------------- #


def one_storey(directory: Path) -> tuple[Path, Path]:
    """A plain single-storey capture: one cluster, one ceiling band.

    The shape of every capture that already worked, and so the one the storey
    split must leave completely alone.

    THE TWO CEILINGS DIFFER, AND THE TALLER ROOM COMES FIRST. Both sit well
    inside one band, so this is still a single-storey capture -- but sheet order
    and height order now disagree, which is the only way a test can tell the
    band search SORTING rooms to find them apart from it EMITTING them sorted.
    Given equal ceilings that sort is stable and the two are indistinguishable:
    the first version of this fixture used 2.40 for both, and the golden test
    passed happily with the reordering bug put back in on purpose.
    """
    sheet = Sheet().floor_label("Floor 1", x=3.0)
    sheet.room("Bedroom", 0.0, 0.0, 4.0, 3.0, 2.60)
    sheet.room("Hallway", 4.0, 0.0, 2.0, 3.0, 2.30)
    for x0, y0, x1, y1 in ((0, 0, 6, 0), (6, 0, 6, 3), (6, 3, 0, 3),
                           (0, 3, 0, 0), (4, 0, 4, 3)):
        sheet.wall(x0, y0, x1, y1)
    sheet.door(4.0, 1.0).swing_arc(4.0, 2.0)
    return sheet.write(directory)


def three_storeys_on_one_cluster(directory: Path) -> tuple[Path, Path]:
    """The capture that broke this: one sheet cluster, three storeys STACKED.

    Polycam reports each ceiling above the CAPTURE DATUM, so the three sit at
    2.4, 5.1 and 7.8 m while their footprints overlap in plan -- which is what
    a building does and what makes the plan position useless for telling them
    apart. `Living Room` spans 3.8 to 8.0 m: a stairwell, on no storey at all.
    """
    sheet = Sheet().floor_label("Floor 1", x=3.0)
    sheet.room("Bedroom", 0.0, 0.0, 4.0, 3.0, 2.40)
    sheet.room("Office", 0.2, 0.2, 3.6, 2.6, 7.80)          # stacked above it
    sheet.room("Landing", 4.0, 0.0, 2.0, 3.0, 5.10)
    sheet.room("Living Room", 0.0, 3.0, 6.0, 3.0, 3.80, 8.00)
    for x0, y0, x1, y1 in ((0, 0, 6, 0), (6, 0, 6, 3), (6, 3, 0, 3),
                           (0, 3, 0, 0), (4, 0, 4, 3),
                           (0, 3, 6, 3), (6, 3, 6, 6), (6, 6, 0, 6), (0, 6, 0, 3)):
        sheet.wall(x0, y0, x1, y1)
    sheet.door(4.0, 1.0)
    return sheet.write(directory)


def labelled_floor_with_no_rooms(directory: Path) -> tuple[Path, Path]:
    """Walls and a floor label, but no closed room outlines.

    Polycam does not always close a room. Without a fallback this cluster
    produces no ceiling bands and therefore no level at all -- the storey and
    every wall on it vanishing because its floors were not traced.
    """
    sheet = Sheet().floor_label("Floor 1", x=3.0)
    for x0, y0, x1, y1 in ((0, 0, 6, 0), (6, 0, 6, 3), (6, 3, 0, 3), (0, 3, 0, 0)):
        sheet.wall(x0, y0, x1, y1)
    return sheet.write(directory)


# --------------------------------------------------------------------------- #
# the demo house
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class DemoRoom:
    """One room of the demo house, in metres in the building's own frame."""

    scanner_name: str           # what the scanner would guess, not an area id
    area: str | None            # the HA area a reader maps it to, or None
    x: float
    y: float
    w: float
    h: float
    ceiling_m: float
    lights: tuple[tuple[float, float], ...] = ()   # fitting positions, room-local

    @property
    def corners(self) -> list[tuple[float, float]]:
        return [(self.x, self.y), (self.x + self.w, self.y),
                (self.x + self.w, self.y + self.h), (self.x, self.y + self.h)]

    def light_points(self) -> list[tuple[float, float]]:
        return [(self.x + u * self.w, self.y + v * self.h) for u, v in self.lights]


@dataclass(frozen=True)
class DemoLevel:
    name: str                   # the HA floor name a reader keys `levels:` by
    elevation_m: float
    rooms: tuple[DemoRoom, ...]


@dataclass(frozen=True)
class DemoCapture:
    """One walk of one level, with the arbitrary frame that walk happened in."""

    capture_id: str
    level: str
    role: str                          # geometry | fixtures
    sees: tuple[str, ...]              # scanner_names this walk covered
    plan_theta_deg: float = 0.0        # the plan's own frame
    plan_dx: float = 0.0
    plan_dy: float = 0.0
    mesh_theta_deg: float = 0.0        # the mesh's frame, different again
    mesh_dx: float = 0.0
    mesh_dy: float = 0.0
    # Wrong on purpose, and NOT rigidly: one room in the wrong place relative to
    # the rest. A whole-capture rotation or shift is just another frame, and
    # registration removes it exactly -- a capture skewed that way agrees with
    # everyone and teaches nothing. Disagreement has to be about the LAYOUT.
    wrong_room: str | None = None
    wrong_shift: tuple[float, float] = (0.0, 0.0)
    wrong_deg: float = 0.0
    seed: int = 0                      # scanner noise, fixed so runs repeat


GROUND = DemoLevel(
    name="Ground Floor",
    elevation_m=0.0,
    rooms=(
        # One open volume the scanner cannot cut, because there is no wall in
        # it. `split:` is the only thing that separates the lounge end from the
        # dining end, and no rescan ever will.
        DemoRoom("Living Room", "open_living", 0.0, 0.0, 6.0, 4.0, 2.45,
                 lights=((0.25, 0.5), (0.75, 0.5))),
        DemoRoom("Hallway", "hallway", 6.0, 0.0, 2.0, 4.0, 2.45,
                 lights=((0.5, 0.5),)),
        DemoRoom("Bathroom", "bathroom", 0.0, 4.0, 3.0, 2.5, 2.35,
                 lights=((0.5, 0.5),)),
        DemoRoom("Kitchen", "kitchen", 3.0, 4.0, 5.0, 2.5, 2.45,
                 lights=((0.35, 0.5), (0.7, 0.5))),
    ),
)

UPSTAIRS = DemoLevel(
    name="Upstairs",
    elevation_m=2.70,
    rooms=(
        DemoRoom("Bedroom", "bedroom", 0.0, 0.0, 4.0, 4.0, 2.40,
                 lights=((0.5, 0.5),)),
        DemoRoom("Office 1", "office", 4.0, 0.0, 4.0, 4.0, 2.40,
                 lights=((0.5, 0.5),)),
        DemoRoom("Landing", "landing", 0.0, 4.0, 8.0, 2.5, 2.40,
                 lights=((0.5, 0.5),)),
    ),
)

LEVELS = (GROUND, UPSTAIRS)

# Three geometry walks per level, because two that disagree cannot say which of
# them is wrong and a third says it immediately. One walk per level skips a
# room, so `combine` has new ground to report; one is wrong about the layout.
CAPTURES = (
    DemoCapture("ground_geometry_0412-0900", "Ground Floor", "geometry",
                ("Living Room", "Hallway", "Bathroom", "Kitchen"),
                plan_theta_deg=0.0, mesh_theta_deg=31.0, mesh_dx=4.0, mesh_dy=-2.0,
                seed=10),
    DemoCapture("ground_geometry_0412-1030", "Ground Floor", "geometry",
                ("Living Room", "Hallway", "Kitchen"),          # skipped the bathroom
                plan_theta_deg=90.0, plan_dx=3.0, mesh_theta_deg=-14.0, mesh_dy=6.0,
                seed=11),
    DemoCapture("ground_geometry_0412-1145", "Ground Floor", "geometry",
                ("Living Room", "Hallway", "Bathroom", "Kitchen"),
                plan_theta_deg=180.0, plan_dy=2.0, mesh_theta_deg=57.0,
                # The odd one out: it puts the kitchen 70 cm from where every
                # other capture puts it, and turns it 9 degrees.
                wrong_room="Kitchen", wrong_shift=(0.70, -0.45), wrong_deg=9.0,
                seed=3),
    DemoCapture("ground_fixtures_0412-1300", "Ground Floor", "fixtures",
                ("Living Room", "Hallway", "Kitchen"),
                plan_theta_deg=270.0, mesh_theta_deg=8.0, mesh_dx=-3.0, seed=12),
    DemoCapture("upstairs_geometry_0412-1420", "Upstairs", "geometry",
                ("Bedroom", "Office 1", "Landing"),
                plan_theta_deg=0.0, mesh_theta_deg=-42.0, mesh_dx=2.0, seed=13),
    DemoCapture("upstairs_geometry_0412-1535", "Upstairs", "geometry",
                ("Bedroom", "Office 1", "Landing"),
                plan_theta_deg=90.0, plan_dx=1.5, mesh_theta_deg=19.0, mesh_dy=-5.0,
                seed=14),
    DemoCapture("upstairs_geometry_0412-1650", "Upstairs", "geometry",
                ("Bedroom", "Landing"),                         # skipped the office
                plan_theta_deg=180.0, mesh_theta_deg=73.0, mesh_dx=-2.5, seed=15),
    DemoCapture("upstairs_fixtures_0412-1755", "Upstairs", "fixtures",
                ("Bedroom", "Office 1", "Landing"),
                plan_theta_deg=45.0, mesh_theta_deg=-6.0, mesh_dy=3.0, seed=16),
)


def _rotate(pts: list[tuple[float, float]], theta_deg: float,
            dx: float, dy: float) -> list[tuple[float, float]]:
    t = math.radians(theta_deg)
    c, s = math.cos(t), math.sin(t)
    return [(x * c - y * s + dx, x * s + y * c + dy) for x, y in pts]


def _level_of(capture: DemoCapture) -> DemoLevel:
    for level in LEVELS:
        if level.name == capture.level:
            return level
    raise ValueError(f"capture {capture.capture_id} names no known level")


def _rooms_of(capture: DemoCapture) -> list[DemoRoom]:
    seen = set(capture.sees)
    return [r for r in _level_of(capture).rooms if r.scanner_name in seen]


def _lerp_quad(pts: list[tuple[float, float]], u: float,
               v: float) -> tuple[float, float]:
    """Bilinear point inside a four-corner outline, u along 0->1, v along 0->3."""
    (x0, y0), (x1, y1), (x2, y2), (x3, y3) = pts
    ax, ay = x0 + (x1 - x0) * u, y0 + (y1 - y0) * u
    bx, by = x3 + (x2 - x3) * u, y3 + (y2 - y3) * u
    return (ax + (bx - ax) * v, ay + (by - ay) * v)


def _stable_seed(base: int, name: str) -> int:
    """A seed that survives a new interpreter.

    `hash()` on a str is salted per process, so seeding with it gives a demo
    whose geometry changes between the run that wrote it and the run that
    checks it -- and a golden test that can never pass twice.
    """
    return (base * 1_000_003 + zlib.crc32(name.encode("utf-8"))) % (2**32)


NOISE_M = 0.025     # per-corner scanner noise. Real captures land 1-5 cm apart,
                    # and a demo that lands at 0.0 makes `combine`'s "x best"
                    # ratio divide by nothing.


def _as_drawn(room: DemoRoom, capture: DemoCapture) -> list[tuple[float, float]]:
    """The room's corners as THIS capture traced them.

    Two things happen here and they are different. Every capture gets noise,
    because two scans of one wall never agree exactly. ONE capture also gets a
    room in the wrong place, which is not noise and does not average out: its
    own mesh is built from the same wrong corners, so it registers perfectly
    against itself and only `combine`, comparing it with the others, can see it.
    """
    import numpy as np

    rng = np.random.default_rng(_stable_seed(capture.seed, room.scanner_name))
    pts = [(x + float(rng.normal(0, NOISE_M)), y + float(rng.normal(0, NOISE_M)))
           for x, y in room.corners]
    if capture.wrong_room == room.scanner_name:
        cx = sum(p[0] for p in pts) / len(pts)
        cy = sum(p[1] for p in pts) / len(pts)
        local = [(x - cx, y - cy) for x, y in pts]
        turned = _rotate(local, capture.wrong_deg, 0.0, 0.0)
        pts = [(x + cx + capture.wrong_shift[0], y + cy + capture.wrong_shift[1])
               for x, y in turned]
    return pts


# --------------------------------------------------------------------------- #
# the mesh: Z-up, textured, in metres
# --------------------------------------------------------------------------- #

ATLAS_PX = 512
TILE_PX = 128           # one ceiling tile per room, in a 4 x 2 grid
CEILING_CELL_M = 0.20   # ceiling subdivision; a fitting must span a few faces
SURFACE_CELL_M = 0.12   # floor and wall subdivision; see grid_quad
LIGHT_RADIUS_PX = 6


def _uv(px: float, py: float) -> tuple[float, float]:
    """Pixel to UV. Row 0 of the image is v = 1: `fixtures` samples with
    `sy = (1 - v) * (h - 1)`, so getting this backwards paints every ceiling
    with the floor's colour and the detector finds nothing."""
    return (px / (ATLAS_PX - 1), 1.0 - py / (ATLAS_PX - 1))


def _build_atlas(rooms: list[DemoRoom]) -> Any:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (ATLAS_PX, ATLAS_PX), (38, 34, 30))
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, 63, 63], fill=(120, 92, 64))         # floor swatch
    draw.rectangle([64, 0, 127, 63], fill=(176, 172, 166))     # wall swatch

    for i, room in enumerate(rooms):
        tx, ty = _tile_origin(i)
        draw.rectangle([tx, ty, tx + TILE_PX - 1, ty + TILE_PX - 1],
                       fill=(208, 206, 202))                # ceiling swatch
        for lu, lv in room.lights:
            cx = tx + lu * (TILE_PX - 1)
            cy = ty + lv * (TILE_PX - 1)
            draw.ellipse([cx - LIGHT_RADIUS_PX, cy - LIGHT_RADIUS_PX,
                          cx + LIGHT_RADIUS_PX, cy + LIGHT_RADIUS_PX],
                         fill=(255, 255, 255))
    return img


def _tile_origin(index: int) -> tuple[int, int]:
    cols = ATLAS_PX // TILE_PX
    return ((index % cols) * TILE_PX,
            ATLAS_PX // 2 + (index // cols) * TILE_PX)


FLOOR_UV = _uv(32, 32)
WALL_UV = _uv(96, 32)


def build_mesh(capture: DemoCapture) -> Any:
    """The capture's mesh, in its own world frame and its own metres.

    Z-up, because `mesh.py` refuses a mesh with no up-facing horizontal faces
    and that refusal is the only thing standing between a Y-up export and a
    floor plan lying on its side.
    """
    import numpy as np
    import trimesh
    from trimesh.visual import TextureVisuals
    from trimesh.visual.material import SimpleMaterial

    level = _level_of(capture)
    rooms = _rooms_of(capture)
    z0 = level.elevation_m
    verts: list[tuple[float, float, float]] = []
    faces: list[tuple[int, int, int]] = []
    uvs: list[tuple[float, float]] = []

    def quad(a: tuple[float, float, float], b: tuple[float, float, float],
             c: tuple[float, float, float], d: tuple[float, float, float],
             uv: list[tuple[float, float]]) -> None:
        n = len(verts)
        verts.extend([a, b, c, d])
        uvs.extend(uv)
        faces.extend([(n, n + 1, n + 2), (n, n + 2, n + 3)])

    def grid_quad(a: tuple[float, float, float], b: tuple[float, float, float],
                  c: tuple[float, float, float], d: tuple[float, float, float],
                  uv: tuple[float, float], cell_m: float = SURFACE_CELL_M) -> None:
        """A quad subdivided into cell-sized pieces, a->b by d->c.

        Registration matches the plan against the CENTROIDS of vertical mesh
        faces, so a wall modelled as one quad contributes two points however
        long it is. A real scan gives thousands. Undivided, the demo registers
        at 38 cm where the house it imitates does 1 to 5, and the fitter is not
        at fault -- there is nothing there to fit.
        """
        import numpy as np

        pa, pb = np.array(a, dtype=float), np.array(b, dtype=float)
        pc, pd = np.array(c, dtype=float), np.array(d, dtype=float)
        nu = max(1, int(round(float(np.linalg.norm(pb - pa)) / cell_m)))
        nv = max(1, int(round(float(np.linalg.norm(pd - pa)) / cell_m)))
        for i in range(nu):
            for j in range(nv):
                u0, u1 = i / nu, (i + 1) / nu
                v0, v1 = j / nv, (j + 1) / nv

                def at(u: float, v: float) -> tuple[float, float, float]:
                    top = pa + (pb - pa) * u
                    bot = pd + (pc - pd) * u
                    p = top + (bot - top) * v
                    return (float(p[0]), float(p[1]), float(p[2]))

                quad(at(u0, v0), at(u1, v0), at(u1, v1), at(u0, v1), [uv] * 4)

    for index, room in enumerate(rooms):
        drawn = _as_drawn(room, capture)
        world = _rotate(drawn, capture.mesh_theta_deg, capture.mesh_dx, capture.mesh_dy)
        top = z0 + room.ceiling_m
        (x0, y0), (x1, y1), (x2, y2), (x3, y3) = world

        # Floor: wound so the normal is +z. mesh.py finds the storey from these.
        grid_quad((x0, y0, z0), (x1, y1, z0), (x2, y2, z0), (x3, y3, z0),
                  FLOOR_UV)

        # Walls, both faces of the loop. Vertical, so registration has wall
        # points to fit the plan's centrelines onto.
        ring = list(world) + [world[0]]
        for (ax, ay), (bx, by) in zip(ring, ring[1:], strict=False):
            grid_quad((ax, ay, top), (bx, by, top), (bx, by, z0), (ax, ay, z0),
                      WALL_UV)

        # Ceiling, subdivided so a fitting covers several faces rather than
        # one. A single quad per room would give the detector one sample and
        # `fixtures` clusters in 3D -- one face is not a cluster.
        tx, ty = _tile_origin(index)
        nx = max(2, int(round(room.w / CEILING_CELL_M)))
        ny = max(2, int(round(room.h / CEILING_CELL_M)))
        for i in range(nx):
            for j in range(ny):
                us = (i / nx, (i + 1) / nx)
                vs = (j / ny, (j + 1) / ny)
                corners_local = [(us[0], vs[0]), (us[1], vs[0]),
                                 (us[1], vs[1]), (us[0], vs[1])]
                pts = [_lerp_quad(drawn, u, v) for u, v in corners_local]
                pts = _rotate(pts, capture.mesh_theta_deg,
                              capture.mesh_dx, capture.mesh_dy)
                cell_uv = [_uv(tx + u * (TILE_PX - 1), ty + v * (TILE_PX - 1))
                           for u, v in corners_local]
                # Reversed against the floor, so the normal points down: that
                # is what marks these faces as ceiling to `fixtures`.
                quad((pts[3][0], pts[3][1], top), (pts[2][0], pts[2][1], top),
                     (pts[1][0], pts[1][1], top), (pts[0][0], pts[0][1], top),
                     [cell_uv[3], cell_uv[2], cell_uv[1], cell_uv[0]])

    mesh = trimesh.Trimesh(vertices=np.array(verts, dtype=float),
                           faces=np.array(faces, dtype=int), process=False)
    mesh.visual = TextureVisuals(uv=np.array(uvs, dtype=float),
                                 material=SimpleMaterial(image=_build_atlas(rooms)))
    return mesh


# --------------------------------------------------------------------------- #
# packaging: two archives per capture, every inner file named the same
# --------------------------------------------------------------------------- #


def write_plan(capture: DemoCapture, directory: Path) -> tuple[Path, Path]:
    """The DXF and CSV, in this capture's own arbitrary plan frame."""
    sheet = Sheet().floor_label("Floor 1", x=3.0)
    for room in _rooms_of(capture):
        pts = _rotate(_as_drawn(room, capture), capture.plan_theta_deg,
                      capture.plan_dx, capture.plan_dy)
        sheet.polygon_room(room.scanner_name, pts, room.ceiling_m)
        ring = list(pts) + [pts[0]]
        for (ax, ay), (bx, by) in zip(ring, ring[1:], strict=False):
            sheet.wall(ax, ay, bx, by)
    # One door and one swing arc, so the 2-point-entity skip is exercised.
    sheet.door(0.5, 0.0).swing_arc(1.5, 0.0)
    return sheet.write(directory, stem=EXPORT_STEM)


def write_capture(capture: DemoCapture, downloads: Path) -> tuple[Path, Path]:
    """Both archives, named as Polycam names them: by date, not by capture."""
    import shutil

    staging = downloads / f".staging_{capture.capture_id}"
    plan_dir = staging / "plan"
    mesh_dir = staging / "mesh"
    plan_dir.mkdir(parents=True, exist_ok=True)
    mesh_dir.mkdir(parents=True, exist_ok=True)

    dxf_path, csv_path = write_plan(capture, plan_dir)
    obj_path = mesh_dir / f"{EXPORT_STEM}.obj"
    build_mesh(capture).export(obj_path)

    plan_zip = downloads / f"{EXPORT_STEM} ({_download_index(capture, 'plan')}).zip"
    mesh_zip = downloads / f"{EXPORT_STEM} ({_download_index(capture, 'mesh')}).zip"

    with zipfile.ZipFile(plan_zip, "w", zipfile.ZIP_DEFLATED) as z:
        for p in (dxf_path, csv_path):
            z.write(p, p.name)
    with zipfile.ZipFile(mesh_zip, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(mesh_dir.iterdir()):
            z.write(p, p.name)

    shutil.rmtree(staging, ignore_errors=True)
    return plan_zip, mesh_zip


def _download_index(capture: DemoCapture, kind: str) -> int:
    """The browser's download counter, which is the ONLY thing telling two
    archives apart. Interleaved plan/mesh exactly as exporting one capture at a
    time produces them, so the counter does not encode the pairing either."""
    order = [c.capture_id for c in CAPTURES].index(capture.capture_id)
    return order * 2 + (1 if kind == "mesh" else 0)


# --------------------------------------------------------------------------- #
# a Home Assistant registry, so `lights` runs with no Home Assistant
# --------------------------------------------------------------------------- #

# Shaped like `ha.fetch_registry`'s cache. Deliberately awkward in the three
# ways a real registry is, because each one drives a different branch of
# `lights` and a tidy registry exercises none of them:
#
#   light.hall_ceiling      entity area set, device area DIFFERENT -- the
#                           entity's own area must win, or a multi-gang switch
#                           files its lights wherever the switch is screwed
#   light.kitchen_group     hangs off the COORDINATOR device: a ZHA group, and
#                           placing it with its members renders them twice
#   light.landing_status    an indicator, not room lighting
DEMO_AREAS = ["open_living", "hallway", "bathroom", "kitchen",
              "bedroom", "office", "landing"]

_ENTITIES: list[dict[str, Any]] = [
    {"entity_id": "light.living_west", "area_id": "open_living",
     "device_id": "dev_bulb_1", "original_name": "Living West"},
    {"entity_id": "light.living_east", "area_id": "open_living",
     "device_id": "dev_bulb_2", "original_name": "Living East"},
    # No area of its own: it has to fall back to its device's.
    {"entity_id": "light.bathroom_ceiling", "area_id": None,
     "device_id": "dev_bulb_3", "original_name": "Bathroom Ceiling"},
    {"entity_id": "light.kitchen_west", "area_id": "kitchen",
     "device_id": "dev_bulb_4", "original_name": "Kitchen West"},
    {"entity_id": "light.kitchen_east", "area_id": "kitchen",
     "device_id": "dev_bulb_5", "original_name": "Kitchen East"},
    {"entity_id": "light.kitchen_group", "area_id": "kitchen",
     "device_id": "dev_coordinator", "original_name": "Kitchen Lights"},
    {"entity_id": "light.hall_ceiling", "area_id": "hallway",
     "device_id": "dev_gang_switch", "original_name": "Hall Ceiling"},
    {"entity_id": "light.bedroom_ceiling", "area_id": "bedroom",
     "device_id": "dev_bulb_6", "original_name": "Bedroom Ceiling"},
    {"entity_id": "light.office_ceiling", "area_id": "office",
     "device_id": "dev_bulb_7", "original_name": "Office Ceiling"},
    {"entity_id": "light.landing_ceiling", "area_id": "landing",
     "device_id": "dev_bulb_8", "original_name": "Landing Ceiling"},
    {"entity_id": "light.landing_status", "area_id": "landing",
     "device_id": "dev_router", "original_name": "Landing Status LED"},
]

_DEVICES: list[dict[str, Any]] = [
    {"id": "dev_bulb_1", "area_id": "open_living", "model": "LWB010"},
    {"id": "dev_bulb_2", "area_id": "open_living", "model": "LWB010"},
    {"id": "dev_bulb_3", "area_id": "bathroom", "model": "LWB010"},
    {"id": "dev_bulb_4", "area_id": "kitchen", "model": "TS0505B"},
    {"id": "dev_bulb_5", "area_id": "kitchen", "model": "TS0505B"},
    {"id": "dev_bulb_6", "area_id": "bedroom", "model": "LWO003"},
    {"id": "dev_bulb_7", "area_id": "office", "model": "LWO003"},
    {"id": "dev_bulb_8", "area_id": "landing", "model": "LWO003"},
    # The gang switch lives in the hallway but drives a light elsewhere; the
    # entity's own area is the only thing that says so.
    {"id": "dev_gang_switch", "area_id": "bathroom", "model": "TS0003"},
    {"id": "dev_coordinator", "area_id": "office",
     "model": "Generic Zigbee Coordinator (EZSP)"},
    {"id": "dev_router", "area_id": "landing", "model": "GT-AX11000"},
]


def demo_registry() -> dict[str, Any]:
    return {
        "areas": [{"area_id": a, "name": a.replace("_", " ").title(),
                   "floor_id": None} for a in DEMO_AREAS],
        "floors": [{"floor_id": "ground", "name": "Ground Floor"},
                   {"floor_id": "upstairs", "name": "Upstairs"}],
        "devices": _DEVICES,
        "entities": _ENTITIES,
        "states": [{"entity_id": e["entity_id"],
                    "state": "off",
                    "attributes": {"friendly_name": e["original_name"],
                                   "supported_color_modes": ["brightness"]}}
                   for e in _ENTITIES],
    }


# --------------------------------------------------------------------------- #
# the project file
# --------------------------------------------------------------------------- #


def demo_project(name: str) -> str:
    """A project.yaml that is already filled in, because the tutorial's job is
    to explain what each section says -- not to have the reader retype it."""
    lines: list[str] = [
        "# lidar2ha demo project. Paths are relative to this file.",
        f"name: {name}",
        "",
        "# Which captures make up each level. ALWAYS A LIST, even of one.",
        "levels:",
    ]
    for level in LEVELS:
        lines.append(f'  "{level.name}":')
        for cap in CAPTURES:
            if cap.level == level.name:
                lines.append(f"    - {cap.capture_id}")
    lines += ["", "# Scanner room name -> Home Assistant area id, per capture.",
              "# The scanner guesses these names; only you know the areas.",
              "rooms:"]
    for cap in CAPTURES:
        lines.append(f"  {cap.capture_id}:")
        for room in _rooms_of(cap):
            lines.append(f'    "{room.scanner_name}": {room.area}')
    lines += [
        "",
        "# The open volume the scanner cannot cut, because there is no wall in",
        "# it to cut on. Coordinates are plan centimetres in the COMBINED",
        "# model's frame -- read them off `python -m lidar2ha.preview`.",
        "split: {}",
        "",
        "lights:",
        "  exclude:",
        "    # A ZHA group: its entity hangs off the coordinator, not off a lamp.",
        "    # Placed alongside its members it is the same bulbs twice, and the",
        "    # plugin SUMS sources sharing a name -- so the room renders quietly",
        "    # too bright rather than erroring.",
        "    - light.kitchen_group",
        "    # An indicator on a router, not room lighting.",
        "    - light.landing_status",
        "",
        "camera:",
        "  yaw: 180",
        "  pitch: 50",
        "",
        "render:",
        "  width: 1280",
        "  height: 720",
        "  quality: HIGH      # LOW does not raytrace; it returns a blank frame.",
        "  renderer: SUNFLOW",
        "  mixing: CSS        # OVERLAY and FULL are exponential. Read `render --list`.",
        "",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# the orchestrator
# --------------------------------------------------------------------------- #


def build_demo(directory: Path) -> dict[str, Any]:
    """Write the demo project. Returns a summary for the caller to print."""
    directory = Path(directory)
    downloads = directory / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)

    written: list[tuple[str, Path, Path]] = []
    for capture in CAPTURES:
        plan_zip, mesh_zip = write_capture(capture, downloads)
        written.append((capture.capture_id, plan_zip, mesh_zip))

    (directory / "project.yaml").write_text(demo_project(directory.name),
                                            encoding="utf-8")
    (directory / "registry.json").write_text(
        json.dumps(demo_registry(), indent=2), encoding="utf-8")
    (directory / "exports").mkdir(exist_ok=True)

    # The answer key. Staging the archives is the reader's job -- that is the
    # exercise -- but a demo you cannot check yourself is a demo you cannot
    # learn from, so the pairing is written down where a reader looks second.
    key = {
        "note": ("Which archive is which. Work it out from the zips first: "
                 "that is what Part 3 is teaching, and it is what "
                 "`lidar2ha add-capture` would do for you."),
        "captures": [
            {"capture_id": cid, "role": c.role, "level": c.level,
             "floorplan_zip": pz.name, "mesh_zip": mz.name}
            for (cid, pz, mz), c in zip(written, CAPTURES, strict=True)
        ],
    }
    (directory / "ANSWER_KEY.json").write_text(json.dumps(key, indent=2),
                                               encoding="utf-8")
    return {"directory": directory, "captures": len(written),
            "archives": 2 * len(written), "downloads": downloads}
