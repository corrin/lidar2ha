"""`project.yaml`, as types rather than as a convention.

The project file is the whole of what a person contributes; every other stage is
mechanical. It used to be read with a plain `.get` at six separate call sites, so
a misspelled key did nothing and said nothing. On my own house six top-level keys
were consumed by nothing at all, one of them holding ten areas of light pairings
under `light_pairing:` where the tool reads `lights.pairing:`. They had never once
been applied, and nothing had ever said so.

So this module is that shape, the way `schema.py` is the shape of `model.json`,
and for the same reason: `extra="forbid"` turns a typo into an error at the file
that holds it rather than into a declaration that quietly never runs.

WHERE THE VALUES ARE PARSED ELSEWHERE, THEY STAY THERE. `levels:` has a strict
parser in `projectlevels`, `split:` has one in `seams`, and `lights:` values are
built by `lights.LightsConfig.from_section`. Those own their contents and keep
owning them; this module types their keys and leaves the values alone. Two
parsers for one section is how they drift apart.
"""

from __future__ import annotations

import difflib
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class _Strict(BaseModel):
    # The point of the module. A key this does not name is a key nothing reads.
    model_config = ConfigDict(extra="forbid")


class Capture(_Strict):
    """One Polycam export.

    `multi_floor` says the walk covered more than one storey, which `combine`
    uses to tell you to pass --storey rather than refusing the capture later
    with nothing saying why.
    """

    multi_floor: bool = False


class Lights(_Strict):
    exclude: list[str] = Field(default_factory=list)
    include: list[str] = Field(default_factory=list)
    # entity_id -> additional area ids to place it in as well.
    extra: dict[str, list[str]] = Field(default_factory=dict)
    power: dict[str, float] = Field(default_factory=dict)
    default_power: float = 0.5
    # area -> entity_id -> plan-cm points. Shaped, not validated: `lights` owns
    # what a badly-formed point means and reports it against the fittings found.
    pairing: dict[str, dict[str, Any]] = Field(default_factory=dict)


class Camera(_Strict):
    yaw: float = 180
    pitch: float = 50


class Render(_Strict):
    width: int = 1920
    height: int = 1080
    quality: Literal["LOW", "MEDIUM", "HIGH", "BEST"] = "HIGH"
    renderer: Literal["SUNFLOW", "YAFARAY"] = "SUNFLOW"
    # CSS is n+1 frames, OVERLAY is 2^(lights in a room), FULL is 2^n. On one
    # 21-light house that is 22 frames, 65,541, and 2,097,152.
    mixing: Literal["CSS", "OVERLAY", "FULL"] = "CSS"


class Deploy(_Strict):
    host: str | None = None
    user: str = "root"
    port: int = 22
    key: str | None = None


class Project(_Strict):
    """Everything `project.yaml` may contain."""

    name: str | None = None
    captures: dict[str, Capture] = Field(default_factory=dict)

    # Parsed strictly by `projectlevels.parse_entries`, which refuses an unknown
    # key inside an entry and knows what a storey list means. Typed loosely here
    # so there is exactly one parser for it.
    levels: dict[str, Any] = Field(default_factory=dict)

    # capture id -> scanner room name -> area id. None is a real value and means
    # "ask me later", so it is not an error.
    rooms: dict[str, dict[str, str | None]] = Field(default_factory=dict)
    merge: dict[str, list[list[str]]] = Field(default_factory=dict)

    # Parsed by `seams`, which owns what a seam, a box and an outline are.
    split: dict[str, Any] = Field(default_factory=dict)

    lights: Lights = Field(default_factory=Lights)
    camera: Camera = Field(default_factory=Camera)
    render: Render = Field(default_factory=Render)
    deploy: Deploy = Field(default_factory=Deploy)

    ha_url: str | None = None
    ha_token: str | None = None

    # Written by `init` before those settings moved to flags, and read by
    # nothing. Accepted so an existing project still loads; the template says
    # where each one really lives now.
    elevations: Any = None
    tile_cm: Any = None
    sweethome3d_jar: Any = None


def _did_you_mean(key: str, candidates: list[str]) -> str:
    near = difflib.get_close_matches(key, candidates, n=1, cutoff=0.6)
    return f", did you mean `{near[0]}`?" if near else ""


def _explain(error: ValidationError, path: Path) -> str:
    """Name the offending key and where it sits, the way `levels:` already does.

    A ValidationError renders as a paragraph about a Python class, which tells
    the person holding a YAML file nothing they can act on.
    """
    lines = [f"{path}:"]
    for err in error.errors():
        loc = [str(part) for part in err["loc"]]
        if err["type"] == "extra_forbidden":
            where = ".".join(loc[:-1])
            model: type[BaseModel] = Project
            for part in loc[:-1]:
                field = model.model_fields.get(part)
                ann = getattr(field, "annotation", None) if field else None
                model = ann if isinstance(ann, type) and issubclass(
                    ann, BaseModel) else model
            known = sorted(model.model_fields)
            lines.append(
                f"  unknown key `{loc[-1]}`"
                + (f" under `{where}`" if where else " at the top level")
                + _did_you_mean(loc[-1], known))
        else:
            lines.append(f"  {'.'.join(loc)}: {err['msg']}")
    if any(e["type"] == "extra_forbidden" for e in error.errors()):
        lines.append("")
        lines.append("A key nothing reads does nothing and says nothing, which "
                     "is why this is an error.")
        lines.append("If it is a note to yourself, make it a `#` comment.")
    return "\n".join(lines)


def load(path: str | Path) -> Project:
    """Read and check `project.yaml`, or raise saying which key is wrong.

    Every stage goes through here. Six separate `yaml.safe_load` calls were what
    let a typo survive: whichever stage you happened to run simply did not look
    at that section.
    """
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"No project file at {p}")
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise SystemExit(f"{p}: expected a mapping of settings, got {type(raw).__name__}")
    try:
        return Project.model_validate(raw)
    except ValidationError as exc:
        raise SystemExit(_explain(exc, p)) from exc


def load_optional(path: str | Path | None) -> Project:
    """The same, for stages where `--project` is optional.

    A missing file is an empty project. A present but wrong one still raises:
    the whole point is that a file you bothered to write gets read.
    """
    return load(path) if path else Project()


def settings(path: str | Path | None) -> dict:
    """The file as a plain dict, checked first.

    Stages that still index into the mapping call this, so they get the check
    without changing how they read it. `exclude_unset` keeps the dict the shape
    the file was written in, rather than materialising every default and turning
    an absent `deploy:` into a present one.
    """
    if not path:
        return {}
    return load(path).model_dump(exclude_unset=True, exclude_none=False)
