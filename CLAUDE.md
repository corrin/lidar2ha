# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

This project is driven by **uv**. `uv.lock` and `.python-version` are part of the
repo — do not delete them as build artefacts.

```bash
uv run pytest -q                    # 263 tests, ~15 s
uv run pytest tests/test_ha.py -q
uv run pytest -q -k pole            # one test by name
uv run pytest -q -m java            # needs Sweet Home 3D + a JDK
uv run ruff check . --fix
uv run mypy                         # src/lidar2ha only, and it stays clean
uv run lidar2ha doctor              # the real build check -- see below
```

**mypy clean is not optional.** It passes on `src/lidar2ha` today; leave it that way
rather than adding an ignore. The numeric stack ships no stubs worth the noise, so
`ignore_missing_imports` is on and the boundary to trimesh/scipy/ezdxf is `Any` on
purpose — annotate that boundary explicitly (`tree: Any`) instead of reaching for
`# type: ignore`.

`doctor` is not a status page. It locates Sweet Home 3D, the floor-plan plugin and
the JDK, then **compiles the Java against the user's own installation**. Run it after
touching anything in `src/lidar2ha/java/`; `pytest` alone will not catch a Java
change that does not compile.

Tests marked `java` are the only ones that exercise code Sweet Home 3D's own
classes have to run. They are deselected by default and skipped where there is no
install.

## Working agreement

- **Check what the branch is FOR before the first commit.** Read its log and any
  open PR. If the work does not belong to that branch's purpose, start a new one.
  Being on a branch other than `main` is not permission — this rule replaces one
  that said "branch first if on `main`", which said nothing while seven commits of
  unrelated feature work went onto a branch named `adopt-uv` and into its open PR.
- **Commit at each logical milestone.** Do not accumulate a session's work into one
  uncommitted blob, and do not wait to be asked. One commit per self-contained
  change, with a message that says why.
- **Fail early.** Validate at the boundary and raise there. A stage that half-runs
  on bad input produces a plausible artefact that fails three stages later.
- **Handle the unhappy cases first.** Guard clauses at the top of a function, the
  happy path unindented at the bottom.
- **DRY, especially for maths.** Two hand-written copies of one transform is how a
  sign error survives: both look right, and a reflected plan is still a plan.
  `placefixtures.mesh_to_plan_cm` inverts `registration.transform` and is tested
  against that forward transform rather than against its own algebra. Do the same.
- **Comment the reasoning, not the code.** Every non-obvious block here explains the
  failure it prevents, usually with the real symptom that was observed. Match that
  register. `# increment the counter` is noise; `# The plugin sums sources sharing a
  name, so placing a group and its members renders the room quietly too bright` is
  the point.
- **A comment is permanent.** It explains the architecture, and what a decision
  forces on everything downstream of it — the consequence, which stays true. Not
  the deliberation that produced it: no alternatives weighed, no case argued, no
  reader addressed. One or two lines, then stop.
- **Config files get no prose.** `dependabot.yml`, `ci.yml`, `.coderabbit.yaml` are
  settings. Comment only what the key itself cannot say, such as a flag that looks
  removable but is not. Rationale belongs in the commit that made the change.
- **Trust the data model.** `schema.py` has already validated the document — pass
  `Model`, `Level`, `Room`, `Registration` around and read their attributes. Do not
  re-check what `extra="forbid"` and the field types guarantee, and do not fall back
  to `dict.get` chains on something that is already typed. Where a field is genuinely
  optional (`floor_z_m`, `elevation_cm`, `ha_area`), that `None` is information and
  needs an answer, not a default.
- **Test first.** Write the failing test that names the symptom, watch it fail, then
  fix it. Most bugs here are invisible at runtime, so a test that was never seen red
  is a test that may be asserting nothing.
- **Test against the real house.** The registry values, capture counts and thresholds
  in the tests come from actual scans and an actual Home Assistant instance — the
  ZHA coordinator's real model string, the four real group entities, 38 candidates
  for 12-14 fittings. Invented data agrees with whatever the code happens to do. Ask
  for the real numbers, or read them off the live system, before making them up.

## The rule this codebase is built around

**Nothing is dropped in silence.** Almost every failure here is invisible: a light in
the wrong room still renders, an entity with no area simply never appears, a fitting
detected in a window looks exactly like one detected in a lamp. So every refusal is
counted and named in a report, and every guess says it is one.

When adding a filter, a threshold or a skip, the question is not "is this right" but
"if this is wrong, what does the user see?" If the answer is "nothing", the code is
wrong however good the heuristic.

Corollary: prefer three answers to two. Where the evidence can be absent rather than
positive or negative, "not known" is its own answer and must not be folded into
either of the others.

## Architecture

### The spine is `model.json`, and `schema.py` *is* its specification

Every stage between the DXF and the scene file reads and writes one JSON document
typed by `src/lidar2ha/schema.py`. There is no separate spec to drift from it.

- `extra="forbid"` — a misspelled key is an error at the stage that wrote it, not a
  missing wall three stages later.
- On-disk geometry keys are camelCase (`xStart`) because that is Sweet Home 3D's own
  naming and what already-written files contain; Python says `x_start`. Keep the
  aliases.
- New fields must be **optional with a default**, or every capture already on disk
  stops loading.

### Units and frames

Plan geometry is **centimetres** (Sweet Home 3D's internal unit). Mesh and
registration are **metres**. Field names carry the unit wherever it is not obvious
(`floor_z_m`, `plan_x_cm`, `elevation_cm`).

**Each capture has its own coordinate frame and geometry never merges across them.**
A `Registration` relates one capture's plan to its own mesh. Getting from one
capture to another is a plan-to-plan fit, and it is the hop that fails — read its
coverage before believing anything downstream of it.

### Two JVMs, and `javabridge.py` is the only place that knows

- Writing a `.sh3d` needs only the model classes: an ordinary 64-bit **JDK** (not a
  JRE — Sweet Home 3D bundles a runtime with no compiler). `run_writer`.
- **Anything touching Java3D** needs Sweet Home 3D's bundled 32-bit runtime, because
  Java3D and YafaRay are 32-bit natives. It ships only `javaw.exe`, so those programs
  log to a file rather than stdout. `run_render`.

Do not inline a `java` invocation at a call site.

Java sources compile on first use into a per-user cache keyed by a hash over the
sources *and* the jars, so a Sweet Home 3D upgrade rebuilds automatically. Everything
targets Java 8 (`--release 8`), because the render JVM refuses class files above 52.

### You cannot write a `.sh3d` without going through Sweet Home 3D

A `.sh3d` is a ZIP containing one entry, `Home`, holding **Java-serialised objects**.
The documented `Home.xml` is for other tools to read; the desktop app will refuse an
archive built from it. Java serialisation of those classes can only be produced by
those classes — hence the Java in `src/lidar2ha/java/`, compiled against a local
install.

The README's "The thing that will waste your afternoon" section is the most reliable
document in the repo. Read it before changing anything that touches Sweet Home 3D or
the floor-plan plugin, and **add to it** when a new trap costs a day.

### The pipeline

Stages are argparse modules run as `python -m lidar2ha.<stage>`; the packaged
subcommands live in `cli.py` (click). Some things exist in both — `lights` is a
module *and* a subcommand — and both entry points must keep working.

```text
polycam        DXF/CSV      -> model.json
mesh           mesh.obj     -> floor elevations
registration   model+mesh   -> per-level Registration
textures_*     model+mesh   -> per-wall or tiled textures
rooms          model+yaml   -> scanner names replaced by HA area ids, open plan merged
seams                       -> split a room the scanner never split
fixtures       fixture mesh -> bright compact clusters, with crops
placefixtures  + geometry   -> those clusters in named rooms
contactsheet   + crops      -> the sheet a human approves
ha / lights    + registry   -> every light.* entity placed in its room
build (cli)    model.json   -> scene.tsv -> Sh3dWriter -> .sh3d -> Sh3dVerify
render (cli)   .sh3d        -> raytraced overlays + floorplan.yaml
deploy (cli)                -> sftp to /config/www/floorplan/
```

`add-capture` is a deliberate stub that exits saying so (`_unbuilt` in `cli.py`).
An honest `--help` matching the documented workflow beats a short one hiding gaps.

### The one thing that makes lighting subtle

The floor-plan plugin matches furniture **by `name == entity_id`** and **sums
multiple sources sharing a name**. Consequences that drive most of `lights.py`:

- One switch driving six bulbs is six placements carrying one entity_id. That is the
  correct representation, not a workaround.
- One entity spanning three floors is three placements.
- A group **and** its members placed together is the same bulbs twice, and the result
  is not an error — it is a room that renders too bright, forever, silently.
- The plugin only sees lights on the **selected** level, so every light is emitted
  against the lowest level with its own level's elevation added. Geometry stays put;
  only the lights move.

An entity's **own** area beats its device's. Resolving device-first files every
integration-native group wherever its coordinator is plugged in.

## Test style

One property per test. The docstring states **the failure the test catches**, not
what the code does — see `tests/test_placefixtures.py` and `tests/test_lights.py`.

Assert against the real symptom. Where a test would pass vacuously, say so in the
test: `assert not poly.contains(poly.centroid), "if this passes the test proves
nothing"`.

`tests/test_entrypoints.py` exists because every unit test passed while a stage's
`main()` crashed on every input. A new stage needs a smoke test there.

## Constants are guesses

Everything here that looks like a threshold is tuned to one house, one scanning app,
one capture. `CLUSTER_M`, `LOW_COVERAGE`, `DROP_CM`, `SPREAD_FRACTION` — expose them
as flags, say in the docstring what evidence would change them, and do not present
them as settled.
