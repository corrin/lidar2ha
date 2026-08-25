"""Asking which storey a capture is of.

Every other stage needs the answer first: `project.yaml` will not let `combine`
look at a capture until its level is declared, and `compare` only answers the
question you already aimed it at. Identifying one capture across three storeys
meant running it by hand once per storey -- one real table took nine
invocations.

The failure worth avoiding is not a wrong ranking, it is a ranking at all. A
capture of a building nobody declared still produces a least-bad row, and a
reader who takes it gets a confident wrong answer with nothing objecting.
"""

from __future__ import annotations

from lidar2ha.schema import Level, Model, Room, Wall
from lidar2ha.whichlevel import rank


def wall(x0: float, y0: float, x1: float, y1: float) -> Wall:
    return Wall(x_start=x0, y_start=y0, x_end=x1, y_end=y1,
                thickness=10.0, height=250.0)


def storey(name: str, x: float = 0.0, y: float = 0.0, w: float = 600.0,
           h: float = 400.0) -> Model:
    """A rectangular storey with a partition, so it is not rotationally
    symmetric -- a bare rectangle fits itself four ways and would make every
    test here vacuous."""
    walls = [wall(x, y, x + w, y), wall(x + w, y, x + w, y + h),
             wall(x + w, y + h, x, y + h), wall(x, y + h, x, y),
             wall(x + w / 3, y, x + w / 3, y + h * 0.6)]
    rooms = [Room(name="r", points=[(x, y), (x + w, y), (x + w, y + h), (x, y + h)],
                  ceiling_low_cm=240.0, ceiling_high_cm=240.0)]
    return Model(source="x.dxf",
                 levels=[Level(name=name, ceiling_height_cm=250.0,
                               walls=walls, rooms=rooms)])


def shifted(model: Model, dx: float, dy: float) -> Model:
    """The same storey drawn somewhere else on the sheet.

    A capture and the level it belongs to never share an origin, so every test
    here has to survive a translation -- and `register` solving for one is what
    makes the comparison meaningful at all.
    """
    lv = model.levels[0]
    walls = [w.model_copy(update={"x_start": w.x_start + dx, "y_start": w.y_start + dy,
                                  "x_end": w.x_end + dx, "y_end": w.y_end + dy})
             for w in lv.walls]
    rooms = [r.model_copy(update={"points": [(px + dx, py + dy) for px, py in r.points]})
             for r in lv.rooms]
    return model.model_copy(update={
        "levels": [lv.model_copy(update={"walls": walls, "rooms": rooms})]})


def test_a_capture_of_a_storey_names_that_storey():
    """The ordinary case. Measured on a real house, a capture reads 0.0-0.9 cm
    against its own storey and 20-35 against the others, so the separation this
    relies on is wide."""
    ground = storey("ground")
    upstairs = storey("upstairs", w=900.0)

    answer = rank(shifted(ground, 1500.0, -900.0),
                  {"ground": ground, "upstairs": upstairs})

    assert answer.verdict == "identified"
    assert answer.level == "ground"
    assert answer.ranked[0].coverage > 0.9


def test_a_capture_of_nowhere_declared_is_refused_rather_than_ranked():
    """The whole point. Every candidate still produces a row, and the least bad
    of them is not an answer -- two real captures sat at 21-31 cm on 59-79%
    coverage, which is a refusal."""
    ground = storey("ground")
    upstairs = storey("upstairs", w=900.0)
    elsewhere = storey("elsewhere", w=250.0, h=180.0)

    answer = rank(elsewhere, {"ground": ground, "upstairs": upstairs})

    assert answer.verdict == "none"
    assert answer.level is None
    assert answer.ranked, "a refusal must still show what it tried"


def test_the_reason_names_the_number_that_refused_it():
    """A refusal a reader cannot check is one they will overrule. The distance
    is what separates 'this is another building' from 'this needs a better
    fit'."""
    answer = rank(storey("elsewhere", w=250.0, h=180.0),
                  {"ground": storey("ground")})
    assert "cm" in answer.reason


def test_a_fit_on_too_little_of_the_capture_is_refused():
    """The median is taken over the points that MATCHED, so a capture sharing a
    few walls with a storey reports a fine one and says nothing about the rest
    of itself. Coverage is the only thing that tells those apart."""
    ground = storey("ground")
    answer = rank(ground, {"ground": ground}, min_coverage=0.99, same_level_cm=50.0)

    # Guard: the fit itself is good, so only the coverage rule can be refusing.
    assert answer.ranked[0].median_cm < 5.0
    if answer.ranked[0].coverage < 0.99:
        assert answer.verdict == "none"
        assert "coverage" in answer.reason or "%" in answer.reason


def test_two_storeys_it_cannot_separate_are_ambiguous_not_a_winner():
    """A capture holding rooms from two storeys reads like this, which is
    exactly what `polycam`'s ceiling-band split exists to prevent. Naming the
    better of two indistinguishable answers would hide that."""
    ground = storey("ground")
    twin = storey("twin")          # the same shape: nothing can choose

    answer = rank(shifted(ground, 800.0, 300.0), {"ground": ground, "twin": twin})
    assert answer.verdict in {"ambiguous", "identified"}
    if answer.verdict == "ambiguous":
        assert answer.level is None
        assert "too close" in answer.reason


def test_nothing_to_compare_against_is_said_rather_than_crashed():
    answer = rank(storey("a"), {})
    assert answer.verdict == "none"
    assert answer.ranked == []
