#!/usr/bin/env python3
"""Put the rendered floor plan where Home Assistant can serve it.

This is the only step that writes to a live system someone depends on, so the
bare command writes nothing: it prints a manifest of what would change, and
`--push` commits. A dry run you have to opt out of is worth the extra word.

WHAT SHIPS, AND WHAT DOES NOT. A render leaves three things:

    render_out/renders/        the raytraced frames
    render_out/floorplan/      the same images, plus the transparent spacer
    render_out/floorplan.yaml  the picture-elements card

`renders/` and `floorplan/` hold byte-identical copies of the same frames --
`floorplan/` is simply the set the card refers to, plus `transparent.png`. Only
`floorplan/` is deployed. Shipping both would double the transfer for nothing.

THE DESTINATION IS NOT A CHOICE. The plugin writes `/local/floorplan/<name>.png`
into the card, and `/local/` is Home Assistant's alias for `/config/www/`. So the
images must land at `/config/www/floorplan/` or the card renders broken links,
and there is no configuration that changes it.

ONE STOREY PER SUBDIRECTORY. Per-light frames are named after entity ids, which
are unique across a house, so they survive sharing a directory. `base.png` and
`transparent.png` are not, and `base.png` is the frame every other one is
composited onto -- so deploying a second storey into the same directory
replaces the first storey's background with this one's, and every frame that
storey owns then composites onto the wrong picture. Pass `--subdir` and the
images go one level down with the card rewritten to match. Without it, a
directory already holding another render's frames is reported before anything
is copied.

THE CARD AND THE IMAGES ARE COUPLED. Each image is referenced with a
`?version=<hash>` cache-buster derived from its content, so a card deployed
without its images points at hashes that do not exist, and images deployed
without the card are ignored by the browser's cache. They go together.

Usage:
    python -m lidar2ha.deploy render_out --push
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

# Fixed by the plugin: it writes /local/floorplan/... into the card, and /local/
# is Home Assistant's alias for /config/www/.
REMOTE_ROOT = PurePosixPath("/config/www/floorplan")
DEFAULT_USER = "root"
DEFAULT_PORT = 22


@dataclass
class RemoteFile:
    name: str
    size: int


@dataclass
class Manifest:
    """What a push would change, decided before anything is written."""

    added: list[tuple[Path, int]] = field(default_factory=list)
    changed: list[tuple[Path, int]] = field(default_factory=list)
    unchanged: list[tuple[Path, int]] = field(default_factory=list)
    # Present on the target and not produced by this render. Reported, never
    # deleted: /config/www is the user's, and a stale overlay is harmless while
    # an over-eager cleanup is not.
    extra: list[str] = field(default_factory=list)

    @property
    def to_push(self) -> list[tuple[Path, int]]:
        return self.added + self.changed

    @property
    def bytes_to_push(self) -> int:
        return sum(size for _, size in self.to_push)

    @property
    def empty(self) -> bool:
        return not self.to_push


def deployable(render_out: Path) -> Path:
    """The one directory that gets copied.

    Raises rather than guessing: deploying the wrong directory to /config/www
    is not something to be relaxed about.
    """
    render_out = Path(render_out)
    floorplan = render_out / "floorplan"
    if not floorplan.is_dir():
        raise SystemExit(
            f"No floorplan/ directory in {render_out}. Run `lidar2ha render` first -- "
            f"floorplan/ is what the card refers to, and it is what gets deployed.")
    return floorplan


def card_path(render_out: Path) -> Path:
    path = Path(render_out) / "floorplan.yaml"
    if not path.is_file():
        raise SystemExit(f"No floorplan.yaml in {render_out}; the render did not finish.")
    return path


def referenced_images(card: str) -> set[str]:
    """The image names the card points at, from `/local/floorplan/<name>?version=`."""
    return set(re.findall(r"/local/floorplan/([^?\s\"']+)", card))


def valid_subdir(name: str) -> bool:
    """One path segment, and one that cannot climb out of the floorplan root.

    This string is joined onto a remote path on a live Home Assistant and then
    created with mkdir, so a `..` or an absolute path in it would write
    somewhere nobody asked for.
    """
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name)) and name != ".."


def card_for_subdir(card: str, subdir: str | None) -> str:
    """The card, pointing at the subdirectory the images are going into.

    THE PLUGIN BAKES `/local/floorplan/` IN and knows nothing about levels, so
    the rewrite has to happen here. A card deployed without it points at the
    root while its images sit one directory down, which renders as a page of
    broken images rather than as an error.
    """
    if subdir is None:
        return card
    return card.replace("/local/floorplan/", f"/local/floorplan/{subdir}/")


def digest(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest().upper()


def plan(local_dir: Path, remote: list[RemoteFile]) -> Manifest:
    """Compare what was rendered against what is already on the target.

    Compared by size, which is what SFTP gives cheaply. Two different PNGs of
    exactly the same length would be missed -- but the card's `?version=` hash
    changes with content, so a stale image is a broken reference rather than a
    silently wrong picture, and `--all` exists for when that is not enough.
    """
    manifest = Manifest()
    by_name = {entry.name: entry for entry in remote}

    for path in sorted(Path(local_dir).iterdir()):
        if not path.is_file():
            continue
        size = path.stat().st_size
        existing = by_name.pop(path.name, None)
        if existing is None:
            manifest.added.append((path, size))
        elif existing.size != size:
            manifest.changed.append((path, size))
        else:
            manifest.unchanged.append((path, size))

    manifest.extra = sorted(by_name)
    return manifest


def another_render_here(manifest: Manifest) -> list[str]:
    """Frames on the target that belong to a render this is not.

    A directory holding another storey's output looks exactly like a directory
    holding a stale copy of this one, EXCEPT that `base.png` is about to be
    replaced while a pile of frames nobody in this render owns is left sitting
    beside it. Those frames are named after entity ids, so they are not stale
    versions of anything here -- they belong to a storey whose background is
    about to become this storey's.

    Reported and never acted on. Deleting them would throw away a working
    dashboard, and refusing would block the ordinary case of re-deploying one
    storey after renaming a light.
    """
    replacing_base = any(path.name in ("base.png", "transparent.png")
                         for path, _ in manifest.changed)
    strays = [name for name in manifest.extra if name.endswith(".png")]
    return strays if replacing_base and strays else []


def check_card_matches_images(card: str, local_dir: Path) -> list[str]:
    """Names the card refers to that are not in the directory being deployed.

    The two are written together by one render, so a mismatch means they came
    from different runs -- and a card pointing at an image that will not be
    there is a broken dashboard, discovered by eye rather than by error.
    """
    present = {p.name for p in Path(local_dir).iterdir() if p.is_file()}
    return sorted(referenced_images(card) - present)


def human_bytes(count: int) -> str:
    if count < 1024:
        return f"{count} B"
    if count < 1024 * 1024:
        return f"{count / 1024:.0f} KB"
    return f"{count / 1024 / 1024:.1f} MB"


def credentials(project: dict | None = None) -> dict:
    """SSH settings, from the environment or project.yaml.

    Same shape as `ha.credentials`. Nothing here is ever printed or written to a
    manifest -- a key path is a hint about the machine, and a password is worse.
    """
    section = (project or {}).get("deploy") or {}
    host = os.environ.get("HA_SSH_HOST") or section.get("host")
    if not host:
        raise SystemExit(
            "Set HA_SSH_HOST (in the environment or a .env beside your project), or\n"
            "deploy.host in project.yaml. This is the machine running Home Assistant;\n"
            "on Home Assistant OS it is the Terminal & SSH add-on, and /config is\n"
            "mounted there.")
    return {
        "host": host,
        "user": os.environ.get("HA_SSH_USER") or section.get("user") or DEFAULT_USER,
        "port": int(os.environ.get("HA_SSH_PORT") or section.get("port") or DEFAULT_PORT),
        "key": os.environ.get("HA_SSH_KEY") or section.get("key"),
    }


class SFTPTransport:
    """The real thing. Kept behind the same shape a fake can implement, so the
    manifest logic is testable without opening a connection to anyone."""

    def __init__(self, host: str, user: str, port: int, key: str | None):
        import paramiko

        self._client = paramiko.SSHClient()
        self._client.load_system_host_keys()
        self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self._client.connect(hostname=host, username=user, port=port,
                             key_filename=key, look_for_keys=key is None)
        self._sftp = self._client.open_sftp()

    def listdir(self, remote: PurePosixPath) -> list[RemoteFile]:
        try:
            return [RemoteFile(entry.filename, entry.st_size or 0)
                    for entry in self._sftp.listdir_attr(str(remote))]
        except FileNotFoundError:
            return []

    def makedirs(self, remote: PurePosixPath) -> None:
        parts = PurePosixPath(remote).parts
        for i in range(2, len(parts) + 1):
            path = PurePosixPath(*parts[:i])
            try:
                self._sftp.stat(str(path))
            except FileNotFoundError:
                self._sftp.mkdir(str(path))

    def put(self, local: Path, remote: PurePosixPath) -> None:
        self._sftp.put(str(local), str(remote))

    def close(self) -> None:
        self._sftp.close()
        self._client.close()


def print_manifest(manifest: Manifest, remote_root: PurePosixPath, pushing: bool) -> None:
    print(f"\n  target : {remote_root}")
    for label, entries in (("new", manifest.added), ("changed", manifest.changed)):
        for path, size in entries:
            print(f"    {label:<9} {path.name:<44} {human_bytes(size)}")
    if manifest.unchanged:
        print(f"    unchanged {len(manifest.unchanged)} file(s)")
    for name in manifest.extra:
        print(f"    on target but not in this render: {name}")

    strays = another_render_here(manifest)
    if strays:
        print(f"\n  WARNING: this replaces base.png while leaving {len(strays)} "
              f"frame(s) behind\n  that no render here owns. Those belong to "
              f"another storey, and every one\n  of them composites onto the "
              f"background about to be replaced. Deploy each\n  storey with "
              f"--subdir <name> to keep them apart.")

    if manifest.empty:
        print("\n  Nothing to do; the target already matches this render.")
        return
    print(f"\n  {len(manifest.to_push)} file(s), {human_bytes(manifest.bytes_to_push)}")
    if not pushing:
        print("  Nothing was copied. Add --push to do it.")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("render_out")
    ap.add_argument("--project")
    ap.add_argument("--push", action="store_true", help="actually copy the files")
    ap.add_argument("--all", action="store_true", help="push every file, not only changes")
    ap.add_argument("--card", action="store_true", help="print floorplan.yaml to paste")
    ap.add_argument("--subdir", help="deploy into /config/www/floorplan/NAME/ and "
                                     "point the card there. One storey per "
                                     "subdirectory; see the module docstring")
    args = ap.parse_args()

    if args.subdir is not None and not valid_subdir(args.subdir):
        raise SystemExit(
            f"--subdir {args.subdir!r} is not a single safe path segment. It is "
            f"joined onto a path on a live Home Assistant and then created, so "
            f"it may hold letters, digits, dot, dash and underscore only.")

    remote_root = REMOTE_ROOT if args.subdir is None else REMOTE_ROOT / args.subdir
    local = deployable(args.render_out)
    # Checked against the card AS THE PLUGIN WROTE IT. The rewrite below adds a
    # directory to every reference, which would stop any of them matching a
    # local file name and turn this check into one that always fails.
    as_written = card_path(args.render_out).read_text(encoding="utf-8")

    missing = check_card_matches_images(as_written, local)
    if missing:
        raise SystemExit(
            f"The card refers to images that are not in {local}: {missing}\n"
            f"They come from one render and must ship together; re-run `lidar2ha render`.")

    project = {}
    if args.project:
        import yaml
        project = yaml.safe_load(Path(args.project).read_text(encoding="utf-8")) or {}

    card = card_for_subdir(as_written, args.subdir)
    settings = credentials(project)
    transport = SFTPTransport(**settings) if args.push else None
    try:
        remote = transport.listdir(remote_root) if transport else []
        manifest = plan(local, remote)
        if args.all:
            manifest.changed += manifest.unchanged
            manifest.unchanged = []
        print_manifest(manifest, remote_root, pushing=args.push)

        if args.push and transport:
            transport.makedirs(remote_root)
            for path, _size in manifest.to_push:
                transport.put(path, remote_root / path.name)
                print(f"    pushed {path.name}")
    finally:
        if transport:
            transport.close()

    if args.card:
        print("\n--- floorplan.yaml ---")
        print(card)


if __name__ == "__main__":
    main()
