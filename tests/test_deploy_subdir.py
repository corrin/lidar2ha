"""Publishing more than one storey to one Home Assistant.

Per-light frames are named after entity ids, which are unique across a house,
so they survive sharing a directory. `base.png` and `transparent.png` are not.
`base.png` is the frame every other one composites onto, so deploying a second
storey into the same directory replaces the first storey's background with this
one's -- and every frame that storey owns then composites onto the wrong
picture. Nothing said so; the second push looked like an ordinary update.
"""

from __future__ import annotations

from pathlib import Path

from lidar2ha.deploy import (
    Manifest,
    RemoteFile,
    another_render_here,
    card_for_subdir,
    plan,
    referenced_images,
    valid_subdir,
)

CARD = (
    'elements:\n'
    '  - image: /local/floorplan/base.png?version=AB12\n'
    '  - image: /local/floorplan/light.kitchen.png?version=CD34\n'
)


def test_the_card_follows_the_images_into_the_subdirectory():
    """The plugin bakes `/local/floorplan/` in and knows nothing about storeys.
    A card deployed unrewritten points at the root while its images sit one
    directory down, which renders as a page of broken images rather than as an
    error anybody sees."""
    moved = card_for_subdir(CARD, "upstairs")
    assert referenced_images(moved) == {"upstairs/base.png",
                                        "upstairs/light.kitchen.png"}
    assert "/local/floorplan/upstairs/base.png" in moved


def test_no_subdir_leaves_the_card_exactly_as_the_plugin_wrote_it():
    """A house with one storey must keep working untouched, and the card is the
    part a person may already have pasted into a dashboard."""
    assert card_for_subdir(CARD, None) == CARD


def test_a_subdir_that_could_escape_the_floorplan_root_is_refused():
    """This is joined onto a path on a live Home Assistant and then created
    with mkdir. A `..` in it writes somewhere nobody asked for."""
    assert valid_subdir("upstairs")
    assert valid_subdir("mid-level_2")
    for bad in ("..", "../etc", "a/b", "/abs", "", ".hidden", "a b"):
        assert not valid_subdir(bad), f"{bad!r} was accepted"


def test_another_storeys_frames_are_noticed_before_anything_is_copied():
    """The signal that distinguishes a second storey from a stale re-render.

    Both leave files on the target that this render does not own. Only the
    second storey does it while ALSO replacing base.png -- the frames sitting
    beside it are named after other entities, so they are not older versions of
    anything here, and their background is about to become this storey's.
    """
    manifest = Manifest(
        changed=[(Path("base.png"), 100)],
        extra=["light.upstairs_lamp.png", "light.upstairs_desk.png"])
    assert another_render_here(manifest) == ["light.upstairs_lamp.png",
                                             "light.upstairs_desk.png"]


def test_a_plain_re_render_of_one_storey_is_not_warned_about():
    """A person who renames a light re-deploys with one stray frame left over
    and no reason to be told about storeys. A warning that fires on the ordinary
    case is one nobody reads."""
    manifest = Manifest(extra=["light.old_name.png"])
    assert another_render_here(manifest) == [], "fired without base.png changing"


def test_a_first_deploy_into_an_empty_directory_is_not_warned_about():
    """Nothing is being replaced and nothing is being left behind, so there is
    no other storey to collide with. A cross-storey warning on the very first
    deploy would teach the reader to ignore it before it ever meant
    anything."""
    manifest = Manifest(added=[(Path("base.png"), 100)])
    assert another_render_here(manifest) == []


def test_the_manifest_is_scoped_to_the_directory_it_lists():
    """Why a subdirectory beats a filename prefix. Sharing one directory makes
    every other storey's frames `extra` for this one, so the report that exists
    to show what is stale shows the rest of the house instead."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp)
        (local / "base.png").write_bytes(b"x" * 10)
        remote = [RemoteFile("base.png", 10),
                  RemoteFile("light.other_storey.png", 99)]

        shared = plan(local, remote)
        assert shared.extra == ["light.other_storey.png"]

        # The same render into its own subdirectory has nothing to explain.
        own = plan(local, [RemoteFile("base.png", 10)])
        assert own.extra == []


def _fake_transport(recorder):
    class Fake:
        def __init__(self, **kw):
            pass

        def listdir(self, remote):
            return [RemoteFile("base.png", 1)]

        def makedirs(self, remote):
            recorder.append(("makedirs", str(remote)))

        def put(self, local, remote):
            recorder.append(("put", str(remote)))

        def close(self):
            pass

    return Fake


def _render_dir(tmp: Path) -> Path:
    out = tmp / "render_out"
    (out / "floorplan").mkdir(parents=True)
    (out / "floorplan" / "base.png").write_bytes(b"x" * 40)
    (out / "floorplan.yaml").write_text(
        "image: /local/floorplan/base.png?version=AAAA\n", encoding="utf-8")
    return out


def test_a_dry_run_writes_nothing_to_the_live_system(tmp_path, monkeypatch):
    """The safeguard the whole stage is built around, and it was not there.

    `deploy` connects even for a dry run, because a manifest that cannot see the
    target can only say "everything is new". The click subcommand then pushed on
    `if transport and not manifest.empty` -- no test of the flag -- so a bare
    `lidar2ha deploy` copied every changed frame to a live Home Assistant and
    printed "Nothing was copied. Add --push to do it." underneath it.
    """
    from click.testing import CliRunner

    from lidar2ha import cli
    from lidar2ha import deploy as deployer

    done: list[tuple[str, str]] = []
    monkeypatch.setattr(deployer, "SFTPTransport", _fake_transport(done))
    monkeypatch.setattr(deployer, "credentials", lambda project=None: {})

    out = _render_dir(tmp_path)
    result = CliRunner().invoke(cli.cli, ["deploy", str(out)])

    assert result.exit_code == 0, result.output
    assert done == [], f"a dry run wrote to the target: {done}"
    assert "Nothing was copied" in result.output


def test_the_push_flag_does_write(tmp_path, monkeypatch):
    """The other half of the same property: guarding the dry run must not have
    turned --push into a second dry run."""
    from click.testing import CliRunner

    from lidar2ha import cli
    from lidar2ha import deploy as deployer

    done: list[tuple[str, str]] = []
    monkeypatch.setattr(deployer, "SFTPTransport", _fake_transport(done))
    monkeypatch.setattr(deployer, "credentials", lambda project=None: {})

    out = _render_dir(tmp_path)
    result = CliRunner().invoke(cli.cli, ["deploy", str(out), "--push"])

    assert result.exit_code == 0, result.output
    assert ("put", "/config/www/floorplan/base.png") in done, done


def test_push_into_a_subdir_writes_only_there(tmp_path, monkeypatch):
    """The paths the card was rewritten to point at have to be the paths the
    files actually land on, or the dashboard is a page of broken images."""
    from click.testing import CliRunner

    from lidar2ha import cli
    from lidar2ha import deploy as deployer

    done: list[tuple[str, str]] = []
    monkeypatch.setattr(deployer, "SFTPTransport", _fake_transport(done))
    monkeypatch.setattr(deployer, "credentials", lambda project=None: {})

    out = _render_dir(tmp_path)
    result = CliRunner().invoke(
        cli.cli, ["deploy", str(out), "--subdir", "upstairs", "--push", "--card"])

    assert result.exit_code == 0, result.output
    assert ("put", "/config/www/floorplan/upstairs/base.png") in done, done
    # The card and the files are one deliverable: a card pointing at the root
    # while its images sit one level down is a page of broken images.
    assert "/local/floorplan/upstairs/base.png" in result.output
