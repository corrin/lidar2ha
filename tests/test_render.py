"""Reading a render log, and knowing the cost before spending it.

The log is the only thing a render says: its JVM has no console and writes to a
file. So parsing has to survive the interesting case — a killed or crashed run,
which is exactly when someone wants to know how far it got.
"""

from __future__ import annotations

import pytest

from lidar2ha.render import (
    CONFIRM_ABOVE,
    describe_output,
    estimate_seconds,
    follow,
    human_duration,
    parse_log,
    rendered_frames,
)

FINISHED = """\
loaded house.sh3d  levels=3 walls=32 rooms=7 furniture=6
detected light entities: 3
    light.hall
    light.kitchen
    light.landing
detected other entities: 1
    binary_sensor.front_door
light groups (rooms): [light.hall, light.kitchen, light.landing]
total renders    : 4
quality          : HIGH
renderer         : SUNFLOW
mixing           : CSS
reusing renders  : false
output directory : render_out
render size      : 640x360
total renders    : 4
DONE in 103.4 s (4 renders)
"""

# A run that was killed part way: no DONE line, nothing after the header.
TRUNCATED = """\
loaded house.sh3d  levels=1 walls=4 rooms=1 furniture=1
detected light entities: 1
    light.hall
detected other entities: 0
light groups (rooms): [light.hall]
total renders    : 2
quality          : HIGH
"""

CRASHED = FINISHED.replace(
    "DONE in 103.4 s (4 renders)",
    "java.lang.RuntimeException: Couldn't locate YafaRay library")


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #


def test_a_finished_run_is_read_back_in_full():
    report = parse_log(FINISHED)
    assert report.lights == ["light.hall", "light.kitchen", "light.landing"]
    assert report.others == ["binary_sensor.front_door"]
    assert report.groups == ["light.hall", "light.kitchen", "light.landing"]
    assert report.total_renders == 4
    assert (report.quality, report.renderer, report.mixing) == ("HIGH", "SUNFLOW", "CSS")
    assert report.size == "640x360"
    assert report.seconds == 103.4
    assert report.finished


def test_a_killed_run_parses_rather_than_raising():
    """The case that matters most: you want to know how far it got."""
    report = parse_log(TRUNCATED)
    assert report.lights == ["light.hall"]
    assert report.total_renders == 2
    assert report.seconds is None
    assert not report.finished


def test_an_empty_log_is_not_an_error():
    report = parse_log("")
    assert report.lights == [] and not report.finished


def test_a_crash_is_surfaced_not_swallowed():
    report = parse_log(CRASHED)
    assert not report.finished
    assert any("YafaRay" in e for e in report.errors)


def test_the_plugin_reporting_an_empty_project_is_an_error():
    report = parse_log("ERROR: plugin reports the project is empty\n")
    assert report.errors == ["ERROR: plugin reports the project is empty"]


def test_entity_names_are_not_confused_with_other_indented_output():
    """Only the lines under a `detected ...` heading are entity names."""
    report = parse_log(FINISHED)
    assert "binary_sensor.front_door" not in report.lights


# --------------------------------------------------------------------------- #
# cost
# --------------------------------------------------------------------------- #


def test_the_estimate_scales_with_frames_and_pixels():
    one = estimate_seconds(1, 640, 360)
    assert estimate_seconds(2, 640, 360) == pytest.approx(2 * one)
    assert estimate_seconds(1, 1280, 720) == pytest.approx(4 * one)


def test_the_estimate_is_in_the_right_order_of_magnitude():
    """Measured: 22 frames at 640x360 took about 4 minutes."""
    assert 120 < estimate_seconds(22, 640, 360) < 600


def test_a_combinatorial_mixing_mode_is_obviously_enormous():
    """The reason the confirmation gate exists. Same house, same size: CSS is
    22 frames, FULL is 2^21 — one option, five orders of magnitude."""
    css = estimate_seconds(22, 640, 360)
    full = estimate_seconds(2 ** 21, 640, 360)
    assert full / css > 10_000
    assert 2 ** 21 > CONFIRM_ABOVE


def test_durations_read_as_a_human_would_say_them():
    assert human_duration(45) == "45 s"
    assert human_duration(600) == "10 min"
    assert "hours" in human_duration(90_000)


# --------------------------------------------------------------------------- #
# progress, which has to come from the filesystem
# --------------------------------------------------------------------------- #


def test_progress_counts_images_because_the_plugin_reports_none(tmp_path):
    """`Controller.render()` is one blocking call with no per-frame signal, so
    counting files is the difference between a progress bar and staring at a
    process that looks the same whether it is working or hung."""
    renders = tmp_path / "renders"
    renders.mkdir()
    assert rendered_frames(tmp_path) == 0

    (renders / "base.png").write_bytes(b"x")
    (renders / "light.hall.png").write_bytes(b"x")
    (renders / "notes.txt").write_text("ignored")
    assert rendered_frames(tmp_path) == 2


def test_counting_a_directory_that_does_not_exist_yet_is_zero(tmp_path):
    """The first poll happens before the plugin has created anything."""
    assert rendered_frames(tmp_path / "nothing") == 0


def test_follow_reports_until_the_process_stops(tmp_path):
    renders = tmp_path / "renders"
    renders.mkdir()
    ticks = iter([True, True, False])
    seen = []

    def running():
        (renders / f"{len(seen)}.png").write_bytes(b"x")
        return next(ticks, False)

    follow(tmp_path / "log", tmp_path, running, total=3, on_progress=
           lambda done, total: seen.append((done, total)), poll=0.0)
    assert seen and seen[-1][1] == 3


# --------------------------------------------------------------------------- #
# what a finished render leaves behind
# --------------------------------------------------------------------------- #


def test_the_output_tree_is_described(tmp_path):
    (tmp_path / "renders").mkdir()
    (tmp_path / "renders" / "base.png").write_bytes(b"x")
    (tmp_path / "floorplan").mkdir()
    for name in ("base.png", "transparent.png"):
        (tmp_path / "floorplan" / name).write_bytes(b"x")
    (tmp_path / "floorplan.yaml").write_text("type: picture-elements")

    result = describe_output(tmp_path)
    assert result["renders"] == 1
    assert result["overlays"] == 2
    assert result["card"] is True
    assert result["deployable"] == tmp_path / "floorplan"


def test_a_render_that_produced_no_card_is_visible(tmp_path):
    """floorplan.yaml missing means the plugin did not finish, whatever the
    images suggest."""
    (tmp_path / "renders").mkdir()
    assert describe_output(tmp_path)["card"] is False
