#!/usr/bin/env python3
"""Assemble the fixture crops into one labelled sheet a human can answer.

Twenty-four separate images is not a review, it is a chore nobody finishes. One
grid, with the index and the room under each crop, is a single question: is each
of these a real fitting, and is it in the right room?

That question has to be asked. Detection finds bright compact blobs on ceilings
and walls, and a rooflight is also a bright compact blob -- so the sheet is the
approval step, not a progress report. Nothing downstream should place a fitting
the human has not looked at.

WINDOWS GO LAST. When the candidates have been differenced against an ordinary
capture (`placefixtures --daylight-mesh`) each carries a verdict, and the sheet
is ordered fittings first, then the ones nothing could be said about, then the
likely windows. Review runs top-down and can stop when the cells stop looking
like lamps -- which is the point, because a ground-level pass produced 38
candidates for perhaps 12-14 real fittings and reading all 38 costs more than
the detection saves. The windows are still HERE, at the bottom, with their
crops: they are demoted, never hidden.

CROPS ARE PAIRED BY NAME, NOT BY POSITION. Each record carries the crop it came
from, because `placefixtures` reorders records and this sorts them again -- and
a positional pairing would put the right pictures under the wrong rooms with
nothing at all to indicate it.

Usage:
    python -m lidar2ha.contactsheet crops/ fixtures_placed.json -o sheet.png
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from PIL import Image, ImageDraw

CELL, PAD, LABEL = 200, 8, 48
COLUMNS = 6

# Fittings first, then the unjudged, then the windows. A record with no verdict
# at all -- no ordinary capture was given -- sorts with the fittings, so a sheet
# built without differencing keeps exactly the order it always had.
VERDICT_ORDER = {"fitting": 0, "": 0, "unseen": 1, "window": 2}
VERDICT_BORDER = {
    "window": (206, 120, 40),      # amber: probably not a lamp
    "unseen": (150, 150, 160),     # grey: nothing was there to compare with
}
DEFAULT_BORDER = (180, 180, 190)


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


def order_for_review(placed: list[dict]) -> list[dict]:
    """Fittings, then the unjudged, then the windows.

    A stable sort, so within each group the detection order survives -- which is
    brightest and biggest first, and is itself a confidence ordering worth
    keeping.
    """
    return sorted(placed, key=lambda r: VERDICT_ORDER.get(str(r.get("verdict") or ""), 0))


def pair_crops(crops: Path, placed: list[dict]) -> tuple[list[tuple[dict, Path]], list[str]]:
    """(record, crop file) pairs, plus everything that could not be paired.

    Records name their crop. Files with no record, and records whose crop is
    missing from the directory, are both returned as problems rather than
    quietly skipped: a sheet with one cell fewer than expected looks completely
    normal, and the missing one is exactly the candidate nobody then reviews.

    Falls back to pairing by position for a fixtures.json written before crops
    were named, and says so, because that is the fragile behaviour this
    replaced.
    """
    files = sorted(crops.glob("*.png"))
    if not files:
        raise SystemExit(f"no crops in {crops} -- run `python -m lidar2ha.fixtures` first")

    problems: list[str] = []
    if not any(record.get("crop") for record in placed):
        problems.append(
            f"No record in this file names its crop, so {len(files)} crop(s) were paired "
            f"by position. Re-run `lidar2ha.fixtures` to name them -- position is only "
            f"correct while nothing has reordered the records.")
        return list(zip(placed, files, strict=False)), problems

    by_name = {path.name: path for path in files}
    pairs = []
    for record in placed:
        name = str(record.get("crop") or "")
        if name in by_name:
            pairs.append((record, by_name.pop(name)))
        else:
            problems.append(f"{name or '(unnamed)'}: no such crop, so "
                            f"{record.get('room', 'this candidate')} is not on the sheet")
    for leftover in sorted(by_name):
        problems.append(f"{leftover}: a crop with no record, so nothing is known about it")
    return pairs, problems


def build(crops: Path, placed: list[dict]) -> tuple[Image.Image, list[str]]:
    pairs, problems = pair_crops(crops, order_for_review(placed))
    if not pairs:
        raise SystemExit("nothing to put on the sheet")

    rows = (len(pairs) + COLUMNS - 1) // COLUMNS
    width = COLUMNS * (CELL + PAD) + PAD
    height = rows * (CELL + PAD + LABEL) + PAD

    sheet = Image.new("RGB", (width, height), (250, 250, 252))
    draw = ImageDraw.Draw(sheet)

    for i, (record, path) in enumerate(pairs):
        row, column = divmod(i, COLUMNS)
        x = PAD + column * (CELL + PAD)
        y = PAD + row * (CELL + PAD + LABEL)
        verdict = str(record.get("verdict") or "")

        sheet.paste(Image.open(path).convert("RGB").resize((CELL, CELL)), (x, y))
        # A flagged cell is outlined thickly enough to find while scrolling,
        # so the boundary between "probably lamps" and "probably glass" is
        # visible without reading a single label.
        draw.rectangle([x, y, x + CELL, y + CELL],
                       outline=VERDICT_BORDER.get(verdict, DEFAULT_BORDER),
                       width=3 if verdict in VERDICT_BORDER else 1)

        # Labelled by the CROP, not by position on the sheet: that is the name
        # in fixtures_placed.json, and the one to quote when excluding it.
        room = shorten(str(record.get("room") or "no room"))
        draw.text((x + 3, y + CELL + 3), f"{path.stem}  {room}", fill=(20, 20, 30))
        draw.text((x + 3, y + CELL + 17),
                  f"{record.get('surface', '?')}  {record.get('faces', '?')}f  "
                  f"z={record.get('z', '?')}",
                  fill=(110, 110, 125))
        if verdict:
            luma = record.get("daylight_luma")
            detail = f"{verdict}" + (f"  luma {luma:.0f}" if luma is not None else "")
            draw.text((x + 3, y + CELL + 31), detail,
                      fill=VERDICT_BORDER.get(verdict, (110, 110, 125)))
    return sheet, problems


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("crops", help="directory of per-candidate crops")
    ap.add_argument("placed", help="fixtures_placed.json from lidar2ha.placefixtures")
    ap.add_argument("-o", "--out", default="contactsheet.png")
    args = ap.parse_args()

    placed = json.loads(Path(args.placed).read_text(encoding="utf-8"))
    sheet, problems = build(Path(args.crops), placed)
    sheet.save(args.out)

    print(f"wrote {args.out}  ({sheet.width}x{sheet.height})")
    counts: dict[str, int] = {}
    for record in placed:
        verdict = str(record.get("verdict") or "not differenced")
        counts[verdict] = counts.get(verdict, 0) + 1
    for verdict, n in sorted(counts.items()):
        print(f"  {verdict:<16} {n}")

    if problems:
        print(f"\n  {len(problems)} PROBLEM(S) -- these are not on the sheet:")
        for problem in problems:
            print(f"    {problem}")

    print("\n  Check every crop before these become light placements: a rooflight is")
    print("  also a bright compact blob, and a mirror reflecting a lit fitting is a")
    print("  convincing phantom one. Anything marked `window` was bright in an")
    print("  ordinary capture too -- likely glass, but the cutoff is only a guess")
    print("  until you have read a sheet against it.")


if __name__ == "__main__":
    main()
