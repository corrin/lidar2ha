#!/usr/bin/env python3
"""Turn the named OBJ into a GLB without losing the names.

WHY THE NAMES ARE THE WHOLE POINT. `ObjExport.java` exists for one reason: to
write each object's OBJ group as the Home Assistant entity id it represents.
A 3D floor-plan card binds entities to objects BY NAME, so the group name is
the entire mapping. Geometry that arrives with the names gone is not a
degraded model, it is a model that does nothing.

WHY NOT trimesh, WHICH IS ALREADY A DEPENDENCY. Because it destroys exactly
that. trimesh keys a loaded scene by MATERIAL, so six lights that happen to
share the `white` material come back as one node called `white`. On a real
export not a single entity id survived, the file was valid, and nothing said a
word. `obj2gltf` preserves group names, so the conversion is a subprocess and
not a library call -- an unusual dependency justified by a specific failure.

WHY THE CHECK BELOW IS NOT OPTIONAL. The failure mode is a perfectly valid,
perfectly openable GLB with no entity names in it, which is indistinguishable
from a good one until you wire it to Home Assistant and nothing lights up. So
the names are counted going in and coming out, every time, and a run that loses
all of them is an error rather than a file.

The GLB parsing here is deliberately stdlib-only: a container this simple does
not warrant a dependency, and adding one that also keys by material would
reintroduce the bug this module exists to catch.
"""

from __future__ import annotations

import json
import re
import shutil
import struct
import subprocess
from pathlib import Path

from .javabridge import ToolchainError

# `light.kitchen_ceiling` and friends: a Home Assistant entity id is a domain,
# a dot, and an object id, all lowercase with underscores. Structural names
# ObjExport writes for scenery -- wall_0, room_Kitchen -- deliberately do not
# match, so the count below is entities and not "names in general".
ENTITY_ID = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")

GLB_MAGIC = b"glTF"
JSON_CHUNK = 0x4E4F534A


def find_obj2gltf() -> list[str]:
    """The command that runs obj2gltf, as an argv prefix.

    Prefers an installed executable, falls back to `npx`, which fetches it on
    demand. Raises with the install line rather than a FileNotFoundError from
    somewhere deeper.
    """
    found = shutil.which("obj2gltf")
    if found:
        return [found]
    npx = shutil.which("npx")
    if npx:
        return [npx, "--yes", "obj2gltf"]
    raise ToolchainError(
        "obj2gltf is not installed, and it is what preserves the object names.\n"
        "  npm install -g obj2gltf        (needs Node.js from https://nodejs.org/)\n"
        "Converting with trimesh instead would produce a valid GLB with every "
        "entity id replaced by a material name -- see lidar2ha.glb."
    )


def obj_group_names(path: str | Path) -> list[str]:
    """Every `g` line in an OBJ, in file order and including duplicates.

    Duplicates are kept because one entity legitimately spans several groups,
    and silently deduplicating here would make the before/after counts below
    disagree for a reason that has nothing to do with the conversion.
    """
    names = []
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("g ") or line.startswith("o "):
            names.append(line[2:].strip())
    return names


def glb_node_names(path: str | Path) -> list[str]:
    """Node and mesh names from a binary glTF.

    Both, because a converter may hang the name on either -- obj2gltf names the
    mesh, some name the node, and asking for only one is how a file that kept
    its names gets reported as having lost them.
    """
    data = Path(path).read_bytes()
    if len(data) < 20 or data[:4] != GLB_MAGIC:
        raise SystemExit(f"{path} is not a binary glTF (no {GLB_MAGIC.decode()} magic)")
    _magic, _version, total = struct.unpack_from("<4sII", data, 0)
    if total != len(data):
        # Not fatal to reading the JSON, but it means the file is truncated,
        # and a truncated file with a readable header is exactly the kind of
        # thing that gets shipped and then fails in a browser.
        print(f"  warning: {path} declares {total} bytes but is {len(data)}")

    offset = 12
    while offset + 8 <= len(data):
        length, kind = struct.unpack_from("<II", data, offset)
        body = data[offset + 8:offset + 8 + length]
        if kind == JSON_CHUNK:
            document = json.loads(body.decode("utf-8"))
            return [item["name"]
                    for key in ("nodes", "meshes")
                    for item in document.get(key, [])
                    if item.get("name")]
        offset += 8 + length + (-length % 4)
    raise SystemExit(f"{path} has no JSON chunk")


def entity_names(names: list[str]) -> set[str]:
    """Just the ones that look like Home Assistant entity ids."""
    return {name for name in names if ENTITY_ID.match(name)}


def check_names(obj: str | Path, glb: str | Path) -> tuple[set[str], set[str], dict[str, int]]:
    """(entity ids in the OBJ, the ones that survived EXACTLY, their split counts).

    Survival means the identical string appears in the GLB, not something like
    it. obj2gltf splits a group into one node per material and suffixes the
    copies -- `light.den_ceiling` becomes that plus `light.den_ceiling_1`,
    `_2`, and so on. Those suffixed siblings are valid geometry and completely
    useless for binding, because a card matches the entity id exactly; counting
    them as survivors would turn one bound lamp into a report of eight.

    The split counts are returned rather than discarded: an entity whose
    geometry is spread over a dozen nodes will light up only in part, and that
    is worth seeing before wondering why half a lamp responds.

    The caller decides what to do about a shortfall; this only measures it. The
    failure worth catching is the second set being empty while the first is not.
    """
    before = entity_names(obj_group_names(obj))
    names = glb_node_names(glb)
    survived = before & set(names)
    splits = {name: sum(1 for other in names if other.startswith(f"{name}_"))
              for name in survived}
    return before, survived, {k: v for k, v in splits.items() if v}


def convert(obj: str | Path, glb: str | Path) -> None:
    """Run obj2gltf. The .mtl and its textures must sit beside the .obj."""
    command = [*find_obj2gltf(), "-i", str(obj), "-o", str(glb)]
    proc = subprocess.run(command, capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit(
            f"obj2gltf failed ({' '.join(command)}):\n{proc.stdout}{proc.stderr}")
