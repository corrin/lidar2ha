"""The `lidar2ha` command.

Only some of this is built. Commands that do not work yet say so and exit
non-zero rather than failing somewhere deep with a traceback -- an honest
`--help` that matches the documented workflow is more use than a short one that
hides the gaps.

Output is plain text on purpose. `doctor` exists to be pasted into a bug report,
and it must not be able to fail because of its own presentation dependency.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import click

from . import __version__, javabridge, render
from .javabridge import ToolchainError

OK = "ok"
WARN = "warn"
FAIL = "FAIL"


def _row(status: str, label: str, detail: str = "") -> None:
    click.echo(f"  [{status:^4}] {label:<26} {detail}")


def _dep_status(start: Path) -> tuple[str, str] | None:
    """Whether the environment still matches the uv.lock above `start`.

    None outside a checkout: an installed wheel has no lock to be out of step
    with. `--frozen` compares against the committed lock without resolving, so
    this never reaches the network; `--all-extras` because without it uv counts
    paramiko and websockets as extraneous and calls a good environment stale.
    """
    root = next((d for d in (start, *start.parents) if (d / "uv.lock").is_file()), None)
    if root is None:
        return None

    try:
        done = subprocess.run(
            ["uv", "sync", "--check", "--frozen", "--all-extras"],
            cwd=root, capture_output=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        return WARN, "uv did not answer within 30s, so this could not be checked"
    except OSError:
        return WARN, "uv is not on PATH, so this could not be checked"

    if done.returncode != 0:
        return WARN, f"stale against {root / 'uv.lock'} -- run `uv sync --all-extras`"
    return OK, f"in sync with {root / 'uv.lock'}"


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="lidar2ha")
def cli() -> None:
    """Turn a phone LiDAR scan into a 3D floorplan in Home Assistant."""


# --------------------------------------------------------------------------- #
# doctor
# --------------------------------------------------------------------------- #


@cli.command()
@click.option("--sh3d-jar", type=click.Path(), default=None,
              help="path to SweetHome3D.jar or its lib directory, if not in the usual place")
def doctor(sh3d_jar: str | None) -> None:
    """Report what is installed, what is missing, and where to get it.

    Ends by actually compiling the Java against your own Sweet Home 3D. That is
    the point: a doctor that only checked paths would pass happily while the
    sources failed to compile, which is a real thing that has happened here.
    """
    click.echo(f"lidar2ha {__version__}")
    click.echo(f"  python  {sys.version.split()[0]}  ({sys.executable})")

    problems = 0
    warnings = 0

    # Before detect(), which exits outright on an incomplete toolchain and would
    # take this row with it.
    deps = _dep_status(Path.cwd())
    if deps is not None:
        status, detail = deps
        if status == WARN:
            warnings += 1
        _row(status, "deps", detail)
    click.echo()

    try:
        tc = javabridge.detect(sh3d_jar)
    except ToolchainError as exc:
        # These messages are written to be actionable, so print them verbatim.
        click.echo("Toolchain incomplete:\n")
        click.echo(f"  {exc}\n")
        raise SystemExit(1) from exc

    _row(OK, "Sweet Home 3D", str(tc.sh3d_lib))
    _row(OK, "SweetHome3D.jar", str(tc.sweethome_jar))

    if tc.furniture_jar:
        _row(OK, "Furniture.jar", str(tc.furniture_jar))
    else:
        problems += 1
        _row(FAIL, "Furniture.jar", "not found next to SweetHome3D.jar -- "
                                    "lights are built from its catalog and cannot be placed")

    if tc.plugin_jar:
        _row(OK, "floor-plan plugin", str(tc.plugin_jar))
    else:
        warnings += 1
        _row(WARN, "floor-plan plugin", "not installed -- needed only to render. Get it from "
                                        "https://github.com/shmuelzon/home-assistant-floor-plan")

    _row(OK, "javac (JDK)", str(tc.javac))
    _row(OK, "java", str(tc.java))

    try:
        from .glb import find_obj2gltf
        _row(OK, "obj2gltf", " ".join(find_obj2gltf()))
    except ToolchainError:
        warnings += 1
        _row(WARN, "obj2gltf", "not installed -- needed only by `export-glb`. "
                               "npm install -g obj2gltf")

    if tc.render_java:
        _row(OK, "render JVM", str(tc.render_java))
    else:
        warnings += 1
        _row(WARN, "render JVM", "Sweet Home 3D's bundled runtime not found. Java3D and "
                                 "YafaRay are 32-bit natives and need it; writing a .sh3d "
                                 "is unaffected")

    click.echo()
    click.echo("  compiling Java against your installation...")
    try:
        classes = javabridge.compile_java(tc)
        _row(OK, "java sources", str(classes))
    except ToolchainError as exc:
        problems += 1
        _row(FAIL, "java sources", "compilation failed")
        click.echo()
        click.echo(str(exc))

    click.echo()
    if problems:
        click.echo(f"{problems} problem(s) must be fixed before `lidar2ha build` will work.")
        raise SystemExit(1)
    if warnings:
        click.echo(f"Ready to build a .sh3d. {warnings} warning(s) affect later stages only.")
    else:
        click.echo("Everything checks out.")


# --------------------------------------------------------------------------- #
# init
# --------------------------------------------------------------------------- #


PROJECT_YAML = """\
# lidar2ha project. Paths are relative to this file.
name: {name}

# Uncomment if Sweet Home 3D is not in the usual place for your platform.
# sweethome3d_jar: C:\\Program Files (x86)\\Sweet Home 3D\\lib\\SweetHome3D.jar

# Physical size of one tiled texture patch, in centimetres.
tile_cm: 100

# Levels whose elevation the mesh could not recover, in centimetres above the
# lowest floor. `lidar2ha build` reports which levels defaulted to 0.
elevations: {{}}

# Which captures make up each level, for `lidar2ha combine <level>`. Geometry
# never merges across levels -- captures of different storeys share no frame --
# so combining is per level, and this is what says which captures share one.
# Key by the names in `homeassistant.floors`; the values are ids from `captures`.
levels: {{}}

# Scanner room name -> Home Assistant area id, per capture. A scanner names
# rooms by guessing, so this is the mapping you confirm once. `rooms` fails
# without an entry for the capture it is given.
#   rooms:
#     midlevel:
#       "Living Room": lounge
#       "Office 1": kitchen        # many scanner rooms may share one area
rooms: {{}}

# Scanner rooms to union, per capture -- the scanner split one open volume in
# two and its boundary is an artefact of that capture alone.
#   merge:
#     midlevel:
#       - ["Kitchen", "Office 1"]
merge: {{}}

# Rooms to CUT, per LEVEL rather than per capture, because an open plan's
# fusion is the architecture: there is no wall to segment on, so every capture
# fuses it and no rescanning separates the kitchen end from the dining end. The
# boundary is your declaration. Coordinates are plan centimetres, read off
# `python -m lidar2ha.preview <level>_combined.json`.
#   split:
#     "Mid Level":
#       - room: lounge                       # a line: two pieces
#         seam: [[-240, -160], [20, -170]]
#         names: [stairwell, den]
#       - room: open_living                  # or a traced outline per room
#         sections:
#           - name: kitchen
#             box: [[80, -420], [310, -140]]
#           - name: dining
#             outline: [[310, -420], [560, -420], [560, -140], [310, -140]]
#           - name: lounge
#             box: [[310, -140], [700, 260]]
split: {{}}

# Where to look from. The tool solves HOW FAR back to stand so the whole house
# fits; these are the choices it cannot make for you.
camera:
  yaw: 180        # degrees. Which side you view the plan from.
  pitch: 50       # degrees below horizontal. 90 is a flat plan view; at 50 you
                  # see wall faces, but near walls can hide rooms behind them.

# The render size, which also fixes the aspect ratio the camera is framed for.
# `render` warns if you ask it for a different shape than `build` framed.
render:
  width: 1920
  height: 1080
  quality: HIGH     # LOW does not raytrace at all -- it screenshots the GL view,
                    # and returns a BLANK frame when there is no GL context.
  renderer: SUNFLOW # or YAFARAY
  # How many images get made, and it is not a small difference:
  #   CSS      one per light, the browser adds them        n + 1
  #   OVERLAY  every combination of each room's lights     2^(lights in room)
  #   FULL     every combination in the house              2^n
  # On one 21-light house: 22 frames, 65541 frames, and 2097152 frames.
  mixing: CSS
"""


@cli.command()
@click.argument("directory", type=click.Path(path_type=Path))
def init(directory: Path) -> None:
    """Create a project directory."""
    directory.mkdir(parents=True, exist_ok=True)
    config = directory / "project.yaml"
    if config.exists():
        raise SystemExit(f"{config} already exists; not overwriting it")
    config.write_text(PROJECT_YAML.format(name=directory.name), encoding="utf-8")
    for sub in ("captures", "build"):
        (directory / sub).mkdir(exist_ok=True)
    click.echo(f"created {directory}/")
    click.echo(f"  {config.name}")
    click.echo("  captures/   put the Polycam exports here")
    click.echo("  build/      generated files land here")


# --------------------------------------------------------------------------- #
# combine
# --------------------------------------------------------------------------- #


@cli.command()
@click.argument("level")
@click.option("--project", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              default=Path("project.yaml"), show_default=True,
              help="project.yaml, which says which captures make up this level")
@click.option("-o", "--out", type=click.Path(path_type=Path), default=None,
              help="combined model json  [default: <level>_combined.json]")
@click.option("--reference", default=None,
              help="capture id to anchor the level on. The default picks the "
                   "geometry capture with the most walls")
@click.option("--max-median-cm", type=float, default=None,
              help="a fit worse than this is not the same rooms. This is a FIT "
                   "QUALITY threshold, never a coverage one -- a capture that sees "
                   "a room the others do not always scores lower on coverage, and "
                   "rejecting on that discards the reason to combine at all")
@click.option("--max-p90-cm", type=float, default=None,
              help="the tail matters more than the median: two captures can agree "
                   "at 6 cm median and still differ by 44 cm at p90, which on a "
                   "2.6 m2 room is a large part of the room")
@click.option("--storey", default=None,
              help="which level to take from INSIDE each capture, when a capture "
                   "holds more than one. This is a Level.name in the model json, "
                   "not the LEVEL argument above -- that names a group of captures "
                   "in project.yaml")
def combine(level: str, project: Path, out: Path | None, reference: str | None,
            max_median_cm: float | None, max_p90_cm: float | None,
            storey: str | None) -> None:
    """Merge every capture of one LEVEL into one model, and say what to re-scan.

    Geometry is SELECTED and never averaged: two plans of one room disagree by
    around 17 cm, and blending them matches neither wall. The winner takes each
    room whole and the room records which capture it came from.

    The model is only half the output. The other half is the work list -- the
    rooms whose best source is a fixture pass, the places the captures disagree,
    and the floor some capture saw that the model does not contain.
    """
    import json

    from . import combine as combining
    from .schema import load_model, save_model

    settings = _project_settings(project)
    levels = settings.get("levels") or {}
    if not levels:
        raise SystemExit(
            f"{project} has no `levels:` section, so there is nothing saying which\n"
            f"captures make up {level!r}. Add one, keyed by the names in\n"
            f"`homeassistant.floors` and listing the ids from `captures:`:\n\n"
            f"levels:\n"
            f'  "Mid Level": [midlevel, midlevel_fixtures]\n')
    if level not in levels:
        raise SystemExit(f"{project} has no level {level!r}. It has: "
                         f"{', '.join(sorted(levels))}")

    ids = list(levels[level] or [])
    if len(ids) < 2:
        raise SystemExit(f"level {level!r} lists {len(ids)} capture(s). One capture "
                         f"needs no combining -- build from it directly.")

    captures = settings.get("captures") or {}
    models = {}
    for capture_id in ids:
        # Prefer the model `rooms` has already named. A scanner name is not
        # identity, and combining before naming makes the work list ask about
        # rooms the project has already answered for.
        found = None
        for suffix in ("_named.json", "_registered.json", ".json"):
            for base in (project.parent / f"exports/{capture_id}",
                         project.parent / "captures" / capture_id, project.parent):
                path = base / f"{capture_id}{suffix}"
                if path.exists():
                    found = path
                    break
            if found:
                break
        if found is None:
            raise SystemExit(
                f"no model json found for capture {capture_id!r}. Looked for "
                f"{capture_id}_named.json, {capture_id}_registered.json and "
                f"{capture_id}.json under {project.parent}.\n"
                f"Run `python -m lidar2ha.polycam` and `.registration` on it first.")
        click.echo(f"  {capture_id:<22} {found.relative_to(project.parent)}")
        models[capture_id] = load_model(found)

    areas: set[str] = set()
    for capture_id in ids:
        mapping = (settings.get("rooms") or {}).get(capture_id) or {}
        areas.update(a for a in mapping.values() if a)
    if not areas:
        click.echo("  note: project.yaml maps no areas for these captures, so the "
                   "work list cannot say which areas ended up with no source.")

    # Areas that only exist after `split` has run. Combining happens first, so
    # the work list is right to say nothing sourced them -- but saying so
    # without this note reads as a missing room rather than a pending step.
    later = {s.get("name") for d in (settings.get("split") or {}).get(level) or []
             for s in (d.get("sections") or [])} | {
        n for d in (settings.get("split") or {}).get(level) or []
        for n in (d.get("names") or [])}
    later &= areas
    if later:
        click.echo(f"  note: {', '.join(sorted(later))} are declared under "
                   f"`split:` and only exist once `lidar2ha split \"{level}\"` "
                   f"has run, so\n        the work list will list them as areas "
                   f"with no source.")

    multi = [c for c in ids if (captures.get(c) or {}).get("multi_floor")]
    if multi and storey is None:
        click.echo(f"  note: {', '.join(multi)} declared multi_floor. Combining is "
                   f"per storey, so pass --storey NAME to say which one to take "
                   f"from each -- otherwise they will be refused.")

    click.echo("")
    try:
        result = combining.combine(
            models, level_name=storey, reference=reference, expected_areas=areas,
            max_median_cm=max_median_cm or combining.MAX_MEDIAN_CM,
            max_p90_cm=max_p90_cm or combining.MAX_P90_CM)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    combining.report(result)
    slug = level.lower().replace(" ", "_")
    out = out or Path(f"{slug}_combined.json")
    save_model(result.model, out)
    work = out.with_name(out.stem + "_worklist.json")
    work.write_text(json.dumps(result.worklist, indent=2), encoding="utf-8")
    click.echo(f"\nwrote  {out}")
    click.echo(f"wrote  {work}  ({len(result.worklist)} thing(s) to do about the house)")
    click.echo(f"\nNext:  lidar2ha lights {out} --project {project} -o lights.json")


# --------------------------------------------------------------------------- #
# split
# --------------------------------------------------------------------------- #


@cli.command()
@click.argument("level")
@click.option("--project", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              default=Path("project.yaml"), show_default=True,
              help="project.yaml, whose `split:` section says where the rooms are")
@click.option("-i", "--model", "model_json",
              type=click.Path(exists=True, dir_okay=False, path_type=Path),
              default=None,
              help="the model to cut  [default: <level>_combined.json]")
@click.option("-o", "--out", type=click.Path(path_type=Path), default=None,
              help="split model json  [default: <level>_split.json]")
@click.option("--mesh", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              default=None,
              help="mesh of the ANCHOR capture, to ask the floor whether each "
                   "declared boundary is a real one. Optional, and the answer is "
                   "never a veto: an open plan's boundaries are as often a matter "
                   "of use as of construction, and the floor beneath one of those "
                   "does not change at all")
@click.option("--overlap-slop-m2", type=float, default=None,
              help="two traces claiming the same floor, below which it is the "
                   "hand rather than a disagreement")
@click.option("--min-remainder-m2", type=float, default=None,
              help="floor no section claimed, above which it is kept as its own "
                   "piece rather than absorbed into the nearest")
def split(level: str, project: Path, model_json: Path | None, out: Path | None,
          mesh: Path | None, overlap_slop_m2: float | None,
          min_remainder_m2: float | None) -> None:
    """Cut this LEVEL's fused rooms into the rooms the house is actually used as.

    An open plan has no wall for a scanner to segment on, so every capture fuses
    the kitchen end and the dining end and no amount of rescanning separates
    them. The boundary is a declaration about the house, and it lives in
    `project.yaml` under `split:`, keyed by level -- unlike `merge:`, which
    repairs one particular scanner's arbitrary segmentation and so is keyed by
    capture.

    Coordinates are plan centimetres in the combined model's own frame. Read
    them off `python -m lidar2ha.preview`, which draws a metre grid labelled in
    centimetres for exactly this.
    """
    from . import seams
    from .schema import load_model, save_model

    settings = _project_settings(project)
    declared = (settings.get("split") or {}).get(level)
    if not declared:
        have = ", ".join(sorted(settings.get("split") or {})) or "none"
        raise SystemExit(
            f"{project} has no `split:` entry for level {level!r} (it has: {have}).\n"
            f"Add one, keyed by level and naming the room to cut:\n\n"
            f"split:\n"
            f'  "{level}":\n'
            f"    - room: open_living\n"
            f"      sections:\n"
            f"        - name: kitchen\n"
            f"          box: [[80, -420], [310, -140]]\n"
            f"        - name: lounge\n"
            f"          box: [[310, -140], [700, 260]]\n")

    slug = level.lower().replace(" ", "_")
    model_json = model_json or Path(f"{slug}_combined.json")
    if not model_json.exists():
        raise SystemExit(
            f"no model at {model_json}. Run `lidar2ha combine {level!r}` first, "
            f"or pass --model.")

    model = load_model(model_json)
    floor = None
    if mesh:
        from . import thresholds
        floor = thresholds.floor_samples(str(mesh))
        click.echo(f"  {len(floor.points):,} floor faces from {mesh.name}")

    try:
        cuts = seams.apply(
            model, declared, floor=floor,
            overlap_slop_m2=(seams.OVERLAP_SLOP_M2 if overlap_slop_m2 is None
                             else overlap_slop_m2),
            min_remainder_m2=(seams.MIN_REMAINDER_M2 if min_remainder_m2 is None
                              else min_remainder_m2))
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    seams.report(cuts)
    out = out or Path(f"{slug}_split.json")
    save_model(model, out)
    click.echo(f"\nwrote  {out}")
    if not mesh:
        click.echo("  no --mesh given, so no boundary was looked at. That is not "
                   "the same\n  as one the floor does not support.")
    click.echo(f"\nNext:  python -m lidar2ha.ceilings {out} <mesh>.obj"
               f"\n       python -m lidar2ha.preview {out} -o plan.png"
               f"\n       lidar2ha lights {out} --project {project} -o lights.json")


# --------------------------------------------------------------------------- #
# build
# --------------------------------------------------------------------------- #


@cli.command()
@click.argument("model_json", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("-o", "--out", type=click.Path(path_type=Path), default=Path("home.sh3d"),
              show_default=True, help="the .sh3d to write")
@click.option("--scene", type=click.Path(path_type=Path), default=None,
              help="where to keep the intermediate scene file [default: alongside --out]")
@click.option("--textures", type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None, help="directory of floor.png / wall.png / ceiling.png")
@click.option("--walltex", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              default=None, help="manifest.json of per-wall rectified textures")
@click.option("--lights", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              default=None, help="JSON list of light placements")
@click.option("--tile-cm", type=float, default=100.0, show_default=True,
              help="physical size of one texture tile, in centimetres")
@click.option("--elevation", "elevations", multiple=True, metavar="NAME=CM",
              help="set a level's elevation, e.g. --elevation 'Upper=262'")
@click.option("--ceilings", is_flag=True,
              help="emit ceiling textures. Off by default: a visible ceiling occludes "
                   "the level being rendered from above")
@click.option("--project", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              default=None, help="project.yaml, for camera.yaw / pitch and render size")
@click.option("--verify/--no-verify", default=True, show_default=True,
              help="reopen the .sh3d through Sweet Home 3D's own reader afterwards")
def build(model_json: Path, out: Path, scene: Path | None, textures: Path | None,
          walltex: Path | None, lights: Path | None, tile_cm: float,
          elevations: tuple[str, ...], ceilings: bool, project: Path | None,
          verify: bool) -> None:
    """Write a .sh3d from a model JSON.

    Runs the scene writer, then Sweet Home 3D's own classes, then reopens the
    result. The last step is not ceremony: an archive can look structurally
    fine and still fail to open, and that is the failure worth catching here
    rather than in the GUI.
    """
    import json

    from .camera import CameraConfig
    from .scene import write_scene
    from .schema import Light, load_model, load_wall_textures

    model = load_model(model_json)
    if model.role != "geometry":
        raise SystemExit(
            f"{model_json} is a {model.role} capture, not a survey of the building.\n"
            "A fixture pass trades geometry away on purpose -- its walls and floor\n"
            "heights are wrong by design. Build from the geometry capture and feed\n"
            "this one in through `placefixtures` and `lights --fittings` instead.")
    settings = _project_settings(project)
    # The render size decides the aspect ratio, and the aspect ratio decides how
    # far back the camera has to be -- so the framing has to agree with whatever
    # `render` will eventually be asked for. One source of truth, in the project.
    render = (settings.get("render") or {})
    width = float(render.get("width", 1920))
    height = float(render.get("height", 1080))

    overrides: dict[str, float] = {}
    for item in elevations:
        name, _, value = item.partition("=")
        if not value:
            raise SystemExit(f"--elevation expects NAME=CM, got {item!r}")
        overrides[name.strip()] = float(value)

    tiled: dict[str, Path] = {}
    if textures:
        for kind in ("floor", "wall", "ceiling"):
            path = textures / f"{kind}.png"
            if path.exists():
                tiled[kind] = path
            else:
                click.echo(f"  note: no {kind}.png in {textures}")

    wall_textures = load_wall_textures(walltex) if walltex else []
    placements = (
        [Light.model_validate(entry)
         for entry in json.loads(Path(lights).read_text(encoding="utf-8"))]
        if lights else []
    )

    scene_path = scene or out.with_suffix(".tsv")
    stats = write_scene(
        model, scene_path,
        wall_textures=wall_textures, tiled_textures=tiled, lights=placements,
        tile_cm=tile_cm, elevation_overrides=overrides, include_ceilings=ceilings,
        camera=CameraConfig.from_project(settings), aspect=width / height,
    )
    click.echo(
        f"scene  {scene_path}  levels={stats['levels']} walls={stats['walls']} "
        f"rooms={stats['rooms']} lights={stats['lights']} "
        f"textured_walls={stats['textured_walls']}"
    )
    click.echo(f"       framed for {width:.0f}x{height:.0f}")
    for name in stats["unknown_elevations"]:
        click.echo(f"  warning: level {name!r} has no elevation, defaulted to 0 "
                   f"-- set it with --elevation {name!r}=CM")

    try:
        tc = javabridge.detect()
        classes = javabridge.compile_java(tc)
    except ToolchainError as exc:
        click.echo(f"\n{exc}\n")
        raise SystemExit("run `lidar2ha doctor` for the full picture") from exc

    out.parent.mkdir(parents=True, exist_ok=True)
    proc = javabridge.run_writer(tc, classes, "Sh3dWriter", str(scene_path), str(out))
    if proc.returncode != 0:
        click.echo(proc.stdout, nl=False)
        click.echo(proc.stderr, nl=False, err=True)
        raise SystemExit("Sh3dWriter failed")
    click.echo(f"wrote  {out}")

    if verify:
        proc = javabridge.run_writer(tc, classes, "Sh3dVerify", str(out))
        click.echo(proc.stdout, nl=False)
        if proc.returncode != 0:
            click.echo(proc.stderr, nl=False, err=True)
            raise SystemExit("the .sh3d opened but did not look right")


# --------------------------------------------------------------------------- #
# lights
# --------------------------------------------------------------------------- #


def _project_settings(project: Path | None) -> dict:
    if not project:
        return {}
    import yaml
    return yaml.safe_load(project.read_text(encoding="utf-8")) or {}


@cli.command()
@click.argument("model_json", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("-o", "--out", type=click.Path(path_type=Path), default=Path("lights.json"),
              show_default=True, help="the light placements to write")
@click.option("--registry", type=click.Path(path_type=Path), default=Path("registry.json"),
              show_default=True, help="where the Home Assistant registry is cached")
@click.option("--refresh", is_flag=True,
              help="fetch the registry from Home Assistant rather than using the cache")
@click.option("--project", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              default=None, help="project.yaml, for lights.exclude / include / extra / power")
@click.option("--fittings", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              default=None, help="real fitting positions, when you have them")
@click.option("--env", type=click.Path(path_type=Path), default=Path(".env"),
              show_default=True, help="file to read HA_URL and HA_TOKEN from")
@click.option("--report/--no-report", default=True, show_default=True,
              help="print the review table")
def lights(model_json: Path, out: Path, registry: Path, refresh: bool, project: Path | None,
           fittings: Path | None, env: Path, report: bool) -> None:
    """Place every Home Assistant light entity in its room.

    Entities resolve to rooms by Home Assistant area, so `rooms` has to have run
    first. Nothing is dropped in silence: an entity with no area, an area with no
    room, and a room nobody lights are each named in the report, because a dark
    room in the finished dashboard is otherwise a mystery with no thread to pull.
    """
    from . import ha
    from .lights import (
        LightsConfig,
        build_lights,
        load_fittings,
        print_report,
        room_index,
        save_lights,
    )
    from .schema import load_model

    settings = _project_settings(project)

    if refresh:
        ha.load_dotenv(env)
        url, token = ha.credentials(settings)
        click.echo(f"fetching the registry from {url} ...")
        ha.save_registry(ha.fetch_registry(url, token), registry)
        click.echo(f"cached {registry}")

    model = load_model(model_json)
    if not room_index(model):
        raise SystemExit(
            "No room in this model carries a Home Assistant area. Run "
            "`python -m lidar2ha.rooms` first -- lights are placed by area, not "
            "by the name the scanner guessed.")

    entities = ha.light_entities(ha.load_registry(registry))
    placements, result = build_lights(
        model, entities, LightsConfig.from_project(settings),
        load_fittings(fittings) if fittings else None)
    result.duplicate_names = ha.duplicate_names(entities)
    save_lights(placements, out)

    click.echo(f"wrote {out}  ({len(placements)} placement(s) from {len(entities)} entities)")
    if report:
        print_report(result, placements)
    click.echo(f"Next:  lidar2ha build {model_json} --lights {out}")


# --------------------------------------------------------------------------- #
# render
# --------------------------------------------------------------------------- #


@cli.command(name="render")
@click.argument("sh3d", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("-o", "--out", type=click.Path(path_type=Path), default=Path("render_out"),
              show_default=True, help="where the images and floorplan.yaml go")
@click.option("--project", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              default=None, help="project.yaml, for the render size and quality")
@click.option("--list", "list_only", is_flag=True,
              help="report what the plugin detects and what it would cost, and stop")
@click.option("--preview", is_flag=True,
              help=f"render small ({render.PREVIEW_WIDTH}x{render.PREVIEW_HEIGHT}) "
                   "to check the framing before committing to a full pass")
@click.option("--reuse", is_flag=True,
              help="regenerate the floor plan and YAML from images already rendered")
@click.option("--yes", "-y", is_flag=True, help="do not ask before a long render")
@click.option("--width", type=int, default=None, help="override the project's render width")
@click.option("--height", type=int, default=None, help="override the project's render height")
@click.option("--quality", type=click.Choice(["HIGH", "LOW"], case_sensitive=False),
              default=None, help="LOW does not raytrace; see the warning it prints")
@click.option("--renderer", type=click.Choice(["SUNFLOW", "YAFARAY"], case_sensitive=False),
              default=None)
@click.option("--mixing", type=click.Choice(["CSS", "OVERLAY", "FULL"], case_sensitive=False),
              default=None, help="OVERLAY and FULL render combinations; read the count first")
def render_cmd(sh3d: Path, out: Path, project: Path | None, list_only: bool, preview: bool,
               reuse: bool, yes: bool, width: int | None, height: int | None,
               quality: str | None, renderer: str | None, mixing: str | None) -> None:
    """Raytrace the model once per light state and emit floorplan.yaml.

    The cost is knowable before any of it is spent, so it always is: `--list`
    reports the frame count for free, and a full run prints it and an estimate
    before starting. The number matters because one setting changes it by orders
    of magnitude -- CSS renders one image per light, FULL renders every
    combination of every light in the house.
    """
    import subprocess

    settings = _project_settings(project)
    conf = settings.get("render") or {}
    if preview:
        width, height = render.PREVIEW_WIDTH, render.PREVIEW_HEIGHT
    width = width or int(conf.get("width", 1920))
    height = height or int(conf.get("height", 1080))
    quality = (quality or conf.get("quality") or "HIGH").upper()
    renderer = (renderer or conf.get("renderer") or "SUNFLOW").upper()
    mixing = (mixing or conf.get("mixing") or "CSS").upper()

    # `build` framed the camera for the project's aspect ratio. Rendering at a
    # different one silently crops or letterboxes a carefully solved shot.
    framed = float(conf.get("width", 1920)) / float(conf.get("height", 1080))
    if abs(width / height - framed) > 0.01:
        click.echo(f"  warning: rendering at {width}x{height} but the camera was framed "
                   f"for {framed:.2f}:1. Re-run `build` with a matching project.yaml, "
                   f"or expect the edges to be wrong.")

    try:
        tc = javabridge.detect()
        classes = javabridge.compile_java(tc)
    except ToolchainError as exc:
        click.echo(f"\n{exc}\n")
        raise SystemExit("run `lidar2ha doctor` for the full picture") from exc
    if not tc.plugin_jar:
        raise SystemExit(
            "The home-assistant-floor-plan plugin is not installed, and it is what "
            "does the rendering. Get it from "
            "https://github.com/shmuelzon/home-assistant-floor-plan")

    out.mkdir(parents=True, exist_ok=True)
    log = out / "render.log"
    properties = {"quality": quality, "renderer": renderer, "mixing": mixing,
                  "useExistingRenders": "true" if reuse else "false"}

    # The free pass: detection and the frame count, without tracing a pixel.
    log.unlink(missing_ok=True)
    javabridge.run_render(tc, classes, log, str(sh3d), "--list", **properties)
    report = render.parse_log(log.read_text(encoding="utf-8", errors="replace"))
    for error in report.errors:
        click.echo(f"  {error}")

    click.echo(f"  detected  : {len(report.lights)} light entities, "
               f"{len(report.others)} other")
    for name in report.lights:
        click.echo(f"      {name}")
    if not report.lights:
        click.echo("  Nothing to render. The plugin matches furniture by name against "
                   "entity ids, so a light it cannot see is usually invisible or has "
                   "power 0.")

    frames = report.total_renders or 0
    estimate = render.estimate_seconds(frames, width, height)
    click.echo(f"  plan      : {frames} frame(s) at {width}x{height}, {mixing} mixing")
    click.echo(f"  estimate  : about {render.human_duration(estimate)}")

    if list_only:
        return
    if frames > render.CONFIRM_ABOVE and not yes:
        click.confirm(f"  That is {frames} renders. Go ahead?", abort=True)

    click.echo(f"  rendering into {out} ...")
    log.unlink(missing_ok=True)
    command = javabridge.render_command(
        tc, classes, log, str(sh3d), str(out), str(width), str(height), **properties)
    process = subprocess.Popen(command)

    def progress(done: int, total: int | None) -> None:
        click.echo(f"      {done}/{total or '?'} frames")

    try:
        render.follow(log, out, lambda: process.poll() is None, frames, progress)
    except KeyboardInterrupt:
        process.terminate()
        raise SystemExit(
            f"\nStopped. The frames already in {out / 'renders'} are kept -- re-run "
            f"with --reuse to rebuild the floor plan from them without raytracing "
            f"again.") from None

    report = render.parse_log(log.read_text(encoding="utf-8", errors="replace"))
    for error in report.errors:
        click.echo(f"  {error}")
    if process.returncode != 0 or not report.finished:
        raise SystemExit(f"the render did not finish -- see {log}")

    result = render.describe_output(out)
    click.echo(f"  done in {render.human_duration(report.seconds or 0)}  "
               f"({result['renders']} frames, {result['overlays']} overlays)")
    if not result["card"]:
        raise SystemExit(f"no floorplan.yaml in {out} -- see {log}")
    click.echo(f"Next:  lidar2ha deploy {out}")


# --------------------------------------------------------------------------- #
# export-glb
# --------------------------------------------------------------------------- #


@cli.command(name="export-glb")
@click.argument("sh3d", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("-o", "--out", type=click.Path(path_type=Path), default=Path("home.glb"),
              show_default=True, help="the .glb to write")
@click.option("--keep-obj/--no-keep-obj", default=True, show_default=True,
              help="keep the intermediate .obj and .mtl beside the .glb")
def export_glb(sh3d: Path, out: Path, keep_obj: bool) -> None:
    """Export the model as a named GLB, for a real-time 3D floor-plan card.

    This is the other consumer of the same model: `render` raytraces it into
    overlay images, and a 3D card loads the geometry and lights it live. The
    card binds entities to objects BY NAME, so the only thing that makes the
    file useful is that each object is called after its entity id -- which is
    the one property the obvious conversion silently destroys. It is therefore
    counted, here, every time.
    """
    from . import glb

    try:
        tc = javabridge.detect()
        classes = javabridge.compile_java(tc)
    except ToolchainError as exc:
        click.echo(f"\n{exc}\n")
        raise SystemExit("run `lidar2ha doctor` for the full picture") from exc

    # obj2gltf resolves the .mtl and its textures relative to the .obj, so the
    # intermediate has to live beside the target rather than in a temp dir.
    out.parent.mkdir(parents=True, exist_ok=True)
    obj = out.with_suffix(".obj")
    log = out.with_suffix(".log")
    log.unlink(missing_ok=True)

    # ObjExport builds Java3D scene graphs even though it traces nothing, so it
    # needs Sweet Home 3D's own 32-bit runtime -- and that runtime has no
    # console, which is why there is a log to read rather than output to print.
    click.echo(f"  exporting {sh3d} -> {obj} ...")
    javabridge.run_render(tc, classes, log, str(sh3d), str(obj),
                          main_class="ObjExport")
    output = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
    if not obj.exists():
        click.echo(output, nl=False)
        raise SystemExit(f"ObjExport wrote no {obj} -- see {log}")
    for line in output.splitlines():
        if line.startswith("wrote ") or "skipped" in line or "Exception" in line:
            click.echo(f"      {line}")

    click.echo(f"  converting -> {out} ...")
    try:
        glb.convert(obj, out)
    except ToolchainError as exc:
        raise SystemExit(str(exc)) from exc

    before, survived, splits = glb.check_names(obj, out)
    click.echo(f"  entity names: {len(before)} in the OBJ, "
               f"{len(survived)} survived into the GLB")
    lost = sorted(before - survived)
    if lost:
        click.echo(f"  LOST {len(lost)}: {', '.join(lost[:8])}"
                   f"{' ...' if len(lost) > 8 else ''}")
    if splits:
        worst = max(splits.values())
        click.echo(f"  {len(splits)} of them are split by material into up to {worst + 1} "
                   f"nodes. Only the un-suffixed one carries the entity id, so a card "
                   f"matching by name lights that part and not the rest.")
    if before and not survived:
        raise SystemExit(
            "Every entity name was lost in the conversion. The GLB is valid and "
            "completely inert -- a card matching entities by name will bind "
            "nothing. Check that obj2gltf, not something else, did the conversion.")
    if not before:
        click.echo("  No entity-shaped names in the OBJ at all. The .sh3d's furniture "
                   "is named after entity ids by `build --lights`; without that there "
                   "is nothing for a card to bind to.")

    if not keep_obj:
        obj.unlink(missing_ok=True)
        obj.with_suffix(".mtl").unlink(missing_ok=True)
    click.echo(f"wrote  {out}")


# --------------------------------------------------------------------------- #
# deploy
# --------------------------------------------------------------------------- #


@cli.command()
@click.argument("render_out", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--project", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              default=None, help="project.yaml, for deploy.host / user / port / key")
@click.option("--push", is_flag=True, help="actually copy the files")
@click.option("--all", "push_all", is_flag=True,
              help="push every file, not only the ones that differ")
@click.option("--card", "show_card", is_flag=True,
              help="print floorplan.yaml, to paste as a dashboard card")
@click.option("--env", type=click.Path(path_type=Path), default=Path(".env"),
              show_default=True, help="file to read HA_SSH_* from")
@click.option("--subdir", default=None,
              help="deploy into /config/www/floorplan/NAME/ and point the card "
                   "there. One storey per subdirectory")
def deploy(render_out: Path, project: Path | None, push: bool, push_all: bool,
           show_card: bool, env: Path, subdir: str | None) -> None:
    """Copy the floor plan to Home Assistant, and print the card.

    Writes nothing unless you pass --push. This is the one step that touches a
    live system, and a manifest you have to opt out of is cheaper than undoing a
    mistake in someone's /config.
    """
    from . import deploy as deployer
    from . import ha

    if subdir is not None and not deployer.valid_subdir(subdir):
        raise SystemExit(
            f"--subdir {subdir!r} is not a single safe path segment. It is joined "
            f"onto a path on a live Home Assistant and then created, so it may "
            f"hold letters, digits, dot, dash and underscore only.")

    remote_root = (deployer.REMOTE_ROOT if subdir is None
                   else deployer.REMOTE_ROOT / subdir)
    local = deployer.deployable(render_out)
    # Checked against the card AS THE PLUGIN WROTE IT: the rewrite below adds a
    # directory to every reference, which would stop any of them matching a
    # local file name and turn this check into one that always fails.
    as_written = deployer.card_path(render_out).read_text(encoding="utf-8")
    card = deployer.card_for_subdir(as_written, subdir)

    missing = deployer.check_card_matches_images(as_written, local)
    if missing:
        raise SystemExit(
            f"The card refers to images that are not in {local}: {missing}\n"
            "They are written by one render and must ship together -- the card's "
            "?version= hashes are derived from the image contents. Re-run "
            "`lidar2ha render`.")

    settings = _project_settings(project)
    ha.load_dotenv(env)

    # Connect even for a dry run. Listing a directory is not a write, and a
    # manifest that cannot see the target can only say "everything is new",
    # which is the one thing you already knew. Failing to connect is reported
    # and not fatal: the local side of the manifest is still worth seeing.
    transport = None
    try:
        transport = deployer.SFTPTransport(**deployer.credentials(settings))
    except SystemExit:
        if push:
            raise
        click.echo("  Not connected, so this lists what exists locally rather than "
                   "what would change. Set HA_SSH_HOST to compare against the target.")
    except Exception as exc:                       # noqa: BLE001 - reported, not swallowed
        if push:
            raise SystemExit(f"could not connect: {exc}") from exc
        click.echo(f"  Could not reach the target ({exc}); listing local files only.")

    try:
        remote = transport.listdir(remote_root) if transport else []
        manifest = deployer.plan(local, remote)
        if push_all:
            manifest.changed += manifest.unchanged
            manifest.unchanged = []
        deployer.print_manifest(manifest, remote_root, pushing=push)

        if transport and not manifest.empty:
            transport.makedirs(remote_root)
            for path, _size in manifest.to_push:
                transport.put(path, remote_root / path.name)
                click.echo(f"    pushed {path.name}")
            click.echo("\n  Done. The card below points at these files.")
    finally:
        if transport:
            transport.close()

    if show_card or not push:
        click.echo("\n--- floorplan.yaml -- paste this as a picture-elements card ---")
        click.echo(card)


# --------------------------------------------------------------------------- #
# not built yet
# --------------------------------------------------------------------------- #


def _unbuilt(name: str, what: str, instead: str) -> None:
    @cli.command(name=name, short_help=f"(not implemented) {what}")
    def _command() -> None:
        raise SystemExit(
            f"`lidar2ha {name}` is not implemented yet -- it would {what}.\n{instead}"
        )
    _command.__doc__ = f"Not implemented yet: {what}."


_unbuilt("add-capture", "unpack a Polycam floorplan zip and mesh into the project",
         "For now, unzip them yourself and run `python -m lidar2ha.polycam` on the DXF.")


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
