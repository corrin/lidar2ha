"""Check `project.yaml` before a stage acts on it.

`project.yaml` is the whole of what a person contributes; every other stage is
mechanical.

Spelling is not checked here. `projectschema` refuses an unknown key when the
file is loaded, before any stage acts on it, which is earlier than this command
and does not need anybody to remember to run it. What is left here is everything
a schema cannot see: a key can be spelt correctly, typed correctly, and still
name a capture that no level uses.

Every check here is a failure that actually happened, and every one of them
produced a plausible model rather than an error. The expensive one is the first:
a capture in a level with no `rooms:` entry keeps its scanner names, and an
unnamed room that wins a group takes the area name with it -- three rooms
vanished from this house that way, and `combine` says something only when NO
capture in the level maps anything.

Reports, never repairs. Which area a scanner room is depends on knowing the
house, and a tool that guessed would be inventing exactly the fact it cannot
have.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Finding:
    """One thing wrong with the project file, and what to do about it."""

    kind: str
    detail: str
    remedy: str = ""


def valid_area_ids(registry: dict | None) -> set[str] | None:
    """The area ids Home Assistant knows, or None if we cannot tell.

    None is the third answer and it matters: no cached registry means the
    question is unanswerable, which is not the same as every area being fine.
    """
    if not registry or "areas" not in registry:
        return None
    return {a["area_id"] for a in registry["areas"] if a.get("area_id")}


def _capture_ids(settings: dict) -> list[str]:
    """Every capture named by a `levels:` entry, in declaration order.

    Parsed the same way `combine` parses it, so a file this accepts is a file
    that stage can read. A malformed entry is left for `parse_entries` to refuse
    where it always did rather than being half-diagnosed here.
    """
    from .projectlevels import parse_entries

    out: list[str] = []
    for entries in (settings.get("levels") or {}).values():
        try:
            out.extend(w.capture_id for w in parse_entries(entries))
        except ValueError:
            continue
    return out


def check(settings: dict, registry: dict | None = None) -> list[Finding]:
    """Everything wrong with this project file that can be seen without a model.

    Ordered by what it costs you: rooms that will silently vanish first, then
    declarations that quietly do nothing.
    """
    found: list[Finding] = []
    rooms = settings.get("rooms") or {}
    used = _capture_ids(settings)
    declared = set(settings.get("captures") or {})
    areas = valid_area_ids(registry)

    # 1. A capture in a level with no mapping. This is the expensive one.
    for capture_id in used:
        if capture_id in rooms:
            continue
        found.append(Finding(
            "capture_not_named",
            f"{capture_id} is in `levels:` and has no `rooms:` entry",
            "Its rooms keep their scanner names. An unnamed room can win a "
            "group and take the area name with it, so a room you mapped on "
            "another capture disappears. Add a `rooms:` block for it -- "
            "`combine` prints what each unnamed room is standing on."))

    # 2. An area value that is really a capture id. Its own verdict because it
    #    is the specific way this goes wrong, and because the generic message
    #    would send the reader looking for a typo that is not there.
    known_captures = declared | set(rooms) | set(used)
    for capture_id, mapping in rooms.items():
        for scanner_name, area in (mapping or {}).items():
            if area and area in known_captures:
                found.append(Finding(
                    "area_is_a_capture_id",
                    f"{capture_id}: {scanner_name!r} -> {area}",
                    "That is a capture id in the area column. Any non-empty "
                    "string is adopted as an area, so the phantom counts as "
                    "mapped while the real area has no geometry."))

    # 3. An area no registry knows. Abstains without one rather than passing.
    if areas is not None:
        for capture_id, mapping in rooms.items():
            for scanner_name, area in (mapping or {}).items():
                # `null` means "ask me later" and is a deliberate state.
                if not area or area in areas or area in known_captures:
                    continue
                found.append(Finding(
                    "area_not_in_registry",
                    f"{area} ({capture_id}: {scanner_name!r})",
                    "No area of that id exists in Home Assistant. Check the "
                    "spelling against `python -m lidar2ha.ha --refresh`."))

    # 4. Declared and never used. Seven of nineteen captures here, each of them
    #    imported, registered and contributing nothing.
    for capture_id in sorted(declared - set(used)):
        found.append(Finding(
            "capture_unused",
            f"{capture_id} is in `captures:` and in no `levels:` entry",
            "`combine` never sees it, so whatever it holds is not in any "
            "model. Add it to a level, or accept that it is only a record."))

    return found


def report(found: list[Finding]) -> None:
    """Print the findings grouped by kind, worst first."""
    if not found:
        print("project.yaml: nothing to report.")
        return

    order: list[str] = []
    for finding in found:
        if finding.kind not in order:
            order.append(finding.kind)

    for kind in order:
        rows = [f for f in found if f.kind == kind]
        print(f"\n{kind.replace('_', ' ').upper()}  ({len(rows)})")
        for row in rows:
            print(f"  {row.detail}")
        if rows[0].remedy:
            for line in _wrap(rows[0].remedy):
                print(f"    {line}")


def _wrap(text: str, width: int = 72) -> list[str]:
    words, lines, line = text.split(), [], ""
    for word in words:
        if len(line) + len(word) + 1 > width:
            lines.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        lines.append(line)
    return lines
