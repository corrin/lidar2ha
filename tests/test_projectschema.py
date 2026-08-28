"""`project.yaml` is refused when it holds a key nothing reads.

The failure this prevents is the one that has no symptom. A misspelled section
is not a crash and not a warning; the declaration simply never runs, and the
model that comes out is plausible. Ten areas of light pairings sat under a
top-level `light_pairing:` while the tool reads `lights.pairing:`, and nothing
said so for the life of the project.
"""

from __future__ import annotations

import textwrap

import pytest

from lidar2ha import projectschema


def write(tmp_path, text: str):
    p = tmp_path / "project.yaml"
    p.write_text(textwrap.dedent(text), encoding="utf-8")
    return p


def test_the_misspelling_that_started_this_is_refused(tmp_path):
    """`light_pairing:` at the top level, which is what my own house had."""
    p = write(tmp_path, """
        rooms: {}
        light_pairing:
          kitchen:
            light.kitchen_west: [[485, 127]]
    """)
    with pytest.raises(SystemExit) as exc:
        projectschema.load(p)
    assert "light_pairing" in str(exc.value)


def test_a_misspelling_inside_a_section_is_refused_too(tmp_path):
    """The old top-level key list could not see this one at all, and it is the
    likelier typo: the section is right and the setting under it is not."""
    p = write(tmp_path, """
        lights:
          pairings:
            kitchen: {}
    """)
    with pytest.raises(SystemExit) as exc:
        projectschema.load(p)
    message = str(exc.value)
    assert "pairings" in message and "lights" in message
    assert "did you mean `pairing`" in message, (
        "a near-miss should name the key it nearly is")


def test_a_wrong_value_is_refused_with_the_choices(tmp_path):
    """`mixing` decides whether a render is 22 frames or 2,097,152, so a value
    outside the three it accepts must not fall through to a default."""
    p = write(tmp_path, """
        render:
          mixing: css
    """)
    with pytest.raises(SystemExit) as exc:
        projectschema.load(p)
    assert "CSS" in str(exc.value)


def test_null_in_rooms_is_a_value_and_not_an_error(tmp_path):
    """`null` means "ask me later" and the template teaches it. If this fails,
    the schema is refusing something the tool documents as correct."""
    p = write(tmp_path, """
        rooms:
          cap1:
            "Living Room": lounge
            "Bedroom": null
    """)
    project = projectschema.load(p)
    assert project.rooms["cap1"]["Bedroom"] is None


def test_settings_keeps_the_shape_the_file_was_written_in(tmp_path):
    """Stages still index into the mapping, and several of them treat an absent
    section as "not configured". Materialising every default would turn an
    absent `deploy:` into a present one with a host of None."""
    p = write(tmp_path, """
        rooms: {}
    """)
    assert projectschema.settings(p) == {"rooms": {}}


def test_the_keys_the_template_writes_all_load(tmp_path):
    """`lidar2ha init` must not write a file its own loader rejects."""
    from lidar2ha.cli import PROJECT_YAML

    p = tmp_path / "project.yaml"
    p.write_text(PROJECT_YAML, encoding="utf-8")
    projectschema.load(p)
