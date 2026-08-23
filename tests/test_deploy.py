"""Deciding what to copy into a live Home Assistant config directory.

This is the only step that writes to a system someone depends on, so the tests
lead with the thing that must never regress: the bare command copies nothing.
No test here opens an SSH connection — the transport is behind an interface so
the decision logic can be exercised against a fake.
"""

from __future__ import annotations

from pathlib import PurePosixPath

import pytest

from lidar2ha.deploy import (
    REMOTE_ROOT,
    Manifest,
    RemoteFile,
    card_path,
    check_card_matches_images,
    credentials,
    deployable,
    human_bytes,
    plan,
    referenced_images,
)

CARD = """\
type: picture-elements
image: /local/floorplan/transparent.png?version=64CD5766282EC16531CE843036769FDF
elements:
  - type: conditional
    elements:
      - type: image
        image: /local/floorplan/base.png?version=6D5EAE63E44263983C1EE736E0EDBA53
  - type: conditional
    elements:
      - type: image
        image: /local/floorplan/light.hall.png?version=29143F56103B474C74A8D46F4F5A374A
"""


def render_out(tmp_path, images=("base.png", "transparent.png", "light.hall.png"),
               card=CARD, renders=True):
    floorplan = tmp_path / "floorplan"
    floorplan.mkdir()
    for i, name in enumerate(images):
        (floorplan / name).write_bytes(b"x" * (100 + i))
    if renders:
        (tmp_path / "renders").mkdir()
        (tmp_path / "renders" / "base.png").write_bytes(b"y" * 9999)
    if card is not None:
        (tmp_path / "floorplan.yaml").write_text(card, encoding="utf-8")
    return tmp_path


# --------------------------------------------------------------------------- #
# what gets deployed, and what does not
# --------------------------------------------------------------------------- #


def test_only_floorplan_is_deployed(tmp_path):
    """renders/ holds the same images as floorplan/. Shipping both would double
    the transfer for nothing."""
    out = render_out(tmp_path)
    assert deployable(out) == out / "floorplan"


def test_renders_is_never_the_deploy_source(tmp_path):
    out = render_out(tmp_path)
    assert (out / "renders").is_dir(), "the fixture should have one to be tempted by"
    assert deployable(out).name == "floorplan"


def test_a_render_that_did_not_finish_is_refused(tmp_path):
    (tmp_path / "renders").mkdir()
    with pytest.raises(SystemExit, match="floorplan/"):
        deployable(tmp_path)


def test_a_missing_card_is_refused(tmp_path):
    out = render_out(tmp_path, card=None)
    with pytest.raises(SystemExit, match="floorplan.yaml"):
        card_path(out)


def test_the_destination_is_where_the_plugin_points(tmp_path):
    """The plugin writes /local/floorplan/... into the card, and /local/ is
    Home Assistant's alias for /config/www/. There is no other valid target."""
    assert REMOTE_ROOT == PurePosixPath("/config/www/floorplan")
    for name in referenced_images(CARD):
        assert f"/local/floorplan/{name}" in CARD


# --------------------------------------------------------------------------- #
# the manifest
# --------------------------------------------------------------------------- #


def test_an_empty_target_makes_everything_new(tmp_path):
    manifest = plan(render_out(tmp_path) / "floorplan", [])
    assert len(manifest.added) == 3
    assert manifest.changed == [] and manifest.unchanged == []
    assert not manifest.empty


def test_a_matching_target_makes_nothing_to_do(tmp_path):
    local = render_out(tmp_path) / "floorplan"
    remote = [RemoteFile(p.name, p.stat().st_size) for p in local.iterdir()]
    manifest = plan(local, remote)

    assert manifest.to_push == []
    assert manifest.empty
    assert len(manifest.unchanged) == 3


def test_a_different_size_is_a_change(tmp_path):
    local = render_out(tmp_path) / "floorplan"
    remote = [RemoteFile(p.name, p.stat().st_size + 1) for p in local.iterdir()]
    manifest = plan(local, remote)

    assert len(manifest.changed) == 3
    assert manifest.added == []


def test_files_on_the_target_we_did_not_render_are_reported_not_deleted(tmp_path):
    """/config/www is the user's directory. A stale overlay is harmless; an
    over-eager cleanup is not."""
    local = render_out(tmp_path) / "floorplan"
    manifest = plan(local, [RemoteFile("light.someone_elses.png", 10)])

    assert manifest.extra == ["light.someone_elses.png"]
    assert all("someone_elses" not in p.name for p, _ in manifest.to_push)


def test_only_changes_are_pushed(tmp_path):
    local = render_out(tmp_path) / "floorplan"
    files = sorted(local.iterdir())
    remote = [RemoteFile(files[0].name, files[0].stat().st_size)]      # one already there
    manifest = plan(local, remote)

    assert len(manifest.unchanged) == 1
    assert len(manifest.to_push) == 2
    assert manifest.bytes_to_push == sum(p.stat().st_size for p in files[1:])


def test_a_dry_run_copies_nothing(tmp_path):
    """The assertion this file exists for. Building a manifest must not touch
    the transport at all."""
    calls = []

    class RefusingTransport:
        def listdir(self, remote):
            calls.append(("listdir", remote))
            return []

        def put(self, local, remote):
            raise AssertionError("a dry run must not copy anything")

        def makedirs(self, remote):
            raise AssertionError("a dry run must not create anything")

    local = render_out(tmp_path) / "floorplan"
    manifest = plan(local, RefusingTransport().listdir(REMOTE_ROOT))
    assert manifest.to_push
    assert calls == [("listdir", REMOTE_ROOT)]


# --------------------------------------------------------------------------- #
# the card and the images are one artefact
# --------------------------------------------------------------------------- #


def test_the_card_and_its_images_must_come_from_one_render(tmp_path):
    """Each image is referenced with a content hash, so a card shipped beside
    images from another run points at hashes that do not exist."""
    local = render_out(tmp_path, images=("base.png", "transparent.png")) / "floorplan"
    assert check_card_matches_images(CARD, local) == ["light.hall.png"]


def test_a_matching_pair_passes(tmp_path):
    local = render_out(tmp_path) / "floorplan"
    assert check_card_matches_images(CARD, local) == []


def test_the_version_hashes_are_not_mistaken_for_filenames():
    assert referenced_images(CARD) == {"base.png", "transparent.png", "light.hall.png"}


# --------------------------------------------------------------------------- #
# connection settings
# --------------------------------------------------------------------------- #


def test_a_missing_host_says_what_to_set(monkeypatch):
    monkeypatch.delenv("HA_SSH_HOST", raising=False)
    with pytest.raises(SystemExit, match="HA_SSH_HOST"):
        credentials({})


def test_settings_come_from_the_project_when_the_environment_is_silent(monkeypatch):
    for name in ("HA_SSH_HOST", "HA_SSH_USER", "HA_SSH_PORT", "HA_SSH_KEY"):
        monkeypatch.delenv(name, raising=False)
    settings = credentials({"deploy": {"host": "ha.local", "user": "hass", "port": 2222}})
    assert settings == {"host": "ha.local", "user": "hass", "port": 2222, "key": None}


def test_the_environment_wins_over_the_project(monkeypatch):
    monkeypatch.setenv("HA_SSH_HOST", "from-env")
    assert credentials({"deploy": {"host": "from-project"}})["host"] == "from-env"


def test_sizes_read_as_a_human_would_say_them():
    assert human_bytes(512) == "512 B"
    assert human_bytes(2048) == "2 KB"
    assert "MB" in human_bytes(5 * 1024 * 1024)


def test_an_empty_manifest_knows_it_is_empty():
    assert Manifest().empty
    assert Manifest().bytes_to_push == 0
