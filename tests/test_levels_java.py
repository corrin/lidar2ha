"""The regression test for the bug that has now shipped twice.

Home.addWall(), addRoom() and addPieceOfFurniture() overwrite an object's level
with the home's SELECTED level. A writer that calls setLevel() before adding
therefore loses every level assignment -- but only once a second level exists,
because with one level the overwrite is a no-op.

That is why this went unnoticed: the prototype was single-level, the fix was
found and documented when a second floor appeared, and the consolidated rewrite
reverted to the original ordering. Nothing failed. The plan simply opened with
every wall, room and light stacked on the top floor.

These tests only mean anything against Sweet Home 3D's real classes, so they
run the writer and then reopen the archive through the same reader the desktop
application uses.
"""

from __future__ import annotations

import math
import re
import subprocess

import pytest

from scan2ha import javabridge
from scan2ha.scene import write_scene
from scan2ha.schema import Level, Light, Model, Room, Wall, WallTexture

pytestmark = pytest.mark.java


def two_level_model() -> Model:
    """One wall, one room and one light on each of two levels."""
    def floor(name: str, height: float, elevation: float) -> Level:
        return Level(
            name=name,
            ceiling_height_cm=height,
            elevation_cm=elevation,
            walls=[Wall(x_start=0, y_start=0, x_end=400, y_end=0,
                        thickness=10, height=height)],
            rooms=[Room(name=f"{name}Room",
                        points=[(0, 0), (400, 0), (400, 300), (0, 300)])],
        )
    return Model(source="test.dxf",
                 levels=[floor("Ground", 250, 0), floor("Upper", 240, 262)])


def run(tc, classes, main, *args) -> str:
    proc: subprocess.CompletedProcess = javabridge.run_writer(tc, classes, main, *args)
    assert proc.returncode == 0, f"{main} failed:\n{proc.stdout}\n{proc.stderr}"
    return proc.stdout


def parse_tally(report: str) -> dict[str, dict[str, int]]:
    """Read Sh3dVerify's per-level counts back out of its report."""
    tally: dict[str, dict[str, int]] = {}
    for line in report.splitlines():
        m = re.match(r"\s+(\S+)\s+walls=(\d+)\s+rooms=(\d+)\s+lights=(\d+)", line)
        if m:
            tally[m.group(1)] = {"walls": int(m.group(2)),
                                 "rooms": int(m.group(3)),
                                 "lights": int(m.group(4))}
    return tally


def test_each_object_lands_on_its_declared_level(tmp_path, toolchain, java_classes):
    model = two_level_model()
    lights = [
        Light(entity_id="light.ground_ceiling", level=0, x=200, y=150, elevation=230),
        Light(entity_id="light.upper_ceiling", level=1, x=200, y=150, elevation=220),
    ]
    scene = tmp_path / "scene.tsv"
    write_scene(model, scene, lights=lights)

    out = tmp_path / "two.sh3d"
    run(toolchain, java_classes, "Sh3dWriter", str(scene), str(out))
    report = run(toolchain, java_classes, "Sh3dVerify", str(out))

    tally = parse_tally(report)
    assert set(tally) == {"Ground", "Upper"}, report
    # The failure this guards against puts everything on Upper and nothing on
    # Ground. Walls and rooms are the test: lights are deliberately all on the
    # base level -- see test_every_light_is_on_the_base_level_at_its_true_height.
    for name in ("Ground", "Upper"):
        assert tally[name]["walls"] == 1, report
        assert tally[name]["rooms"] == 1, report
    assert tally["Ground"]["lights"] + tally["Upper"]["lights"] == 2, report


def test_every_light_is_on_the_base_level_at_its_true_height(tmp_path, toolchain,
                                                            java_classes):
    """Deliberate, and the whole reason a multi-level house renders at all.

    The floor-plan plugin only detects lights on the home's SELECTED level, so a
    light left on the upper storey is invisible to it and that floor renders
    dark. A light's elevation is measured from its own level's floor, so moving
    it to the base level and adding that level's elevation leaves it in exactly
    the same place in space.

    The height assertion is the one that matters: without it this is just
    putting lights on the wrong floor.
    """
    model = two_level_model()
    lights = [
        Light(entity_id="light.ground_ceiling", level=0, x=200, y=150, elevation=230),
        Light(entity_id="light.upper_ceiling", level=1, x=200, y=150, elevation=220),
    ]
    scene = tmp_path / "scene.tsv"
    write_scene(model, scene, lights=lights)
    out = tmp_path / "two.sh3d"
    run(toolchain, java_classes, "Sh3dWriter", str(scene), str(out))
    report = run(toolchain, java_classes, "Sh3dVerify", str(out))

    placed = re.findall(r"LIGHT\s+\[(\S+)\s*\]\s+name=(\S+).*?elev=(-?\d+)", report)
    by_id = {name: (level, int(elev)) for level, name, elev in placed}

    # Both on the base level, so the plugin can see both.
    assert {lvl for lvl, _ in by_id.values()} == {"Ground"}, report
    # And at the heights they would have had on their own levels: the upper
    # light was declared at 220 on a level standing at 262.
    assert by_id["light.ground_ceiling"][1] == 230, report
    assert by_id["light.upper_ceiling"][1] == 262 + 220, report

    # A light the plugin would ignore has no sources or no power.
    assert "sources=0" not in report
    assert "power=0.00" not in report


def test_single_level_still_works(tmp_path, toolchain, java_classes):
    """The case that always passed, and must keep passing."""
    model = Model(source="one.dxf", levels=[
        Level(name="Ground", ceiling_height_cm=250, elevation_cm=0,
              walls=[Wall(x_start=0, y_start=0, x_end=400, y_end=0,
                          thickness=10, height=250)],
              rooms=[Room(name="Hall",
                          points=[(0, 0), (400, 0), (400, 300), (0, 300)])])])
    scene = tmp_path / "one.tsv"
    write_scene(model, scene)
    out = tmp_path / "one.sh3d"
    run(toolchain, java_classes, "Sh3dWriter", str(scene), str(out))
    report = run(toolchain, java_classes, "Sh3dVerify", str(out))
    assert parse_tally(report) == {"Ground": {"walls": 1, "rooms": 1, "lights": 0}}, report


def test_textures_reach_the_archive(tmp_path, toolchain, java_classes):
    """A texture that is referenced but not embedded leaves a .sh3d that only
    renders on the machine that wrote it."""
    import zipfile

    from PIL import Image

    tex = tmp_path / "tex"
    tex.mkdir()
    for name, colour in (("floor", (150, 110, 70)), ("wall", (200, 195, 185)),
                         ("w0_0_left", (80, 140, 190))):
        Image.new("RGB", (64, 64), colour).save(tex / f"{name}.png")

    model = two_level_model()
    scene = tmp_path / "tex.tsv"
    write_scene(
        model, scene,
        tiled_textures={"floor": tex / "floor.png", "wall": tex / "wall.png"},
        wall_textures=[WallTexture(level=0, wall=0, left=tex / "w0_0_left.png")],
    )
    out = tmp_path / "tex.sh3d"
    run(toolchain, java_classes, "Sh3dWriter", str(scene), str(out))
    report = run(toolchain, java_classes, "Sh3dVerify", str(out))

    # The rectified image on the covered side, the tiled patch on the other.
    assert "tex=w0_0_left/wall" in report, report
    assert "tex=floor/-" in report, report

    # Beyond `Home` there must be real content entries, or nothing was embedded.
    names = zipfile.ZipFile(out).namelist()
    assert "Home" in names
    assert len(names) > 1, names


def parse_camera(report: str) -> dict:
    m = re.search(r"camera\s+: \((-?[\d.]+), (-?[\d.]+), (-?[\d.]+)\) "
                  r"yaw=(-?[\d.]+) deg pitch=(-?[\d.]+) deg", report)
    assert m, report
    x, y, z, yaw, pitch = (float(g) for g in m.groups())
    return {"x": x, "y": y, "z": z, "yaw": yaw, "pitch": pitch}


def test_the_camera_actually_points_at_the_house(tmp_path, toolchain, java_classes):
    """The bug this guards produced a uniform white frame, not an error.

    Sweet Home 3D's yaw looks along (sin yaw, cos yaw), so yaw=0 looks toward
    increasing y. The camera is placed on the far side of the plan, so yaw=0
    aimed it at empty sky and every render came back blank -- after paying the
    full raytracing cost to find out.
    """
    model = two_level_model()
    scene = tmp_path / "scene.tsv"
    write_scene(model, scene)
    out = tmp_path / "two.sh3d"
    run(toolchain, java_classes, "Sh3dWriter", str(scene), str(out))
    camera = parse_camera(run(toolchain, java_classes, "Sh3dVerify", str(out)))

    # The plan centre, in the same frame write_scene emitted.
    look = (math.sin(math.radians(camera["yaw"])), math.cos(math.radians(camera["yaw"])))
    to_centre = (200.0 - camera["x"], 150.0 - camera["y"])
    assert look[0] * to_centre[0] + look[1] * to_centre[1] > 0, \
        "the camera is facing away from the house"


def test_the_camera_clears_a_multi_level_building(tmp_path, toolchain, java_classes):
    """Framing from the plan alone left the top floor out of the picture."""
    model = two_level_model()
    scene = tmp_path / "scene.tsv"
    write_scene(model, scene)
    out = tmp_path / "two.sh3d"
    run(toolchain, java_classes, "Sh3dWriter", str(scene), str(out))
    camera = parse_camera(run(toolchain, java_classes, "Sh3dVerify", str(out)))

    # Upper level sits at 262 with a 240 ceiling, so the building tops out at 502.
    assert camera["z"] > 502, "the camera is below the roof it is meant to look down on"


def test_a_home_of_rooms_with_no_walls_is_still_framed(tmp_path, toolchain, java_classes):
    """Open plan is a room polygon with no wall along its open edge, which is
    the geometry this writer exists to support -- and framing on walls alone
    would have ignored it entirely."""
    model = Model(source="open.dxf", levels=[
        Level(name="Ground", ceiling_height_cm=250, elevation_cm=0,
              walls=[Wall(x_start=0, y_start=0, x_end=10, y_end=0,
                          thickness=10, height=250)],
              rooms=[Room(name="Open", points=[(0, 0), (900, 0), (900, 700), (0, 700)])])])
    scene = tmp_path / "open.tsv"
    write_scene(model, scene)
    out = tmp_path / "open.sh3d"
    run(toolchain, java_classes, "Sh3dWriter", str(scene), str(out))
    camera = parse_camera(run(toolchain, java_classes, "Sh3dVerify", str(out)))

    # Framed on the single tiny wall, the camera would sit almost on top of it.
    assert camera["y"] > 700, "the room polygon was not included in the framing"
