"""A room's Home Assistant identity, across the three stages that decide it.

`rooms` writes `ha_area`, `combine` carries it, `seams` reads it to decide
whether a piece it cuts gets one. Each of those is covered on its own against
input that already holds the answer -- `test_combine.py` writes the field on
with a `model_copy` of its own, `test_seams.py` hand-builds a parent that has
one -- so the JOIN between them is what nothing else looks at.

That join fails silently by construction. Every polygon stays correct, every
name stays right, and the only symptom is that no light can be placed in the
house. This is not `test_entrypoints.py`: that file holds smoke tests, which
assert a stage starts. These assert an invariant three stages have to keep
between them.
"""

from __future__ import annotations

from lidar2ha import combine, rooms, seams
from lidar2ha.schema import Level, Model, Room, Wall, load_model, save_model

# An L, in centimetres. Asymmetric so a mirrored fit is not a coin toss.
CORNERS = [(0, 0), (600, 0), (600, 400), (200, 400), (200, 200), (0, 200)]

PROJECT = (
    'rooms:\n'
    '  first:\n'
    '    Living Room: open_living\n'
    '  second:\n'
    '    Living Room: open_living\n'
    'split:\n'
    '  Ground:\n'
    '    - room: open_living\n'
    '      sections:\n'
    '        - name: kitchen\n'
    '          box: [[0, 0], [200, 400]]\n'
    '        - name: lounge\n'
    '          box: [[200, 0], [600, 400]]\n'
)


def run(monkeypatch, module, *argv):
    monkeypatch.setattr("sys.argv", [module.__name__, *[str(a) for a in argv]])
    module.main()


def capture(shift_cm: float = 0.0) -> Model:
    corners = [(x + shift_cm, y) for x, y in CORNERS]
    walls = [
        Wall(x_start=a[0], y_start=a[1], x_end=b[0], y_end=b[1],
             thickness=10, height=250)
        for a, b in zip(corners, corners[1:] + corners[:1], strict=True)
    ]
    return Model(source="synthetic.dxf", levels=[
        Level(name="Ground", ceiling_height_cm=250, elevation_cm=0, walls=walls,
              rooms=[Room(name="Living Room", points=corners,
                          ceiling_low_cm=250, ceiling_high_cm=250)])])


def test_an_area_survives_rooms_then_combine_then_split(monkeypatch, tmp_path):
    """The one invariant no single stage's tests can see.

    A break anywhere along the join leaves the model loadable, the outlines
    exact and the names as declared, and `lights` with nothing to bind to.
    Reintroducing an unconditional `ha_area=None` in `seams`, or dropping the
    field from `combine`'s selection, fails here and nowhere else.
    """
    project = tmp_path / "project.yaml"
    project.write_text(PROJECT, encoding="utf-8")

    # Two captures of the same L, one shifted: `combine` refuses a level only
    # one capture saw.
    named = []
    for name, shift in (("first", 0.0), ("second", 40.0)):
        raw = tmp_path / f"{name}_raw.json"
        save_model(capture(shift), raw)
        out = tmp_path / f"{name}_named.json"
        run(monkeypatch, rooms, raw, project, "-o", out, "--capture", name)
        assert load_model(out).levels[0].rooms[0].ha_area == "open_living", \
            "if this fails the chain never started and the rest proves nothing"
        named.append(out)

    combined = tmp_path / "combined.json"
    run(monkeypatch, combine, *named, "-o", combined)

    out = tmp_path / "split.json"
    run(monkeypatch, seams, combined, "-o", out,
        "--project", project, "--level", "Ground")

    pieces = load_model(out).levels[0].rooms
    assert {r.ha_area for r in pieces} == {"kitchen", "lounge"}


def test_a_split_piece_records_the_room_it_was_cut_from(monkeypatch, tmp_path):
    """`split_from` is the only trace of a fusion the architecture imposed.

    Losing it makes a declared boundary indistinguishable from a wall some
    capture actually saw, which is the distinction `split:` exists to record.
    """
    project = tmp_path / "project.yaml"
    project.write_text(PROJECT, encoding="utf-8")

    named = []
    for name, shift in (("first", 0.0), ("second", 40.0)):
        raw = tmp_path / f"{name}_raw.json"
        save_model(capture(shift), raw)
        out = tmp_path / f"{name}_named.json"
        run(monkeypatch, rooms, raw, project, "-o", out, "--capture", name)
        named.append(out)

    combined = tmp_path / "combined.json"
    run(monkeypatch, combine, *named, "-o", combined)
    out = tmp_path / "split.json"
    run(monkeypatch, seams, combined, "-o", out,
        "--project", project, "--level", "Ground")

    pieces = load_model(out).levels[0].rooms
    assert pieces, "if this fails nothing was cut and the test proves nothing"
    assert all(r.split_from == "open_living" for r in pieces)
