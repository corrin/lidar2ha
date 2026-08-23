#!/usr/bin/env python3
"""Drive the raytracer, and make a long wait legible.

Rendering is the slow step and the only one that can be started by accident at a
ruinous size, so most of this module is about knowing the cost beforehand and
showing progress during.

WHAT IT COSTS. The raytracer runs on Sweet Home 3D's bundled runtime, which is a
32-bit Java 8 JRE, because Java3D and YafaRay ship as 32-bit natives. One
process, CPU only, and it cannot use a fast machine the way you would hope.
Measured on a real model -- 32 walls, 7 rooms -- it took 179.7 s for 7 renders at
800x600, about 26 seconds a frame. Scene complexity matters as much as pixel
count: a near-empty test scene managed 6.5 s a frame at 640x360.

AND ONE SETTING MULTIPLIES IT. The plugin's light mixing mode decides how many
images get made:

    CSS      one render per light; the browser adds them together      n + 1
    OVERLAY  every combination of the lights in each room              2^(room)
    FULL     every combination in the house                            2^n

A house with twenty lights is twenty-one renders in CSS and about a million in
FULL. `getNumberOfTotalRenders()` knows which before a single pixel is traced, so
`--list` answers "what will this cost" for free. That is what the confirmation
threshold is for -- not slow hardware, but one option turning an hour into a
fortnight.

NO PROGRESS COMES OUT OF THE PLUGIN. `Controller.render()` is a single blocking
call, and the JVM running it has no console at all -- it writes to a log file we
tail. The only per-frame signal is images appearing in the output directory, so
that is what the progress here counts.

Usage:
    python -m lidar2ha.render house.sh3d -o render_out --list
"""

from __future__ import annotations

import argparse
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

# Measured: 179.7 s for 7 renders at 800x600 on a real model. Used only to give
# a human a number before they commit an hour, so it is deliberately a single
# rate rather than a model of scene complexity -- and it is reported as an
# estimate, never relied upon.
SECONDS_PER_MEGAPIXEL = 54.0
# Above this many frames, ask before starting. A preview is two or three frames
# and should never prompt; an OVERLAY run is thousands and never should not.
CONFIRM_ABOVE = 50
PREVIEW_WIDTH, PREVIEW_HEIGHT = 640, 360


@dataclass
class RenderReport:
    """What HeadlessRender said, parsed back out of its log."""

    lights: list[str] = field(default_factory=list)
    others: list[str] = field(default_factory=list)
    groups: list[str] = field(default_factory=list)
    total_renders: int | None = None
    quality: str | None = None
    renderer: str | None = None
    mixing: str | None = None
    size: str | None = None
    seconds: float | None = None
    levels: int | None = None
    furniture: int | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def finished(self) -> bool:
        return self.seconds is not None


def parse_log(text: str) -> RenderReport:
    """Read a HeadlessRender log.

    Tolerant on purpose: a killed render leaves a truncated log, and that is
    exactly when someone most wants to know what it had got through.
    """
    report = RenderReport()
    section = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line:
            continue

        if line.startswith("    ") and section is not None:
            section.append(line.strip())
            continue
        section = None

        if m := re.match(r"detected light entities: (\d+)", line):
            section = report.lights
        elif re.match(r"detected other entities: (\d+)", line):
            section = report.others
        elif m := re.match(r"light groups \(rooms\): \[(.*)\]", line):
            report.groups = [g.strip() for g in m.group(1).split(",") if g.strip()]
        elif m := re.match(r"total renders\s*:\s*(\d+)", line):
            report.total_renders = int(m.group(1))
        elif m := re.match(r"quality\s*:\s*(\S+)", line):
            report.quality = m.group(1)
        elif m := re.match(r"renderer\s*:\s*(\S+)", line):
            report.renderer = m.group(1)
        elif m := re.match(r"mixing\s*:\s*(\S+)", line):
            report.mixing = m.group(1)
        elif m := re.match(r"render size\s*:\s*(\S+)", line):
            report.size = m.group(1)
        elif m := re.match(r"DONE in ([\d.]+) s", line):
            report.seconds = float(m.group(1))
        elif m := re.search(r"levels=(\d+).*furniture=(\d+)", line):
            report.levels, report.furniture = int(m.group(1)), int(m.group(2))
        elif line.startswith("ERROR") or "Exception" in line:
            report.errors.append(line)
    return report


def estimate_seconds(frames: int, width: int, height: int) -> float:
    """Rough wall-clock for a render, from a measured per-megapixel rate."""
    return frames * (width * height / 1_000_000) * SECONDS_PER_MEGAPIXEL


def human_duration(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f} s"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min"
    return f"{seconds / 3600:.1f} hours"


def rendered_frames(out_dir: Path) -> int:
    """How many images exist so far.

    The only progress signal there is: the plugin renders inside one opaque
    call, so counting files is the difference between a progress bar and
    staring at a process that looks identical whether it is working or hung.
    """
    renders = Path(out_dir) / "renders"
    if not renders.is_dir():
        return 0
    return sum(1 for p in renders.iterdir() if p.suffix.lower() in (".png", ".jpg", ".jpeg"))


def describe_output(out_dir: Path) -> dict:
    """What a finished render left behind.

    `renders/` and `floorplan/` hold the same images -- floorplan/ additionally
    has the transparent spacer the card uses as its own background. Only
    floorplan/ is deployed; shipping both would double the transfer for nothing.
    """
    out_dir = Path(out_dir)
    floorplan = out_dir / "floorplan"
    return {
        "renders": rendered_frames(out_dir),
        "overlays": sum(1 for p in floorplan.glob("*.png")) if floorplan.is_dir() else 0,
        "card": (out_dir / "floorplan.yaml").exists(),
        "deployable": floorplan if floorplan.is_dir() else None,
    }


def follow(log: Path, out_dir: Path, is_running, total: int | None,
           on_progress, poll: float = 2.0) -> None:
    """Report progress while a render runs, by watching images appear.

    `is_running` is a callable so this does not need to know how the process was
    started, which keeps it testable without a JVM.
    """
    seen = -1
    while is_running():
        done = rendered_frames(out_dir)
        if done != seen:
            seen = done
            on_progress(done, total)
        time.sleep(poll)
    on_progress(rendered_frames(out_dir), total)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("log", help="a render.log written by HeadlessRender")
    args = ap.parse_args()

    report = parse_log(Path(args.log).read_text(encoding="utf-8", errors="replace"))
    print(f"  lights detected : {len(report.lights)}")
    for name in report.lights:
        print(f"      {name}")
    print(f"  total renders   : {report.total_renders}")
    print(f"  quality         : {report.quality}   mixing: {report.mixing}")
    print(f"  size            : {report.size}")
    if report.finished:
        print(f"  finished in     : {human_duration(report.seconds)}")
    else:
        print("  DID NOT FINISH -- the log has no DONE line")
    for error in report.errors:
        print(f"  ERROR: {error}")


if __name__ == "__main__":
    main()
