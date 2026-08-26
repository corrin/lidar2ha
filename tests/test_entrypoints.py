"""Every stage's main(), run end to end on synthetic input.

This exists because of a specific escape. The schema migration left one
`lv['name']` behind in registration.main(), on a line that fires the first time
any level registers -- so the stage crashed on every input, while the whole
suite stayed green. Nothing called a main() at all: the unit tests exercised the
functions underneath and the CLI tests went through click, so the argparse
stages were untested as a class.

These are smoke tests, not behaviour tests. They assert the stage runs, writes
what it says it writes, and produces something loadable. That is enough to catch
a stage that cannot start.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import trimesh

from lidar2ha import combine, ha, lights, preview, registration, rooms, seams
from lidar2ha.schema import Level, Model, Registration, Room, Wall, load_model, save_model

# An L, in metres. Asymmetric on purpose: a rectangle reads the same mirrored,
# so the fitter's handedness choice would be a coin toss and the test vacuous.
OUTLINE_M = [(0, 0), (6, 0), (6, 4), (2, 4), (2, 2), (0, 2)]


@pytest.fixture
def model_path(tmp_path):
    """A one-level L-shaped room, in centimetres."""
    corners = [(x * 100, y * 100) for x, y in OUTLINE_M]
    walls = [
        Wall(x_start=a[0], y_start=a[1], x_end=b[0], y_end=b[1], thickness=10, height=250)
        for a, b in zip(corners, corners[1:] + corners[:1], strict=True)
    ]
    model = Model(source="synthetic.dxf", levels=[
        Level(name="Ground", ceiling_height_cm=250, elevation_cm=0, walls=walls,
              rooms=[Room(name="Living Room", points=corners,
                          ceiling_low_cm=250, ceiling_high_cm=250)])])
    path = tmp_path / "model.json"
    save_model(model, path)
    return path


@pytest.fixture
def mesh_path(tmp_path):
    """Walls standing on the model's outline, sampled densely enough to fit.

    Built as bare wall quads rather than an extrusion: extrude_polygon needs a
    triangulation engine that is not a dependency here, and the floor and ceiling
    it would add are exactly the faces the fitter discards anyway.

    Subdivision matters: two triangles per wall gives the fitter a handful of
    points and a meaningless result.
    """
    height = 2.5
    vertices, faces = [], []
    for (x0, y0), (x1, y1) in zip(OUTLINE_M, OUTLINE_M[1:] + OUTLINE_M[:1], strict=True):
        i = len(vertices)
        vertices += [(x0, y0, 0.0), (x1, y1, 0.0), (x1, y1, height), (x0, y0, height)]
        faces += [(i, i + 1, i + 2), (i, i + 2, i + 3)]

    mesh = trimesh.Trimesh(vertices=np.array(vertices), faces=np.array(faces),
                           process=False).subdivide_to_size(0.15)
    path = tmp_path / "mesh.obj"
    mesh.export(path)
    return path


def run(monkeypatch, module, *argv):
    monkeypatch.setattr("sys.argv", [module.__name__, *[str(a) for a in argv]])
    module.main()


def test_registration_main_runs(monkeypatch, tmp_path, model_path, mesh_path, capsys):
    out = tmp_path / "registered.json"
    run(monkeypatch, registration, model_path, mesh_path, "-o", out)

    # The line that used to crash: it fires the first time a level registers.
    assert "handedness fixed by Ground" in capsys.readouterr().out

    registered = load_model(out)
    assert registered.levels[0].registration is not None


def test_rooms_main_runs(monkeypatch, tmp_path, model_path):
    project = tmp_path / "project.yaml"
    project.write_text(
        "rooms:\n  upstairs:\n    Living Room: lounge\n", encoding="utf-8")
    out = tmp_path / "named.json"
    run(monkeypatch, rooms, model_path, project, "-o", out, "--capture", "upstairs")

    named = load_model(out)
    assert named.levels[0].rooms[0].name == "lounge"
    assert named.levels[0].rooms[0].scanner_name == "Living Room"


def test_rooms_main_reports_a_band_crossing_and_a_typo(monkeypatch, tmp_path, capsys):
    """Six print branches were added to `main` and the existing smoke test
    declares no `merge:` at all, so none of them ever ran -- which is exactly
    the escape this file exists for: every unit test passing while a stage's
    `main()` was broken on the input that reaches it.
    """
    from lidar2ha.schema import Level, Room, Wall, save_model

    def sq(name, x0, x1, low, high):
        return Room(name=name, points=[(x0, 0), (x1, 0), (x1, 300), (x0, 300)],
                    ceiling_low_cm=low, ceiling_high_cm=high)

    model = Model(source="x.dxf", levels=[
        Level(name="Floor 1 (210cm)", from_level="0:Floor 1", ceiling_height_cm=800,
              rooms=[sq("Living Room 1", 0, 400, 380, 800)],
              walls=[Wall(x_start=0, y_start=0, x_end=400, y_end=0,
                          thickness=10.0, height=800.0)]),
        Level(name="Floor 1 (480cm)", from_level="0:Floor 1", ceiling_height_cm=480,
              rooms=[sq("Living Room 2", 400, 700, 480, 480)],
              walls=[Wall(x_start=400, y_start=0, x_end=700, y_end=0,
                          thickness=10.0, height=480.0)])])
    src = tmp_path / "banded.json"
    save_model(model, src)

    project = tmp_path / "project.yaml"
    project.write_text(
        'rooms:\n  walk:\n    Living Room 1: lounge\n'
        'merge:\n  walk:\n    - ["Living Room 1", "Living Room 2"]\n'
        '    - ["Living Room 1", "Nonexistent"]\n', encoding="utf-8")

    out = tmp_path / "named.json"
    run(monkeypatch, rooms, src, project, "-o", out, "--capture", "walk")
    printed = capsys.readouterr().out

    assert "spanned" in printed, printed
    assert "walls only" in printed, "the emptied band has to be named"
    assert "Nonexistent" in printed, "and so does the declaration that did nothing"

    named = load_model(out)
    assert [len(lv.rooms) for lv in named.levels] == [1, 0]
    assert len(named.levels) == 2, "the level count is a contract with the textures"


def test_seams_main_runs(monkeypatch, tmp_path, model_path):
    out = tmp_path / "split.json"
    run(monkeypatch, seams, model_path, "-o", out, "--room", "Living Room",
        "--seam", "300,-50", "300,450", "--names", "west", "east")

    split = load_model(out)
    assert {r.name for r in split.levels[0].rooms} == {"west", "east"}


def test_seams_main_runs_from_a_project(monkeypatch, tmp_path, model_path):
    """The declarative path, which is the one a pipeline re-run goes through."""
    project = tmp_path / "project.yaml"
    project.write_text(
        'split:\n'
        '  Ground:\n'
        '    - room: "Living Room"\n'
        '      sections:\n'
        '        - name: kitchen\n'
        '          box: [[0, 0], [200, 400]]\n'
        '        - name: lounge\n'
        '          box: [[200, 0], [600, 400]]\n', encoding="utf-8")
    out = tmp_path / "split.json"
    run(monkeypatch, seams, model_path, "-o", out,
        "--project", project, "--level", "Ground")

    assert {r.name for r in load_model(out).levels[0].rooms} == {"kitchen", "lounge"}


def test_split_command_runs(tmp_path, model_path):
    """The click subcommand, whose path resolution is its own code."""
    from click.testing import CliRunner

    from lidar2ha.cli import cli

    (tmp_path / "project.yaml").write_text(
        'split:\n'
        '  "Mid Level":\n'
        '    - room: "Living Room"\n'
        '      seam: [[300, -50], [300, 450]]\n'
        '      names: [west, east]\n', encoding="utf-8")
    combined = tmp_path / "mid_level_combined.json"
    combined.write_bytes(model_path.read_bytes())

    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as work:
        result = runner.invoke(cli, [
            "split", "Mid Level",
            "--project", str(tmp_path / "project.yaml"),
            "--model", str(combined),
            "-o", str(Path(work) / "split.json")])

        assert result.exit_code == 0, result.output
        rooms_out = load_model(Path(work) / "split.json").levels[0].rooms
        assert {r.name for r in rooms_out} == {"west", "east"}


def test_split_with_an_untextured_mesh_says_it_saw_nothing(tmp_path, model_path,
                                                           mesh_path):
    """A mesh with no atlas carries no floor colour, so it can say nothing.

    The failure this catches is the stage dying inside numpy on an empty sample
    instead — and the one after that, reporting "the floor does not change here"
    about a mesh that was never photographed at all.
    """
    from click.testing import CliRunner

    from lidar2ha.cli import cli

    (tmp_path / "project.yaml").write_text(
        'split:\n'
        '  "Mid Level":\n'
        '    - room: "Living Room"\n'
        '      seam: [[300, -50], [300, 450]]\n'
        '      names: [west, east]\n', encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(cli, [
        "split", "Mid Level",
        "--project", str(tmp_path / "project.yaml"),
        "--model", str(model_path),
        "--mesh", str(mesh_path),
        "-o", str(tmp_path / "split.json")])

    assert result.exit_code == 0, result.output
    assert "NOT LOOKED AT" in result.output
    assert "the floor does not change here" not in result.output


def test_preview_main_runs(monkeypatch, tmp_path, model_path):
    out = tmp_path / "plan.png"
    run(monkeypatch, preview, model_path, "-o", out)
    assert out.exists() and out.stat().st_size > 0


def test_combine_main_runs(monkeypatch, tmp_path, model_path):
    """Two captures of the same L, one shifted. Nothing here checks the
    selection -- test_combine.py does that on real captures. This checks the
    stage starts, writes both its outputs, and that what it writes still loads."""
    shifted = load_model(model_path)
    for lv in shifted.levels:
        for wall in lv.walls:
            wall.x_start += 40
            wall.x_end += 40
        for room in lv.rooms:
            room.points = [(x + 40, y) for x, y in room.points]
    second = tmp_path / "second.json"
    save_model(shifted, second)

    out = tmp_path / "combined.json"
    run(monkeypatch, combine, model_path, second, "-o", out)

    assert load_model(out).levels[0].rooms
    assert (tmp_path / "combined_worklist.json").exists()


def test_every_stage_exposes_a_main():
    """A stage without main() cannot be run, and nothing else would say so."""
    import importlib

    stages = ["ceilings", "combine", "compare", "contactsheet", "deploy", "fixtures",
              "floormap", "ha", "inspect_dxf", "inspect_mesh", "lights", "mesh",
              "placefixtures", "render", "polycam", "preview", "registration", "rooms",
              "seams", "textures_project", "textures_tile", "thresholds",
              "whichlevel"]
    missing = [s for s in stages
               if not callable(getattr(importlib.import_module(f"lidar2ha.{s}"), "main", None))]
    assert missing == []


def test_model_json_round_trips_through_a_stage(tmp_path, model_path):
    """Whatever a stage writes must still load as a Model.

    extra="forbid" means a stage inventing a key breaks the next stage, and it
    should break here instead.
    """
    model = load_model(model_path)
    out = tmp_path / "again.json"
    save_model(model, out)
    assert json.loads(out.read_text(encoding="utf-8"))["levels"][0]["walls"][0]["xStart"] == 0
    assert load_model(out).levels[0].rooms[0].points[0] == (0.0, 0.0)


def test_registration_reports_a_credible_fit(monkeypatch, tmp_path, model_path, mesh_path):
    """A stage that 'succeeds' with a nonsense transform is worse than one that
    fails, since everything downstream trusts it."""
    out = tmp_path / "registered.json"
    run(monkeypatch, registration, model_path, mesh_path, "-o", out)

    reg = load_model(out).levels[0].registration
    assert reg is not None
    assert np.isfinite(reg.median_error_m)
    assert reg.median_error_m < 0.30
    assert reg.coverage > 0.5


def test_lights_main_runs(monkeypatch, tmp_path, model_path, capsys):
    """The whole stage, from a cached registry to placements on disk."""
    import json

    named = tmp_path / "named.json"
    model = load_model(model_path)
    model.levels[0].rooms[0].ha_area = "lounge"
    model.levels[0].rooms[0].name = "lounge"
    save_model(model, named)

    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({
        "areas": [], "devices": [{"id": "d1", "area_id": "lounge"}],
        "entities": [
            {"entity_id": "light.ceiling", "name": "Ceiling", "area_id": None,
             "device_id": "d1", "disabled_by": None, "hidden_by": None},
            {"entity_id": "light.lamp", "name": "Lamp", "area_id": "lounge",
             "device_id": None, "disabled_by": None, "hidden_by": None},
        ],
        "states": [],
    }), encoding="utf-8")

    out = tmp_path / "lights.json"
    run(monkeypatch, lights, named, registry, "-o", out, "--report")

    placed = json.loads(out.read_text(encoding="utf-8"))
    assert {p["entity_id"] for p in placed} == {"light.ceiling", "light.lamp"}
    assert all(p["level"] == 0 and p["power"] > 0 for p in placed)
    assert "placed 2 light(s)" in capsys.readouterr().out


def test_lights_refuses_a_model_that_has_not_been_through_rooms(monkeypatch, tmp_path,
                                                               model_path):
    """Placement is by Home Assistant area, so a model still carrying scanner
    guesses has nothing to join on — and should say so, not place nothing."""
    import json

    import pytest

    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({"areas": [], "devices": [], "entities": [],
                                    "states": []}), encoding="utf-8")
    out = tmp_path / "lights.json"
    with pytest.raises(SystemExit, match="lidar2ha.rooms"):
        run(monkeypatch, lights, model_path, registry, "-o", out)


def test_ha_main_reads_a_cached_registry(monkeypatch, tmp_path, capsys):
    import json

    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({
        "areas": [], "devices": [],
        "entities": [{"entity_id": "light.a", "name": "A", "area_id": "hall",
                      "device_id": None, "disabled_by": None, "hidden_by": None}],
        "states": [],
    }), encoding="utf-8")

    run(monkeypatch, ha, "-o", registry)
    assert "light.a" in capsys.readouterr().out


def test_placefixtures_and_contactsheet_mains_run(monkeypatch, tmp_path, capsys):
    """The two stages that the `--daylight-mesh` and multi-capture work rewrote.

    No mesh here on purpose: without an ordinary capture to difference against,
    every record must simply carry no verdict and the pair must behave exactly
    as they did before differencing existed.
    """
    from PIL import Image

    from lidar2ha import contactsheet, placefixtures

    corners = [(x * 100, y * 100) for x, y in OUTLINE_M]
    walls = [
        Wall(x_start=a[0], y_start=a[1], x_end=b[0], y_end=b[1], thickness=10, height=250)
        for a, b in zip(corners, corners[1:] + corners[:1], strict=True)
    ]
    level = Level(name="Ground", ceiling_height_cm=250, elevation_cm=0, walls=walls,
                  rooms=[Room(name="lounge", ha_area="lounge", points=corners)])
    level.registration = Registration(theta_deg=0.0, tx_m=0.0, ty_m=0.0, mirror=False,
                                      median_error_m=0.02, coverage=1.0, floor_z_m=0.0)

    fixture_model = tmp_path / "fixture.json"
    save_model(Model(source="fixture.dxf", role="fixtures", levels=[level]), fixture_model)
    geometry_model = tmp_path / "named.json"
    save_model(Model(source="house.dxf", levels=[level]), geometry_model)

    found = [{"x": 3.0, "y": 1.0, "z": 2.3, "faces": 40, "extent_m": 0.2,
              "luma": 240.0, "surface": "ceiling", "crop": "00.png"}]
    fixtures_json = tmp_path / "fixtures.json"
    fixtures_json.write_text(json.dumps(found), encoding="utf-8")

    placed = tmp_path / "placed.json"
    run(monkeypatch, placefixtures, fixtures_json, fixture_model, geometry_model,
        "-o", placed)

    record = json.loads(placed.read_text(encoding="utf-8"))[0]
    assert record["room"] == "lounge"
    assert record["capture"] == "house.dxf"
    assert "verdict" not in record, "no ordinary capture was given, so nothing was judged"

    crops = tmp_path / "crops"
    crops.mkdir()
    Image.new("RGB", (16, 16)).save(crops / "00.png")
    sheet = tmp_path / "sheet.png"
    run(monkeypatch, contactsheet, crops, placed, "-o", sheet)
    assert sheet.exists()
    assert "PROBLEM" not in capsys.readouterr().out


@pytest.mark.java
def test_compiled_classes_can_load_on_the_bundled_java_8_jvm(toolchain):
    """Sweet Home 3D ships a Java 8 JRE, which refuses class file version 61.

    The failure mode is why this earns a test: class loading fails BEFORE
    main(), so HeadlessRender's own uncaught-exception handler never installs,
    and the bundled javaw.exe has no console -- the stack trace arrives as a
    modal dialog on the user's screen rather than in any log.
    """
    from lidar2ha import javabridge

    classes = javabridge.compile_java(toolchain)
    for name in ("HeadlessRender", "ObjExport", "Sh3dWriter", "Sh3dVerify"):
        raw = (classes / f"{name}.class").read_bytes()
        version = int.from_bytes(raw[6:8], "big")
        assert version <= 52, f"{name} is class file {version}; the render JVM caps at 52"


# --------------------------------------------------------------------------- #
# the GLB export, whose whole value is the names
# --------------------------------------------------------------------------- #


def glb_bytes(document: dict) -> bytes:
    """The smallest valid binary glTF carrying this JSON."""
    import struct

    body = json.dumps(document).encode("utf-8")
    body += b" " * (-len(body) % 4)
    chunk = struct.pack("<II", len(body), 0x4E4F534A) + body
    return struct.pack("<4sII", b"glTF", 2, 12 + len(chunk)) + chunk


def test_names_are_read_from_both_nodes_and_meshes(tmp_path):
    """A converter may hang the name on either. Asking for only one is how a
    file that kept its names gets reported as having lost them."""
    path = tmp_path / "a.glb"
    path.write_bytes(glb_bytes({"nodes": [{"name": "light.a"}, {}],
                                "meshes": [{"name": "light.b"}]}))
    from lidar2ha.glb import glb_node_names

    assert set(glb_node_names(path)) == {"light.a", "light.b"}


def test_a_conversion_that_lost_every_name_is_measurable(tmp_path):
    """The failure this whole module exists for: trimesh keys geometry by
    MATERIAL, so six lights sharing `white` come back as one node called
    `white`. The file is valid, opens fine, and binds nothing."""
    from lidar2ha.glb import check_names

    obj = tmp_path / "a.obj"
    obj.write_text("g light.hallway_ceiling\nv 0 0 0\ng wall_0\nv 1 1 1\n",
                   encoding="utf-8")
    glb = tmp_path / "a.glb"
    glb.write_bytes(glb_bytes({"meshes": [{"name": "white"}, {"name": "wood"}]}))

    before, survived, _splits = check_names(obj, glb)
    assert before == {"light.hallway_ceiling"}, "wall_0 is scenery, not an entity"
    assert survived == set()


def test_a_suffixed_sibling_does_not_count_as_a_survivor(tmp_path):
    """obj2gltf splits a group per material and suffixes the copies. Only the
    un-suffixed one carries the entity id, so counting the siblings would turn
    one bound lamp into a report of eight."""
    from lidar2ha.glb import check_names

    obj = tmp_path / "a.obj"
    obj.write_text("g light.den_ceiling\nv 0 0 0\n", encoding="utf-8")
    glb = tmp_path / "a.glb"
    glb.write_bytes(glb_bytes({"meshes": [{"name": "light.den_ceiling"},
                                          {"name": "light.den_ceiling_1"},
                                          {"name": "light.den_ceiling_2"}]}))

    before, survived, splits = check_names(obj, glb)
    assert before == survived == {"light.den_ceiling"}
    assert splits == {"light.den_ceiling": 2}


def test_something_that_is_not_a_glb_is_refused_by_name(tmp_path):
    """Reading the OBJ as a GLB would otherwise fail somewhere in struct."""
    from lidar2ha.glb import glb_node_names

    path = tmp_path / "a.glb"
    path.write_bytes(b"not a gltf at all, but long enough to unpack")
    with pytest.raises(SystemExit, match="binary glTF"):
        glb_node_names(path)


def test_whichlevel_runs_end_to_end(tmp_path, model_path):
    """Every unit test passed once while a stage's main() crashed on every
    input, which is why this file exists.

    Fitting a model against ITSELF is the degenerate case, and the one most
    likely to divide by zero or trip a margin rule -- so it is the right smoke
    test for a stage whose whole job is comparing two models.
    """
    import subprocess
    import sys

    done = subprocess.run(
        [sys.executable, "-m", "lidar2ha.whichlevel", str(model_path),
         "--against", str(model_path)],
        capture_output=True, text=True)

    assert done.returncode == 0, done.stderr
    # A model fits itself exactly, so anything but IDENTIFIED means the stage
    # cannot recognise the one case it certainly should.
    assert "IDENTIFIED" in done.stdout, done.stdout


def test_whichlevel_offers_the_same_flags_from_both_entry_points():
    """`--write` is the whole point of the storey declaration -- it prints the
    block you paste into project.yaml -- and it shipped on `python -m
    lidar2ha.whichlevel` alone. `lidar2ha whichlevel` is the one the README
    tells people to run, so the documented workflow named a flag that command
    did not have.

    The two are written by hand in different files and nothing connects them
    but this. Compared through `--help`, which is what a reader sees.
    """
    import re
    import subprocess
    import sys

    def flags(argv):
        done = subprocess.run(argv, capture_output=True, text=True)
        assert done.returncode == 0, done.stderr
        return set(re.findall(r"--[a-z][a-z0-9-]*", done.stdout)) - {"--help"}

    module = flags([sys.executable, "-m", "lidar2ha.whichlevel", "--help"])
    packaged = flags([sys.executable, "-m", "lidar2ha.cli", "whichlevel", "--help"])

    assert "--write" in module, "if this fails the test proves nothing"
    assert not module - packaged, (
        f"`lidar2ha whichlevel` is missing {sorted(module - packaged)}")


def test_whichlevel_with_nothing_to_compare_against_says_so(tmp_path, model_path):
    """A stage that exits non-zero with a usable sentence beats one that raises
    somewhere deep -- see this module's docstring."""
    import subprocess
    import sys

    done = subprocess.run(
        [sys.executable, "-m", "lidar2ha.whichlevel", str(model_path)],
        capture_output=True, text=True)

    assert done.returncode != 0
    assert "Nothing to compare against" in (done.stdout + done.stderr)
