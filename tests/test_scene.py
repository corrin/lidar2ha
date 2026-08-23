"""The scene writer, without needing Java.

The two coordinate corrections are what these lock down. Both are silent when
wrong: a mirrored plan looks like a plan, and a model floating 12 metres from
the origin opens fine. The only way either is caught is by asserting on the
numbers.
"""

from __future__ import annotations

import math

import pytest

from scan2ha.scene import write_scene
from scan2ha.schema import Level, Light, Model, Room, Wall, WallTexture


def records(path):
    """Parse a scene file into (type, fields) pairs, dropping comments."""
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            fields = line.split("\t")
            out.append((fields[0], fields[1:]))
    return out


def by_type(path, kind):
    return [fields for t, fields in records(path) if t == kind]


@pytest.fixture
def model():
    """Two levels sharing one sheet, offset far from the origin.

    Y increases upward here, as it does in the DXF.
    """
    def floor(name, height, elevation):
        return Level(
            name=name, ceiling_height_cm=height, elevation_cm=elevation,
            walls=[Wall(x_start=1000, y_start=1000, x_end=1400, y_end=1000,
                        thickness=10, height=height)],
            rooms=[Room(name=f"{name}Room",
                        points=[(1000, 1000), (1400, 1000), (1400, 1300), (1000, 1300)])],
        )
    return Model(source="house.dxf",
                 levels=[floor("Ground", 250, 0), floor("Upper", 240, 262)])


def test_y_is_flipped_and_coordinates_reoriented(model, tmp_path):
    scene = tmp_path / "s.tsv"
    write_scene(model, scene)

    # x spans 1000..1400 and y spans 1000..1300 on the sheet, so after
    # re-origining and flipping Y the wall runs along y = 300, not y = 0.
    wall = by_type(scene, "wall")[0]
    assert wall[1:5] == ["0", "300", "400", "300"]

    # The room's top edge on the sheet becomes its bottom edge in the plan.
    room = by_type(scene, "room")[0]
    assert room[4:] == ["0,300", "400,300", "400,0", "0,0"]


def test_levels_share_one_origin(model, tmp_path):
    """Re-origining each level independently would slide the floors apart.

    Move one level's geometry and the *other* level's output must move too,
    because they are laid out on a shared sheet.
    """
    scene = tmp_path / "a.tsv"
    write_scene(model, scene)
    before = by_type(scene, "wall")

    model.levels[1].walls[0].y_end = 1800  # extends the model's bounds upward
    scene_b = tmp_path / "b.tsv"
    write_scene(model, scene_b)
    after = by_type(scene_b, "wall")

    assert before[0][1:5] != after[0][1:5], "ground floor did not follow the shared origin"


def test_lights_get_the_same_transform_as_geometry(model, tmp_path):
    """A light left in raw plan coordinates lights the wrong room."""
    scene = tmp_path / "s.tsv"
    write_scene(model, scene, lights=[
        Light(entity_id="light.hall", level=0, x=1000, y=1000, elevation=230),
    ])
    light = by_type(scene, "light")[0]
    wall = by_type(scene, "wall")[0]
    # The light sits exactly on the wall's start point, and must stay there.
    assert light[2:4] == wall[1:3] == ["0", "300"]


def test_level_ids_match_declaration_order(model, tmp_path):
    scene = tmp_path / "s.tsv"
    write_scene(model, scene)
    assert [f[0] for f in by_type(scene, "level")] == ["l0", "l1"]
    assert [f[0] for f in by_type(scene, "wall")] == ["l0", "l1"]


def test_levels_are_declared_before_any_geometry(model, tmp_path):
    """Sh3dWriter resolves a level id at the moment it reads the record."""
    scene = tmp_path / "s.tsv"
    write_scene(model, scene)
    kinds = [t for t, _ in records(scene)]
    assert kinds.index("level") < kinds.index("wall")
    assert max(i for i, k in enumerate(kinds) if k == "level") < kinds.index("wall")


def test_textures_are_declared_before_use_and_sized_to_their_wall(model, tmp_path):
    scene = tmp_path / "s.tsv"
    write_scene(
        model, scene,
        tiled_textures={"floor": tmp_path / "floor.png", "wall": tmp_path / "wall.png"},
        wall_textures=[WallTexture(level=0, wall=0, left=tmp_path / "L0_W0_left.png")],
    )
    kinds = [t for t, _ in records(scene)]
    assert max(i for i, k in enumerate(kinds) if k == "texture") < kinds.index("wall")

    sized = {f[0]: (f[2], f[3]) for f in by_type(scene, "texture")}
    # The tiled patches repeat at their tile size...
    assert sized["floor"] == ("100", "100")
    # ...while the rectified image is the size of the wall, so it appears once.
    assert sized["w0_0_left"] == ("400", "250")

    walls = by_type(scene, "wall")
    assert walls[0][7:9] == ["w0_0_left", "wall"], "covered side should use its own image"
    assert walls[1][7:9] == ["wall", "wall"], "uncovered wall should fall back to the tile"


def test_ceilings_are_off_unless_asked_for(model, tmp_path):
    """A visible ceiling occludes the level being rendered from above."""
    tiled = {"floor": tmp_path / "f.png", "ceiling": tmp_path / "c.png"}
    scene = tmp_path / "s.tsv"
    write_scene(model, scene, tiled_textures=tiled)
    assert by_type(scene, "room")[0][3] == "-"
    assert "ceiling" not in {f[0] for f in by_type(scene, "texture")}

    scene_on = tmp_path / "on.tsv"
    write_scene(model, scene_on, tiled_textures=tiled, include_ceilings=True)
    assert by_type(scene_on, "room")[0][3] == "ceiling"


def test_missing_elevation_is_reported_not_guessed(tmp_path):
    model = Model(source="x.dxf", levels=[
        Level(name="Ground", ceiling_height_cm=250, elevation_cm=0,
              walls=[Wall(x_start=0, y_start=0, x_end=100, y_end=0,
                          thickness=10, height=250)]),
        Level(name="Upper", ceiling_height_cm=250,
              walls=[Wall(x_start=0, y_start=0, x_end=100, y_end=0,
                          thickness=10, height=250)]),
    ])
    stats = write_scene(model, tmp_path / "s.tsv")
    assert stats["unknown_elevations"] == ["Upper"]

    stats = write_scene(model, tmp_path / "s2.tsv", elevation_overrides={"Upper": 262})
    assert stats["unknown_elevations"] == []
    assert by_type(tmp_path / "s2.tsv", "level")[1][2] == "262"


def test_a_model_with_no_geometry_fails_loudly(tmp_path):
    model = Model(source="empty.dxf", levels=[Level(name="Ground", ceiling_height_cm=250)])
    with pytest.raises(ValueError, match="no geometry"):
        write_scene(model, tmp_path / "s.tsv")


def test_output_has_no_bom_and_uses_tabs(model, tmp_path):
    """Sh3dWriter splits on tabs and a BOM makes the first record unparseable."""
    scene = tmp_path / "s.tsv"
    write_scene(model, scene)
    raw = scene.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert b"\t" in raw


# --------------------------------------------------------------------------- #
# the camera, and where the lights end up
# --------------------------------------------------------------------------- #


def test_a_camera_record_is_emitted(model, tmp_path):
    scene = tmp_path / "s.tsv"
    write_scene(model, scene)
    cameras = by_type(scene, "camera")
    assert len(cameras) == 1
    name, x, y, z, yaw, pitch, fov = cameras[0]
    assert name == "house"
    assert float(pitch) == 50.0
    assert float(yaw) == 180.0
    assert float(fov) > 0


def test_the_camera_is_solved_in_the_scene_frame_not_the_models(model, tmp_path):
    """The Y flip reverses handedness, so a camera solved in the model's frame
    and transformed afterwards would face the wrong way — which is exactly the
    failure that produced blank white renders."""
    scene = tmp_path / "s.tsv"
    write_scene(model, scene)
    _n, x, y, _z, yaw, _p, _f = by_type(scene, "camera")[0]

    # Geometry spans x 0..400 and y 0..300 once transformed.
    look = (math.sin(math.radians(float(yaw))), math.cos(math.radians(float(yaw))))
    to_centre = (200.0 - float(x), 150.0 - float(y))
    assert look[0] * to_centre[0] + look[1] * to_centre[1] > 0


def test_a_wider_render_moves_the_camera_back(model, tmp_path):
    def distance(aspect):
        scene = tmp_path / f"{aspect}.tsv"
        write_scene(model, scene, aspect=aspect)
        _n, x, y, z, *_ = by_type(scene, "camera")[0]
        return math.dist((float(x), float(y), float(z)), (200.0, 150.0, 0.0))

    assert distance(21 / 9) > distance(4 / 3)


def test_every_light_is_emitted_against_the_base_level(model, tmp_path):
    """The plugin only detects lights on the selected level, so a light left on
    an upper storey is invisible to it and that floor renders dark."""
    scene = tmp_path / "s.tsv"
    write_scene(model, scene, lights=[
        Light(entity_id="light.ground", level=0, x=1000, y=1000, elevation=230),
        Light(entity_id="light.upper", level=1, x=1000, y=1000, elevation=220),
    ])
    assert [f[0] for f in by_type(scene, "light")] == ["l0", "l0"]


def test_a_relocated_light_keeps_its_true_height(model, tmp_path):
    """Elevation is measured from a light's own level's floor, so moving it down
    a level without adding that level's elevation would drop it through the
    ceiling of the room it is meant to light."""
    scene = tmp_path / "s.tsv"
    write_scene(model, scene, lights=[
        Light(entity_id="light.ground", level=0, x=1000, y=1000, elevation=230),
        Light(entity_id="light.upper", level=1, x=1000, y=1000, elevation=220),
    ])
    heights = {f[1]: float(f[4]) for f in by_type(scene, "light")}
    assert heights["light.ground"] == 230
    # The Upper level stands at 262.
    assert heights["light.upper"] == 262 + 220


def test_an_elevation_override_moves_the_lights_with_the_level(model, tmp_path):
    scene = tmp_path / "s.tsv"
    write_scene(model, scene,
                lights=[Light(entity_id="light.upper", level=1, x=1000, y=1000, elevation=220)],
                elevation_overrides={"Upper": 500})
    assert float(by_type(scene, "light")[0][4]) == 500 + 220
