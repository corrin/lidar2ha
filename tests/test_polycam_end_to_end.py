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
import pathlib
import subprocess
import sys
from unittest import mock

from lidar2ha import polycam
from lidar2ha.schema import load_model
from synthetic_dxf import (
    labelled_floor_with_no_rooms,
    one_storey,
    three_storeys_on_one_cluster,
)


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
    heights = {str(r.name): r.ceiling_high_cm for r in model.levels[0].rooms}
    assert heights["Bedroom"] == 260.0
    assert round(heights["Hallway"]) == 230.0


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


def test_each_band_records_the_level_it_was_cut_from(tmp_path):
    """`rooms` merges a room the split separated, and it may only do that
    between bands cut from ONE Polycam level -- those come off one sheet and
    share a frame, while Polycam's own levels are separate clusters that on one
    house fit the same reference 17.36 m apart.

    Recorded as a field rather than read back out of the `(510cm)` in the name,
    because the name is a label and this is the fact.
    """
    dxf, csv = three_storeys_on_one_cluster(tmp_path / "cap")
    run_polycam(dxf, csv, tmp_path / "out.json")

    model = load_model(tmp_path / "out.json")
    assert {lv.from_level for lv in model.levels} == {"Floor 1"}


def test_an_unsplit_level_was_cut_from_nothing(tmp_path):
    """None has to mean "never split", because that is what every capture
    written before the field says -- so a level that was not split must not
    claim itself as its own origin, which would make two unrelated levels of
    one capture look like bands of each other."""
    dxf, csv = one_storey(tmp_path / "cap")
    run_polycam(dxf, csv, tmp_path / "out.json")

    model = load_model(tmp_path / "out.json")
    assert model.levels[0].from_level is None


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


def test_a_single_band_capture_matches_the_committed_golden(tmp_path):
    """THE REGRESSION THIS FILE EXISTS FOR, and one already made once.

    `split_into_storeys` sorts rooms by ceiling height to FIND the bands. An
    early version emitted them in that order, which reshuffled the rooms of
    every capture that had only one band -- which is every capture that was
    already working. I caught it by hand-diffing against captures that are not
    in this repo, and nothing guarded it.

    AGAINST A COMMITTED FILE, not against a second run of the same code. Two
    runs agreeing proves the stage is deterministic and nothing more: a
    reordering is perfectly deterministic and would have passed that happily.
    `tests/golden/one_storey.json` is the output as it stands, so a future
    change to the band search has to explain itself by updating a file a
    reviewer can read.
    """
    dxf, csv = one_storey(tmp_path / "cap")
    run_polycam(dxf, csv, tmp_path / "out.json")

    golden = pathlib.Path("tests/golden/one_storey.json").read_text(encoding="utf-8")
    got = (tmp_path / "out.json").read_text(encoding="utf-8")
    assert json.loads(got) == json.loads(golden), (
        "single-band output changed; if that is intended, regenerate "
        "tests/golden/one_storey.json and say why in the commit")

    # Stated separately, because it is the property the golden is protecting
    # and a reviewer should not have to diff a file to see it.
    rooms = json.loads(got)["levels"][0]["rooms"]
    assert [r["name"] for r in rooms] == ["Bedroom", "Hallway"], (
        "rooms came out in ceiling-height order rather than sheet order")


def test_a_labelled_floor_with_no_rooms_still_produces_a_level(tmp_path, capsys):
    """Polycam does not always close a room. Without the fallback this cluster
    yields no ceiling bands and so no level at all -- the storey and every wall
    on it vanishing because its floors were not traced. Tested through `main()`
    because the fallback lives there, not in `split_into_storeys`."""
    dxf, csv = labelled_floor_with_no_rooms(tmp_path / "cap")
    run_polycam(dxf, csv, tmp_path / "out.json", capsys)

    model = load_model(tmp_path / "out.json")
    assert len(model.levels) == 1, "the labelled floor vanished"
    assert model.levels[0].rooms == []
    assert len(model.levels[0].walls) == 4, "its walls went with it"


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


def test_a_zero_storey_height_is_refused_at_the_boundary(tmp_path):
    """It does not fail on its own. A storey height of zero makes every room
    its own band, and what comes out is a plausible model with the wrong number
    of floors in it -- which is the silent kind of wrong."""
    import pytest

    dxf, csv = one_storey(tmp_path / "cap")
    argv = ["lidar2ha.polycam", str(dxf), "--csv", str(csv),
            "-o", str(tmp_path / "out.json"), "--storey-m", "0"]

    with mock.patch.object(sys, "argv", argv), pytest.raises(SystemExit) as caught:
        polycam.main()
    assert "greater than zero" in str(caught.value)
    assert not (tmp_path / "out.json").exists(), "it wrote a model anyway"
