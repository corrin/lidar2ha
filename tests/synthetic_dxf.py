"""Build a Polycam-shaped DXF, so `polycam.main()` can run in a test.

There is no DXF in this repo and there should not be: a real one is a floor plan
of somebody's house. But without one the whole assembly half of `polycam` --
the loop that turns clusters into `Level`s, the ceiling-band split, the report
-- could only ever be exercised by hand against a capture that is not here, and
40% of the module went untested for exactly that reason.

FAITHFUL WHERE IT MATTERS, and only there. What `polycam` actually reads is
four layers and one header field:

    Floor Label       MTEXT, one per sheet cluster; the COUNT sets the number
                      of clusters and the x positions order them
    Poly-RoomLabels   MTEXT, matched to rooms by proximity
    Poly-Rooms        LWPOLYLINE, a closed room outline
    Poly-Walls        LWPOLYLINE, the 7-point wall OUTLINE, emitted TWICE
                      because Polycam does, and the reader deduplicates
    Poly-Doors        LWPOLYLINE of 4+ points; 2-point entities on that layer
                      are swing arcs and are skipped

Everything else a real export carries -- dimensions, furniture, fixtures, the
compass, the logo -- is noise this reader ignores, so none of it is here. If
that stops being true the fixture is wrong rather than incomplete, and the test
that reads a real export's layer list is the one that would say so.
"""

from __future__ import annotations

import csv
import math
from pathlib import Path

import ezdxf


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
            out.writerow(["All", "Entire Roomplan", "Latitude", "-36.944422 m"])
            for name, (low, high) in self.ceilings.items():
                value = (f"{high:.1f}" if low == high
                         else f"{low:.1f} - {high:.1f}")
                out.writerow(["Floor 1", name,
                              "Ceiling height [m] (approx)", value])
                out.writerow(["Floor 1", name, "Area [m2]", "12.0"])
        return dxf_path, csv_path


def one_storey(directory: Path) -> tuple[Path, Path]:
    """A plain single-storey capture: one cluster, one ceiling band.

    The shape of every capture that already worked, and so the one the storey
    split must leave completely alone.
    """
    sheet = Sheet().floor_label("Floor 1", x=3.0)
    sheet.room("Bedroom", 0.0, 0.0, 4.0, 3.0, 2.40)
    sheet.room("Hallway", 4.0, 0.0, 2.0, 3.0, 2.40)
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
