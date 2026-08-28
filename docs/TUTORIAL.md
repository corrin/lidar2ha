# Your house, end to end

You have a dozen Polycam scans and you want a floorplan in Home Assistant that
lights up when you tap a light. This is the whole path.

It is long because the path is long. Every step has a **what you should see** and
a **what it looks like when it's wrong**, and the numbers in them were measured on
real captures rather than invented — the difference matters, because most failures
here look exactly like success.

**You can run Parts 1–10 without a house.** `lidar2ha demo` writes eight captures
of a building that does not exist, packaged the way Polycam packages yours. Every
command below works on it. If you are reading to decide whether this is worth your
weekend, do that first.

```bash
uv run lidar2ha demo ~/demo-house
cd ~/demo-house
```

---

## Part 0 — Getting your scans out of Polycam

### Two exports per capture, and only one right format for each

You have the scans. Before anything else you have to get them off Polycam, and
the export picker is where this pipeline is most often lost — because the wrong
format produces files that import cleanly, look fine, and are missing something
you will not notice for days.

Per capture, **two** exports. The picker is single-select, so you do them one at
a time:

| Menu | Choose | You get | Why that one |
|---|---|---|---|
| Floor Plan | **Zip (all)** | `.dxf` + `.csv` (+ pdf, svg, png) | the DXF is the plan; **the CSV is the ceiling heights** |
| Mesh | **OBJ** | `.obj` + `.mtl` + `textures/` | the atlas comes as real image files |

Export settings: **Metric / Meters**, point density **High**, **Mesh up axis: Z**.

Floor-plan export is a paid Polycam tier. There is no way around that: the DXF is
the only thing that carries room polygons.

### What the other formats cost you, measured

Both wrong choices are easy to make, neither errors, and both were made on the
house this was written from — three of its nineteen captures went out as
`Floor Plan → DXF` and `Mesh → GLB` instead. Here is what that cost.

**Floor Plan → DXF (without the CSV): every room gets a made-up ceiling.** The
ceiling heights live in the CSV, not the DXF. With no CSV, `polycam` falls back to
`--default-height`, which is 2.4 m. Those three captures against the rest:

```
ground_geometry_0823-1038   ceiling=520cm      <- Zip (all): real, varied heights
mid_geometry_0823-1020      ceiling=470cm
upstairs_geometry_0823-1058 ceiling=400cm

ground_geometry_0823-2006   ceiling=240cm      <- DXF only: every room, every capture
mid_geometry_0823-1810      ceiling=240cm
upstairs_geometry_0823-1904 ceiling=240cm
```

Every room in all three is exactly 240 cm. A double-height stairwell and a laundry
come out the same height, which is precisely the geometry that makes cross-floor
light spill worth raytracing.

`polycam` warns when it gets no heights, whether that is a `--csv` it could not
parse or no `--csv` at all:

```
WARNING: no --csv given, and the DXF does not carry ceiling heights.
         Falling back to 2.4 m for every room.
```

The warning scrolls past, though, and the model it writes is perfectly valid.
The durable signal is the one above: uniform 240 cm across every room.

**Mesh → GLB: no wall textures and no fitting detection.** A GLB is a valid mesh
and registers fine, so it looks like a working capture. But trimesh gives glTF a
`PBRMaterial`, whose atlas hangs off `.baseColorTexture` rather than `.image` —
and `.image` is what `fixtures` and `textures_project` select geometry on:

```
OBJ capture: 2 geoms, material=SimpleMaterial, .image=set,  usable = 2/2
GLB capture: 5 geoms, material=PBRMaterial,    .image=None, usable = 0/5
```

`textures_project` reports `coverage 0.0% -- skipped` for every wall and writes an
empty manifest with exit status 0, which reads as *"this scan didn't see any
walls"* when it means *"the loader could not find the atlas"*.

So: **Zip (all)** and **OBJ**. If you have already exported the other way, you do
not need to rescan — just re-export those captures from Polycam.

### Downloads arrive named by date, not by capture

Worth knowing before you click, because it decides how much sorting you do later:
every download is named for the **capture date**, so a day's scanning gives you
`23_08_2026.zip`, `23_08_2026 (1).zip`, `23_08_2026 (2).zip`, and the number is
your browser's counter — the order you clicked, not which capture is which.

The cheapest fix is free: **export one capture at a time and rename the two files
the moment they land**, before you export the next. Part 3 is what you do if you
did not.

### Before you scan (or rescan)

Nothing downstream repairs a bad capture, so if you are not finished — or the work
list later tells you to go back:

**Cover every mirror.** A scanner cannot tell a reflection from a room, so it
builds a phantom copy of the space behind the wall. One 2.2 m room produced a
5.55 m mesh in 882 disconnected pieces.

**LiDAR, Space mode — never Floorplan mode.** Floorplan mode produces no mesh, and
without a mesh there is no registration, no ceiling heights, no fitting positions
and no textures. You get a plan you cannot place.

**One continuous capture per wall-bounded volume**, not per Home Assistant area.
Open doors, turn lights on, and accept that glass is invisible to LiDAR — windows
will simply be missing and you add them by hand later.

**Scan every level at least three times.** This is the one that sounds like
over-caution and is not. Two captures that disagree cannot tell you which of them
is wrong; a third identifies the odd one out immediately. On the house this was
built for, the capture the entire mid-level model had been built from turned out
to be the worst of its three, and only the third scan revealed it.

**Then take a fixture pass per level.** A second, deliberately different capture:
every light switched on, phone aimed at each fitting in turn. Geometry quality is
sacrificed on purpose. What you are recording is where the lights physically
are — and, more importantly, *how high they hang*, which nothing else can tell you
and which the raytracer needs.

---

## Part 1 — Install, and prove the toolchain

```bash
git clone https://github.com/corrin/lidar2ha && cd lidar2ha
uv sync --all-extras
uv run lidar2ha doctor
```

`--all-extras` is not optional. `paramiko` and `websockets` are extras and a bare
`uv sync` removes them again, taking `deploy` and the Home Assistant registry with
them.

You also need [Sweet Home 3D](https://www.sweethome3d.com/), the
[floor-plan plugin][plugin], and a **JDK** 17+ ([Temurin](https://adoptium.net/)).

**What you should see:** every line `[ ok ]`, ending with

```
  compiling Java against your installation...
  [ ok ] java sources               ...\jclasses\f1e5d77d81003ece
Everything checks out.
```

`doctor` does not check paths and stop. It **compiles the Java against your own
Sweet Home 3D**, which is the only thing that catches a version mismatch — an
earlier version that only looked at paths passed happily while the sources would
not build.

**What it looks like when it's wrong:** `javac (JDK)` missing while `java` is
found means you have a JRE. Sweet Home 3D bundles a runtime with no compiler, so
"Java is installed" is not the same as "a JDK is installed".

---

## Part 2 — Make a project, and get your area ids first

```bash
uv run lidar2ha init ~/my-house
cd ~/my-house
```

That writes `project.yaml`, plus `captures/` and `build/`. Every section of the
template is empty, and commented with what it declares and an example of the
shape; the parts below fill them in in order.

**Now fetch your Home Assistant registry, before anything else.** This is out of
order compared to how the stages are numbered, and it has to be: Part 4 asks you
to write down which scanner room is which HA **area id**, and you cannot do that
until you know what your area ids are.

```bash
export HA_URL=http://homeassistant.local:8123
export HA_TOKEN=...        # Profile -> Long-lived access tokens
uv run python -m lidar2ha.ha --refresh -o registry.json
```

Put those two in a `.env` beside `project.yaml` instead if you prefer; it is
already gitignored, which `project.yaml` is not — `ha_url` and `ha_token` are
read from there too, but a long-lived access token is a house key. After this
one fetch everything works from the cached
`registry.json`, so the rest of the loop runs offline.

**What you should see:** a count of areas, floors, devices and light entities, and
a table classifying each `light.*`. Read it now — you will need it in Part 9.

> **On the demo:** skip this. `lidar2ha demo` writes a `registry.json` for you, so
> every command below runs with no Home Assistant at all.

---

## Part 3 — Staging your captures

This is the part no other document covers and the part that costs the most. It is
also where you can lose a capture without any error.

### Every file in every archive has the same name

Polycam names a download by **capture date**, not by capture. Fifteen captures
shot on one day give you this:

```
23_08_2026.zip        23_08_2026 (1).zip     23_08_2026 (2).zip   ...
```

and inside:

```
23_08_2026 (13).zip  ->  23_08_2026.csv  23_08_2026.dxf  23_08_2026.pdf ...
23_08_2026 (12).zip  ->  23_08_2026.mtl  23_08_2026.obj  textures/
```

**Unpack two captures into one directory and the second silently overwrites the
first.** No error. You find out much later, when a registration fits the wrong
mesh. The number in the filename is your browser's download counter — it records
the order you clicked, not which capture is which, and nothing pairs a plan
archive with its mesh archive.

So the archives are a puzzle before they are data. Tell them apart by size (a
floor-plan zip is well under 1 MB, a mesh zip is 7–19 MB), then open each plan
zip: the CSV lists the room names, which is usually enough to recognise the room
you walked. Single-floor exports also carry a `Compass direction [deg]` you can
use to group them.

> `lidar2ha add-capture` would do all of this in one command. It is not
> implemented, and it is the biggest ergonomic gap in the project.

### Give each capture a name and its own directory

Name it `<where>_<what>_<MMDD-HHMM>`:

- **where** — the smallest *true* scope: a level (`ground`, `mid`, `upstairs`), one
  room, an outdoor place, or `unknown`. Never guess; `unknown` is a real answer and
  Part 5 has a tool for resolving it.
- **what** — `geometry` or `fixtures`.
- **when** — the Polycam capture time, so the id is its own cross-reference back to
  the app, which labels captures by timestamp and nothing else.

Then one directory per capture, so identically-named files cannot collide:

```
my-house/
  project.yaml
  registry.json
  exports/
    ground_geometry_0823-1038/
      floorplan/   23_08_2026.csv  23_08_2026.dxf
      mesh_obj/    23_08_2026.obj  23_08_2026.mtl  textures/
    ground_geometry_0823-1144/
      ...
```

`combine` looks for a capture's model in `exports/<id>/`, then `captures/<id>/`,
then the project root. `exports/` is the convention this document uses.

### Two traps in this directory, both of which have cost real time

**A capture directory fills up with things that look like inputs.** After a few
runs it holds `walltex/`, `render/`, and — if you ever ran `export-glb --keep-obj`
— a `gltf/` containing an `.obj` that is lidar2ha's *output*. A glob for `*.obj`
finds it, because `gltf/` sorts before `mesh_obj/`. Registering a plan against the
tool's own OBJ export produced this:

```
mesh wall points : 1,913          every other capture: 25,000 - 162,000
mesh z range     : -3.33 .. 1353.87 m
median error     : inf cm   coverage=0%
```

The same capture against the right file is the best-registered in the house, at
**1.0 cm and 100% coverage**. Always name the mesh explicitly.

**A stale derived file wins over a corrected one.** `combine` prefers
`<id>_named.json`, then `<id>_registered.json`, then `<id>.json`. A `_named.json`
left over from an old mapping is taken in preference to everything else — and a
`_named.json` that named *nothing* looks identical to one that named everything.
They are regenerable build artefacts: if in doubt, delete them and re-run.

**What you should see:** one directory per capture, each with a `floorplan/` and a
`mesh_obj/`, and no capture directory containing a mesh you did not put there.

---

## Part 4 — Per capture: plan, register, name

Three commands per capture, and no batch form, so for fifteen captures this is
forty-five invocations. Write a loop.

```bash
ID=ground_geometry_0823-1038
D=exports/$ID

uv run python -m lidar2ha.polycam $D/floorplan/23_08_2026.dxf \
    --csv $D/floorplan/23_08_2026.csv -o $D/$ID.json

uv run python -m lidar2ha.registration $D/$ID.json \
    $D/mesh_obj/23_08_2026.obj -o $D/${ID}_registered.json

uv run python -m lidar2ha.rooms $D/${ID}_registered.json project.yaml \
    -o $D/${ID}_named.json --capture $ID
```

Add `--role fixtures` to `polycam` for a fixture pass. Its geometry is bad on
purpose, and marking it keeps its walls and floor heights out of the building
while keeping the fittings it found.

### `polycam` — the DXF becomes a model

**What you should see:** one line per level with wall, room and door counts. A
capture that walked more than one storey comes back as more than one level, named
for the ceiling band it sat at:

```
WARNING: Floor 1 holds 7 room(s) across 3 ceiling bands: 210cm x3, 480cm x1, 710cm x3
  Floor 1 (210cm) walls= 25 rooms=3 doors=5
  Floor 1 (480cm) walls= 11 rooms=1 doors=1
  Floor 1 (710cm) walls= 14 rooms=3 doors=3
  Floor 2         walls= 18 rooms=2 doors=4
  Floor 3         walls= 17 rooms=4 doors=9
```

That warning is not a problem — it is the whole-house walk being taken apart into
storeys, and Part 5 is where you say which storey belongs to which level.

**What it looks like when it's wrong:** every room reporting the same ceiling,
and that ceiling being 240 cm.

```
  Floor 1    walls= 22 rooms=3 doors=3 ceiling=240cm
      Bedroom         7 pts   ceiling 240cm
      Hallway        13 pts   ceiling 240cm
      Living Room    18 pts   ceiling 240cm
```

That is `--default-height`, and it means no CSV reached this command — either you
did not pass `--csv`, or the capture was exported as a bare DXF (Part 0). A
`WARNING` line above says so, but this is what it looks like once the warning has
scrolled away. Compare against a capture that has a CSV: real heights vary room to
room and run well past 240.

### `registration` — the plan and the mesh into one frame

This is the weak link in the pipeline and the number to actually read.

**What you should see:** median error of a few centimetres at 100% coverage.
Measured across one real house, the good captures ran **1.0 to 3.4 cm**.

```
  rotation      : 359.99 deg   mirror=False
  median error  : 1.0 cm   coverage=100%
  floor z       : -2.241 m
```

**What it looks like when it's wrong:**

```
  median error  : 18.4 cm   coverage=95%
  ** LOW COVERAGE: 89% of the plan found no wall within a metre.
```

Anything in double figures is a capture to re-shoot or discard. And treat that
`LOW COVERAGE` banner as the most useful line the tool prints: it is what catches
the wrong-mesh mistake above. Check the mesh wall-point count and z range on the
lines before it — a z range of hundreds of metres, or a point count two orders of
magnitude below your other captures, means you fed it the wrong file.

### `rooms` — scanner names become HA area ids

The scanner guesses room names, and guesses badly: one real capture confidently
labelled an entrance hall "Living Room" and "Dining Room", and returned an open
kitchen as "Kitchen" plus "Office 1". You supply the truth, per capture, in
`project.yaml`:

```yaml
rooms:
  ground_geometry_0823-1038:
    "Living Room": open_living
    "Office 1": kitchen          # many scanner rooms may share one area
    "Bedroom": null              # null means "ask me later", and is not an error

merge:
  ground_geometry_0823-1038:
    - ["Kitchen", "Office 1"]    # one volume this capture split in two
```

`merge:` is keyed **by capture** on purpose: a scanner's over-segmentation belongs
to the walk that made it, and another scan of the same room splits it somewhere
else or not at all.

**What it looks like when it's wrong:** `rooms` exits 1 with *"No rooms mapping
for capture X"*. Good — that is the failure you want. The bad case is the capture
you never noticed had no mapping, because `combine` will then fall back to its
`_registered.json` and quietly contribute scanner-named rooms to your union.

---

## Part 5 — Say which storeys belong to which level

A capture that walked the whole house holds several levels of its own, so which
level it belongs to is not a property of the capture. Declare it in
`project.yaml`, keyed by the names of your Home Assistant floors:

```yaml
levels:
  "Ground Floor":
    - ground_geometry_0823-1038          # a bare id, for a single-storey capture
    - ground_geometry_0823-1144
    - ground_fixtures_0823-1317
    - id: unknown_geometry_0825-1649     # the whole-house walk
      storeys: ["Floor 1 (210cm)"]
  "Upstairs":
    - upstairs_geometry_0823-1058
    - id: unknown_geometry_0825-1649     # the SAME capture, different storeys
      storeys: ["Floor 1 (710cm)", "Floor 3"]
```

**Always a list, even of one.** One capture can contribute several storeys to the
same level — Polycam laid one walk of an upstairs across two sheet clusters, and
naming a single storey per capture would have discarded 23 m² of it.

If you do not know which storey is which, ask:

```bash
uv run lidar2ha whichlevel exports/unknown_.../unknown_..._registered.json \
    --project project.yaml --write
```

It fits each of the capture's levels onto the levels you have already combined and
**refuses rather than naming a weak winner** — a capture of somewhere undeclared
still produces a least-bad row, and taking it would be a confident wrong answer.
`--write` prints the block to paste, leaving refusals out.

Paste it at the **top level** of `project.yaml`. The printed block carries its own
`levels:` key, so pasting it underneath the one you have nests the declaration
where `combine` never looks.

Unlike the rest of the file, `levels:` is checked strictly: an unknown key, a
missing `- `, a storey the capture does not have, or the same storey claimed twice
are all errors that name themselves.

---

## Part 6 — `combine`: align or discard

```bash
uv run lidar2ha combine "Ground Floor" --project project.yaml \
    -o exports/ground_floor_combined.json
```

Two steps and no third branch: align the capture, or report it and discard it.
There is no "align poorly and carry on".

**Read the second table, not the verdict column.** This is the single most
important thing in this document. `combine` prints a per-capture fit against the
chosen reference, and then a distance from the **averaged walls of every other
capture**. They can disagree, and the second one is the one that decides:

```
capture                     rot      median  cover     p90  verdict
ground_geometry_0412-1030   269.97   1.6cm    100%    2.7cm  ok
ground_geometry_0412-1145   180.67   2.6cm    100%   55.7cm  ok

  distance from the AVERAGED walls of the other captures:
    ground_geometry_0412-0900    1.6 cm    1.0x best
    ground_geometry_0412-1030    1.9 cm    1.2x best
    ground_geometry_0412-1145    3.5 cm    2.2x best   <- the odd one out
```

`ground_geometry_0412-1145` reads "ok" against the reference and is the worst
thing on the level. Note its **p90 of 55.7 cm** against everyone else's 2.7–6.3:
the tail is where a capture that is wrong about one room shows up, because the
median is dominated by the rooms it got right.

Measured over twelve captures on three storeys, the averaged figure separates
**2.5–4.3 cm from 14.7–29.2 cm**, where judging by the friendliest single pairing
gave 2.6–4.5 against 7.9–28.2 — good enough to let a capture that landed 65° out
on top of a hallway read 7.9 cm.

**Never reject on coverage.** Coverage is the fraction of the *source's* walls the
reference explains, so a capture that sees a new room always scores lower. A 90%
threshold once rejected the one capture containing a whole bathroom, at 88%.

**What you should also read:** the work list.

```
FLOOR NOT IN THE MODEL -- 1.5 m2 in 1 piece(s)
    1.5 m2 at (832, 525) cm  in ground_geometry_0412-1145/kitchen
```

A capture saw floor the combined model does not contain. That is either new ground
worth keeping or, as here, the signature of the capture that is wrong.

---

## Part 7 — Cut the rooms an open plan fuses

There is no wall between the lounge end and the dining end, so **every** capture
returns them as one polygon and no amount of rescanning separates them. The
boundary is yours to declare.

```bash
uv run python -m lidar2ha.preview exports/ground_floor_combined.json -o plan.png
```

`plan.png` draws a metre grid labelled in centimetres for exactly this purpose.
Read your coordinates off it — they are plan centimetres **in the combined model's
own frame** — and write them into `project.yaml`:

```yaml
split:
  "Ground Floor":
    - room: open_living
      seam: [[-240, -160], [20, -170]]     # a line: two pieces
      names: [lounge, dining]
    - room: other_open_space               # or trace an outline per room
      sections:
        - name: kitchen
          box: [[80, -420], [310, -140]]
        - name: dining
          outline: [[310, -420], [560, -420], [560, -140], [310, -140]]
```

```bash
uv run lidar2ha split "Ground Floor" --project project.yaml \
    -i exports/ground_floor_combined.json -o exports/ground_floor_split.json
```

> **On the demo:** the declaration is already written, cutting `open_living` in
> two at x=300. You should see
> `open_living 23.8 m2 -> 2 pieces` with `lounge 11.85 m2` and `dining 11.95 m2`,
> and the registry has a light in each end — which is the reason to cut it.
> With no `split:` entry for the level, `split` refuses and prints a template to
> paste rather than guessing where the boundary goes.

`split:` is keyed **by level**, not by capture, and that asymmetry with `merge:` is
deliberate: an open plan's fusion belongs to the building, so it is the same in
every capture, and it runs after `combine` where there is exactly one frame to
measure against.

Add `--mesh` and the floor is asked whether it agrees — a step, or a change from
wood to carpet. It reports and never decides: an unsupported boundary is not a
wrong boundary, because the floor under a sofa end is the same floor as under the
table.

**Rules that will bite you, each learned by hitting it:**

- Sections must be **disjoint**. A "rest of the room" box that spans the whole
  room plus a smaller box inside it is refused — *"overlap by 5.37 m2"*. Trace the
  remainder as an `outline:`.
- A section must lie **inside** the room being cut. A solid object like a kitchen
  island is a *hole* in the polygon, not a piece of it, and cannot be split out.
- Anything no section claims comes back flagged as *"N m2 traced by nobody"*
  rather than quietly making the room smaller.
- The pieces may come back in the **opposite order to `names:`**. Check the
  resulting bounds against a wall you know; areas alone will not tell you.
- **One unresolvable declaration aborts the whole level.** If a `split:` entry
  names a room that no longer exists — because a capture lost its `rooms:` mapping,
  or `merge:` changed which capture won — you get no cuts at all on that level,
  including the ones that were fine.

The pieces come back with **no ceiling**, because one number standing for two
spaces is the error the split exists to remove. Measure them:

```bash
uv run python -m lidar2ha.ceilings exports/ground_floor_split.json \
    exports/<anchor>/mesh_obj/23_08_2026.obj -o exports/ground_floor_split.json
```

---

## Part 8 — Find the fittings

Optional, and the difference between lights in roughly the right room and lights
where they actually hang.

```bash
uv run python -m lidar2ha.fixtures exports/<fixture-pass>/mesh_obj/23_08_2026.obj \
    -o fixtures.json --crops crops/

uv run python -m lidar2ha.placefixtures fixtures.json \
    exports/<fixture-pass>/<id>_registered.json \
    exports/<geometry>/<id>_named.json \
    --daylight-mesh exports/<geometry>/mesh_obj/23_08_2026.obj \
    -o fixtures_placed.json

uv run python -m lidar2ha.contactsheet crops/ fixtures_placed.json -o sheet.png
```

Pass every geometry capture the fixture pass walked through — one pass routinely
spans two, and each fitting is sent to whichever model contains it.

**This step needs a human, and not as a formality.** Brightness cannot separate a
lit bulb from a sunlit window: both saturate the sensor. Across 38 ground-level
candidates the luma range was 247.6 to 253.9 out of 255 — a 3 W cupboard LED and a
60 W pendant clip to the same white. The detector finds windows, and on one real
run it found a candle burning on a desk.

`--daylight-mesh` removes the windows mechanically rather than with a cleverer
threshold: a window is bright in *every* capture, a fitting only when switched on,
so differencing a fixture pass against an ordinary capture of the same rooms
isolates the fittings. There are **three** answers, not two — `fitting`, `window`,
and `unseen`, because an ordinary capture photographs ceilings badly and "the scan
never looked there" is not evidence either way.

**What you should see:** far more candidates than fittings, and far more fittings
than entities. One upstairs has roughly 18 fittings and 5 `light.*` entities; the
rest are dumb switches. Open `sheet.png` and read it top-down — likely windows are
sorted to the bottom and outlined, so you can stop early.

**Why this is worth it:** a fitting's height. The fallback places a light at the
room's ceiling minus 20 cm, which is wrong wherever a ceiling is not flat. In a
double-height room with a mezzanine projecting into it, a fitting hanging under
the projection at ~3 m gets placed near 6.8 m. Same room, same "ceiling height",
four metres out.

---

## Part 9 — Place the entities

```bash
uv run lidar2ha lights exports/ground_floor_split.json \
    --project project.yaml --registry registry.json \
    -o exports/ground_floor_lights.json \
    --fittings fixtures_placed.json          # omit to place at the pole instead
```

The floor-plan plugin matches furniture **by `name == entity_id`** and **sums
multiple sources sharing a name**. Everything odd about this stage follows:

- One switch driving six bulbs is six placements carrying one entity id. That is
  correct, not a workaround.
- One entity spanning three floors is three placements.
- A group **and** its members placed together is the same bulbs twice — and
  because the plugin sums them, that is not an error. It is a room that renders
  quietly too bright, forever.

**What you should see:** every entity accounted for, placed or named.

```
placed 6 light(s) in 4 room(s)
    light.hall_ceiling          hallway       level 0
    ...
  NOT PLACED (5):
    light.kitchen_group         excluded in project.yaml
    light.landing_status        excluded in project.yaml
    light.bedroom_ceiling       area 'bedroom' has no room in the model
```

Note `light.hall_ceiling` landing in `hallway` even though its device — a
multi-gang switch — is screwed to a wall in the bathroom. **An entity's own area
beats its device's.** Resolving device-first files every integration-native group
wherever its coordinator happens to be plugged in.

Which is the other thing to handle. A ZHA group's entity hangs off the
**coordinator** device rather than any lamp, which makes it mechanically
detectable; Hue rooms and deCONZ groups expose neither a member list nor a
coordinator and are flagged by name for you to judge. Exclude what you must:

```yaml
lights:
  exclude:
    - light.kitchen_lights      # a ZHA group; its members are placed instead
    - light.landing_status      # an indicator on a router, not room lighting
  include:
    - light.pantry              # force one back in past the group filters
```

Not every `light.*` is a light — status LEDs, indicator rings and controllers
exposing sound channels all turn up in the light domain. They are placed and
flagged rather than dropped, because a real fitting that merely looks like an
indicator would otherwise vanish and leave a room dark for no visible reason.

And **which entity drives which fitting is not in the geometry.** A room with four
downlights on two switches looks identical to one with four on one switch. The
only place that answer exists is in your head, so write it down:

```yaml
lights:
  pairing:
    kitchen:
      light.kitchen_west: [[485, 127]]
      light.kitchen_east: [[620, 127]]
```

A declaration that cannot be honoured — nothing near the point, or two fittings
equally close — is neither dropped nor placed on a guess. It goes back in with the
undeclared entities and is reported.

---

## Part 10 — Build, render, deploy

`lights` must run before `build`: `build --lights` is the only thing that names
objects after entity ids, and that naming is the sole signal the raytracer and
any 3D card match on.

```bash
uv run lidar2ha build exports/ground_floor_split.json \
    -o exports/ground_floor.sh3d --project project.yaml \
    --lights exports/ground_floor_lights.json \
    --walltex exports/ground_floor_walltex/manifest.json
```

**What you should see:** counts per level, then a `.sh3d` that reopens. `build`
refuses to report success on an archive Sweet Home 3D will not read, and names any
level whose elevation the mesh could not recover instead of defaulting it to zero.

### Three gates, none of them optional

```bash
# 1. FREE. Reports what the plugin detected and what a render will cost.
uv run lidar2ha render exports/ground_floor.sh3d -o exports/ground_floor_render \
    --project project.yaml --list

# 2. Minutes, at 640x360.
uv run lidar2ha render ... --preview

# 3. The real thing.
uv run lidar2ha render ...
```

**Gate 1** costs nothing and answers the only question that matters at this point:

```
  detected  : 6 light entities, 0 other
  plan      : 7 frame(s) at 1280x720, CSS mixing
  estimate  : about 6 min
```

**Zero detected means Part 9 failed** — do not render past it.

It also tells you what the render will cost, and the range is not small. The light
mixing mode changes the frame count by five orders of magnitude, and nothing warns
you. On one 21-light house at 640×360:

| mixing | what it renders | frames | time |
|---|---|---|---|
| `CSS` | one per light; the browser adds them | 22 | 5 min |
| `OVERLAY` | every combination of each room's lights | 65,541 | 9 days |
| `FULL` | every combination in the house | 2,097,152 | 10 months |

Keep `mixing: CSS`. Budget about 26 s a frame at 800×600; the machine barely
matters, because it runs single-process on Sweet Home 3D's bundled 32-bit runtime.

**Gate 2** catches the two failures that produce perfectly-formed useless images.
A **uniform white frame** is a picture of the sky — the camera yaw is wrong. A
**blank frame produced in about a second** means quality slipped to `LOW`, which
does not raytrace at all; it screenshots the GL view and returns nothing when
there is no GL context.

### Deploy

```bash
uv run lidar2ha deploy exports/ground_floor_render --project project.yaml
uv run lidar2ha deploy exports/ground_floor_render --project project.yaml --push --card
```

Without `--push` it connects read-only, prints a manifest of what would change,
and shows the card. That is gate 3.

```yaml
deploy:
  host: homeassistant.local     # or HA_SSH_HOST in your .env
  user: root
  port: 22
```

Images must land at `/config/www/floorplan/` because the plugin hard-codes
`/local/floorplan/` into the card it emits. **Use `--subdir <level>` once you have
more than one storey**: every level's render produces a `base.png`, and without a
subdirectory the second storey overwrites the first one's base frame while leaving
the first one's per-light frames beside it, named after entity ids nothing in the
new render owns. Those are reported, never deleted — they are somebody's working
dashboard.

Finally, paste the printed card into your dashboard. Nothing in lidar2ha writes it
for you.

**If you republish an image and the dashboard does not change**, it is the cache,
not the upload. `/local/...` is served with a long lifetime, so `deploy --card`
writes a `?version=<hash>` into every image URL. Check the checksums before
suspecting the transfer — the bytes are usually fine and the URL is the bug.

---

## Appendix — every stage, and what it reads and writes

Two entry points exist and they are not interchangeable. `lidar2ha <cmd>` is the
packaged CLI; `python -m lidar2ha.<stage>` is the stage module. Where both exist
they differ.

| Stage | Invocation | Reads | Writes |
|---|---|---|---|
| `doctor` | `lidar2ha doctor` | your install, `uv.lock` | compiled Java cache |
| `demo` | `lidar2ha demo DIR` | — | a whole demo project |
| `init` | `lidar2ha init DIR` | — | `project.yaml`, `captures/`, `build/` |
| `polycam` | `python -m lidar2ha.polycam DXF --csv CSV` | DXF + CSV | `<id>.json` |
| `mesh` | `python -m lidar2ha.mesh MESH` | `.obj` | prints floor candidates |
| `registration` | `python -m lidar2ha.registration JSON MESH` | model + `.obj` | `_registered.json` |
| `rooms` | `python -m lidar2ha.rooms MODEL PROJECT --capture ID` | model + yaml | `_named.json` |
| `textures_project` | `python -m lidar2ha.textures_project REG MESH` | registered + `.obj` | `walltex/` + manifest |
| `textures_tile` | `python -m lidar2ha.textures_tile MESH` | `.obj` | `floor/wall/ceiling.png` |
| `whichlevel` | `lidar2ha whichlevel CAPTURE --project P` | models + yaml | prints; `--write` prints a block |
| `combine` | `lidar2ha combine LEVEL --project P` | yaml + per-capture models | `_combined.json` + worklist |
| `preview` | `python -m lidar2ha.preview MODEL` | model | `plan.png` with a cm grid |
| `split` | `lidar2ha split LEVEL --project P` | yaml + combined model | `_split.json` |
| `ceilings` | `python -m lidar2ha.ceilings MODEL MESH -o OUT` | model + `.obj` | model with heights; **prints only without `-o`** |
| `fixtures` | `python -m lidar2ha.fixtures MESH --crops D` | fixture `.obj` | `fixtures.json` + crops |
| `placefixtures` | `python -m lidar2ha.placefixtures F REG GEO...` | the above + models | `fixtures_placed.json` |
| `contactsheet` | `python -m lidar2ha.contactsheet CROPS PLACED` | crops + placed | `sheet.png` |
| `ha` | `python -m lidar2ha.ha --refresh` | HA WebSocket | `registry.json` |
| `lights` | `lidar2ha lights MODEL --registry R` | model + registry + yaml | `lights.json` |
| `build` | `lidar2ha build MODEL -o OUT.sh3d` | model, textures, lights | `.tsv` then `.sh3d` |
| `render` | `lidar2ha render SH3D -o OUT` | `.sh3d` + yaml | `renders/`, `floorplan/`, `floorplan.yaml` |
| `deploy` | `lidar2ha deploy RENDER_OUT` | `floorplan/` + card | remote `/config/www/floorplan/` |
| `export-glb` | `lidar2ha export-glb SH3D` | `.sh3d` | `.glb` (+ `.obj` with `--keep-obj`) |
| `add-capture` | — | — | **not implemented** |

Differences worth knowing:

- **`render` means two different things.** `lidar2ha render` raytraces;
  `python -m lidar2ha.render` parses a `render.log` that already exists.
- **`split` is the subcommand for the `seams` module.** Only the CLI form accepts
  `--mesh`; only the module form accepts an ad-hoc `--room`/`--seam` declaration.
- **`lights` differs at both ends.** The module takes the registry as a required
  positional and defaults its report off; the CLI takes `--registry` with a
  default, reports by default, and can `--refresh` from Home Assistant.
- **`combine` differs.** The module takes model paths and writes an extra
  `_alignment.json`; the CLI takes a level name, resolves the paths from
  `project.yaml`, and does not.

### One thing `project.yaml` will not tell you

Apart from `levels:`, the project file is read leniently: every section is looked
up with a plain `.get`, so **a misspelled or unrecognised key does nothing and
says nothing**. There is no schema and no warning. On the house this was built
for, ten areas of light pairings sat under a top-level `light_pairing:` while the
tool reads `lights.pairing:` — the declarations had never once been applied.

When a section seems to have no effect, check its spelling and its nesting before
you check anything else.

[plugin]: https://github.com/shmuelzon/home-assistant-floor-plan
