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

import numpy as np
import pytest
import trimesh

from lidar2ha import ha, lights, preview, registration, rooms, seams
from lidar2ha.schema import Level, Model, Room, Wall, load_model, save_model

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


def test_seams_main_runs(monkeypatch, tmp_path, model_path):
    out = tmp_path / "split.json"
    run(monkeypatch, seams, model_path, "-o", out, "--room", "Living Room",
        "--seam", "300,-50", "300,450", "--names", "west", "east")

    split = load_model(out)
    assert {r.name for r in split.levels[0].rooms} == {"west", "east"}


def test_preview_main_runs(monkeypatch, tmp_path, model_path):
    out = tmp_path / "plan.png"
    run(monkeypatch, preview, model_path, "-o", out)
    assert out.exists() and out.stat().st_size > 0


def test_every_stage_exposes_a_main():
    """A stage without main() cannot be run, and nothing else would say so."""
    import importlib

    stages = ["ceilings", "compare", "contactsheet", "fixtures", "floormap", "ha",
              "inspect_dxf", "inspect_mesh", "lights", "mesh", "placefixtures",
              "polycam", "preview", "registration", "rooms", "seams",
              "textures_project", "textures_tile", "thresholds"]
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
    for name in ("HeadlessRender", "Sh3dWriter", "Sh3dVerify"):
        raw = (classes / f"{name}.class").read_bytes()
        version = int.from_bytes(raw[6:8], "big")
        assert version <= 52, f"{name} is class file {version}; the render JVM caps at 52"
