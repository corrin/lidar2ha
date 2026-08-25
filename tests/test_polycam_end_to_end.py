"""`polycam` from a DXF on disk to a Model, which nothing tested before.

Every test in `test_polycam.py` calls a pure function. The half of the module
that ASSEMBLES a model -- the loop over sheet clusters, the ceiling-band split,
the wall and door attribution, the report -- was reachable only by running the
stage against a real capture, and a real capture is a floor plan of somebody's
house and does not belong in this repo. So 40% of the module went untested and
the band split landed squarely in the untested part.

`tests/synthetic_dxf.py` writes a Polycam-shaped DXF instead. It is faithful to
the four layers and one header field the reader actually consults, and to
nothing else.

The test this file exists for is the LAST one: a single-band capture must come
out of the split exactly as it went in. That property was broken once already
-- the band search sorts rooms by height, and emitting them in that order
quietly reshuffled every capture that already worked -- and it was caught by
hand-diffing against captures that are not here. Nothing guarded it until now.
"""

from __future__ import annotations

import json
import subprocess
import sys
from unittest import mock

from lidar2ha import polycam
from lidar2ha.schema import load_model
from synthetic_dxf import one_storey, three_storeys_on_one_cluster


def run_polycam(dxf, csv, out, capsys=None):
    """Drive the stage IN PROCESS, so what it prints is assertable and what it
    executes is measurable. `test_the_stage_runs_as_a_subprocess` covers the
    other half -- that it works when actually invoked."""
    argv = ["lidar2ha.polycam", str(dxf), "--csv", str(csv), "-o", str(out)]
    with mock.patch.object(sys, "argv", argv):
        polycam.main()
    return capsys.readouterr().out if capsys is not None else ""


# --------------------------------------------------------------------------- #
# the ordinary capture
# --------------------------------------------------------------------------- #


def test_a_single_storey_capture_becomes_one_level(tmp_path):
    """The shape of every capture that already worked."""
    dxf, csv = one_storey(tmp_path / "cap")
    run_polycam(dxf, csv, tmp_path / "out.json")

    model = load_model(tmp_path / "out.json")
    assert [lv.name for lv in model.levels] == ["Floor 1"]
    assert {str(r.name) for r in model.levels[0].rooms} == {"Bedroom", "Hallway"}


def test_a_wall_drawn_twice_is_read_once(tmp_path):
    """Polycam emits every wall as two identical polylines. Five walls were
    drawn, ten polylines written."""
    dxf, csv = one_storey(tmp_path / "cap")
    run_polycam(dxf, csv, tmp_path / "out.json")

    model = load_model(tmp_path / "out.json")
    assert len(model.levels[0].walls) == 5


def test_a_door_swing_arc_is_not_an_opening(tmp_path):
    """Polycam draws swing arcs and jamb ticks on the door layer. They are
    2-point entities and not doors, and counting them would put an opening in
    a wall that has none."""
    dxf, csv = one_storey(tmp_path / "cap")
    run_polycam(dxf, csv, tmp_path / "out.json")

    model = load_model(tmp_path / "out.json")
    assert len(model.levels[0].doors) == 1, "the swing arc was counted as a door"


def test_ceilings_come_from_the_csv_row_that_says_ceiling(tmp_path):
    """The CSV carries latitude and area rows too. A reader taking every row
    would give a room a ceiling of -36.9 m."""
    dxf, csv = one_storey(tmp_path / "cap")
    run_polycam(dxf, csv, tmp_path / "out.json")

    model = load_model(tmp_path / "out.json")
    for r in model.levels[0].rooms:
        assert r.ceiling_high_cm == 240.0


# --------------------------------------------------------------------------- #
# the capture that walked the whole house
# --------------------------------------------------------------------------- #


def test_one_cluster_of_three_storeys_becomes_three_levels(tmp_path):
    """The failure §36 exists for, end to end. The three storeys are STACKED --
    their footprints overlap in plan -- so nothing about their position can
    separate them; the ceiling, measured from the capture datum, can."""
    dxf, csv = three_storeys_on_one_cluster(tmp_path / "cap")
    run_polycam(dxf, csv, tmp_path / "out.json")

    model = load_model(tmp_path / "out.json")
    assert len(model.levels) == 3, [lv.name for lv in model.levels]
    assert [lv.name for lv in model.levels] == [
        "Floor 1 (240cm)", "Floor 1 (510cm)", "Floor 1 (780cm)"]


def test_the_split_is_announced_rather_than_done_quietly(tmp_path, capsys):
    """A capture silently gaining two levels is a capture whose owner cannot
    tell what happened to it."""
    dxf, csv = three_storeys_on_one_cluster(tmp_path / "cap")
    out = run_polycam(dxf, csv, tmp_path / "out.json", capsys)

    assert "3 ceiling bands" in out
    assert "CAPTURE DATUM" in out


def test_the_stairwell_lands_on_the_lowest_storey_and_is_named(tmp_path, capsys):
    """A room spanning more than a storey is on none of them. It has to go
    somewhere, so it goes to the bottom -- and the report says so, because
    otherwise it reads as an ordinary room with a very tall ceiling."""
    dxf, csv = three_storeys_on_one_cluster(tmp_path / "cap")
    out = run_polycam(dxf, csv, tmp_path / "out.json", capsys)

    model = load_model(tmp_path / "out.json")
    lowest = model.levels[0]
    assert "Living Room" in {str(r.name) for r in lowest.rooms}
    assert "SHAFT" in out
    assert "more than a storey" in out


def test_every_room_survives_the_split(tmp_path):
    """Four rooms in, four rooms out. A storey split that loses one is worse
    than no split at all -- the room renders nowhere and nothing says why."""
    dxf, csv = three_storeys_on_one_cluster(tmp_path / "cap")
    run_polycam(dxf, csv, tmp_path / "out.json")

    model = load_model(tmp_path / "out.json")
    names = [str(r.name) for lv in model.levels for r in lv.rooms]
    assert sorted(names) == ["Bedroom", "Landing", "Living Room", "Office"]


def test_each_storey_keeps_the_walls_that_bound_it(tmp_path):
    """Non-exclusively: a building's envelope runs its full height, so it is a
    wall of every storey. A level with no walls cannot be fitted onto anything,
    which is the whole point of splitting."""
    dxf, csv = three_storeys_on_one_cluster(tmp_path / "cap")
    run_polycam(dxf, csv, tmp_path / "out.json")

    model = load_model(tmp_path / "out.json")
    for lv in model.levels:
        assert lv.walls, f"{lv.name} came out with no walls"


# --------------------------------------------------------------------------- #
# the guard that was missing
# --------------------------------------------------------------------------- #


def test_a_single_band_capture_is_emitted_byte_for_byte(tmp_path):
    """THE REGRESSION THIS FILE EXISTS FOR, and one already made once.

    `split_into_storeys` sorts rooms by ceiling height to FIND the bands. An
    early version emitted them in that order, which reshuffled the rooms of
    every capture that had only one band -- which is every capture that was
    already working. Nothing downstream reads room order by contract, but
    `ceilings` matches rooms positionally, models are diffed between runs, and
    a silent reordering is exactly the change nobody reviews.

    Caught last time by hand-diffing against captures that are not in this
    repo. This is the guard that does not depend on having them.
    """
    dxf, csv = one_storey(tmp_path / "cap")

    run_polycam(dxf, csv, tmp_path / "first.json")
    run_polycam(dxf, csv, tmp_path / "second.json")

    first = (tmp_path / "first.json").read_text(encoding="utf-8")
    second = (tmp_path / "second.json").read_text(encoding="utf-8")
    assert first == second, "the same input produced two different models"

    # And the order is the one the sheet gave, not the one the band search used.
    rooms = json.loads(first)["levels"][0]["rooms"]
    assert [r["name"] for r in rooms] == ["Bedroom", "Hallway"]


def test_the_stage_runs_as_a_subprocess(tmp_path):
    """The other half of end-to-end. Everything above drives `main()` directly,
    which is how the assertions and the coverage get made -- but a stage that
    only works when imported is not a stage anyone can run."""
    dxf, csv = three_storeys_on_one_cluster(tmp_path / "cap")
    done = subprocess.run(
        [sys.executable, "-m", "lidar2ha.polycam", str(dxf),
         "--csv", str(csv), "-o", str(tmp_path / "out.json")],
        capture_output=True, text=True)

    assert done.returncode == 0, done.stderr
    assert len(load_model(tmp_path / "out.json").levels) == 3
