#!/usr/bin/env python3
"""Read the Home Assistant registries, and work out what each light really is.

Home Assistant is the source of room identity and of the entity ids the
floor-plan plugin matches on, so this is where the model stops being a building
and starts being a house someone lives in.

AN ENTITY'S AREA IS NOT ITS DEVICE'S AREA, and following the device is actively
wrong for lighting. A multi-gang switch is one device on one wall driving bulbs
in several rooms; the per-entity area override is exactly how a user says so.
Resolution order is therefore the entity's own area first, the device's only as
a fallback. Backwards, and a third of such a device's lights land in the room
the switch is on rather than the room they light.

NOT EVERY `light.*` ENTITY IS A LIGHT. Every installation has some: status LEDs,
indicator rings on voice assistants, integrations exposing non-illumination
controls in the light domain. Nothing here drops them silently -- a real fitting
that happens to look like an indicator would vanish without trace, and the
render would be dark in one room with no explanation. They are proposed and
classified, and the human excludes what is wrong in project.yaml, where the
decision is recorded and survives a re-run.

GROUPS ARE FOUND BY THREE MECHANISMS, and the report always says which one
found what, because their coverage is not the same. A group and its members are
the same bulbs counted twice, and the raytracer will duly light that room twice
as brightly -- there is no error, just a room that comes out wrong.

  exact   Home Assistant's own light-group helper lists its members in an
          `entity_id` state attribute. Both sides are known, so a group is
          skipped only when its members are genuinely present.
  device  ZHA hangs a Zigbee group's light entity off the COORDINATOR device --
          the radio, not a lamp. Nothing else in the light domain lives there,
          because a real bulb is its own device. So the device is a mechanical
          test where the name is a guess: on this house it finds four groups
          and the name heuristic finds one of them.
  name    Hue rooms, deCONZ groups and anything else integration-native expose
          neither a member list nor a distinguishing device, and are simply
          indistinguishable from an ordinary light from outside their
          integration. Those are flagged for review and excluded by hand.

Nothing found any of these ways disappears quietly, and `lights.include` in
project.yaml puts any of them back.

The registry is fetched once and cached. The review loop is meant to be run
repeatedly while you fix names and areas, and it should not need the network or
a token to do that.

Usage:
    python -m lidar2ha.ha --refresh -o registry.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

# Anything in the light domain whose name or device class says it reports
# status rather than illuminating a room. These are hints for the human, never
# grounds for dropping an entity.
INDICATOR_WORDS = (
    "led ring", "status led", "router led", "indicator", "status light",
    "led strip status", "night light indicator", "backlight", "nightlight led",
    # RGB controllers routinely expose non-illumination channels in the light
    # domain -- an effect's sound is not a lamp, however the integration files it.
    "effect sound", "sound", "buzzer", "siren",
)
# An integration-native group (ZHA, Hue room, deCONZ) looks exactly like an
# ordinary light from outside its integration, so the name is the only hint
# there is that placing it may double-count bulbs placed individually.
GROUP_WORDS = ("group", "all lights", "all")
INDICATOR_SUFFIXES = ("_led", "_status_led", "_led_ring", "_indicator", "_status")
# A Zigbee group's light entity is attached to the coordinator device. Nothing
# else in the light domain is, so this is a mechanical test rather than a hint.
COORDINATOR_WORD = "coordinator"


@dataclass
class LightEntity:
    """One `light.*` entity, with everything needed to place or refuse it."""

    entity_id: str
    name: str
    area: str | None
    area_from: str = "none"          # "entity" | "device" | "none"
    device_id: str | None = None
    # The device is not just where the entity lives, it is evidence about what
    # the entity IS -- see coordinator_groups.
    device_model: str | None = None
    device_name: str | None = None
    disabled: bool = False
    hidden: bool = False
    # A light group names its members in its state attributes. Placing the
    # group AND its members double-counts the same bulbs.
    members: list[str] = field(default_factory=list)

    @property
    def is_group(self) -> bool:
        return bool(self.members)


def resolve_area(entity: dict, devices: dict[str, dict]) -> tuple[str | None, str]:
    """The entity's own area, else its device's. Returns (area_id, source).

    The order is the whole point -- see the module docstring.
    """
    if entity.get("area_id"):
        return entity["area_id"], "entity"
    device = devices.get(entity.get("device_id") or "")
    if device and device.get("area_id"):
        return device["area_id"], "device"
    return None, "none"


def light_entities(registry: dict) -> list[LightEntity]:
    """Every light entity in the registry, annotated and ready to judge."""
    devices = {d["id"]: d for d in registry.get("devices", [])}
    states = {s["entity_id"]: s for s in registry.get("states", [])}

    out = []
    for entity in registry.get("entities", []):
        entity_id = entity.get("entity_id", "")
        if not entity_id.startswith("light."):
            continue

        area, source = resolve_area(entity, devices)
        device = devices.get(entity.get("device_id") or "") or {}
        state = states.get(entity_id, {})
        attributes = state.get("attributes", {}) or {}

        # A light group's members. Guarded because `entity_id` in attributes is
        # a list for groups and absent for everything else -- but a malformed
        # integration could put a bare string there.
        members = attributes.get("entity_id") or []
        if isinstance(members, str):
            members = [members]

        out.append(LightEntity(
            entity_id=entity_id,
            name=(entity.get("name") or entity.get("original_name")
                  or attributes.get("friendly_name") or entity_id),
            area=area,
            area_from=source,
            device_id=entity.get("device_id"),
            device_model=device.get("model"),
            # name_by_user first, for the same reason the entity's own area
            # beats its device's: it is the name a human chose deliberately.
            device_name=device.get("name_by_user") or device.get("name"),
            disabled=bool(entity.get("disabled_by")),
            hidden=bool(entity.get("hidden_by")),
            members=[m for m in members if isinstance(m, str)],
        ))
    return sorted(out, key=lambda e: e.entity_id)


def _words(text: str) -> str:
    """Lowercase, with every separator flattened to a single space.

    So `light.den_router_led` and "Den - Router LED" both reduce to a form
    where "router led" is two whole words.
    """
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _mentions(text: str, phrase: str) -> bool:
    """Whole-word phrase match.

    Bare substring matching is not good enough, and the failure is not
    hypothetical: "wall lights" contains "all lights", so an ordinary pair of
    wall lights got reported as a light group. Since `_words` has already
    reduced the text to space-separated tokens, padding both sides is an exact
    whole-phrase test -- and unlike a regex it has nothing to escape wrong.
    """
    return f" {phrase} " in f" {text} "


def classify(entity: LightEntity) -> tuple[str, str]:
    """("fitting" | "check", reason) -- a hint for the human, not a filter.

    Only names are consulted, because that is the only signal that travels
    across integrations. It is deliberately weak: the cost of a wrong "check" is
    that someone reads a line in a report, and the cost of a wrong "fitting" is
    one extra entry in project.yaml. Neither is the cost of a silent drop.

    The name and the entity_id are searched SEPARATELY. Concatenating them
    invents phrases that exist in neither -- "...effect status" followed by
    "light.den..." reads as "status light" across the join, and a perfectly
    ordinary entity gets flagged for a reason its owner cannot see.
    """
    fields = (_words(entity.name), _words(entity.entity_id))
    for word in INDICATOR_WORDS:
        if any(_mentions(field, word) for field in fields):
            return "check", f"name says {word!r} -- indicator rather than a fitting?"
    for suffix in INDICATOR_SUFFIXES:
        if entity.entity_id.endswith(suffix):
            return "check", f"entity_id ends {suffix!r} -- indicator rather than a fitting?"
    for word in GROUP_WORDS:
        if any(_mentions(field, word) for field in fields):
            return "check", f"name says {word!r} -- a group whose members are also placed?"
    return "fitting", ""


def redundant_groups(entities: list[LightEntity]) -> dict[str, list[str]]:
    """Groups whose members are themselves present, mapped to those members.

    Placing both is the same bulbs twice, and the raytracer cannot tell.
    A group whose members are NOT present is a real placement -- it may be the
    only handle on those bulbs -- so it is left alone.

    This finds Home Assistant's own light-group helper only. See the module
    docstring: integration-native groups carry no member list and are caught by
    review, not by this.
    """
    present = {e.entity_id for e in entities}
    out = {}
    for entity in entities:
        if not entity.is_group:
            continue
        covered = [m for m in entity.members if m in present]
        if covered:
            out[entity.entity_id] = sorted(covered)
    return out


def coordinator_groups(entities: list[LightEntity]) -> dict[str, str]:
    """Integration-native groups identified by the DEVICE they hang off.

    ZHA gives a Zigbee group its own `light.*` entity and attaches it to the
    coordinator -- the radio itself. Nothing else in the light domain lives on
    that device, because a real bulb is its own device. So "this light's device
    is the coordinator" is a mechanical test for a group, where the name is a
    guess: on the house this was built for it finds four, and the name
    heuristic finds one of the same four.

    Unlike `redundant_groups` this CANNOT confirm the members are present,
    because a ZHA group publishes no member list. It relies instead on ZHA
    exposing every member device individually, which it does; `lights.include`
    is the escape hatch for an installation where that is somehow untrue.

    Returns entity_id -> the reason, so the report can name the mechanism that
    found it rather than lumping every group together.
    """
    out = {}
    for entity in entities:
        for text in (entity.device_model, entity.device_name):
            if text and _mentions(_words(text), COORDINATOR_WORD):
                out[entity.entity_id] = (
                    f"ZHA group -- its entity hangs off the coordinator device "
                    f"({text.strip()!r}), and its members are placed instead")
                break
    return out


# --------------------------------------------------------------------------- #
# fetching and caching
# --------------------------------------------------------------------------- #

WS_COMMANDS = {
    "areas": "config/area_registry/list",
    "devices": "config/device_registry/list",
    "entities": "config/entity_registry/list",
    "floors": "config/floor_registry/list",
}


def websocket_url(url: str) -> str:
    """http(s)://host -> ws(s)://host/api/websocket."""
    base = url.rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://"):]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://"):]
    if not base.endswith("/api/websocket"):
        base += "/api/websocket"
    return base


def fetch_registry(url: str, token: str) -> dict:
    """One WebSocket session; returns the raw registries plus states.

    `websockets` is imported here rather than at module scope because it is an
    optional extra (`pip install lidar2ha[ha]`), and `doctor` has to keep working
    on an install that never intends to talk to Home Assistant.
    """
    import asyncio

    try:
        from websockets.asyncio.client import connect
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise SystemExit(
            "The `websockets` package is required to read the Home Assistant "
            "registry. Install it with:  pip install 'lidar2ha[ha]'"
        ) from exc

    async def run() -> dict:
        async with connect(websocket_url(url), max_size=None) as ws:
            await ws.recv()  # auth_required
            await ws.send(json.dumps({"type": "auth", "access_token": token}))
            reply = json.loads(await ws.recv())
            if reply.get("type") != "auth_ok":
                raise SystemExit(
                    "Home Assistant rejected the token. Create a long-lived "
                    "access token under your profile and set HA_TOKEN."
                )

            registry: dict = {}
            message_id = 0
            for key, command in {**WS_COMMANDS, "states": "get_states"}.items():
                message_id += 1
                await ws.send(json.dumps({"id": message_id, "type": command}))
                while True:
                    message = json.loads(await ws.recv())
                    if message.get("id") == message_id and message.get("type") == "result":
                        if not message.get("success"):
                            raise SystemExit(f"{command} failed: {message.get('error')}")
                        registry[key] = message.get("result") or []
                        break
            return registry

    return asyncio.run(run())


def save_registry(registry: dict, path: str | Path) -> None:
    """Cache the registry. The token is never part of this."""
    Path(path).write_text(json.dumps(registry, indent=2, sort_keys=True), encoding="utf-8")


def load_registry(path: str | Path) -> dict:
    p = Path(path)
    if not p.exists():
        raise SystemExit(
            f"No cached registry at {p}. Run with --refresh to fetch one "
            f"(needs HA_URL and HA_TOKEN)."
        )
    return json.loads(p.read_text(encoding="utf-8"))


def credentials(project: dict | None = None) -> tuple[str, str]:
    """(url, token) from the environment, falling back to project.yaml.

    Read from a .env if one is beside the project; .gitignore already excludes
    it, which is the point -- a long-lived access token is a house key.
    """
    project = project or {}
    url = os.environ.get("HA_URL") or project.get("ha_url")
    token = os.environ.get("HA_TOKEN") or project.get("ha_token")
    if not url or not token:
        raise SystemExit(
            "Set HA_URL and HA_TOKEN (in the environment or a .env beside your "
            "project), or ha_url/ha_token in project.yaml.\n"
            "The token is a long-lived access token from your Home Assistant "
            "profile page. It is never written to the registry cache."
        )
    return url, token


def load_dotenv(path: str | Path) -> None:
    """Minimal KEY=VALUE reader. Existing environment variables win."""
    p = Path(path)
    if not p.exists():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def duplicate_names(entities: list[LightEntity]) -> dict[str, list[str]]:
    """Friendly names shared by several entities.

    Harmless to the plugin, which matches on entity_id -- but usually it means
    one physical fitting is exposed several ways, and placing all of them lights
    that spot two or three times over.
    """
    by_name: dict[str, list[str]] = {}
    for entity in entities:
        by_name.setdefault(entity.name.strip().lower(), []).append(entity.entity_id)
    return {n: sorted(ids) for n, ids in by_name.items() if len(ids) > 1}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-o", "--out", default="registry.json")
    ap.add_argument("--refresh", action="store_true",
                    help="fetch from Home Assistant instead of reading the cache")
    ap.add_argument("--env", default=".env", help="file to read HA_URL/HA_TOKEN from")
    args = ap.parse_args()

    if args.refresh:
        load_dotenv(args.env)
        url, token = credentials()
        registry = fetch_registry(url, token)
        save_registry(registry, args.out)
        print(f"wrote {args.out}")
    else:
        registry = load_registry(args.out)

    entities = light_entities(registry)
    groups = redundant_groups(entities)
    coordinated = coordinator_groups(entities)

    print(f"  areas    : {len(registry.get('areas', []))}")
    print(f"  devices  : {len(registry.get('devices', []))}")
    print(f"  lights   : {len(entities)}")
    print()
    for entity in entities:
        kind, reason = classify(entity)
        flags = []
        if entity.disabled:
            flags.append("disabled")
        if entity.hidden:
            flags.append("hidden")
        if entity.entity_id in groups:
            flags.append(f"group of {len(groups[entity.entity_id])}")
        if entity.entity_id in coordinated:
            flags.append("ZHA group (on the coordinator)")
        # Both, not one or the other. A ZHA group that ALSO looks like an
        # indicator by name is two separate things worth knowing, and the old
        # `reason or flags` hid whichever came second.
        note = "; ".join(part for part in (", ".join(flags), reason) if part)
        print(f"  {kind:<8} {entity.entity_id:<52} {str(entity.area):<22} "
              f"[{entity.area_from}] {note}")


if __name__ == "__main__":
    main()
