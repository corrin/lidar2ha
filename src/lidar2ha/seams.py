#!/usr/bin/env python3
"""Cut an over-merged room into the rooms a person actually uses.

The counterpart to `rooms.merge`. A scanner segments space by its own logic,
which sometimes fuses rooms a person -- and Home Assistant -- treat as separate.
One capture returned a den and a front entrance as a single 27.7 m2 room with
one 5.2 m ceiling, which erases the lower space's height and makes per-room
selection across captures impossible.

Two ways to say where the rooms are, because they fail differently:

  seam      two points. The polygon is cut by the infinite line through them.
            Cheap, and enough whenever one straight line does it.
  sections  a traced outline per room. The only way to say something a line
            cannot -- an L-shaped kitchen, a dining end that is a corner --
            and the only form that takes more than two rooms at once.

Ceilings are NOT inherited by either: a fused room reports one height for two
spaces, so each piece takes its own, which is the entire reason for splitting.
Measure them with `ceilings` afterwards rather than guessing.

Where to put a seam is often not a matter of taste. `thresholds` sweeps the mesh
floor for the step and the flooring change that mark a real boundary; on the
capture above, a 19 cm step and a wood-to-carpet edge landed on the same line.
But an open plan's boundaries are frequently a matter of use rather than
construction -- the table end against the sofa end -- and no mesh holds those.
The declaration is then the only evidence there is, which is why nothing here
requires the mesh to agree before cutting.

Usage:
    python -m lidar2ha.seams model.json -o split.json --room "Living Room" \\
        --seam -240,-160 20,-170 --names stairwell den
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import yaml
from shapely.geometry import LineString, Polygon
from shapely.ops import split as shapely_split
from shapely.ops import unary_union

from .placefixtures import plan_cm_to_mesh_m
from .rooms import Placed, covered_rooms, polygon_of
from .schema import Level, Model, Registration, Room, load_model, save_model
from .thresholds import FloorSample, Support, boundary_support

MIN_PIECE_CM2 = 1_000     # 0.1 m2; below this it is a sliver, not a room
EXTEND_CM = 10_000        # push the seam well past the polygon so it cuts clean
CM2_PER_M2 = 10_000

# Two traces claiming the same floor. Below this it is the hand that read the
# coordinates off a preview; above it, the declaration genuinely disagrees with
# itself about where a room is. Tuned to nothing -- it is a guess at how
# accurately a person points at a 1 m grid. Raise it if honest traces are being
# refused; lower it if a real disagreement is being absorbed.
OVERLAP_SLOP_M2 = 0.25

# Floor no section claimed. Above this it is a part of the house that wants
# tracing and is kept visible; below it, tracing slop. Set at 1 m2 to match
# `combine.MIN_FRAGMENT_M2`, which draws the same line for the same reason.
MIN_REMAINDER_M2 = 1.0

# A shared edge has zero area, so an absorbed sliver is attributed to the
# section it overlaps once both are grown by this much.
ABSORB_REACH_CM = 1.0


def pair(text: str) -> tuple[float, float]:
    x, y = text.split(",")
    return float(x), float(y)


def split_room(poly: Polygon, a, b) -> list[Polygon]:
    """Cut a polygon by the infinite line through a and b."""
    if not poly.is_valid:
        poly = poly.buffer(0)

    dx, dy = b[0] - a[0], b[1] - a[1]
    n = (dx ** 2 + dy ** 2) ** 0.5
    if n < 1e-9:
        raise ValueError("seam endpoints are the same point")
    ux, uy = dx / n, dy / n

    seam = LineString([(a[0] - ux * EXTEND_CM, a[1] - uy * EXTEND_CM),
                       (b[0] + ux * EXTEND_CM, b[1] + uy * EXTEND_CM)])

    pieces = [p for p in shapely_split(poly, seam).geoms if p.area > MIN_PIECE_CM2]

    # Order by which side of the seam each piece lies on, so the caller's names
    # and ceilings attach predictably instead of by shapely's internal ordering.
    def side(p: Polygon) -> float:
        r = p.representative_point()
        return (r.x - a[0]) * uy - (r.y - a[1]) * ux

    return sorted(pieces, key=side)


@dataclass(frozen=True)
class Piece:
    """One room cut out of a fused one, and why to distrust it."""

    name: str
    poly: Polygon
    reasons: tuple[str, ...] = ()


@dataclass
class Tiling:
    """The pieces, and every way the traces failed to tile the room.

    Each figure is a thing the user did that the geometry had to answer for.
    They are reported rather than merely used, because a trace 3 m2 too big and
    a trace 3 m2 too small both produce a plausible set of rooms.
    """

    pieces: list[Piece]
    spill_m2: dict[str, float] = field(default_factory=dict)
    offcut_m2: dict[str, float] = field(default_factory=dict)
    moved_m2: float = 0.0
    remainder_m2: float = 0.0
    absorbed_m2: float = 0.0

    @property
    def accounted(self) -> bool:
        """Nothing needs saying about this declaration beyond the areas."""
        return not (self.spill_m2 or self.offcut_m2 or self.moved_m2
                    or self.remainder_m2 or self.absorbed_m2)


def _repair(poly: Polygon) -> Polygon:
    return poly if poly.is_valid else poly.buffer(0)


def _polygons(geom) -> list[Polygon]:
    """Only the parts with area.

    Clipping or differencing polygons that share an edge -- which is every
    interesting case here, since the sections are meant to abut -- returns a
    GeometryCollection with the shared edges in it as dangling lines. A line is
    not a thin room; it is the seam itself, and it has no floor.
    """
    parts = getattr(geom, "geoms", [geom])
    return [g for g in parts if g.geom_type == "Polygon" and not g.is_empty]


def _largest(geom) -> tuple[Polygon, float]:
    """The real polygon out of a clip that came back in several pieces."""
    parts = sorted(_polygons(geom), key=lambda g: g.area, reverse=True)
    if not parts:
        return Polygon(), 0.0
    return parts[0], sum(g.area for g in parts[1:])


def sections_of(poly: Polygon, sections: list[tuple[str, Polygon]], *,
                parent_name: str,
                overlap_slop_m2: float = OVERLAP_SLOP_M2,
                min_remainder_m2: float = MIN_REMAINDER_M2) -> Tiling:
    """Partition a fused room into traced sections, accounting for every cm2.

    THE PIECES TILE THE ROOM EXACTLY. A hand-traced outline never matches a
    scanned polygon, so the interesting part of this is not the intersection --
    it is that a trace which overshoots, falls short, or collides with its
    neighbour has an answer here rather than quietly resizing a room. Floor that
    silently stops existing renders as a house that is simply smaller than the
    real one, and nothing on screen says why.

    Overlaps are resolved in DECLARATION ORDER, and only up to a slop: at that
    scale the choice cannot matter, and a rule the user can read off their own
    file beats a geometric tiebreak they would have to reverse-engineer. An
    overlap too big to be slop is refused instead, because it means the
    declaration disagrees with itself about where a room is.
    """
    if len(sections) < 2:
        raise ValueError(
            f"{parent_name!r}: a split needs at least two sections, got "
            f"{len(sections)}")

    poly = _repair(poly)
    tiling = Tiling(pieces=[])

    # --- clip to the room, so a trace over a wall cannot annex the neighbour --
    clipped: list[tuple[str, Polygon]] = []
    for name, traced in sections:
        traced = _repair(traced)
        inside = traced.intersection(poly)
        if traced.area - inside.area > 0:
            tiling.spill_m2[name] = (traced.area - inside.area) / CM2_PER_M2
        part, offcut = _largest(inside)
        if part.is_empty:
            raise ValueError(
                f"section {name!r} lies entirely outside {parent_name!r} -- "
                "read off the wrong preview, or the wrong room named?")
        if offcut:
            tiling.offcut_m2[name] = offcut / CM2_PER_M2
        clipped.append((name, part))

    # --- resolve collisions, refusing anything bigger than the hand ----------
    kept: list[tuple[str, Polygon]] = []
    for i, (name, part) in enumerate(clipped):
        for earlier_name, earlier in clipped[:i]:
            overlap = part.intersection(earlier).area / CM2_PER_M2
            if overlap > overlap_slop_m2:
                raise ValueError(
                    f"{name!r} and {earlier_name!r} overlap by {overlap:.2f} m2, "
                    f"which is more than {overlap_slop_m2} m2 of tracing slop. "
                    "Two sections claim the same floor and nothing here can say "
                    "which is right -- fix the traces.")
            part = part.difference(earlier)
        part, _ = _largest(_repair(part))
        if part.area < MIN_PIECE_CM2:
            raise ValueError(
                f"section {name!r} is left with {part.area / CM2_PER_M2:.2f} m2 "
                "once its neighbours have taken theirs, which is not a room.")
        kept.append((name, part))
    tiling.moved_m2 = (sum(p.area for _, p in clipped)
                       - sum(p.area for _, p in kept)) / CM2_PER_M2

    # --- and give the floor nobody traced somewhere to go --------------------
    tiling.pieces = [Piece(name, part) for name, part in kept]
    left = poly.difference(unary_union([p for _, p in kept]))
    remainders = []
    for bit in _polygons(left):
        if bit.area / CM2_PER_M2 >= min_remainder_m2:
            # Kept under the parent's name: it is a real part of the house, and
            # folding it into a neighbour would hide the trace that needs fixing.
            remainders.append(Piece(parent_name, bit, ("unclaimed_remainder",)))
            tiling.remainder_m2 += bit.area / CM2_PER_M2
            continue
        near = max(range(len(tiling.pieces)),
                   key=lambda k: bit.buffer(ABSORB_REACH_CM)
                   .intersection(tiling.pieces[k].poly).area)
        grown, _ = _largest(_repair(unary_union([tiling.pieces[near].poly, bit])))
        tiling.pieces[near] = Piece(tiling.pieces[near].name, grown,
                                    tiling.pieces[near].reasons)
        tiling.absorbed_m2 += bit.area / CM2_PER_M2
    tiling.pieces.extend(remainders)

    return tiling


@dataclass(frozen=True)
class Edge:
    """A boundary between two pieces, and what the floor said about it.

    `support` is None when nothing measured it, and `unmeasured` says why. That
    is deliberately not the same answer as a measured boundary the floor does
    not corroborate: an unbroken floor is evidence, and no mesh is not.
    """

    between: tuple[str, str]
    support: Support | None = None
    unmeasured: str = ""


@dataclass
class Cut:
    """One declaration, carried out."""

    level: str
    room: str
    tiling: Tiling
    edges: list[Edge] = field(default_factory=list)


def shared_edge(a: Polygon, b: Polygon) -> tuple[tuple[float, float],
                                                 tuple[float, float]] | None:
    """The ends of the line two pieces have in common, in plan cm."""
    common = a.intersection(b)
    if common.is_empty or "Line" not in common.geom_type:
        return None
    if common.geom_type == "MultiLineString":
        common = max(common.geoms, key=lambda g: g.length)
    if common.length < 1:
        return None
    ends = list(common.coords)
    return (ends[0][0], ends[0][1]), (ends[-1][0], ends[-1][1])


def boundaries_of(pieces: list[Piece], reg: Registration | None,
                  floor: FloorSample | None) -> list[Edge]:
    """Ask the mesh about every boundary the declaration invented.

    Corroboration is all this can offer. An open plan's boundaries are as often
    a matter of use as of construction -- the table end against the sofa end --
    and the floor beneath one of those does not change at all. So a boundary the
    floor cannot see is not a wrong boundary, and nothing here refuses one.
    """
    edges: list[Edge] = []
    for i, one in enumerate(pieces):
        for other in pieces[i + 1:]:
            ends = shared_edge(one.poly, other.poly)
            if ends is None:
                continue
            between = (one.name, other.name)
            if floor is None:
                edges.append(Edge(between, unmeasured="no mesh given"))
                continue
            if reg is None:
                edges.append(Edge(between, unmeasured="level is unregistered"))
                continue
            a_m, b_m = plan_cm_to_mesh_m(np.array(ends), reg)
            support = boundary_support(tuple(a_m), tuple(b_m), floor)
            edges.append(Edge(between, support) if support is not None
                         else Edge(between, unmeasured="floor never photographed"))
    return edges


def outline_of(section: dict, where: str) -> Polygon:
    """A traced section, from either `box:` (two corners) or `outline:`."""
    name = section.get("name")
    if not name:
        raise ValueError(f"{where}: a section needs a `name`")
    if ("box" in section) == ("outline" in section):
        raise ValueError(
            f"{where}: section {name!r} needs exactly one of `box` or `outline`")

    if "box" in section:
        corners = section["box"]
        if len(corners) != 2:
            raise ValueError(
                f"{where}: section {name!r} has a `box` of {len(corners)} "
                "point(s); it is two opposite corners")
        (x0, y0), (x1, y1) = corners
        return Polygon([(x0, y0), (x1, y0), (x1, y1), (x0, y1)])

    points = section["outline"]
    if len(points) < 3:
        raise ValueError(
            f"{where}: section {name!r} has an `outline` of {len(points)} "
            "point(s); a room needs at least three")
    return Polygon([(x, y) for x, y in points])


def find_room(level: Level, wanted: str) -> Room | None:
    """The declared room, by identity first and the scanner's guess last.

    A declaration is written after `rooms` and `combine` have run, so it names
    an HA area. The other two are accepted so the same file works on a raw
    capture, and are tried in decreasing order of how much they mean.
    """
    for key in ("ha_area", "name", "scanner_name"):
        hit = [r for r in level.rooms if getattr(r, key) == wanted]
        if len(hit) > 1:
            raise ValueError(
                f"{len(hit)} rooms on {level.name!r} answer to {wanted!r} as "
                f"{key} -- rename them before splitting, or nothing can say "
                "which one this declaration means")
        if hit:
            return hit[0]
    return None


def tile(target: Room, declaration: dict, where: str, *,
         overlap_slop_m2: float = OVERLAP_SLOP_M2,
         min_remainder_m2: float = MIN_REMAINDER_M2) -> Tiling:
    """Whichever form the declaration took, as pieces.

    Both forms come back through here so the report, the provenance and the
    ceiling rule are written once. Two hand-written copies of one operation is
    how the seam path and the section path drift apart.
    """
    has_seam, has_sections = "seam" in declaration, "sections" in declaration
    if has_seam == has_sections:
        raise ValueError(
            f"{where}: needs exactly one of `seam` or `sections`")

    label = target.ha_area or target.name or target.scanner_name or "?"
    poly = Polygon([(x, y) for x, y in target.points])

    if has_sections:
        sections = [(s["name"], outline_of(s, where))
                    for s in declaration["sections"]]
        return sections_of(poly, sections, parent_name=label,
                           overlap_slop_m2=overlap_slop_m2,
                           min_remainder_m2=min_remainder_m2)

    names = declaration.get("names")
    if not names or len(names) != 2:
        raise ValueError(f"{where}: a `seam` needs exactly two `names`")
    a, b = (tuple(p) for p in declaration["seam"])
    pieces = split_room(poly, a, b)
    if len(pieces) != 2:
        raise ValueError(
            f"{where}: seam produced {len(pieces)} piece(s), expected 2 -- "
            "does the line actually cross the room?")
    return Tiling(pieces=[Piece(n, p)
                          for n, p in zip(names, pieces, strict=True)])


def apply(model: Model, declarations: list[dict], *,
          level_name: str | None = None, floor: FloorSample | None = None,
          overlap_slop_m2: float = OVERLAP_SLOP_M2,
          min_remainder_m2: float = MIN_REMAINDER_M2) -> list[Cut]:
    """Carry out every declaration, in place. Returns what to report.

    Ceilings are left unmeasured on purpose. The fused room's height is one
    number standing for two spaces -- inheriting it would carry the very error
    the split exists to remove, and it would look like a measurement.

    A boundary the floor does not corroborate is reported and NOT marked
    provisional. That flag means "the best geometry available is still not good
    enough, go and re-scan", and no rescan will ever reveal the edge of a sofa
    end. Setting it on every open-plan room forever would fire on everything and
    so mean nothing, which is the same reason the redundancy report judges a
    level against itself rather than an absolute.
    """
    levels = [lv for lv in model.levels
              if level_name is None or lv.name == level_name]
    if level_name is not None and not levels:
        raise ValueError(f"no level named {level_name!r} in the model")

    cuts: list[Cut] = []
    for declaration in declarations:
        wanted = declaration.get("room")
        if not wanted:
            raise ValueError("a split declaration needs a `room`")

        hits: list[tuple[Level, Room]] = []
        for lv in levels:
            here = find_room(lv, wanted)
            if here is not None:
                hits.append((lv, here))
        if not hits:
            raise ValueError(
                f"no room {wanted!r} on "
                f"{level_name or 'any level'} -- check the name against "
                "`python -m lidar2ha.preview`")

        for lv, target in hits:
            where = f"{lv.name}/{wanted}"
            tiling = tile(target, declaration, where,
                          overlap_slop_m2=overlap_slop_m2,
                          min_remainder_m2=min_remainder_m2)
            ceilings = declaration.get("ceilings")

            new_rooms = []
            for i, piece in enumerate(tiling.pieces):
                ceiling = ceilings[i] if ceilings and i < len(ceilings) else None
                new_rooms.append(Room(
                    name=piece.name,
                    points=[(round(x, 1), round(y, 1))
                            for x, y in piece.poly.exterior.coords[:-1]],
                    scanner_name=target.scanner_name or target.name,
                    ha_area=piece.name if target.ha_area else None,
                    split_from=target.ha_area or target.name,
                    ceiling_high_cm=ceiling,
                    ceiling_low_cm=ceiling,
                    # The geometry is the parent's, cut. How it was won and how
                    # far to trust it are unchanged by where the line went.
                    source=target.source,
                    score=target.score,
                    provisional=target.provisional or bool(piece.reasons),
                    provisional_reason=list(target.provisional_reason)
                    + list(piece.reasons),
                ))

            lv.rooms = [r for r in lv.rooms if r is not target] + new_rooms
            cuts.append(Cut(lv.name, wanted, tiling,
                            boundaries_of(tiling.pieces, lv.registration, floor)))

    return cuts


def report_overlaps(model: Model, *, level_name: str | None = None) -> None:
    """Whether any room now sits on top of another, across the whole level.

    THE PIECES OF ONE CUT TILE THEIR PARENT EXACTLY, so a split cannot overlap
    itself -- but it is where the small piece comes into existence, and a small
    piece is what gets buried. On one real house a 3.2 m2 fused room split into
    a 1.44 m2 toilet and a 1.74 m2 hallway, and the toilet then sat entirely
    inside a hallway polygon that came from somewhere else entirely. Before the
    split there was one room and nothing to see; after it there is a room that
    disappears.

    Checked over every room on the level rather than within the cut, because
    the polygon that buries a new piece belongs to a different room.
    """
    findings = []
    for lv in model.levels:
        if level_name is not None and lv.name != level_name:
            continue
        placed = [Placed(str(r.ha_area or r.name or r.scanner_name or "?"),
                         str(r.source or lv.name), polygon_of(r))
                  for r in lv.rooms if len(r.points) >= 3]
        findings += covered_rooms(placed)

    if not findings:
        return
    print(f"\n  {len(findings)} room(s) now lie inside another:")
    for item in findings:
        print(f"    {item['overlap_m2']:.2f} m2 of {item['covered']} "
              f"({item['covered_area_m2']} m2) is inside {item['covering']} "
              f"-- {item['share_of_covered'] * 100:.0f}% of it")
    print("    Both are in the model, so the smaller draws underneath and an "
          "audit\n    counting named areas cannot see it. Decide which is right.")


def report(cuts: list[Cut]) -> None:
    for cut in cuts:
        whole = sum(p.poly.area for p in cut.tiling.pieces) / CM2_PER_M2
        print(f"\n{cut.room}  {whole:.1f} m2  ->  {len(cut.tiling.pieces)} pieces"
              f"   [{cut.level}]")
        for piece in cut.tiling.pieces:
            print(f"  {piece.name:<16} {piece.poly.area / CM2_PER_M2:6.2f} m2"
                  f"   {len(piece.poly.exterior.coords) - 1:>2} pts")
            for reason in piece.reasons:
                print(f"     provisional: {reason}")

        for edge in cut.edges:
            pair_of = " | ".join(edge.between)
            if edge.support is None:
                print(f"  {pair_of:<24} NOT LOOKED AT   {edge.unmeasured}")
            elif edge.support.corroborated:
                print(f"  {pair_of:<24} CORROBORATED    "
                      f"{edge.support.step_cm:.0f} cm step, colour shift "
                      f"{edge.support.colour:.0f}, "
                      f"{abs(edge.support.offset_cm):.0f} cm off the line")
            else:
                print(f"  {pair_of:<24} DECLARED        "
                      "the floor does not change here")

        t = cut.tiling
        for name, m2 in sorted(t.spill_m2.items()):
            print(f"  note: {name} was traced {m2:.2f} m2 outside the room; "
                  "clipped to it")
        for name, m2 in sorted(t.offcut_m2.items()):
            print(f"  note: {name} clipped into disjoint pieces; kept the "
                  f"largest, dropped {m2:.2f} m2")
        if t.moved_m2:
            print(f"  note: {t.moved_m2:.2f} m2 of overlap resolved in "
                  "declaration order")
        if t.remainder_m2:
            print(f"  note: {t.remainder_m2:.2f} m2 traced by nobody, kept as "
                  f"{cut.room}; extend a section to claim it")
        if t.absorbed_m2:
            print(f"  note: {t.absorbed_m2:.2f} m2 of slop absorbed into the "
                  "nearest section")

    if cuts:
        print("\n  ceilings are unmeasured on every piece -- a fused room's "
              "height is\n  one number for two spaces. Run "
              "`python -m lidar2ha.ceilings`.")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("model")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--project", help="project.yaml holding a `split:` section")
    ap.add_argument("--level", help="which level's `split:` entry to apply")
    ap.add_argument("--room", help="split one room from the command line instead")
    ap.add_argument("--seam", nargs=2, metavar="X,Y")
    ap.add_argument("--names", nargs=2,
                    help="names for the two pieces, ordered by side of the seam")
    ap.add_argument("--ceilings", nargs=2, type=float,
                    help="ceiling height in cm per piece; prefer measuring with "
                         "`ceilings` to asserting a number here")
    ap.add_argument("--overlap-slop-m2", type=float, default=OVERLAP_SLOP_M2,
                    help="traced overlap treated as the hand rather than a "
                         "disagreement")
    ap.add_argument("--min-remainder-m2", type=float, default=MIN_REMAINDER_M2,
                    help="untraced floor kept as its own piece rather than "
                         "absorbed")
    args = ap.parse_args()

    if bool(args.project) == bool(args.room):
        raise SystemExit(
            "give either --project (with --level) or --room (with --seam)")

    if args.project:
        if not args.level:
            raise SystemExit("--project needs --level to say which entry to apply")
        project = yaml.safe_load(
            Path(args.project).read_text(encoding="utf-8")) or {}
        declarations = (project.get("split") or {}).get(args.level) or []
        if not declarations:
            raise SystemExit(
                f"no `split:` entry for level {args.level!r} in {args.project}")
    else:
        if not args.seam or not args.names:
            raise SystemExit("--room needs --seam and --names")
        declarations = [{"room": args.room,
                         "seam": [pair(args.seam[0]), pair(args.seam[1])],
                         "names": args.names,
                         "ceilings": args.ceilings}]

    model = load_model(args.model)
    try:
        cuts = apply(model, declarations, level_name=args.level,
                     overlap_slop_m2=args.overlap_slop_m2,
                     min_remainder_m2=args.min_remainder_m2)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    report(cuts)
    report_overlaps(model, level_name=args.level)
    save_model(model, args.out)
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
