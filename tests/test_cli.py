"""`doctor`'s view of the Python environment.

The Java half of `doctor` is covered by actually compiling (see `test_levels_java`).
This is the other half: the row that says whether the installed packages still match
the committed lock. It is the only part of `doctor` that can be wrong quietly --
every other row either finds a path or does not.
"""

from __future__ import annotations

import subprocess

import pytest

from lidar2ha.cli import OK, WARN, _dep_status


def _result(returncode: int) -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(args=["uv"], returncode=returncode)


def test_no_lock_reports_nothing_at_all(tmp_path):
    """Someone running an installed wheel has no lock to be out of step with.

    Reporting a row there would put an unexplained warning in a bug report from a
    user who cannot act on it and does not have a checkout to fix.
    """
    assert _dep_status(tmp_path) is None


def test_the_lock_is_found_from_a_subdirectory(tmp_path, monkeypatch):
    """`doctor` is run from wherever the user happens to be, not the repo root.

    If the walk upwards were missing, running it from a capture directory would
    silently report nothing and the stale environment would go unmentioned.
    """
    (tmp_path / "uv.lock").write_text("")
    deep = tmp_path / "captures" / "front-room"
    deep.mkdir(parents=True)

    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cwd"] = kwargs.get("cwd")
        return _result(0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    status, _ = _dep_status(deep)
    assert status == OK
    assert seen["cwd"] == tmp_path, "uv must run against the checkout, not the cwd"


def test_a_missing_uv_warns_rather_than_claiming_success(tmp_path, monkeypatch):
    """Not knowing is not the same as being in sync.

    A contributor without uv on PATH must not read a green row and conclude the
    environment was verified, and `doctor` must not fail because of a check it
    only added for convenience.
    """
    (tmp_path / "uv.lock").write_text("")

    def fake_run(cmd, **kwargs):
        raise FileNotFoundError("uv")

    monkeypatch.setattr(subprocess, "run", fake_run)

    status, detail = _dep_status(tmp_path)
    assert status == WARN
    assert "uv" in detail


def test_a_timeout_is_not_reported_as_a_missing_uv(tmp_path, monkeypatch):
    """A wedged uv and an absent uv need different answers.

    Both degrade to a warning rather than taking the command out, but telling
    someone whose uv timed out to put it on their PATH sends them after the wrong
    problem entirely.
    """
    (tmp_path / "uv.lock").write_text("")

    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd="uv", timeout=30)

    monkeypatch.setattr(subprocess, "run", fake_run)

    status, detail = _dep_status(tmp_path)
    assert status == WARN
    assert "PATH" not in detail
    assert "30s" in detail


def test_a_stale_environment_says_what_to_run(tmp_path, monkeypatch):
    """The whole point of the row: a pulled lock with the old packages installed.

    Naming the command is the difference between a warning that gets fixed and one
    that gets ignored, and `--all-extras` is not optional -- a bare `uv sync` would
    strip paramiko and websockets back out again.
    """
    (tmp_path / "uv.lock").write_text("")
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: _result(1))

    status, detail = _dep_status(tmp_path)
    assert status == WARN
    assert "uv sync --all-extras" in detail


@pytest.mark.parametrize("flag", ["--check", "--frozen", "--all-extras"])
def test_the_check_is_offline_and_forgiving_of_extras(tmp_path, monkeypatch, flag):
    """Three flags, three separate ways this row could lie.

    `--check` so it reports instead of installing; `--frozen` so it compares against
    the committed lock without resolving, which keeps `doctor` off the network; and
    `--all-extras` because without it uv counts paramiko and websockets as
    extraneous and calls a perfectly good environment outdated.
    """
    (tmp_path / "uv.lock").write_text("")
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return _result(0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    _dep_status(tmp_path)
    assert flag in seen["cmd"]
