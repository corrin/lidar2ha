# Your house, one lap at a time

You have a phone and an empty directory. By the end of this you have a floorplan
in Home Assistant that lights up when you tap a light.

## The shape of the work

This is a loop, not a pipeline:

> scan → run it through → put it on your dashboard → **look at it** → fix the
> control file, or take another scan → round again

The feedback is you, looking at your own house on your own dashboard. Every
capture after the first is a response to something you saw. Every line of
`project.yaml` is something you learned by looking, written down so it stays
fixed — it is the control file, and it accumulates.

Two questions run through every lap, and telling them apart is the whole skill:

- **Is this a bad scan?** Then rescan.
- **Is this something true about the house that nothing has been told?** Then
  write it in `project.yaml`.

Most people are working against Polycam's 7-day trial, because floor-plan export
costs around $1,000 a year. That changes the order. **Everything that needs
Polycam — scanning and exporting both — happens inside those seven days.**
Everything else runs offline forever. So the first lap has to close on day one,
while you can still act on what it tells you.

Numbers below were measured on real captures. They are what makes the difference
between a step working and a step looking like it worked.


## Where to go

Lap 0 and Lap 1 are in order. After that, come back to the section that matches
what you saw.

| | |
|---|---|
| [Lap 0 — before you start the clock](#lap-0--before-you-start-the-clock) | install, `doctor`, the demo, your area ids |
| [Lap 1 — one room on your dashboard today](#lap-1--one-room-on-your-dashboard-today) | scan settings, the two exports, the seven commands |
| [Now scan the house](#now-scan-the-house) | how many passes, naming, staging, the seven-day list |
| *"I don't know which storey is which"* | `whichlevel`, `levels:` |
| *"My captures disagree about the layout"* | `combine` |
| *"A room is missing, or has no name"* | `coverage`, `validate` |
| *"Two rooms light up as one"* | `split:`, and whether you actually need it |
| *"A room is the wrong height"* | `ceilings` |
| *"The lights are in the wrong place"* | `fixtures`, pairing |
| *"A room is too bright, or never lights"* | `lights`, groups, exclusions |
| [The last lap](#the-last-lap) | full render, `--subdir`, the card |
| [Appendix](#appendix--every-stage) | every stage, and what `project.yaml` will not tell you |

---

# Lap 0 — before you start the clock

No Polycam account yet. Nothing here can be lost.

## Install, and prove the toolchain

```bash
git clone https://github.com/corrin/lidar2ha && cd lidar2ha
uv sync --all-extras
uv run lidar2ha doctor
```

`--all-extras` is not optional. `paramiko` and `websockets` are extras, and a
bare `uv sync` removes them again, taking `deploy` and the Home Assistant
registry with them.

You also need [Sweet Home 3D](https://www.sweethome3d.com/), the
[floor-plan plugin][plugin], and a **JDK** 17+ ([Temurin](https://adoptium.net/)).

**What you should see:** every line `[ ok ]`, ending with

```
  compiling Java against your installation...
  [ ok ] java sources               ...\jclasses\f1e5d77d81003ece
Everything checks out.
```

`doctor` compiles the Java against your own Sweet Home 3D. That is the step that
catches a version mismatch; an earlier version only looked at paths, and passed
happily while the sources would not build.

**What it looks like when it's wrong:** `javac (JDK)` missing while `java` is
found means you have a JRE. Sweet Home 3D bundles a runtime with no compiler.

## Run the whole thing on a house that does not exist

```bash
uv run lidar2ha demo ~/demo-house
cd ~/demo-house
```

Eight captures of a two-storey building with a stairwell, packaged the way
Polycam packages yours: two archives per capture, every file inside them sharing
one name. One of the eight is wrong about where the kitchen is, so `combine` has
something to catch — which you cannot rehearse on your own house until you have
three passes of a level, by which point the trial is nearly over.

Every command in this document runs on it. A `registry.json` is included, so it
needs no Home Assistant.

## Fetch your area ids

```bash
export HA_URL=http://homeassistant.local:8123
export HA_TOKEN=...        # Profile -> Long-lived access tokens
uv run python -m lidar2ha.ha --refresh -o registry.json
```

The token needs an admin account: this reads the area, floor, device and entity
registries. Put both in a `.env` beside `project.yaml` if you prefer — it is
gitignored and `project.yaml` is not, and a long-lived token is a house key.

After this one fetch everything works from the cached `registry.json`, so the
rest of the loop runs offline.

**What you should see:** a count of areas, floors, devices and light entities,
and a table classifying each `light.*`. Read it now. You are about to name rooms
after these ids, and you will need the table again when you place entities.

---

# Lap 1 — one room on your dashboard today

Start the trial here. The goal is not a good model. It is to see your own room
light up, so that you know what your scans look like when they work while you
can still take more of them.

## Before you scan

**Cover every mirror.** A scanner cannot tell a reflection from a room, so it
builds a phantom copy of the space behind the wall. One 2.2 m room produced a
5.55 m mesh in 882 disconnected pieces.

**LiDAR, Space mode. Never Floorplan mode.** Floorplan mode produces no mesh, and
without a mesh there is no registration, no ceiling heights, no fitting positions
and no textures. You get a plan you cannot place.

**Open interior doors, turn the lights on.** Glass is invisible to LiDAR, so
windows will be missing and you add them by hand in Sweet Home 3D later.

**One continuous capture per wall-bounded volume.** Not per Home Assistant area.
A capture that walks several storeys is fine and common; it comes back as several
levels and you say which is which later.

Now scan one room. Any room with a light in it.

## Two exports, and one right format for each

The export picker is single-select, so this is two trips per capture:

| Menu | Choose | You get | Why that one |
|---|---|---|---|
| Floor Plan | **Zip (all)** | `.dxf` + `.csv` (+ pdf, svg, png) | the DXF is the plan; **the CSV is the ceiling heights** |
| Mesh | **OBJ** | `.obj` + `.mtl` + `textures/` | the atlas comes as real image files |

Export settings: **Metric / Meters**, point density **High**, **Mesh up axis: Z**.

Both wrong choices import cleanly and cost you something you will not notice for
days. Three of one real house's nineteen captures went out this way.

**Floor Plan → DXF, without the CSV: every room gets a made-up ceiling.**

```
ground_geometry_0823-1038   ceiling=520cm      <- Zip (all): real, varied heights
mid_geometry_0823-1020      ceiling=470cm
upstairs_geometry_0823-1058 ceiling=400cm

ground_geometry_0823-2006   ceiling=240cm      <- DXF only: every room, every capture
mid_geometry_0823-1810      ceiling=240cm
upstairs_geometry_0823-1904 ceiling=240cm
```

240 cm is `--default-height`. A double-height stairwell and a laundry come out
the same, which is the geometry that makes cross-floor light spill worth
raytracing at all.

**Mesh → GLB: no wall textures and no fitting detection.** A GLB registers fine,
so it looks like a working capture. trimesh gives glTF a `PBRMaterial`, whose
atlas hangs off `.baseColorTexture` rather than `.image`, and `.image` is what
`fixtures` and `textures_project` select on:

```
OBJ capture: 2 geoms, material=SimpleMaterial, .image=set,  usable = 2/2
GLB capture: 5 geoms, material=PBRMaterial,    .image=None, usable = 0/5
```

`textures_project` then reports `coverage 0.0% -- skipped` for every wall and
exits 0, which reads as *"this scan saw no walls"* and means *"the loader could
not find the atlas"*.

Re-exporting fixes both, and only while you still have Polycam. After the trial
ends your scans stay in the library and you cannot get them out.

## Run it through

Downloads are named by capture **date**, not by capture, so rename the two files
as they land. Then:

```bash
ID=lounge_geometry_0823-1038
mkdir -p exports/$ID && cd exports/$ID     # unpack the two archives here
```

```bash
uv run python -m lidar2ha.polycam floorplan/plan.dxf --csv floorplan/plan.csv \
    -o $ID.json
uv run python -m lidar2ha.registration $ID.json mesh_obj/mesh.obj \
    -o ${ID}_registered.json
uv run python -m lidar2ha.rooms ${ID}_registered.json ../../project.yaml \
    -o ${ID}_named.json --capture $ID
```

`rooms` needs to know which Home Assistant area this room is. Write it in
`project.yaml` first, keyed by capture id, using the scanner's own name on the
left and your area id on the right:

```yaml
rooms:
  lounge_geometry_0823-1038:
    "Living Room": lounge
```

The scanner guesses names and guesses badly. One real capture confidently
labelled an entrance hall "Living Room" and "Dining Room", and returned an open
kitchen as "Kitchen" plus "Office 1". You supply the truth.

Then place the lights, build, render small, and ship it:

```bash
cd ../..
uv run lidar2ha lights exports/$ID/${ID}_named.json --project project.yaml \
    --registry registry.json -o exports/$ID/lights.json
uv run lidar2ha build exports/$ID/${ID}_named.json -o exports/$ID/lounge.sh3d \
    --project project.yaml --lights exports/$ID/lights.json
uv run lidar2ha render exports/$ID/lounge.sh3d -o exports/$ID/render \
    --project project.yaml --list
uv run lidar2ha render exports/$ID/lounge.sh3d -o exports/$ID/render \
    --project project.yaml --preview
uv run lidar2ha deploy exports/$ID/render --project project.yaml --push --card
```

`--list` costs nothing and reports what the plugin detected. **Zero detected
lights means the `lights` step failed** and there is nothing to render. Fix that
before spending minutes on frames.

`deploy` needs SSH to the machine running Home Assistant; on Home Assistant OS
that is the Terminal & SSH add-on, where `/config` is mounted. Images have to
land in `/config/www/floorplan/` because the plugin bakes `/local/floorplan/`
into the card.

## Look at it

Paste the printed card into a dashboard. Tap the light.

That is the loop closed. You now know what one of your captures looks like when
it works, and every number below has something of yours to compare against.

---

# Now scan the house

Still inside the seven days, and now you know what you are doing.

## What to shoot

**Three geometry captures of every storey.** Two captures that disagree cannot
say which of them is wrong; a third identifies the odd one out immediately. On
the house this was built for, the capture the entire mid-level model had been
built from turned out to be the worst of its three, and only the third scan
revealed it.

**One fixture pass per storey.** A deliberately different capture: every light
switched on, including ones Home Assistant cannot control, phone aimed at each
fitting in turn. Geometry quality is sacrificed on purpose. What you are
recording is where the fittings are and how high they hang, which nothing else
can tell you and which the raytracer needs.

**Level by level, finishing each one.** If you run out of days you want a
complete ground floor, not a third of everything.

## The seven-day list

Inside the window, or never:

- every geometry capture, three per storey
- every fixture pass
- **every export**, both formats, for all of them
- re-exports of anything you got wrong

Any time after, forever, offline: naming, combining, splitting, ceilings,
fittings, lights, building, rendering, deploying, and every rescan-driven fix
that does not need a new scan.

## Give each capture a name and its own directory

Polycam names a download by capture date. Fifteen captures on one day give you
`23_08_2026.zip`, `23_08_2026 (1).zip`, `23_08_2026 (2).zip`, and the number is
your browser's download counter — the order you clicked, not which capture is
which. Inside, every file carries that same date name:

```
23_08_2026 (13).zip  ->  23_08_2026.csv  23_08_2026.dxf  23_08_2026.pdf ...
23_08_2026 (12).zip  ->  23_08_2026.mtl  23_08_2026.obj  textures/
```

**Unpack two captures into one directory and the second overwrites the first.**
No error. You find out much later, when a registration fits the wrong mesh.

Export one capture at a time and rename both files the moment they land. If you
did not, tell the archives apart by size — a floor-plan zip is well under 1 MB, a
mesh zip is 7–19 MB — then open each plan zip, where the CSV lists room names.

Name each capture `<where>_<what>_<MMDD-HHMM>`:

- **where** — the smallest *true* scope: a level (`ground`, `mid`, `upstairs`),
  one room, an outdoor place, or `unknown`. Never guess. `unknown` is a real
  answer and there is a tool for resolving it.
- **what** — `geometry` or `fixtures`.
- **when** — the Polycam capture time, so the id cross-references back to the
  app, which labels captures by timestamp and nothing else.

One directory each:

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
then the project root.

> `lidar2ha add-capture` would do all of this in one command. It is not
> implemented, and it is the biggest ergonomic gap in the project — you pay this
> by hand on every lap.

**Two traps in this directory.**

A capture directory fills up with things that look like inputs. After a few runs
it holds `walltex/`, `render/`, and — if you ever ran `export-glb --keep-obj` — a
`gltf/` containing an `.obj` that is lidar2ha's own output. A glob for `*.obj`
finds it, because `gltf/` sorts before `mesh_obj/`:

```
mesh wall points : 1,913          every other capture: 25,000 - 162,000
mesh z range     : -3.33 .. 1353.87 m
median error     : inf cm   coverage=0%
```

The same capture against the right file is the best-registered in the house, at
**1.0 cm and 100% coverage**. Always name the mesh explicitly.

A stale derived file wins over a corrected one. `combine` prefers
`<id>_named.json`, then `<id>_registered.json`, then `<id>.json`. A `_named.json`
left from an old mapping is taken ahead of everything else, and one that named
*nothing* looks identical to one that named everything. They are regenerable: if
in doubt, delete them and re-run.

## Per capture: plan, register, name

Three commands each, and no batch form, so fifteen captures is forty-five
invocations. Write a loop.

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

That warning is the whole-house walk being taken apart into storeys. You say
which storey belongs to which level below.

**What it looks like when it's wrong:** every room reporting 240 cm. That is
`--default-height`, and it means no CSV reached this command.

### `registration` — the plan and the mesh into one frame

This is the weak link in the pipeline and the number to actually read.

**What you should see:** a few centimetres at 100% coverage. Across one real
house the good captures ran **1.0 to 3.4 cm**.

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

Anything in double figures is a capture to re-shoot or discard. `LOW COVERAGE` is
what catches the wrong-mesh mistake above — check the mesh wall-point count and z
range on the lines before it.

### Every capture in a level needs a mapping

Including the fixture passes, which are the ones people leave out.

`combine` picks one capture to win each group of overlapping rooms, and **the
winner's names survive**. A capture with no `rooms:` entry keeps its scanner
names, so if it wins, the area you carefully mapped on a *different* capture is
gone, replaced by `Other 1`.

Measured on a real house: three captures in `levels:` had no `rooms:` block, and
`master_bedroom`, `girl_bedroom`, `sewing_room` and `boy_bedroom` all vanished
from the model. Each was correctly mapped on two other captures. It made no
difference.

The failure is quiet in both directions. `rooms` exits 1 for a capture you *run*
it on with no mapping, but nothing makes you run it on every capture, and
`combine` falls back without comment. So check:

```bash
uv run lidar2ha validate --project project.yaml
```

```
CAPTURE NOT NAMED  (3)
  ground_geometry_0823-1038 is in `levels:` and has no `rooms:` entry
  boy_bedroom_geometry_0823-1349 is in `levels:` and has no `rooms:` entry
  upstairs_fixtures_0823-1216 is in `levels:` and has no `rooms:` entry
```

It exits non-zero, so it can gate a build. It also catches an area id that is
really a capture id, a capture declared and used in no level, and a key nothing
reads.

**One trap inside the trap.** A capture with a plan and **no mesh** never gets a
`_registered.json`, so a loop keyed on that file skips it silently — which is how
`boy_bedroom` was missed. Run `rooms` from `<id>_registered.json` where it
exists and `<id>.json` where it does not.

`merge:` is keyed by capture, and takes rooms one walk over-segmented:

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

A scanner's over-segmentation belongs to the walk that made it. Another scan of
the same room splits it somewhere else, or not at all.

---

# The repairs

Each of these starts with something you saw. Each ends back at the dashboard.

## "I don't know which storey of this capture is which"

A capture that walked the whole house holds several levels of its own, so the
level it belongs to is not a property of the capture. Declare it in
`project.yaml`, keyed by your Home Assistant floor names:

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
same level: Polycam laid one walk of an upstairs across two sheet clusters, and
naming a single storey per capture would have discarded 23 m² of it.

If you do not know which storey is which, ask:

```bash
uv run lidar2ha whichlevel exports/unknown_.../unknown_..._registered.json \
    --project project.yaml --write
```

It fits each of the capture's levels onto the levels you have already combined
and refuses rather than naming a weak winner — a capture of somewhere undeclared
still produces a least-bad row, and taking it would be a confident wrong answer.
`--write` prints the block to paste, leaving refusals out.

Paste it at the **top level** of `project.yaml`. The printed block carries its own
`levels:` key, so pasting it underneath the one you have nests the declaration
where `combine` never looks.

Unlike the rest of the file, `levels:` is checked strictly: an unknown key, a
missing `- `, a storey the capture does not have, or the same storey claimed
twice are all errors that name themselves.

## "My captures disagree about the layout"

```bash
uv run lidar2ha combine "Ground Floor" --project project.yaml \
    -o exports/ground_floor_combined.json
```

Align the capture, or report it and discard it. There is no third branch, and no
"align poorly and carry on".

**Read the second table, not the verdict column.** `combine` prints a per-capture
fit against the chosen reference, then a distance from the **averaged walls of
every other capture**. They disagree, and the second one decides:

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
thing on the level. Its **p90 of 55.7 cm** against everyone else's 2.7–6.3 is
where a capture that is wrong about one room shows up; the median is dominated by
the rooms it got right.

Measured over twelve captures on three storeys, the averaged figure separates
**2.5–4.3 cm from 14.7–29.2 cm**. Judging by the friendliest single pairing gave
2.6–4.5 against 7.9–28.2, which let a capture that landed 65° out on top of a
hallway read 7.9 cm.

**Never reject on coverage.** Coverage is the fraction of the *source's* walls the
reference explains, so a capture that sees a new room always scores lower. A 90%
threshold once rejected the one capture containing a whole bathroom, at 88%.

**Read the work list.**

```
FLOOR NOT IN THE MODEL -- 1.5 m2 in 1 piece(s)
    1.5 m2 at (832, 525) cm  in ground_geometry_0412-1145/kitchen
```

A capture saw floor the combined model does not contain. Either new ground worth
keeping, or — as here — the signature of the capture that is wrong.

### When to stop and fix

Skipping this is how a model with a 1 m² "basement" and half the house missing
reached a live dashboard.

| | why it is a stop |
|---|---|
| a room's `score` is below 0.70, or it is in the FLAGGED list | the geometry is the best available and still not good enough. Rendering it does not improve it |
| a capture reads several times the best against the **averaged walls** | it disagrees with everything else on the level. Its rooms are in your model |
| the naming table shows `SPLIT` or `LOOKS_LIKE` | an unnamed room won a group, and it is standing on an area you mapped |
| `lidar2ha coverage` is missing a room you know exists | that room is not in the render, whatever the render looks like |
| a room's area is far from what Polycam measured | the CSV in every floor-plan zip has per-room floor areas. A room at a quarter of its measured size is not a room |

None of these raise an error, and every one produced a plausible model that was
wrong.

## "A room is missing, or has no name"

```bash
uv run lidar2ha coverage --project project.yaml
```

```
20 of 29 area(s) across 3 level(s) have geometry.

Ground Level: 3 of 10 area(s), from ground_level_split.json
  NO GEOMETRY -- not scanned, or the room needs this area id under `rooms.<capture>`:
    den
    downstairs_hallway
    garage
  NO AREA -- each needs a name in `rooms:`, a `split:`, or to be left as not-an-area:
    downstairs_hallway             17.0 m2
    den                            12.9 m2
```

Read the two lists together. `den` appears in both, so the room is there,
correctly shaped, carrying no `ha_area`. That is a `rooms:` line, not a rescan.

The same output tells you when an area legitimately spans storeys. A stairwell is
one area and three floors, and that is not a fault.

## "Two rooms light up as one"

Writing the declaration is the easy half. Knowing you need one is the hard half.

### First: is this actually open plan?

Two things look identical in the output and want opposite fixes.

**Genuinely open plan.** There is no wall between the lounge end and the dining
end, so **every** capture returns them as one polygon and no amount of rescanning
separates them. `split:` is the only thing that can divide them, and the boundary
is a declaration about how you use the house.

**One capture that fused what others resolved.** A fixture pass is shot with
geometry sacrificed on purpose and routinely lays one polygon over two rooms. If
it wins the group, those two rooms are gone. The other captures got it right, and
a `split:` there writes a claim about the *building* that is false — one that
becomes actively wrong the moment you replace the bad capture.

Ask which captures resolve the room separately:

```bash
uv run python -c "
import json,sys
for f in sys.argv[1:]:
    m=json.load(open(f,encoding='utf-8'))
    got=[r.get('ha_area') or r.get('name') for L in m['levels'] for r in L['rooms']]
    print(f, [g for g in got if g in ('lounge','dining')] or 'NEITHER — fused')
" exports/*/*_named.json
```

If **no capture** resolves them, it is open plan: declare the split. If **some
do**, the fused one is a capture that should be losing, and cutting its polygon
by hand papers over that.

### Five ways the tool already told you

1. **`combine`'s naming table.**

   ```
   SPLIT       upstairs_fixtures/Hallway 1  20.2 m2  sewing_room 47%  girl_bedroom 46%
   LOOKS_LIKE  upstairs_fixtures/Other 1    18.1 m2  master_bedroom 99%
   ```

   `SPLIT` means one polygon sits on two areas you named. `LOOKS_LIKE` means an
   unnamed room *is* an area you named, at that confidence.

2. **`lidar2ha coverage` shows an area with no geometry** that you know exists.
3. **`combine`'s `area_with_no_source` row** — `project.yaml` maps the area and no
   room carries it.
4. **An implausible area.** One 46 m² polygon covered a living room, a dining
   room, a kitchen and an office. Compare against the CSV's per-room areas.
5. **`preview`, read against the house you live in.** The only check that catches
   a room being the wrong *shape*.

### Then read the coordinates off the preview

```bash
uv run python -m lidar2ha.preview exports/ground_floor_combined.json -o plan.png
```

`plan.png` draws a metre grid labelled in centimetres. Coordinates are plan
centimetres in the combined model's own frame:

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
> two at x=300. You should see `open_living 23.8 m2 -> 2 pieces` with
> `lounge 11.85 m2` and `dining 11.95 m2`, and the registry has a light in each
> end. With no `split:` entry for the level, `split` refuses and prints a
> template rather than guessing where the boundary goes.

`split:` is keyed by **level** and `merge:` by capture. An open plan's fusion
belongs to the building, so it is the same in every capture, and it runs after
`combine` where there is exactly one frame to measure against.

Add `--mesh` and the floor is asked whether it agrees — a step, or a change from
wood to carpet. It reports and never decides. The floor under a sofa end is the
same floor as under the table, so an unsupported boundary is not a wrong one.

**Rules that will bite you:**

- Sections must be **disjoint**. A "rest of the room" box spanning the whole room
  plus a smaller box inside it is refused — *"overlap by 5.37 m2"*. Trace the
  remainder as an `outline:`.
- A section must lie **inside** the room being cut. A kitchen island is a *hole*
  in the polygon, not a piece of it.
- Anything no section claims comes back flagged as *"N m2 traced by nobody"*.
- The pieces may come back in the **opposite order to `names:`**. Check the bounds
  against a wall you know; areas alone will not tell you.
- **A declaration that cannot be carried out is reported, and the rest still
  run.** A stale name comes back under `DECLARED, AND NOT CUT` with the reason.
  A *malformed* declaration still stops the run: a `box` with three corners is a
  typo, not a judgement.

**The pieces inherit `ha_area` only if the parent had one**, and this trap is
silent. Cut a room carrying no area and you get pieces that are named, outlined
and unreachable — `lights` binds by `ha_area`, so nothing can ever be placed in
them:

```
'Living Room' carries no ha_area, so neither does any piece of it.
```

Map the parent. Which area it names does not matter, since the pieces replace it.
If the parent is itself a piece of an earlier cut, go to the room at the top of
the chain. On a real house this cost two of five ground-floor rooms their areas.

## "A room is the wrong height"

Split pieces come back with **no ceiling**, because one number standing for two
spaces is the error the split exists to remove.

```bash
uv run python -m lidar2ha.ceilings exports/ground_floor_split.json \
    exports/<anchor>/mesh_obj/23_08_2026.obj -o exports/ground_floor_split.json
```

**What you should see:** a height per piece, and a refusal where the scan could
not see one.

**What it looks like when it's wrong:** every room reporting the level's ceiling
height, or a double-height space reading like a normal room. One real den
measured 398 cm against a hand-measured ~7 m, because the scan was truncated
rather than the room being short. `ceilings` says `NOT WRITTEN` when it has only
a lower bound. Believe it.

## "The lights are in the wrong place"

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

Pass every geometry capture the fixture pass walked through. One pass routinely
spans two, and each fitting is sent to whichever model contains it.

**This step needs a human, and not as a formality.** Brightness cannot separate a
lit bulb from a sunlit window: both saturate the sensor. Across 38 ground-level
candidates the luma range was 247.6 to 253.9 out of 255 — a 3 W cupboard LED and
a 60 W pendant clip to the same white. The detector finds windows, and on one run
it found a candle burning on a desk.

`--daylight-mesh` removes the windows mechanically. A window is bright in *every*
capture and a fitting only when switched on, so differencing a fixture pass
against an ordinary capture of the same rooms isolates the fittings. There are
**three** answers: `fitting`, `window`, and `unseen`. An ordinary capture
photographs ceilings badly, and "the scan never looked there" is not evidence
either way.

**What you should see:** far more candidates than fittings, and far more fittings
than entities. One upstairs has roughly 18 fittings and 5 `light.*` entities; the
rest are dumb switches. Read `sheet.png` top-down — likely windows are sorted to
the bottom and outlined, so you can stop early.

**Why it is worth it:** height. The fallback places a light at the room's ceiling
minus 20 cm, which is wrong wherever a ceiling is not flat. In a double-height
room with a mezzanine projecting into it, a fitting hanging under the projection
at ~3 m gets placed near 6.8 m. Same room, same "ceiling height", four metres out.

Which entity drives which fitting is not in the geometry. A room with four
downlights on two switches looks identical to one with four on one switch, so
write it down:

```yaml
lights:
  pairing:
    kitchen:
      light.kitchen_west: [[485, 127]]
      light.kitchen_east: [[620, 127]]
```

A declaration that cannot be honoured — nothing near the point, or two fittings
equally close — goes back in with the undeclared entities and is reported.

## "A room is too bright, or never lights, or the wrong one lights"

```bash
uv run lidar2ha lights exports/ground_floor_split.json \
    --project project.yaml --registry registry.json \
    -o exports/ground_floor_lights.json \
    --fittings fixtures_placed.json          # omit to place at the pole instead
```

The floor-plan plugin matches furniture **by `name == entity_id`** and **sums
multiple sources sharing a name**. Everything odd about this stage follows:

- One switch driving six bulbs is six placements carrying one entity id. That is
  correct.
- One entity spanning three floors is three placements.
- A group **and** its members placed together is the same bulbs twice. Because
  the plugin sums them it raises no error: the room renders quietly too bright,
  forever.

**What you should see:**

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

Groups are found three ways and the report says which found what. Home
Assistant's own group helper lists its members, so those are exact. ZHA hangs a
Zigbee group's entity off the **coordinator** device — the radio, not a lamp —
and nothing else in the light domain lives there, which makes it a mechanical
test: on this house it found four groups where the name heuristic found one of
the same four. Hue rooms and deCONZ groups expose neither, and are flagged by
name for you to judge.

```yaml
lights:
  exclude:
    - light.kitchen_lights      # a ZHA group; its members are placed instead
    - light.landing_status      # an indicator on a router, not room lighting
  include:
    - light.pantry              # force one back in past the group filters
```

Not every `light.*` is a light. Status LEDs, indicator rings and controllers
exposing sound channels all turn up in the light domain. They are placed and
flagged rather than dropped, because a real fitting that merely looks like an
indicator would otherwise vanish and leave a room dark for no visible reason.

Fittings with no entity are reported, never invented. Placing an uncontrollable
light would render prettily and respond to nothing.

---

# The last lap

`lights` must run before `build`. `build --lights` is the only thing that names
objects after entity ids, and that naming is the sole signal the raytracer and
any 3D card match on.

```bash
uv run lidar2ha build exports/ground_floor_split.json \
    -o exports/ground_floor.sh3d --project project.yaml \
    --lights exports/ground_floor_lights.json \
    --walltex exports/ground_floor_walltex/manifest.json
```

**What you should see:** counts per level, then a `.sh3d` that reopens. `build`
refuses to report success on an archive Sweet Home 3D will not read, and names
any level whose elevation the mesh could not recover instead of defaulting it to
zero.

## Three gates

```bash
uv run lidar2ha render exports/ground_floor.sh3d -o exports/ground_floor_render \
    --project project.yaml --list       # free
uv run lidar2ha render ... --preview    # minutes, at 640x360
uv run lidar2ha render ...              # the real thing
```

**Gate 1** costs nothing:

```
  detected  : 6 light entities, 0 other
  plan      : 7 frame(s) at 1280x720, CSS mixing
  estimate  : about 6 min
```

Zero detected means the `lights` step failed. Do not render past it.

It also prices the render, and the range is not small. The light mixing mode
changes the frame count by five orders of magnitude, and nothing warns you. On
one 21-light house at 640×360:

| mixing | what it renders | frames | time |
|---|---|---|---|
| `CSS` | one per light; the browser adds them | 22 | 5 min |
| `OVERLAY` | every combination of each room's lights | 65,541 | 9 days |
| `FULL` | every combination in the house | 2,097,152 | 10 months |

Keep `mixing: CSS`. Budget about 26 s a frame at 800×600; the machine barely
matters, because it runs single-process on Sweet Home 3D's bundled 32-bit
runtime.

**Gate 2** catches the two failures that produce perfectly-formed useless images.
A **uniform white frame** is a picture of the sky: the camera yaw is wrong. A
**blank frame produced in about a second** means quality slipped to `LOW`, which
screenshots the GL view instead of raytracing and returns nothing when there is
no GL context.

## Deploy

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

**Use `--subdir <level>` once you have more than one storey.** Every level's
render produces a `base.png`, and without a subdirectory the second storey
overwrites the first one's base frame while leaving the first one's per-light
frames beside it, named after entity ids nothing in the new render owns. Those
are reported, never deleted — they are somebody's working dashboard.

Paste the printed card into your dashboard. Nothing writes it for you.

**If you republish an image and the dashboard does not change**, it is the cache.
`/local/...` is served with a long lifetime, so `deploy --card` writes a
`?version=<hash>` into every image URL. Check the checksums before suspecting the
transfer; the bytes are usually fine and the URL is the bug.

---

# Appendix — every stage

`lidar2ha <cmd>` is the packaged CLI; `python -m lidar2ha.<stage>` is the stage
module. Where both exist they differ.

| Stage | Invocation | Reads | Writes |
|---|---|---|---|
| `doctor` | `lidar2ha doctor` | your install, `uv.lock` | compiled Java cache |
| `demo` | `lidar2ha demo DIR` | — | a whole demo project |
| `init` | `lidar2ha init DIR` | — | `project.yaml`, `captures/`, `build/` |
| `validate` | `lidar2ha validate` | `project.yaml`, `registry.json` | prints; **exits 1** so it can gate a build |
| `coverage` | `lidar2ha coverage` | `project.yaml`, `registry.json`, the level models | prints which areas have geometry |
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

## One thing `project.yaml` will not tell you

Apart from `levels:`, the project file is read leniently: every section is looked
up with a plain `.get`, so **a misspelled or unrecognised key does nothing and
says nothing**. There is no schema and no warning. On the house this was built
for, ten areas of light pairings sat under a top-level `light_pairing:` while the
tool reads `lights.pairing:` — the declarations had never once been applied.

When a section seems to have no effect, check its spelling and its nesting before
you check anything else.

[plugin]: https://github.com/shmuelzon/home-assistant-floor-plan
