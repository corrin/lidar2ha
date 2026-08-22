#!/usr/bin/env python3
"""Turn home.json into the flat text format BuildHome.java reads.

Why a second format rather than reading JSON in Java: the writer has to run on
Sweet Home 3D's own classes, and dragging a JSON library onto that classpath
buys nothing. A pipe-delimited line format parses with split() and cannot fail
in an interesting way.

Two corrections are applied here, in one place, rather than being scattered:

1. **Y is flipped.** DXF has Y increasing upward; Sweet Home 3D's plan has Y
   increasing downward. Without the flip every plan comes out mirrored, which
   is the kind of error that looks plausible until a door is on the wrong side.

2. **Coordinates are re-origined** so the model starts near (0, 0). Polycam
   sheet coordinates are fine but arbitrary; keeping them would leave the model
   floating away from Sweet Home 3D's origin.

Format:
    LEVEL|name|elevationCm|floorThicknessCm|heightCm
    WALL|levelIndex|xStart|yStart|xEnd|yEnd|thicknessCm|heightCm
    ROOM|levelIndex|name|x1,y1;x2,y2;...

Run from PowerShell (see CLAUDE.md). Usage:
    python home_to_plan.py home.json -o plan.txt [--elevation "Floor 2=140"]
"""

import argparse
import json
from pathlib import Path

FLOOR_THICKNESS_CM = 12.0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("json_path")
    ap.add_argument("-o", "--out", default="plan.txt")
    ap.add_argument("--elevation", action="append", default=[],
                    metavar="NAME=CM",
                    help="set a level's elevation in cm, e.g. --elevation 'Floor 2=140'. "
                         "The DXF is 2D and carries none; the mesh may be too "
                         "incomplete to recover it.")
    ap.add_argument("--textures", metavar="DIR",
                    help="directory holding floor.png / wall.png / ceiling.png "
                         "from extract_textures.py")
    ap.add_argument("--tile-cm", type=float, default=100.0,
                    help="physical size of one texture tile, in cm")
    ap.add_argument("--walltex", metavar="MANIFEST",
                    help="manifest.json from project_wall_textures.py, giving "
                         "each wall its own rectified image")
    args = ap.parse_args()

    # Per-wall rectified textures, keyed (level, wall). Walls the scan missed
    # simply have no entry and fall back to the tiled patch.
    walltex = {}
    if args.walltex:
        for e in json.loads(Path(args.walltex).read_text(encoding="utf-8")):
            walltex[(e["level"], e["wall"])] = (e.get("left", ""), e.get("right", ""))

    overrides = {}
    for item in args.elevation:
        name, _, value = item.partition("=")
        overrides[name.strip()] = float(value)

    model = json.loads(Path(args.json_path).read_text(encoding="utf-8"))
    levels = model["levels"]

    # Re-origin against every coordinate in the model, so levels keep their
    # relative placement on the sheet.
    xs, ys = [], []
    for lv in levels:
        for w in lv["walls"]:
            xs += [w["xStart"], w["xEnd"]]
            ys += [w["yStart"], w["yEnd"]]
        for r in lv["rooms"]:
            xs += [p[0] for p in r["points"]]
            ys += [p[1] for p in r["points"]]
    if not xs:
        raise SystemExit("no geometry in " + args.json_path)
    x0, y1 = min(xs), max(ys)

    def fx(x):
        return round(x - x0, 3)

    def fy(y):
        # Flip Y (DXF up -> SH3D down) and re-origin in the same step.
        return round(y1 - y, 3)

    lines = []

    # Textures are emitted first so BuildHome has them registered before any
    # wall or room needs one.
    if args.textures:
        tex_dir = Path(args.textures).resolve()
        for kind in ("floor", "wall", "ceiling"):
            path = tex_dir / f"{kind}.png"
            if path.exists():
                lines.append(f"TEXTURE|{kind}|{path}|{args.tile_cm}|{args.tile_cm}")
            else:
                print(f"  note: no {kind}.png in {tex_dir}, skipping")

    for i, lv in enumerate(levels):
        name = lv["name"]
        elev = overrides.get(name, lv.get("elevation_cm"))
        if elev is None:
            elev = 0.0
        lines.append(
            f"LEVEL|{name}|{elev}|{FLOOR_THICKNESS_CM}|{lv['ceiling_height_cm']}"
        )

    for i, lv in enumerate(levels):
        for wi, w in enumerate(lv["walls"]):
            left, right = walltex.get((i, wi), ("", ""))
            lines.append(
                f"WALL|{i}|{fx(w['xStart'])}|{fy(w['yStart'])}"
                f"|{fx(w['xEnd'])}|{fy(w['yEnd'])}"
                f"|{round(w['thickness'], 3)}|{round(w['height'], 3)}"
                f"|{left}|{right}"
            )
        for r in lv["rooms"]:
            pts = ";".join(f"{fx(p[0])},{fy(p[1])}" for p in r["points"])
            lines.append(f"ROOM|{i}|{r['name']}|{pts}")

    Path(args.out).write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"wrote {args.out}  ({len(lines)} lines)")
    for i, lv in enumerate(levels):
        elev = overrides.get(lv["name"], lv.get("elevation_cm")) or 0.0
        flag = "" if lv["name"] in overrides or lv.get("elevation_cm") is not None \
            else "   <- elevation UNKNOWN, defaulted to 0"
        print(f"  level {i}: {lv['name']:<10} elevation={elev:>6.1f}cm{flag}")


if __name__ == "__main__":
    main()
