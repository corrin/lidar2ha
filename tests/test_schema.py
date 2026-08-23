"""The model types, and the on-disk names they have to keep.

The alias mapping is the load-bearing part: captures already on disk use
Sweet Home 3D's camelCase geometry keys, and renaming them in Python must not
rename them in the JSON.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from lidar2ha.schema import (
    Level,
    Model,
    Registration,
    Room,
    Wall,
    load_model,
    load_wall_textures,
    save_model,
)


def test_geometry_keys_stay_camelcase_on_disk(tmp_path):
    model = Model(source="house.dxf", levels=[
        Level(name="Ground", ceiling_height_cm=250,
              walls=[Wall(x_start=1, y_start=2, x_end=3, y_end=4,
                          thickness=10, height=250)])])
    path = tmp_path / "m.json"
    save_model(model, path)

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert set(raw["levels"][0]["walls"][0]) >= {"xStart", "yStart", "xEnd", "yEnd"}
    assert load_model(path).levels[0].walls[0].x_start == 1


def test_parses_what_polycam_and_registration_actually_write(tmp_path):
    """The shape produced by `polycam` and then added to by `registration`."""
    document = {
        "source": "house.dxf",
        "units": "cm",
        "levels": [{
            "name": "Ground Floor",
            "ceiling_height_cm": 240.0,
            "elevation_cm": None,
            "walls": [{"xStart": 0.0, "yStart": 0.0, "xEnd": 400.0, "yEnd": 0.0,
                       "thickness": 10.0, "height": 240.0}],
            "rooms": [{"name": "Living Room", "points": [[0.0, 0.0], [400.0, 0.0],
                                                         [400.0, 300.0]]}],
            "doors": [{"x": 100.0, "y": 0.0, "width": 80.0}],
            "registration": {"theta_deg": 91.5, "tx_m": 1.2, "ty_m": -3.4,
                             "mirror": True, "median_error_m": 0.031,
                             "coverage": 0.87, "floor_z_m": 0.12},
        }],
    }
    path = tmp_path / "registered.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    model = load_model(path)
    level = model.levels[0]
    assert level.elevation_cm is None
    assert isinstance(level.registration, Registration)
    assert level.registration.mirror is True
    assert level.rooms[0].points[0] == (0.0, 0.0)


def test_floor_z_may_be_absent_when_the_mesh_had_no_floor(tmp_path):
    reg = Registration(theta_deg=0, tx_m=0, ty_m=0, mirror=False,
                       median_error_m=0.1, coverage=0.5, floor_z_m=None)
    assert reg.floor_z_m is None


def test_a_misspelled_key_is_an_error_here_not_a_missing_wall_later():
    with pytest.raises(ValidationError):
        Wall.model_validate({"xStart": 0, "yStart": 0, "xEnd": 1, "yEnd": 1,
                             "thickness": 10, "hieght": 250})


def test_wall_length_sizes_a_rectified_texture():
    wall = Wall(x_start=0, y_start=0, x_end=300, y_end=400, thickness=10, height=250)
    assert wall.length_cm == pytest.approx(500.0)


def test_bounds_span_every_level():
    model = Model(source="x.dxf", levels=[
        Level(name="A", ceiling_height_cm=250,
              walls=[Wall(x_start=0, y_start=0, x_end=10, y_end=10,
                          thickness=1, height=250)]),
        Level(name="B", ceiling_height_cm=250,
              rooms=[Room(points=[(-5, -5), (100, 100), (0, 0)])]),
    ])
    assert model.bounds() == (-5, -5, 100, 100)


def test_wall_texture_manifest_round_trips(tmp_path):
    """The shape `textures_project` writes, including a side it skipped."""
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps([
        {"level": 0, "wall": 3, "left": "walltex/L0_W3_left.png", "left_coverage": 0.42},
    ]), encoding="utf-8")
    entries = load_wall_textures(path)
    assert entries[0].right is None
    assert entries[0].left_coverage == 0.42
