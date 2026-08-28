# Your house, one lap at a time

You've got a phone and an empty directory, and you want a floorplan in Home
Assistant that lights up when you tap a light. This is how I did mine.

## How it actually works

You scan something, run it through, put it on your dashboard, and then go and
look at it. Whatever's wrong tells you what to do
next, which is either another scan or a line in `project.yaml`.

`project.yaml` is where you tell it the things it can't work out on its own.
Mostly that's which scanner room is which Home Assistant area. It grows as you go.

Every time something looks wrong, the question is which of these it is:

1. A bad scan. Go and rescan it.
2. Something true about the house that you've never told it. Write that in
   `project.yaml`.

They look identical in the output and they want opposite fixes, so it's worth
slowing down on that one.

The other thing to plan around is Polycam. Floor plan export is about $1,000 a
year, so I'm assuming you're on the 7 day trial. Scanning and exporting both need
Polycam, so both have to happen inside that week. Everything after runs offline
forever.

Which means get the first lap done on day one, while you can still go and take
more scans.

The numbers below are all from my own captures. I've included them because when
this goes wrong it usually still produces something that looks fine, and having a
real figure to compare against is the only way I found to tell.

## Where to go

Lap 0 and Lap 1 are in order. After that just come back to whichever heading
matches what you saw.

| | |
|---|---|
| [Lap 0 - before you start the clock](#lap-0---before-you-start-the-clock) | install, `doctor`, the demo, your area ids |
| [Lap 1 - one room on your dashboard today](#lap-1---one-room-on-your-dashboard-today) | scan settings, the two exports, the seven commands |
| [Now scan the house](#now-scan-the-house) | how many passes, naming, staging, the seven day list |
| *"I don't know which storey is which"* | `whichlevel`, `levels:` |
| *"My captures disagree about the layout"* | `combine` |
| *"A room is missing, or has no name"* | `coverage`, `validate` |
| *"Two rooms light up as one"* | `split:`, and whether you actually need it |
| *"A room is the wrong height"* | `ceilings` |
| *"The lights are in the wrong place"* | `fixtures`, pairing |
| *"A room is too bright, or never lights"* | `lights`, groups, exclusions |
| [The last lap](#the-last-lap) | full render, `--subdir`, the card |
| [Appendix](#appendix---every-stage) | every stage, and how `project.yaml` gets checked |

---

# Lap 0 - before you start the clock

No Polycam account yet, so there's nothing to lose here.

## Install, and check the toolchain

```bash
git clone https://github.com/corrin/lidar2ha && cd lidar2ha
uv sync --all-extras
uv run lidar2ha doctor
```

Use `--all-extras`. `paramiko` and `websockets` are extras, so a bare `uv sync`
takes them out again and you lose `deploy` and the Home Assistant registry.

You also need [Sweet Home 3D](https://www.sweethome3d.com/), the
[floor-plan plugin][plugin], and a JDK 17+ ([Temurin](https://adoptium.net/)).

You want every line `[ ok ]`, ending with:

```
  compiling Java against your installation...
  [ ok ] java sources               ...\jclasses\f1e5d77d81003ece
Everything checks out.
```

`doctor` compiles the Java against your copy of Sweet Home 3D. Earlier versions of
it just checked paths, which passed happily on an install where the sources
wouldn't build, so now it does the compile.

If it finds `java` but not `javac (JDK)`, you've got a JRE. Sweet Home 3D ships a
runtime with no compiler in it.

## Run it all on a house that doesn't exist

```bash
uv run lidar2ha demo ~/demo-house
cd ~/demo-house
```

That writes eight captures of a made-up two-storey house with a stairwell,
packaged the way Polycam packages yours: two archives per capture, every file
inside them with the same name. One of the eight is wrong about where the kitchen
is, so `combine` has something to find.

That last bit is the real reason to bother with it. You can't practise `combine`
on your own house until you've got three passes of a level, and by then your trial
is nearly up.

Everything below runs on it, and it comes with a `registry.json`, so you don't
need Home Assistant connected.

## Get your area ids

```bash
export HA_URL=http://homeassistant.local:8123
export HA_TOKEN=...        # Profile -> Long-lived access tokens
uv run python -m lidar2ha.ha --refresh -o registry.json
```

The token has to be from an admin account, because this reads the area, floor,
device and entity registries. You can put both in a `.env` next to
`project.yaml` instead. That's gitignored and `project.yaml` isn't, and a
long-lived token is basically a house key.

It only needs doing once. After that everything works off the cached
`registry.json` and the rest of the loop is offline.

You'll get counts of areas, floors, devices and light entities, plus a table
classifying each `light.*`. Read it now, because you're about to start naming
rooms after those ids, and you'll want the table again later when you place
entities.

---

# Lap 1 - one room on your dashboard today

Start the trial here.

Don't try to get a good model out of this one. The point is to see one of your own
rooms light up on your own dashboard, so you know what a scan of yours looks like
when it works, while you can still go and take more.

## Before you scan

Cover the mirrors. This cost me a whole capture. A scanner can't tell a reflection
from a room, so it happily builds a copy of the bathroom on the other side of the
wall. My 2.2 m ensuite came back 5.55 m tall in 882 disconnected pieces.

Use LiDAR mode, and Space rather than Floorplan. Floorplan mode gives you no mesh,
and the mesh is where ceiling heights, fitting positions and textures come from.
Without one you've got a plan you can't put anywhere.

Then the obvious stuff: open the internal doors, turn the lights on. Windows
won't be in the scan at all, because LiDAR goes straight through glass, so you
add them by hand in Sweet Home 3D at the end.

You can scan whatever you like. One room, one floor, the whole house in one walk.
It doesn't have to match your Home Assistant areas, and a walk over several
storeys is fine, it just comes back as several levels and you sort out which is
which later.

For now pick a room with a light in it.

## Two exports, and only one right format for each

The picker is single-select so it's two trips per capture:

| Menu | Choose | You get | Why that one |
|---|---|---|---|
| Floor Plan | **Zip (all)** | `.dxf` + `.csv` (+ pdf, svg, png) | the DXF is the plan, and the CSV is your ceiling heights |
| Mesh | **OBJ** | `.obj` + `.mtl` + `textures/` | the atlas comes as real image files |

Settings: **Metric / Meters**, point density **High**, **Mesh up axis: Z**.

I got both of these wrong, on three of my nineteen captures. Neither errors, both
import fine, and you don't notice for days.

**Floor Plan -> DXF, without the CSV.** Every room gets a made-up ceiling:

```
ground_geometry_0823-1038   ceiling=520cm      <- Zip (all): real, varied heights
mid_geometry_0823-1020      ceiling=470cm
upstairs_geometry_0823-1058 ceiling=400cm

ground_geometry_0823-2006   ceiling=240cm      <- DXF only: every room, every capture
mid_geometry_0823-1810      ceiling=240cm
upstairs_geometry_0823-1904 ceiling=240cm
```

240 cm is `--default-height`. My double-height stairwell and my laundry came out
the same height, which throws away the whole reason to raytrace.

**Mesh -> GLB.** No wall textures and no fitting detection. It registers fine so
it looks like a working capture, but trimesh gives glTF a `PBRMaterial` whose
atlas hangs off `.baseColorTexture` rather than `.image`, and `.image` is what
`fixtures` and `textures_project` look for:

```
OBJ capture: 2 geoms, material=SimpleMaterial, .image=set,  usable = 2/2
GLB capture: 5 geoms, material=PBRMaterial,    .image=None, usable = 0/5
```

`textures_project` then says `coverage 0.0% -- skipped` for every wall and exits
0. That reads like "this scan didn't see any walls" and means "the loader couldn't
find the atlas".

Re-exporting fixes both, but only while you've still got Polycam. Once the trial's
over your scans stay in the library and you can't get them out.

## Run it through

Downloads are named by date, not by capture, so rename both files as they land.

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

`rooms` needs to know which Home Assistant area this is, so put that in
`project.yaml` first. Scanner's name on the left, your area id on the right:

```yaml
rooms:
  lounge_geometry_0823-1038:
    "Living Room": lounge
```

The scanner guesses room names and it's not good at it. Mine labelled my entrance
hall as "Living Room" and "Dining Room", and gave me an open kitchen back as
"Kitchen" plus "Office 1".

Then lights, build, a small render, and ship it:

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

`--list` is free and tells you what the plugin found. If it says zero lights then
`lights` didn't work and there's nothing to render, so sort that out before you
spend minutes on frames.

`deploy` needs SSH to whatever runs Home Assistant. On HA OS that's the Terminal &
SSH add-on, where `/config` is mounted. Images have to go to
`/config/www/floorplan/`, because the plugin hard-codes `/local/floorplan/` into
the card and there's no setting for it.

## Look at it

Paste the printed card into a dashboard and tap the light.

That's one lap. You now know what one of your captures looks like when it works,
which makes all the numbers below mean something.

---

# Now scan the house

Still inside the seven days, but now you know what you're doing.

## What to shoot

Three geometry captures of every storey. I know that sounds like overkill. But two
captures that disagree can't tell you which one is wrong, and a third sorts it out
straight away. On my house the capture I'd built the entire mid-level model from
turned out to be the worst of its three, and I only found that out from the third
scan.

Then one fixture pass per storey. That's a deliberately different capture: every
light on, including the ones Home Assistant can't control, and you point the phone
at each fitting in turn. The geometry will be rubbish and that's fine, that's the
trade. You're recording where the fittings are and how high they hang, which
nothing else can tell you.

Do it level by level and finish each one. If you run out of days you want a
complete ground floor rather than a third of everything.

## What has to happen this week

1. every geometry capture, three per storey
2. every fixture pass
3. every export, both formats, for all of them
4. re-exports of anything you got wrong

Everything else (naming, combining, splitting, ceilings, fittings, lights,
building, rendering, deploying) can wait as long as you like.

## Name each capture and give it its own directory

Polycam names downloads by date. Fifteen captures in one day gets you
`23_08_2026.zip`, `23_08_2026 (1).zip`, `23_08_2026 (2).zip`, and that number is
your browser's download counter, so it's the order you clicked rather than
anything useful. Inside, every file has the same date name:

```
23_08_2026 (13).zip  ->  23_08_2026.csv  23_08_2026.dxf  23_08_2026.pdf ...
23_08_2026 (12).zip  ->  23_08_2026.mtl  23_08_2026.obj  textures/
```

Unpack two captures into one directory and the second one overwrites the first.
Silently. You find out weeks later when a registration fits the wrong mesh.

So export one at a time and rename both files as they arrive. If you didn't, you
can tell the archives apart by size (floor plan zips are well under 1 MB, mesh
zips are 7-19 MB) and then open each plan zip, since the CSV lists the room names.

I name mine `<where>_<what>_<MMDD-HHMM>`:

1. **where** is the smallest scope that's actually true. A level (`ground`, `mid`,
   `upstairs`), one room, somewhere outside, or `unknown`. Don't guess, `unknown`
   is a real answer and there's a tool below for sorting it out.
2. **what** is `geometry` or `fixtures`.
3. **when** is the Polycam capture time, so the id points back at the app, which
   labels captures by timestamp and nothing else.

One directory each, so identically-named files can't collide:

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

`combine` looks in `exports/<id>/`, then `captures/<id>/`, then the project root.

> `lidar2ha add-capture` was meant to do all of this in one command. It isn't
> built, and it's the biggest ergonomic gap in the project. You do this by hand
> every time.

Two things in here have cost me real time.

The first is that a capture directory fills up with stuff that looks like input.
After a few runs it's got `walltex/`, `render/`, and if you ever ran
`export-glb --keep-obj`, a `gltf/` with an `.obj` in it that lidar2ha wrote
itself. A glob for `*.obj` picks that one, because `gltf/` sorts before
`mesh_obj/`:

```
mesh wall points : 1,913          every other capture: 25,000 - 162,000
mesh z range     : -3.33 .. 1353.87 m
median error     : inf cm   coverage=0%
```

Same capture against the right file is the best-registered one in my house, 1.0 cm
at 100% coverage. Name the mesh explicitly and it can't happen.

The second is that a stale derived file beats a corrected one. `combine` prefers
`<id>_named.json`, then `<id>_registered.json`, then `<id>.json`. So a
`_named.json` left over from an old mapping gets used ahead of everything else,
and one that named nothing looks exactly like one that named everything. They're
all regenerable, so if you're not sure, delete them and re-run.

## Per capture: plan, register, name

Three commands each and there's no batch form, so fifteen captures is forty-five
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
while still using the fittings it found.

### `polycam`

You get a line per level with wall, room and door counts. A capture that walked
more than one storey comes back as more than one level, named after the ceiling
band it sat at:

```
WARNING: Floor 1 holds 7 room(s) across 3 ceiling bands: 210cm x3, 480cm x1, 710cm x3
  Floor 1 (210cm) walls= 25 rooms=3 doors=5
  Floor 1 (480cm) walls= 11 rooms=1 doors=1
  Floor 1 (710cm) walls= 14 rooms=3 doors=3
  Floor 2         walls= 18 rooms=2 doors=4
  Floor 3         walls= 17 rooms=4 doors=9
```

That warning is fine, it's just the whole-house walk being pulled apart into
storeys, and you say which storey is which further down.

What you don't want is every room reporting the same ceiling, especially if it's
240 cm. That's `--default-height` and it means no CSV got to the command.

### `registration`

This is the weakest part of the pipeline and the number worth actually reading.

You want a few centimetres at 100% coverage. My good captures ran 1.0 to 3.4 cm:

```
  rotation      : 359.99 deg   mirror=False
  median error  : 1.0 cm   coverage=100%
  floor z       : -2.241 m
```

A bad one:

```
  median error  : 18.4 cm   coverage=95%
  ** LOW COVERAGE: 89% of the plan found no wall within a metre.
```

Anything in double figures is a capture to re-shoot or bin. `LOW COVERAGE` is also
what catches the wrong-mesh problem above, so check the wall-point count and z
range on the lines before it.

### Every capture in a level needs a mapping

Including the fixture passes, which are the ones people forget.

`combine` picks one capture to win each group of overlapping rooms, and the
winner's names are the ones that survive. A capture with no `rooms:` entry keeps
its scanner names, so if it wins, the area you carefully mapped on a different
capture is gone and you get `Other 1` instead.

This one got me. Three captures in my `levels:` had no `rooms:` block, and
`master_bedroom`, `girl_bedroom`, `sewing_room` and `boy_bedroom` all
disappeared out of the model. Every one of them was correctly mapped on two other
captures and it made no difference at all.

It's quiet in both directions, which is why it's easy to miss. `rooms` does exit 1
if you run it on a capture with no mapping, but nothing makes you run it on every
capture, and `combine` falls back without saying anything.

So check instead of remembering:

```bash
uv run lidar2ha validate --project project.yaml
```

```
CAPTURE NOT NAMED  (3)
  ground_geometry_0823-1038 is in `levels:` and has no `rooms:` entry
  boy_bedroom_geometry_0823-1349 is in `levels:` and has no `rooms:` entry
  upstairs_fixtures_0823-1216 is in `levels:` and has no `rooms:` entry
```

It exits non-zero so you can gate a build on it. It also picks up an area id
that's really a capture id, a capture you declared and never used, and keys
nothing reads.

There's a trap inside the trap. A capture with a plan and no mesh never gets a
`_registered.json`, so a loop keyed on that file skips it and says nothing, which
is exactly how I lost `boy_bedroom`. Run `rooms` from `<id>_registered.json`
where it exists and `<id>.json` where it doesn't.

`merge:` is the other half of this, for rooms one walk over-segmented:

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

It's keyed by capture deliberately. Over-segmentation belongs to the walk that
did it, and another scan of the same room will split it somewhere else, or not at
all.

---

# The repairs

Each of these starts with something you noticed on the dashboard.

## "I don't know which storey of this capture is which"

A capture that walked the whole house has several levels in it, so which level it
belongs to isn't a property of the capture. You declare it in `project.yaml`,
keyed by your Home Assistant floor names:

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

Always a list, even when it's one thing. A single capture can put several storeys
into the same level. Polycam laid one walk of my upstairs across two sheet
clusters, and if I'd only been able to name one storey per capture I'd have thrown
away 23 m2 of it.

If you don't know which storey is which, ask:

```bash
uv run lidar2ha whichlevel exports/unknown_.../unknown_..._registered.json \
    --project project.yaml --write
```

It fits each of the capture's levels onto the levels you've already combined, and
it refuses rather than naming a weak winner. A capture of somewhere you haven't
declared still produces a least-bad row, and taking that would just be a confident
wrong answer. `--write` prints the block to paste and leaves the refusals out.

Paste it at the top level of `project.yaml`. The printed block has its own
`levels:` key, so if you paste it under the one you've already got you nest the
whole thing somewhere `combine` never looks. I did that.

`levels:` is the one section that's checked strictly. An unknown key, a missing
`- `, a storey the capture hasn't got, or the same storey claimed twice are all
errors that tell you what they are.

## "My captures disagree about the layout"

```bash
uv run lidar2ha combine "Ground Floor" --project project.yaml \
    -o exports/ground_floor_combined.json
```

It either aligns a capture or reports it and throws it out. There's no middle
option, and I'm fairly sure that's right, because there used to be one and it
built my mid-level model out of the worst of its three captures.

Read the second table rather than the verdict column. `combine` prints each
capture's fit against the reference it picked, and then its distance from the
averaged walls of every other capture. Those two can disagree, and the second one
is the one to believe:

```
capture                     rot      median  cover     p90  verdict
ground_geometry_0412-1030   269.97   1.6cm    100%    2.7cm  ok
ground_geometry_0412-1145   180.67   2.6cm    100%   55.7cm  ok

  distance from the AVERAGED walls of the other captures:
    ground_geometry_0412-0900    1.6 cm    1.0x best
    ground_geometry_0412-1030    1.9 cm    1.2x best
    ground_geometry_0412-1145    3.5 cm    2.2x best   <- the odd one out
```

`ground_geometry_0412-1145` says "ok" against the reference and is the worst thing
on that level. Look at its p90 of 55.7 cm next to everyone else's 2.7-6.3. A
capture that's wrong about one room shows up in the tail, because the median is
dominated by all the rooms it got right.

Over twelve captures on three storeys, the averaged figure separated 2.5-4.3 cm
from 14.7-29.2 cm. Going by the friendliest single pairing instead gave 2.6-4.5
against 7.9-28.2, and that let a capture which had landed 65 degrees out on top of
a hallway read 7.9 cm.

Don't reject on coverage. Coverage is the fraction of the source's walls the
reference explains, so a capture that saw a new room always scores lower. I had a
90% threshold for a while and it rejected the only capture containing my bathroom,
at 88%.

Then read the work list:

```
FLOOR NOT IN THE MODEL -- 1.5 m2 in 1 piece(s)
    1.5 m2 at (832, 525) cm  in ground_geometry_0412-1145/kitchen
```

A capture saw floor that isn't in the combined model. Either that's new ground
worth keeping, or, like here, it's the capture that's wrong.

### When to stop and fix something

This is the bit that's easy to skip, and skipping it is how I got a model with a
1 m2 "basement" and half the house missing onto a live dashboard.

| | why it's a stop |
|---|---|
| a room's `score` is below 0.70, or it's in the FLAGGED list | that's the best geometry available and it's still not good enough. Rendering it won't improve it |
| a capture reads several times the best against the averaged walls | it disagrees with everything else on the level, and its rooms are in your model |
| the naming table shows `SPLIT` or `LOOKS_LIKE` | an unnamed room won a group and it's sitting on an area you mapped |
| `lidar2ha coverage` is missing a room you know exists | that room isn't in the render, whatever the render looks like |
| a room's area is nowhere near what Polycam measured | the CSV in every floor plan zip has per-room floor areas. A room at a quarter of its measured size isn't a room |

None of those are errors. They just give you a wrong model that looks fine.

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

Read both lists together. `den` is in both, so the room is there and the right
shape, it just hasn't got an `ha_area`. That's a `rooms:` line, not a rescan.

The same output shows you when an area genuinely spans storeys. A stairwell is one
area across three floors and that's not a fault.

## "Two rooms light up as one"

Writing the declaration is easy. Working out that you need one is the annoying
part.

### First: is this actually open plan?

Two different things look the same here and want opposite fixes.

If it's genuinely open plan, there's no wall between the lounge end and the dining
end, so every capture gives you one polygon and no amount of rescanning will
separate them. A `split:` is the only thing that can, and where you put the
boundary is a decision about how you use the house rather than anything the
building will tell you.

The other case is one capture that fused what the others resolved. A fixture pass
has bad geometry on purpose and quite often lays one polygon over two rooms. If it
wins the group then those two rooms are gone, but the other captures got it right,
so a `split:` there would be writing something into your config that's false about
the building. And it goes properly wrong the moment you replace the bad capture.

Work out which it is by asking which captures resolve the room separately:

```bash
uv run python -c "
import json,sys
for f in sys.argv[1:]:
    m=json.load(open(f,encoding='utf-8'))
    got=[r.get('ha_area') or r.get('name') for L in m['levels'] for r in L['rooms']]
    print(f, [g for g in got if g in ('lounge','dining')] or 'NEITHER - fused')
" exports/*/*_named.json
```

If none of them resolve it, it's open plan and you should declare the split. If
some do, then the fused one is a capture that should be losing, and cutting its
polygon by hand is papering over that.

### Five ways it already told you

1. `combine`'s naming table, which is the clearest of them:

   ```
   SPLIT       upstairs_fixtures/Hallway 1  20.2 m2  sewing_room 47%  girl_bedroom 46%
   LOOKS_LIKE  upstairs_fixtures/Other 1    18.1 m2  master_bedroom 99%
   ```

   `SPLIT` means one polygon is sitting on two areas you named. `LOOKS_LIKE` means
   an unnamed room is an area you named, at that confidence.

2. `lidar2ha coverage` showing an area with no geometry that you know exists.
3. `combine`'s `area_with_no_source` row, where `project.yaml` maps the area and
   no room carries it.
4. An area that doesn't make sense. One 46 m2 polygon of mine covered a living
   room, a dining room, a kitchen and an office. Compare against the CSV.
5. `preview`, read against the house you live in. It's the only one that catches a
   room being the wrong shape.

### Then read the coordinates off the preview

```bash
uv run python -m lidar2ha.preview exports/ground_floor_combined.json -o plan.png
```

`plan.png` has a metre grid labelled in centimetres for exactly this. The numbers
are plan centimetres in the combined model's own frame:

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

> On the demo this is already written, cutting `open_living` in two at x=300. You
> should get `open_living 23.8 m2 -> 2 pieces`, with `lounge 11.85 m2` and
> `dining 11.95 m2`, and there's a light in each end in the registry. If a level
> has no `split:` entry, `split` refuses and prints a template to paste rather
> than guessing where the boundary goes.

`split:` is keyed by level and `merge:` by capture. That asymmetry is on purpose:
an open plan's fusion belongs to the building so it's identical in every capture,
and it runs after `combine` where there's only one frame to measure against.

Add `--mesh` and it asks the floor whether it agrees, looking for a step or a
change from wood to carpet. It reports and never decides. The floor under a sofa
end is the same floor as under the table, so a boundary the mesh can't see isn't a
wrong boundary.

Things that will bite you, all of which bit me:

1. Sections have to be disjoint. A "rest of the room" box covering the whole room
   plus a smaller box inside it gets refused with *"overlap by 5.37 m2"*. Trace
   the remainder as an `outline:` instead.
2. A section has to be inside the room you're cutting. A kitchen island is a hole
   in the polygon, not a piece of it, so you can't split it out.
3. Anything no section claims comes back flagged as *"N m2 traced by nobody"*
   rather than just making the room smaller.
4. The pieces can come back in the opposite order to `names:`. Check the bounds
   against a wall you know, because the areas won't tell you.
5. A declaration that can't be carried out gets reported and the rest still run. A
   stale name turns up under `DECLARED, AND NOT CUT` with the reason. It used to
   abandon the whole level, and one stale entry cost me three other cuts that were
   perfectly fine. A malformed declaration does still stop the run, because a
   `box` with three corners is a typo rather than a judgement call.

The pieces only inherit `ha_area` if the parent had one, and it's silent about it.
Cut a room with no area and you get pieces that are named, outlined, and
unreachable, because `lights` binds by `ha_area` and nothing can be placed in
them. `split` does tell you:

```
'Living Room' carries no ha_area, so neither does any piece of it.
```

Fix it by mapping the parent. Which area you give it doesn't matter, since the
pieces replace it anyway. If the parent is itself a piece of an earlier cut then
no `rooms:` line can name it, so go to the room at the top of the chain. This cost
me two of five ground floor rooms.

## "A room is the wrong height"

Split pieces come back with no ceiling, because one number covering two spaces is
the thing the split was for. So measure them:

```bash
uv run python -m lidar2ha.ceilings exports/ground_floor_split.json \
    exports/<anchor>/mesh_obj/23_08_2026.obj -o exports/ground_floor_split.json
```

You want a height per piece, and a refusal where the scan couldn't see one.

What you don't want is every room reporting the level's ceiling height, or a
double-height space reading like a normal one. My den measured 398 cm against a
hand-measured 7 m or so, because the scan was truncated rather than the room being
short. `ceilings` says `NOT WRITTEN` when all it's got is a lower bound, and it's
worth believing.

## "The lights are in the wrong place"

Optional, but it's the difference between lights in roughly the right room and
lights where they actually hang.

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

Give it every geometry capture the fixture pass walked through. One pass often
spans two, and each fitting goes to whichever model contains it.

You have to review this by hand. Brightness can't tell a
lit bulb from a sunlit window, they both saturate the sensor. Across 38 ground
level candidates the luma range was 247.6 to 253.9 out of 255, so a 3 W cupboard
LED and a 60 W pendant come out the same white. It finds windows. On one run it
found a candle on a desk.

`--daylight-mesh` gets the windows out for you, using a second capture. A window
is bright in every capture and a fitting only when it's switched on, so
differencing a fixture pass against an ordinary capture of the same rooms leaves
you the fittings.

It gives three answers: `fitting`, `window`, and `unseen`. An ordinary
capture photographs ceilings badly, because the camera meters for the room and
nobody points a phone at a dark ceiling, so "never looked there" is common and it
isn't evidence either way.

Expect way more candidates than fittings, and way more fittings than entities. My
upstairs has about 18 fittings and 5 `light.*` entities, the rest are dumb
switches. Read `sheet.png` top down, since likely windows are sorted to the bottom
and outlined, so you can stop early.

The reason it's worth doing is height. The fallback puts a light at the room's
ceiling minus 20 cm, which is wrong anywhere the ceiling isn't flat. I've got a
double-height room with a mezzanine sticking into it, and a fitting hanging under
the mezzanine at about 3 m was getting placed near 6.8 m. Same room, same "ceiling
height", four metres out.

Which entity drives which fitting isn't in the geometry at all. A room with four
downlights on two switches looks identical to one with four on one switch, so the
only place that answer exists is in your head:

```yaml
lights:
  pairing:
    kitchen:
      light.kitchen_west: [[485, 127]]
      light.kitchen_east: [[620, 127]]
```

If a declaration can't be honoured, meaning nothing near the point or two fittings
equally close, it isn't dropped and it isn't placed on the guess. It goes back in
with the undeclared entities and gets reported. You declare a house one room at a
time, and I didn't want a half-declared room to lose its other half.

## "A room is too bright, or never lights, or the wrong one lights"

```bash
uv run lidar2ha lights exports/ground_floor_split.json \
    --project project.yaml --registry registry.json \
    -o exports/ground_floor_lights.json \
    --fittings fixtures_placed.json          # omit to place at the pole instead
```

The plugin matches furniture by `name == entity_id`, and it sums multiple sources
that share a name. Everything odd about this stage comes from that:

1. One switch driving six bulbs is six placements all carrying one entity id.
   That is what the plugin wants.
2. One entity spanning three floors is three placements.
3. A group and its members placed together is the same bulbs twice. The plugin
   sums them, so nothing errors. You just get a room that renders slightly too
   bright, forever.

What you should see:

```
placed 6 light(s) in 4 room(s)
    light.hall_ceiling          hallway       level 0
    ...
  NOT PLACED (5):
    light.kitchen_group         excluded in project.yaml
    light.landing_status        excluded in project.yaml
    light.bedroom_ceiling       area 'bedroom' has no room in the model
```

`light.hall_ceiling` lands in `hallway` even though its device, a multi-gang
switch, is screwed to a wall in the bathroom. An entity's own area beats its
device's. If you resolve device-first, every integration-native group ends up
wherever its coordinator happens to be plugged in.

Groups get found three ways and the report says which found what, because the
coverage differs. Home Assistant's own group helper lists its members so those are
exact. ZHA hangs a Zigbee group's entity off the coordinator, which is the radio
rather than a lamp, and nothing else in the light domain lives there, so that's a
mechanical test: on my house it finds four groups where the name heuristic finds
one of the same four. Hue rooms and deCONZ groups expose neither, so they get
flagged by name for you to judge.

```yaml
lights:
  exclude:
    - light.kitchen_lights      # a ZHA group; its members are placed instead
    - light.landing_status      # an indicator on a router, not room lighting
  include:
    - light.pantry              # force one back in past the group filters
```

Not every `light.*` is a light. Status LEDs, indicator rings, and controllers
exposing sound channels all turn up in the light domain. They get placed and
flagged rather than dropped, because a real fitting that looks a bit like an
indicator would otherwise vanish and leave a room dark for no visible reason.

If it finds a fitting with no entity it tells you rather than inventing one. A
light you can't control would render nicely and do nothing.

---

# The last lap

`lights` has to run before `build`. `build --lights` is the only thing that names
objects after entity ids, and that naming is the only signal the raytracer and any
3D card have to match on.

```bash
uv run lidar2ha build exports/ground_floor_split.json \
    -o exports/ground_floor.sh3d --project project.yaml \
    --lights exports/ground_floor_lights.json \
    --walltex exports/ground_floor_walltex/manifest.json
```

You get counts per level and a `.sh3d` that reopens. `build` won't report success
on an archive Sweet Home 3D can't read, and it names any level whose elevation the
mesh couldn't recover instead of quietly calling it zero.

## Three gates

```bash
uv run lidar2ha render exports/ground_floor.sh3d -o exports/ground_floor_render \
    --project project.yaml --list       # free
uv run lidar2ha render ... --preview    # minutes, at 640x360
uv run lidar2ha render ...              # the real thing
```

The first one is free:

```
  detected  : 6 light entities, 0 other
  plan      : 7 frame(s) at 1280x720, CSS mixing
  estimate  : about 6 min
```

Zero detected means `lights` failed, so don't carry on past it.

It also tells you what the render will cost, and the range is enormous. The light
mixing mode changes the frame count by five orders of magnitude and nothing warns
you. Same model, same size, one 21-light house at 640x360:

| mixing | what it renders | frames | time |
|---|---|---|---|
| `CSS` | one per light; the browser adds them | 22 | 5 min |
| `OVERLAY` | every combination of each room's lights | 65,541 | 9 days |
| `FULL` | every combination in the house | 2,097,152 | 10 months |

Keep `mixing: CSS`. Budget about 26 s a frame at 800x600. The machine barely
matters, it runs single-process on Sweet Home 3D's bundled 32-bit runtime.

The preview catches the two failures that produce perfectly good useless images. A
uniform white frame is a picture of the sky, which means the camera yaw is wrong.
A blank frame that appears in about a second means quality dropped to `LOW`, which
doesn't raytrace at all, it screenshots the GL view and gives you nothing when
there's no GL context.

## Deploy

```bash
uv run lidar2ha deploy exports/ground_floor_render --project project.yaml
uv run lidar2ha deploy exports/ground_floor_render --project project.yaml --push --card
```

Without `--push` it connects read-only, prints a manifest of what would change,
and shows you the card. That's the third gate.

```yaml
deploy:
  host: homeassistant.local     # or HA_SSH_HOST in your .env
  user: root
  port: 22
```

Use `--subdir <level>` once you've got more than one storey. Every level's render
produces a `base.png`, so without a subdirectory the second storey overwrites the
first one's base frame and leaves the first one's per-light frames sitting beside
it, named after entity ids nothing in the new render owns. Those get reported and
never deleted, because they're somebody's working dashboard.

Then paste the printed card into your dashboard. Nothing writes it for you.

If you republish an image and the dashboard doesn't change, it's the cache rather
than the upload. `/local/...` is served with a long lifetime, so `deploy --card`
puts a `?version=<hash>` into every image URL. Check the checksums before you
suspect the transfer, the bytes are usually fine and it's the URL that's wrong.

---

# Appendix - every stage

There are two entry points and they're not interchangeable. `lidar2ha <cmd>` is
the packaged CLI, `python -m lidar2ha.<stage>` is the stage module, and where both
exist they differ.

| Stage | Invocation | Reads | Writes |
|---|---|---|---|
| `doctor` | `lidar2ha doctor` | your install, `uv.lock` | compiled Java cache |
| `demo` | `lidar2ha demo DIR` | - | a whole demo project |
| `init` | `lidar2ha init DIR` | - | `project.yaml`, `captures/`, `build/` |
| `validate` | `lidar2ha validate` | `project.yaml`, `registry.json` | prints; exits 1 so it can gate a build |
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
| `ceilings` | `python -m lidar2ha.ceilings MODEL MESH -o OUT` | model + `.obj` | model with heights; prints only without `-o` |
| `fixtures` | `python -m lidar2ha.fixtures MESH --crops D` | fixture `.obj` | `fixtures.json` + crops |
| `placefixtures` | `python -m lidar2ha.placefixtures F REG GEO...` | the above + models | `fixtures_placed.json` |
| `contactsheet` | `python -m lidar2ha.contactsheet CROPS PLACED` | crops + placed | `sheet.png` |
| `ha` | `python -m lidar2ha.ha --refresh` | HA WebSocket | `registry.json` |
| `lights` | `lidar2ha lights MODEL --registry R` | model + registry + yaml | `lights.json` |
| `build` | `lidar2ha build MODEL -o OUT.sh3d` | model, textures, lights | `.tsv` then `.sh3d` |
| `render` | `lidar2ha render SH3D -o OUT` | `.sh3d` + yaml | `renders/`, `floorplan/`, `floorplan.yaml` |
| `deploy` | `lidar2ha deploy RENDER_OUT` | `floorplan/` + card | remote `/config/www/floorplan/` |
| `export-glb` | `lidar2ha export-glb SH3D` | `.sh3d` | `.glb` (+ `.obj` with `--keep-obj`) |
| `add-capture` | - | - | not implemented |

Differences worth knowing:

1. `render` means two different things. `lidar2ha render` raytraces,
   `python -m lidar2ha.render` parses a `render.log` that already exists.
2. `split` is the subcommand for the `seams` module. Only the CLI form takes
   `--mesh`, and only the module form takes an ad-hoc `--room`/`--seam`.
3. `lights` differs at both ends. The module takes the registry as a required
   positional and defaults its report off. The CLI takes `--registry` with a
   default, reports by default, and can `--refresh` from Home Assistant.
4. `combine` differs. The module takes model paths and writes an extra
   `_alignment.json`. The CLI takes a level name, resolves paths from
   `project.yaml`, and doesn't.

## `project.yaml` gets checked when it loads

Every stage validates the whole file before it does anything, and refuses a key
it doesn't read:

```
project.yaml:
  unknown key `pairings` under `lights`, did you mean `pairing`?

A key nothing reads does nothing and says nothing, which is why this is an error.
```

It didn't always do that, which is why it does now. I had ten areas of light
pairings sitting under a top-level `light_pairing:` while the tool reads
`lights.pairing:`. Nothing crashed and nothing warned, the models just came out
without them, and I didn't find out for months.

`levels:` and `split:` have their own parsers on top of this, which check the
contents rather than only the key names.

[plugin]: https://github.com/shmuelzon/home-assistant-floor-plan
