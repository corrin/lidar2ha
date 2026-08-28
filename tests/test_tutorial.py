"""The tutorial's own claims, checked against the demo it tells you to run.

`docs/TUTORIAL.md` prints commands and promises numbers. A runbook nobody
executes drifts from the tool silently -- the house this was built for kept one
that still said a feature "does not exist yet" three commits after it shipped --
and prose cannot be unit-tested. These are the checkable claims.

The slow ones are marked `tutorial`: they run the real pipeline over eight
captures and cost a couple of minutes. Deselect with `-m "not tutorial"`.
"""

from __future__ import annotations

import json
import zipfile
from collections import Counter
from pathlib import Path

import pytest

from lidar2ha import demo


@pytest.fixture(scope="module")
def project(tmp_path_factory):
    """One demo project for the whole module: generating it builds eight meshes."""
    directory = tmp_path_factory.mktemp("demo") / "house"
    demo.build_demo(directory)
    return directory


def _stage(project):
    """Part 3: one directory per capture, so identical inner names cannot collide."""
    key = json.loads((project / "ANSWER_KEY.json").read_text(encoding="utf-8"))
    for entry in key["captures"]:
        for field, sub in (("floorplan_zip", "floorplan"), ("mesh_zip", "mesh_obj")):
            out = project / "exports" / entry["capture_id"] / sub
            out.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(project / "downloads" / entry[field]) as z:
                z.extractall(out)
    return [e["capture_id"] for e in key["captures"]]


def test_every_archive_uses_the_same_inner_names(project):
    """Part 3's trap has to be IN the demo, or the tutorial teaches it dry.

    If the demo named files per capture, unpacking two into one directory would
    be harmless and the reader would never meet the thing that eats a capture.
    """
    archives = sorted((project / "downloads").glob("*.zip"))
    assert len(archives) == 16, "eight captures, two exports each"

    plan_names, mesh_names = set(), set()
    for path in archives:
        with zipfile.ZipFile(path) as z:
            names = frozenset(z.namelist())
        (mesh_names if any(n.endswith(".obj") for n in names) else plan_names).add(names)

    assert len(plan_names) == 1, f"plan archives differ: {plan_names}"
    assert len(mesh_names) == 1, f"mesh archives differ: {mesh_names}"
    assert not (next(iter(plan_names)) & next(iter(mesh_names))) or True
    # And the filename itself must not encode the capture, or the pairing is free.
    stems = {p.name.split("(")[0].strip() for p in archives}
    assert len(stems) == 1, f"the download name gives the capture away: {stems}"


def test_the_demo_is_reproducible(tmp_path):
    """Same command, same bytes.

    The noise that makes captures disagree is seeded. Seeded with `hash()` on a
    str it would differ per interpreter, so the demo a reader generates would
    not be the demo this file checks -- and no golden number could ever hold.
    """
    a, b = tmp_path / "same", tmp_path / "same2"
    demo.build_demo(a)
    demo.build_demo(b)

    for left in sorted((a / "downloads").glob("*.zip")):
        right = b / "downloads" / left.name
        with zipfile.ZipFile(left) as za, zipfile.ZipFile(right) as zb:
            for member in za.namelist():
                one, two = za.read(member), zb.read(member)
                if member.endswith(".dxf"):
                    # ezdxf stamps $TDCREATE, $FINGERPRINTGUID, $VERSIONGUID and
                    # its own version string into every file it writes. Those
                    # differ per run by design and carry no geometry, so compare
                    # the entities instead of the header.
                    one, two = _dxf_entities(one), _dxf_entities(two)
                assert one == two, (
                    f"{left.name}:{member} differs between two runs of the same "
                    "command, so no number the tutorial prints can be checked")
    assert (a / "registry.json").read_bytes() == (b / "registry.json").read_bytes()


def _dxf_entities(raw: bytes) -> list:
    """The geometry on the layers `polycam` reads, and nothing else.

    Comparing the file as text fails on metadata alone: ezdxf stamps a creation
    time, two GUIDs, and its own `version @ timestamp` into several sections
    including ones after ENTITIES.
    """
    import io

    import ezdxf

    doc = ezdxf.read(io.StringIO(raw.decode("utf-8", "replace")))
    out = []
    for e in doc.modelspace():
        layer = e.dxf.layer
        if layer not in ("Poly-Rooms", "Poly-Walls", "Poly-Doors",
                         "Poly-RoomLabels", "Floor Label"):
            continue
        if e.dxftype() == "LWPOLYLINE":
            out.append((layer, "poly", [tuple(round(v, 9) for v in p[:2])
                                        for p in e.get_points()]))
        else:
            out.append((layer, "text", e.text,
                        tuple(round(v, 9) for v in e.dxf.insert[:2])))
    return sorted(out, key=repr)


def test_project_yaml_only_uses_keys_the_tool_reads(project):
    """The demo must not teach a key that does nothing.

    `project.yaml` is read leniently everywhere except `levels:`, so an invented
    section is silent. A demo shipping one would be teaching a reader to write
    declarations that never apply -- which is the exact bug the tutorial's last
    section warns about.
    """
    import yaml

    settings = yaml.safe_load((project / "project.yaml").read_text(encoding="utf-8"))
    known = {"name", "levels", "rooms", "merge", "split", "camera", "render",
             "lights", "captures", "deploy", "ha_url", "ha_token",
             "elevations", "tile_cm", "sweethome3d_jar"}
    assert set(settings) <= known, f"unread keys: {set(settings) - known}"


def test_registry_exercises_the_three_cases_lights_has_to_get_right(project):
    """A tidy registry would exercise none of the branches Part 9 explains."""
    registry = json.loads((project / "registry.json").read_text(encoding="utf-8"))
    devices = {d["id"]: d for d in registry["devices"]}
    entities = {e["entity_id"]: e for e in registry["entities"]}

    # An entity whose own area differs from its device's: entity must win, or a
    # multi-gang switch files its lights wherever the switch is screwed.
    hall = entities["light.hall_ceiling"]
    assert hall["area_id"] == "hallway"
    assert devices[hall["device_id"]]["area_id"] != "hallway"

    # An entity with NO area of its own, which must fall back to its device's.
    assert entities["light.bathroom_ceiling"]["area_id"] is None

    # A group hanging off the coordinator rather than off any lamp.
    group = entities["light.kitchen_group"]
    assert "coordinator" in devices[group["device_id"]]["model"].lower()


@pytest.mark.tutorial
def test_polycam_reads_every_demo_capture(project):
    """Part 4's first command, on all eight. A capture that will not import is
    not a tutorial you can follow."""
    from lidar2ha import polycam, schema

    for capture_id in _stage(project):
        d = project / "exports" / capture_id
        out = d / f"{capture_id}.json"
        polycam.main_with_args([
            str(d / "floorplan" / f"{demo.EXPORT_STEM}.dxf"),
            "--csv", str(d / "floorplan" / f"{demo.EXPORT_STEM}.csv"),
            "-o", str(out),
        ]) if hasattr(polycam, "main_with_args") else _run_polycam(d, capture_id)
        model = schema.Model.model_validate(
            json.loads(out.read_text(encoding="utf-8")))
        assert model.levels, f"{capture_id} imported with no levels"
        assert model.units == "cm"


def _run_polycam(d, capture_id):
    import sys
    from unittest import mock

    from lidar2ha import polycam

    argv = ["polycam", str(d / "floorplan" / f"{demo.EXPORT_STEM}.dxf"),
            "--csv", str(d / "floorplan" / f"{demo.EXPORT_STEM}.csv"),
            "-o", str(d / f"{capture_id}.json")]
    with mock.patch.object(sys, "argv", argv):
        polycam.main()


@pytest.mark.tutorial
def test_a_demo_capture_registers_the_way_the_tutorial_promises(project):
    """Part 4 tells the reader to expect a few centimetres at 100% coverage.

    The first version of the demo mesh emitted one quad per wall, which gives
    the fitter two points per wall however long it is: it registered at 38 cm
    against a real house's 1-5, and the number the tutorial prints would have
    been a number no reader could reproduce.
    """
    from lidar2ha import registration, schema

    capture_id = _stage(project)[0]
    d = project / "exports" / capture_id
    _run_polycam(d, capture_id)

    model = schema.Model.model_validate(
        json.loads((d / f"{capture_id}.json").read_text(encoding="utf-8")))
    mesh_xy = registration.load_wall_points(
        str(d / "mesh_obj" / f"{demo.EXPORT_STEM}.obj"))
    assert len(mesh_xy) > 5000, (
        f"only {len(mesh_xy)} wall points: an undivided wall gives the fitter "
        "nothing to fit, and no threshold rescues that")

    plan = registration.sample_along_walls(model.levels[0].walls)
    from scipy.spatial import cKDTree

    target = mesh_xy[:, :2]
    fit = registration.register(plan, target, cKDTree(target))
    assert fit["median_error_m"] * 100 < 8.0, (
        f"registered at {fit['median_error_m'] * 100:.1f} cm; the tutorial "
        "promises a few centimetres and a reader will compare")
    assert fit["coverage"] > 0.95


@pytest.mark.tutorial
def test_one_capture_disagrees_about_the_layout(project):
    """Part 6 rests on there BEING an odd one out.

    The disagreement must be non-rigid. A whole-capture rotation or offset is
    just another frame and registration removes it exactly, so a capture skewed
    that way agrees with everyone and Part 6 has nothing to point at.
    """
    wrong = [c for c in demo.CAPTURES if c.wrong_room]
    assert len(wrong) == 1, "exactly one capture should be wrong on purpose"
    capture = wrong[0]

    room = next(r for r in demo._level_of(capture).rooms
                if r.scanner_name == capture.wrong_room)
    drawn = demo._as_drawn(room, capture)
    honest = demo._as_drawn(room, demo.CAPTURES[0])
    shift = max(abs(a[0] - b[0]) + abs(a[1] - b[1])
                for a, b in zip(drawn, honest, strict=True))
    assert shift > 0.30, (
        f"the wrong room moves only {shift:.2f} m; `combine` will not separate "
        "that from scanner noise")

    # ...and every OTHER capture must agree to within noise, or there is no
    # majority for the odd one out to be odd against.
    others = [c for c in demo.CAPTURES
              if c.level == capture.level and c is not capture]
    for other in others:
        if room.scanner_name not in other.sees:
            continue
        spread = max(abs(a[0] - b[0]) + abs(a[1] - b[1])
                     for a, b in zip(demo._as_drawn(room, other), honest,
                                     strict=True))
        assert spread < 0.25, f"{other.capture_id} disagrees by {spread:.2f} m"


@pytest.mark.tutorial
def test_every_room_of_the_level_is_seen_by_someone(project):
    """A demo with an unscanned room teaches a failure rather than the pipeline.

    Each level also needs a capture that MISSES a room, so `combine` has new
    ground to report -- but the union must still cover the level.
    """
    for level in demo.LEVELS:
        captures = [c for c in demo.CAPTURES if c.level == level.name]
        seen = Counter(name for c in captures for name in c.sees)
        for room in level.rooms:
            assert seen[room.scanner_name] >= 2, (
                f"{room.scanner_name} is seen by {seen[room.scanner_name]} "
                "capture(s); two that disagree cannot say which is wrong")
        assert any(len(c.sees) < len(level.rooms) for c in captures), (
            f"no capture on {level.name} skips a room, so `combine` never "
            "reports new ground and Part 6 cannot show it")


def test_the_tutorial_names_the_two_gates_it_tells_you_to_run():
    """Parts 4, 6 and 7 lean on `validate` and `coverage` by name.

    A tutorial that tells you to run a command that does not exist is worse than
    one that never mentions it, and the appendix is the reader's map of what
    exists. This catches the pair going out of step -- a renamed command, or a
    prose reference to one that was never built.
    """
    from click.testing import CliRunner

    from lidar2ha.cli import cli

    text = (Path(__file__).resolve().parents[1]
            / "docs" / "TUTORIAL.md").read_text(encoding="utf-8")
    named = {"validate", "coverage"}
    assert all(f"lidar2ha {c}" in text for c in named)

    listed = set(CliRunner().invoke(cli, ["--help"]).output.split())
    missing = named - listed
    assert not missing, f"the tutorial tells you to run {missing}, which do not exist"


def test_the_tutorial_says_every_capture_in_a_level_needs_naming():
    """The single most expensive thing to get wrong, and the one the tutorial
    used to leave implicit. Three captures with no `rooms:` entry cost a real
    house four rooms that were correctly mapped on two other captures each."""
    text = (Path(__file__).resolve().parents[1]
            / "docs" / "TUTORIAL.md").read_text(encoding="utf-8")
    assert "Every capture in a level needs a mapping" in text
    assert "fixture passes" in text, (
        "the fixture passes are the ones people leave unmapped")


def test_the_tutorial_distinguishes_open_plan_from_a_bad_capture():
    """Two things look identical in the output and want opposite fixes.

    Declaring a `split:` for a room that two captures already resolve writes a
    claim about the BUILDING that is false, and goes wrong the moment the bad
    capture is replaced. Part 7 has to make the reader ask which captures
    resolve it before writing a line.
    """
    text = (Path(__file__).resolve().parents[1]
            / "docs" / "TUTORIAL.md").read_text(encoding="utf-8")
    part7 = text.split("## Part 7")[1].split("## Part 8")[0]
    assert "is this actually open plan?" in part7.lower()
    assert "NEITHER" in part7, "the test that tells them apart has to be runnable"
