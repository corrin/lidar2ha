#!/usr/bin/env python3
"""Assemble the fixture crops into one labelled sheet a human can answer.

Twenty-four separate images is not a review, it is a chore nobody finishes. One
grid, with the index and the room under each crop, is a single question: is each
of these a real fitting, and is it in the right room?

That question has to be asked. Detection finds bright compact blobs on ceilings
and walls, and a rooflight is also a bright compact blob -- so the sheet is the
approval step, not a progress report. Nothing downstream should place a fitting
the human has not looked at.

Usage:
    python -m lidar2ha.contactsheet crops/ fixtures_placed.json -o sheet.png
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from PIL import Image, ImageDraw

CELL, PAD, LABEL = 200, 8, 34
COLUMNS = 6


def shorten(text: str, width: int = 26) -> str:
    """Fit an area id under a cell without inventing a nicer name for it.

    Area ids are the human's own, and can be long. Truncating in the middle
    keeps both ends, which is what distinguishes `upstairs_bathroom` from
    `upstairs_bedroom` -- a plain prefix cut would render them identical.
    """
    if len(text) <= width:
        return text
    keep = (width - 1) // 2
    return f"{text[:keep]}…{text[-keep:]}"


def build(crops: Path, placed: list[dict]) -> Image.Image:
    files = sorted(crops.glob("*.png"))
    if not files:
        raise SystemExit(f"no crops in {crops} -- run `python -m lidar2ha.fixtures` first")

    rows = (len(files) + COLUMNS - 1) // COLUMNS
    width = COLUMNS * (CELL + PAD) + PAD
    height = rows * (CELL + PAD + LABEL) + PAD

    sheet = Image.new("RGB", (width, height), (250, 250, 252))
    draw = ImageDraw.Draw(sheet)

    for i, path in enumerate(files):
        row, column = divmod(i, COLUMNS)
        x = PAD + column * (CELL + PAD)
        y = PAD + row * (CELL + PAD + LABEL)

        sheet.paste(Image.open(path).convert("RGB").resize((CELL, CELL)), (x, y))
        draw.rectangle([x, y, x + CELL, y + CELL], outline=(180, 180, 190))

        record = placed[i] if i < len(placed) else {}
        room = shorten(str(record.get("room") or "no room"))
        draw.text((x + 3, y + CELL + 3), f"#{i}  {room}", fill=(20, 20, 30))
        draw.text((x + 3, y + CELL + 17),
                  f"{record.get('surface', '?')}  {record.get('faces', '?')}f  "
                  f"z={record.get('z', '?')}",
                  fill=(110, 110, 125))
    return sheet


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("crops", help="directory of per-candidate crops")
    ap.add_argument("placed", help="fixtures_placed.json from lidar2ha.placefixtures")
    ap.add_argument("-o", "--out", default="contactsheet.png")
    args = ap.parse_args()

    placed = json.loads(Path(args.placed).read_text(encoding="utf-8"))
    sheet = build(Path(args.crops), placed)
    sheet.save(args.out)

    print(f"wrote {args.out}  ({sheet.width}x{sheet.height})")
    print("  Check every crop before these become light placements: a rooflight is")
    print("  also a bright compact blob, and a mirror reflecting a lit fitting is a")
    print("  convincing phantom one.")


if __name__ == "__main__":
    main()
