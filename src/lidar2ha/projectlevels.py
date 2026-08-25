#!/usr/bin/env python3
"""Read a `levels:` entry, which may name which storeys of a capture to take.

`combine` merges the captures of one level, and a capture holding more than one
level of its own has to say which. A single global `--storey` cannot: measured
on one real project, `--storey "Floor 1"` combines four single-storey captures
and DISCARDS the whole-house walk, while `--storey "Floor 1 (210cm)"` aborts
because the other four have no such level. There is no value of one flag that
admits both, so the fact belongs per capture, where every other per-capture
fact in `project.yaml` already lives.

ALWAYS A LIST, EVEN OF ONE. A capture can contribute several storeys to the
SAME level: Polycam laid one walk of an upstairs across two sheet clusters, and
after the ceiling-band split two of its levels both belong to that floor while
holding different rooms -- 10.6 m2 and 23.1 m2 of it. A scalar with a plural
special case would have made the second one the exception rather than the
shape.

    levels:
      "Ground Level":
        - ground_geometry_0823-2006          # a bare id still works
        - id: unknown_geometry_0825-1649
          storeys: ["Floor 1 (210cm)"]
      "Upstairs":
        - upstairs_geometry_0823-1058
        - id: unknown_geometry_0825-1649
          storeys: ["Floor 1 (710cm)", "Floor 3"]
"""

from __future__ import annotations

from dataclasses import dataclass

from .schema import Model


@dataclass(frozen=True)
class Wanted:
    """One capture, and which of its storeys this level wants."""

    capture_id: str
    # None means "whatever level this capture has", which is the ordinary case
    # and what a bare string in the list parses to.
    storeys: tuple[str, ...] | None = None


def parse_entries(entries) -> list[Wanted]:
    """The `levels: <name>:` list, as captures and the storeys wanted of them.

    Raises on a shape it does not recognise rather than skipping it. A level
    entry nobody can read is a capture that silently does not reach the union,
    and the whole point of this file is that such a capture was hard enough to
    notice already.
    """
    if entries is None:
        return []
    # THE LIST ITSELF, before its items. `"Ground": my_capture` -- the dash
    # forgotten -- iterates as one capture per character, and four one-letter
    # ids that match no file are a declaration that quietly did nothing. A
    # mapping iterates as its keys, which is worse: the ids look plausible.
    if isinstance(entries, (str, bytes)) or not isinstance(entries, (list, tuple)):
        raise ValueError(
            f"a `levels:` level takes a LIST of captures, got {entries!r}. "
            f"Write each capture on its own `- ` line")

    out: list[Wanted] = []
    for entry in entries:
        if isinstance(entry, str):
            out.append(Wanted(checked_id(entry)))
            continue
        if not isinstance(entry, dict):
            raise ValueError(
                f"a `levels:` entry must be a capture id or a mapping with `id:`, "
                f"got {entry!r}")

        capture_id = entry.get("id")
        if not capture_id:
            raise ValueError(
                f"a `levels:` mapping needs an `id:` naming the capture, got "
                f"{sorted(entry)}")
        if not isinstance(capture_id, str):
            # `id: 2006` reads as an int and would stringify to something that
            # matches no export, so the level would combine one capture short.
            raise ValueError(
                f"a `levels:` `id:` must be the capture id as a string, got "
                f"{capture_id!r}. Quote it")

        # NO KEY GOES UNREAD, the way `schema.py` forbids an extra field. The
        # singular `storey:` is the one a person writes first, and ignoring it
        # gave them the UNDECLARED path -- a declaration that did nothing, said
        # nothing, and left the capture out of the union it was written to put
        # it in.
        unknown = sorted(set(entry) - {"id", "storeys"})
        if unknown:
            hint = (" -- did you mean `storeys:`?"
                    if "storey" in unknown else "")
            raise ValueError(
                f"{capture_id!r} has {'keys' if len(unknown) > 1 else 'a key'} "
                f"this does not read: {unknown}{hint}. A `levels:` entry takes "
                f"`id:` and `storeys:`")

        raw = entry.get("storeys")
        storeys: tuple[str, ...] | None
        if raw is None:
            storeys = None
        elif isinstance(raw, str):
            # Tolerated because it is what a person writes first, and refusing
            # a clear intention over a bracket helps nobody.
            storeys = (raw,)
        elif not isinstance(raw, (list, tuple)):
            # A mapping iterated as its keys and read as storey names; a number
            # raised `TypeError` from inside a comprehension, which names no
            # capture and no key.
            raise ValueError(
                f"{capture_id!r} has a `storeys:` that is not a list of storey "
                f"names: {raw!r}")
        else:
            if not raw:
                raise ValueError(
                    f"{capture_id!r} has an empty `storeys:` list. Leave the key "
                    f"out to take the capture's only level, or name the storeys "
                    f"this level should take")
            bad = [s for s in raw if not isinstance(s, str) or not s.strip()]
            if bad:
                # `expand` matches these against `Level.name` exactly, so a
                # blank or a number matches nothing and refuses -- naming the
                # empty string, which tells the reader nothing about which of
                # their entries is wrong.
                raise ValueError(
                    f"{capture_id!r} has a `storeys:` entry that is not a "
                    f"storey name: {bad[0]!r}. Names come from the model json "
                    f"-- `lidar2ha whichlevel` prints them")
            storeys = tuple(raw)
        out.append(Wanted(checked_id(capture_id), storeys))
    return out


def checked_id(capture_id) -> str:
    """A capture id that cannot be confused with a storey-marked key.

    `entry_key` marks a storey with ` [name]` and `origin_of` reads it back to
    tell which entries came from one export. An id carrying the same marker
    splits in the wrong place, and two unrelated captures then look like one --
    silently, the only symptom being a consensus that counted wrong.

    Checked for BOTH shapes. The first version of this guard sat in the mapping
    branch alone, so a bare string walked past it, and the test only exercised
    the mapping.
    """
    text = str(capture_id)
    if " [" in text:
        raise ValueError(
            f"capture id {text!r} contains ' [', which is how a storey is "
            f"marked on a combined-model key. Rename the capture")
    return text


def entry_key(capture_id: str, storey: str | None) -> str:
    """How one (capture, storey) pair is named in the combined model.

    A capture contributing ONE storey keeps its plain id, so every project that
    never needed this combines to byte-identical output. Only a capture split
    across several entries gains the suffix, and then it needs one: two entries
    sharing a key would collide in the dict and one would vanish.
    """
    return capture_id if storey is None else f"{capture_id} [{storey}]"


def expand(wanted: Wanted, model: Model,
           fallback: str | None = None) -> list[tuple[str, Model]]:
    """(key, one-level model) for each entry this capture contributes.

    `fallback` is the global `--storey`, which stays because it is what makes
    one real project's ground floor work today: every single-storey capture
    there is called `Floor 1`, so one flag picks the two-storey capture's
    ground level and everybody else's only level at once. PROJECT.YAML WINS
    where both speak -- it is the recorded decision and the flag is the ad-hoc
    one.

    A named storey the capture does not have RAISES, naming the ones it does.
    That is exactly the message the global flag produces today, and it was
    never the problem; the missing part was a way to act on it.
    """
    have = [lv.name for lv in model.levels]

    if wanted.storeys is not None:
        missing = [s for s in wanted.storeys if s not in have]
        if missing:
            raise ValueError(
                f"{wanted.capture_id!r} has no level named {missing[0]!r}; it "
                f"has {have}. Storey names come from the model json, not from "
                f"project.yaml -- `lidar2ha whichlevel` prints them")
        return [(entry_key(wanted.capture_id, s), reduced(model, s))
                for s in wanted.storeys]

    if fallback is not None and fallback in have:
        return [(wanted.capture_id, reduced(model, fallback))]

    # UNDECLARED AND AMBIGUOUS IS NOT BAD INPUT, so it does not stop the level.
    # The model goes through whole and `combine` discards it by its own rule,
    # naming the levels it has -- which is what happens today, and the four
    # other captures of that floor still combine. Raising here would refuse to
    # build anything until an unrelated capture was sorted out, and the thing
    # that was missing was never the complaint: it was the remedy.
    #
    # A DECLARED storey the capture lacks still raises, above. That is a typo
    # in the declaration, and carrying on would place nothing while looking
    # like it had worked.
    return [(wanted.capture_id, model)]


def reduced(model: Model, storey: str) -> Model:
    """The model holding only that one level.

    Selected here rather than by passing a name into `combine`, because
    `combine` takes ONE name for every capture and the whole point is that they
    now differ.
    """
    level = next(lv for lv in model.levels if lv.name == storey)
    return model.model_copy(update={"levels": [level]})


def origin_of(key: str) -> str:
    """The capture a combined-model key came from.

    Defined here beside `entry_key`, which is the only thing that makes one, so
    the format is written and read in one file rather than parsed by whoever
    needs it.
    """
    head, sep, _ = key.partition(" [")
    return head if sep else key
