"""The `lidar2ha` command.

Only some of this is built. Commands that do not work yet say so and exit
non-zero rather than failing somewhere deep with a traceback -- an honest
`--help` that matches the documented workflow is more use than a short one that
hides the gaps.

Output is plain text on purpose. `doctor` exists to be pasted into a bug report,
and it must not be able to fail because of its own presentation dependency.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

from . import __version__, javabridge
from .javabridge import ToolchainError

OK = "ok"
WARN = "warn"
FAIL = "FAIL"


def _row(status: str, label: str, detail: str = "") -> None:
    click.echo(f"  [{status:^4}] {label:<26} {detail}")


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
    click.echo()

    problems = 0
    warnings = 0

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

# Where to look from. The tool solves HOW FAR back to stand so the whole house
# fits; these are the choices it cannot make for you.
camera:
  yaw: 180        # degrees. Which side you view the plan from.
  pitch: 50       # degrees below horizontal. 90 is a flat plan view; at 50 you
                  # see wall faces, but near walls can hide rooms behind them.

# The render size, which also fixes the aspect ratio the camera is framed for.
render:
  width: 1920
  height: 1080
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
_unbuilt("render", "raytrace the model once per light state and emit floorplan.yaml",
         "For now, drive HeadlessRender directly -- see javabridge.run_render.")
_unbuilt("deploy", "copy the renders and floorplan.yaml to Home Assistant over SSH",
         "For now, copy them to /config/www/ yourself.")


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
