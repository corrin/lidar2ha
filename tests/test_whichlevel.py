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

    # Either refusal is right and the distinction is not the point: `none` is
    # "nothing fits", `ambiguous` is "these two are the same answer", and a
    # small capture landing plausibly on both is genuinely the second.
    assert answer.level is None, f"named {answer.level!r} for a capture of nowhere"
    assert answer.verdict in {"none", "ambiguous"}
    assert answer.ranked, "a refusal must still show what it tried"


def test_the_reason_names_the_number_that_refused_it():
    """A refusal a reader cannot check is one they will overrule. The distance
    is what separates 'this is another building' from 'this needs a better
    fit'."""
    answer = rank(storey("elsewhere", w=250.0, h=180.0),
                  {"ground": storey("ground")})
    assert "cm" in answer.reason


def test_thin_coverage_is_reported_and_never_refuses():
    """`combine` paid for this rule once: coverage is the fraction of the
    CAPTURE a storey explains, so a capture seeing a room the storey does not
    always scores lower, and a 90% threshold there rejected the one capture
    holding a mid-level bathroom at 88% -- the very room worth having.

    The error decides; a thin fit is said out loud beside it.
    """
    ground = storey("ground")
    # The capture has a wing the storey does not, so part of it matches nothing
    # -- which is exactly the shape of a capture worth keeping.
    lv = ground.levels[0]
    wider = ground.model_copy(update={"levels": [lv.model_copy(update={
        "walls": [*lv.walls, wall(2000.0, 2000.0, 2600.0, 2000.0),
                  wall(2600.0, 2000.0, 2600.0, 2400.0)]})]})
    answer = rank(wider, {"ground": ground}, low_coverage=0.999)

    assert answer.ranked[0].coverage < 0.999, "guard: this fit must read as thin"
    assert answer.verdict == "identified", "coverage refused a good fit"
    assert "%" in answer.reason, "the thinness has to be visible"


def test_a_level_that_cannot_be_fitted_at_all_is_named():
    """A level quietly absent from the ranking looks exactly like one that was
    tried and lost. A capture with no walls to compare against is a fact about
    the input, and the reader has to be told which levels never ran."""
    from lidar2ha.schema import Level as L
    from lidar2ha.schema import Model as M

    wall_less = M(source="x.dxf", levels=[L(name="empty", ceiling_height_cm=250.0)])
    answer = rank(storey("a"), {"good": storey("a"), "empty": wall_less})

    assert any("empty" in u for u in answer.unfittable)
    assert "empty" in answer.reason


def test_two_storeys_it_cannot_separate_are_ambiguous_not_a_winner():
    """A capture holding rooms from two storeys reads like this, which is
    exactly what `polycam`'s ceiling-band split exists to prevent. Naming the
    better of two indistinguishable answers would hide that."""
    ground = storey("ground")
    twin = storey("twin")          # the same shape: nothing can choose

    answer = rank(shifted(ground, 800.0, 300.0), {"ground": ground, "twin": twin})

    # Guard: the two really are indistinguishable, or this proves nothing.
    assert abs(answer.ranked[0].median_cm - answer.ranked[1].median_cm) < 0.5
    assert answer.verdict == "ambiguous", (
        f"picked {answer.level!r} out of two identical storeys")
    assert answer.level is None
    assert "too close" in answer.reason


def test_nothing_to_compare_against_is_said_rather_than_crashed():
    answer = rank(storey("a"), {})
    assert answer.verdict == "none"
    assert answer.ranked == []
