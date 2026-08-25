# lidar2ha

Turn a phone LiDAR scan of a house into an interactive 3D floorplan in Home Assistant —
tap a light on your dashboard, watch the room illuminate, raytraced, with light spilling
through stairwells and across open-plan volumes.

That's the goal. Read the next section before you invest any time in this.

## What this actually is

I wanted my house in Home Assistant as a 3D floorplan, and I didn't want to draw it by
hand. So I sat down with [Claude](https://claude.ai/code) and we built this. Claude wrote
essentially all of the code here; I supplied the house, the scans, the goal, and the
judgement about whether each output was actually right. The commit history is honest about
this and so is this README.

What that means for you:

- **It has been run against exactly one house.** Mine. Every default, threshold and
  heuristic here is tuned to one capture of one building in one scanning app.
- **It is not a product.** There is no installer, no unified command, no test suite.
- **It is a pile of scripts that work, plus a design for the thing they'd become.**

I'm publishing it because the hard part — discovering that you *cannot* write a `.sh3d`
without going through Sweet Home 3D's own Java classes, and the dozen smaller traps behind
that — took a long time to work out, and nobody had written it down. That knowledge is at
the bottom of this file and it is the most reliable thing in the repo.

## What works and what doesn't

| Stage | State |
|---|---|
| Parse Polycam floor-plan DXF/CSV → JSON model | works (`polycam.py`) |
| Recover floor elevations from the mesh | works (`mesh.py`) |
| Register DXF floors onto the mesh | works, but the weak link — see limits |
| Rectify per-wall textures from the photo atlas | works (`textures_project.py`) |
| Tiled textures by surface class (fallback) | works (`textures_tile.py`) |
| Emit the scene file | works (`scene.py`) |
| Write a real `.sh3d` | works (`Sh3dWriter.java`) |
| Headless raytraced render + `floorplan.yaml` | works (`HeadlessRender.java`), all levels in one pass |
| Frame the camera so the house fits | works (`camera.py`), solved rather than guessed |
| Rename rooms to HA areas, merge open-plan splits | works (`rooms.py`), mapping written by hand |
| Merge several captures of one level | works (`combine.py`), align-or-discard, needs 3+ scans |
| Read your HA area/entity registry | works (`ha.py`), over the WebSocket API |
| Place every `light.*` entity in its room | works (`lights.py`), positions are a guess |
| Find real fittings in the scan | works (`fixtures.py`, `placefixtures.py`), **needs human review** |
| Separate windows from fittings mechanically | works (`daylight.py`), differences two captures |
| Review sheet for the fittings found | works (`contactsheet.py`), windows sorted last |
| Export a named GLB for a real-time 3D card | works (`ObjExport.java`, `glb.py`) |
| Cut an open-plan room into the rooms it is used as | works (`seams.py`), boundary declared by you |
| Corroborate a declared boundary against the floor | works (`thresholds.py`), reports, never decides |
| `lidar2ha doctor`, `build`, `combine`, `split`, `lights`, `render`, `deploy`, `export-glb` | works (`cli.py`) |
| **`lidar2ha add-capture`** | **does not exist** (exits saying so) |

`pip install .` now gives you a working `lidar2ha`: `doctor`, `build`, `lights`, `render`
and `deploy`. Only `add-capture` is still a stub, and it exits telling you what to run
instead. The package is not on PyPI. The remaining stages are still scripts you run
by hand.

Roughly: the geometry-and-rendering half is real, the Home-Assistant-integration half is
still manual, and the glue between them is a shell prompt and me.

---

## Why bother at all

Home Assistant has no native 3D floorplan. The thing that produces a clickable one is
[home-assistant-floor-plan][plugin], a **Sweet Home 3D plugin**: it raytraces your model
once per light state, emits overlay images and a `picture-elements` card, and Home
Assistant swaps overlays as entity states change.

So you need a Sweet Home 3D model of your house, and everyone builds that by hand.

As of August 2026 there is no public tool that *writes* a `.sh3d`. There are readers
([sh3d.py][sh3dpy], FreeCAD's importer, [sh3dtoblender][blender]) and headless renderers
([SH3D-ConsolePhotoGenerator][console]), but nothing that generates one, and no published
phone-scan → Home Assistant workflow at all.

[plugin]: https://github.com/shmuelzon/home-assistant-floor-plan
[sh3dpy]: https://pypi.org/project/sh3d.py/
[blender]: https://github.com/lcgamboa/sh3dtoblender
[console]: https://github.com/AnimMouse/SH3D-ConsolePhotoGenerator

### Why not a WebGL floorplan card?

`floor3d-card` and Floorplan 3D render in real time, which means no raytracing and **no
cross-floor light spill**. If your house is a set of sealed boxes that may not matter. If
it has a stairwell, a double-height space, or open-plan living, it's the whole point.

That said, they are not mutually exclusive, and one model can drive both — a real-time card
is clickable and instant where the raytraced one is a set of pre-rendered overlays. Both
bind Home Assistant entities to objects **by name**, which is the only thing `export-glb`
has to get right. It emits `.obj` (what `floor3d-card` loads) and `.glb` (what the
better-maintained cards want) from the same `.sh3d`, with each object named after its
entity id.

---

## Capturing the house

This part is advice, not code, and it's the part I'm most confident about because getting
it wrong cost me the most time.

**Cover every mirror.** This matters more than anything else here. A scanner cannot tell a
reflection from a room, so it builds a phantom copy of the space *behind the wall*. In my
first capture a 2.2 m room produced a 5.55 m tall mesh in 882 disconnected pieces.

Then: open interior doors, turn the lights on, and accept that glass is invisible to LiDAR
— windows will simply be missing.

**Scan in Polycam, LiDAR mode, as one continuous capture** covering every level you want
modelled. Walk the stairs; don't stop and restart. The mesh is the world coordinate frame,
a second capture has a different and unrelated frame, and **geometry cannot be merged
across captures** — fitting one capture's plan onto another's gives 17 cm median and 80 cm
p90 disagreement, against 1–5 cm for a plan fitted to its own mesh.

**Then take a second, different capture of the same level: a fixture pass.** Turn on every
light — including the ones Home Assistant cannot control — and walk the level aiming the
phone at each fitting in turn. Geometry does not matter here and will be poor; that is the
trade. What you are recording is where the lights physically are, which no amount of
careful geometry scanning will tell you.

Two passes per level, serving different purposes, neither compromised for the other. See
[Lights](#lights-which-room-is-solved-where-in-the-room-is-not) for what happens to it —
including the human review step, which is not optional.

That is a limit on *merging*, not on rescanning. Rescanning is how this gets repaired:
cover the mirror you missed, point the phone up at the double-height space, walk the room
the first pass skipped. Captures accumulate and nothing is ever "the" scan. What combines,
and how, differs by kind of data:

- **Geometry is selected, not averaged.** The best-registered capture wins a room outright;
  blending two plans of one room blurs corners and squares nothing.
- **Textures composite.** Per-wall coverage runs 6–60% and the gaps fall in different
  places each pass, so pixels union. This is the biggest reason to rescan.
- **Detected features union.** A window found in any capture is a real window; one capture
  here found none where another found four in the same house.

So: one capture per region, many captures per project. Compositing needs the captures to
overlap heavily, which repeat scans of one space do and scans of adjacent spaces never do.

**Two separate exports** (the picker is single-select):

| | |
|---|---|
| Floor Plan → **Zip (all)** | DXF + CSV |
| Mesh → **OBJ** | the textured mesh |

In export settings: **Metric / Meters**, point density **High**, **Mesh up axis: Z**. The
last one matters — several stages assume the mesh shares the plan's ground plane.

Floor-plan export is a paid Polycam tier. Also, don't open a capture URL carrying an
`?invitation=` parameter; it crashes their web app. Strip it.

---

## Running it today

You need [uv](https://docs.astral.sh/uv/), [Sweet Home 3D](https://www.sweethome3d.com/),
the [floor-plan plugin][plugin], and a **JDK** 17+ ([Temurin](https://adoptium.net/) —
Sweet Home 3D bundles a runtime but no compiler). uv fetches the Python itself; the version
is pinned in `.python-version`.

```bash
git clone https://github.com/corrin/lidar2ha && cd lidar2ha
uv sync --all-extras
uv run lidar2ha doctor
```

`--all-extras` is not optional. `paramiko` and `websockets` are extras, and a bare
`uv sync` removes them again — taking `deploy` and the Home Assistant registry with it.

`doctor` finds Sweet Home 3D, the plugin and your JDK, then **compiles the Java against
your own installation** and reports the compiler's own errors. That last step is the point:
a version check that only looked at paths passed happily while the sources would not build.
It also says whether your installed packages still match `uv.lock`, which is the one way
this project goes wrong without anything looking wrong.

Each stage is a module you run by hand, inspecting the output before moving on. Run them
with `python -m` — they import their shared model from `lidar2ha.schema`, so running the
files by path fails on the relative import. Prefix them with `uv run` unless you have
activated `.venv` yourself. Paths below are illustrative; substitute your own.

```bash
# 1. floor plan -> intermediate model.  Add --role fixtures for a fixture pass:
#    its geometry is bad on purpose, and marking it keeps its walls and floor
#    heights out of the building.
#    A capture that walked more than one storey comes out as more than one
#    level: Polycam returns every storey on one sheet, so the model is cut on
#    ceiling height and each band becomes a level named for the height it sat
#    at -- `Floor 1 (210cm)`.  Those names are what step 5c declares.
python -m lidar2ha.polycam floorplan.dxf --csv rooms.csv -o home.json

# 2. what elevation is each floor at?
python -m lidar2ha.mesh mesh.obj

# 3. put the plan and the mesh in the same coordinate frame
python -m lidar2ha.registration home.json mesh.obj -o registered.json

# 4. one rectified photo per wall (or textures_tile.py for the cheap fallback)
python -m lidar2ha.textures_project registered.json mesh.obj -o walltex

# 5. give rooms their HA area names, merging open-plan splits
python -m lidar2ha.rooms registered.json project.yaml -o named.json --capture upstairs

# 5b. OPTIONAL, from a fixture pass: find the real fittings, put them in rooms,
#     and build the sheet you approve them against before anything is placed
python -m lidar2ha.fixtures fixture_mesh.obj -o fixtures.json --crops crops/
#     List every geometry capture the fixture pass walked through -- one pass
#     routinely spans two.  --daylight-mesh is an ORDINARY capture of the same
#     rooms: a window is bright in it too and a fitting is not, which separates
#     them mechanically.
python -m lidar2ha.placefixtures fixtures.json fixture_registered.json \
    ground_named.json hall_named.json \
    --daylight-mesh ground_mesh.obj -o fixtures_placed.json
python -m lidar2ha.contactsheet crops/ fixtures_placed.json -o sheet.png
#     Look at sheet.png. The likely windows are sorted to the bottom and
#     outlined, but the cutoff is a guess until you have read a sheet against
#     it -- and a candle or a mirror still looks exactly like a fitting.

# 5c. OPTIONAL, when a capture walked more than one storey: say which storey
#     of it belongs to which level.  `whichlevel` fits each of the capture's
#     levels onto the levels you have already combined and REFUSES rather than
#     naming a weak winner -- a capture of somewhere undeclared still produces
#     a least-bad row, and taking it is a confident wrong answer.
#     --write prints the project.yaml block to paste.  It leaves refusals out:
#     writing an unidentified storey down as a declaration would turn a refusal
#     into a fact.
lidar2ha whichlevel unknown_registered.json --project project.yaml --write

#     which prints a block to merge into `levels:` at the TOP level of
#     project.yaml -- it carries its own `levels:` key, so pasting it
#     underneath one nests the whole declaration where combine never looks:
#
#       levels:
#         "Upstairs":
#           - id: "unknown_geometry_0825-1649"
#             storeys: ["Floor 1 (710cm)", "Floor 3"]
#
#     merged with the entries already there, which stay as they are:
#
#       levels:
#         "Upstairs":
#           - upstairs_geometry_0823-1058          # a bare id still works
#           - id: "unknown_geometry_0825-1649"
#             storeys: ["Floor 1 (710cm)", "Floor 3"]
#
#     ALWAYS A LIST, even of one.  One capture can hold several storeys of the
#     SAME floor -- Polycam laid one walk of an upstairs across two sheet
#     clusters, and after the split two of its levels both belong to that floor
#     while holding different rooms.  Naming one storey per capture would have
#     discarded 23 m2 of it.

# 5d. OPTIONAL, when a level was scanned more than once: merge the captures.
#     Geometry is SELECTED, never averaged -- two plans of one room disagree by
#     ~17 cm and blending matches neither wall -- so the best-scoring capture
#     takes each room whole and the room records which one it came from.
#     The work list is the other half of the output: which rooms only a fixture
#     pass has ever seen, where the captures disagree about the layout, and any
#     floor a capture saw that the model does not contain.
python -m lidar2ha.combine midlevel_named.json midlevel_fixtures_named.json     -o midlevel_combined.json
lidar2ha combine "Mid Level" --project project.yaml   # same, via project.yaml

# 5e. OPTIONAL, and the one step no scan can do for you: cut the rooms an open
#     plan fuses.  There is no wall to segment on, so EVERY capture returns the
#     kitchen end and the dining end as one polygon and rescanning never
#     separates them -- the boundary is yours to declare.  Read coordinates off
#     the preview, which draws a metre grid labelled in centimetres for exactly
#     this, and write them into project.yaml under `split:`.
#     The pieces come back with NO ceiling: a fused room reports one height for
#     two spaces, which is the whole reason to split, so measure them after.
python -m lidar2ha.preview midlevel_combined.json -o plan.png     # where are the rooms?
python -m lidar2ha.floormap  scan7.obj -o floor                   # optional: the floor,
python -m lidar2ha.thresholds scan7.obj --axis y                  # photographed and swept
lidar2ha split "Mid Level" --project project.yaml --mesh scan7.obj
python -m lidar2ha.ceilings midlevel_split.json scan7.obj

# 6. read the HA registry and place every light.* entity in its room
lidar2ha lights named.json --refresh --project project.yaml -o lights.json \
    --fittings fixtures_placed.json   # omit to place at the pole instead

# 7. scene file -> .sh3d: compiles and runs the Java for you, then reopens the
#    result through Sweet Home 3D's own reader
lidar2ha build named.json -o house.sh3d \
    --walltex walltex/manifest.json --lights lights.json --elevation 'Upper=262'

# 8. raytrace it. --list first: free, and tells you what it will cost.
lidar2ha render house.sh3d -o render_out --project project.yaml --list
lidar2ha render house.sh3d -o render_out --project project.yaml --preview
lidar2ha render house.sh3d -o render_out --project project.yaml

# 9. copy it to Home Assistant. Writes nothing without --push.
lidar2ha deploy render_out --project project.yaml
lidar2ha deploy render_out --project project.yaml --push

# OPTIONAL. The same model as geometry, for a real-time 3D card rather than
# raytraced overlays. Each object is named after its entity id, and the count
# printed at the end is how many of those names survived the conversion --
# which is the only thing that makes the file useful.
lidar2ha export-glb house.sh3d -o house.glb
```

`lights` needs `HA_URL` and a long-lived access token in `HA_TOKEN` (environment or a
`.env`, both already gitignored) the first time; after that it works from a cached
`registry.json`, so the review loop runs offline.

`build` names any level whose elevation the mesh could not recover instead of quietly
defaulting it to zero, and refuses to report success on a `.sh3d` that will not reopen.

`render` and `export-glb` both go through `javabridge`, which locates Sweet Home 3D,
compiles into a per-user cache keyed by a hash over sources and jars, and knows which of the
**two JVMs** each program needs. Anything touching Java3D — the raytracer *and* the
geometry-only OBJ export — has to run on Sweet Home 3D's bundled 32-bit runtime, which has
no console, so both log to a file:

```python
from lidar2ha import javabridge
tc = javabridge.detect()
classes = javabridge.compile_java(tc)
javabridge.run_render(tc, classes, "render.log", "house.sh3d", "render_out")
javabridge.run_render(tc, classes, "obj.log", "house.sh3d", "house.obj",
                      main_class="ObjExport")
```

`export-glb` needs [obj2gltf](https://github.com/CesiumGS/obj2gltf) (`npm install -g
obj2gltf`); `doctor` reports whether it can find it.

`examples/minimal.tsv` is a complete four-wall, one-light scene — the smallest thing that
exercises the writer end to end, and the right place to start if you only care about
generating `.sh3d` files. `examples/twolevel.tsv` is the one that matters if you change the
writer: a single-level scene cannot catch a level-assignment bug, because with one level
every wrong answer is also the right one.

`deploy` writes nothing until you pass `--push`: the bare command connects read-only,
prints a manifest of what would change, and shows the card. The images must land at
`/config/www/floorplan/` because the plugin hard-codes `/local/floorplan/` into the card,
and the card and its images ship together -- each image is referenced with a `?version=`
hash of its own contents.

### Lights: which room is solved, where in the room is not

The plugin matches furniture **by `name == entity_id`** and sums multiple light sources
sharing a name — so a switch driving six bulbs is six placements carrying one name, and one
entity spanning three floors is three placements. `lights` does that join.

**Which room** it gets right, and the ordering matters: an entity's own area wins, and its
device's area is only a fallback. A multi-gang switch is one device on one wall driving
bulbs in different rooms, and an integration-native light group sits on whatever device its
coordinator lives in — so device-first files half a house's lights in the cupboard with the
USB stick.

**Where in the room** is a guess by default: the pole of inaccessibility, the point
furthest from any wall, with several lights in a room spread around it. Better than the
centroid, which for an L-shaped room can lie outside the room entirely — but still not
where your fittings actually are.

A **fixture pass** replaces that guess with a measurement. It is a second capture taken
deliberately differently: every light switched on, the phone aimed at each fitting,
geometry quality sacrificed on purpose. `fixtures` clusters bright faces in 3D,
`placefixtures` carries them into the geometry capture's rooms through both registrations,
and `contactsheet` crops each candidate out of the photograph it came from.

**That step needs a human, and not as a formality.** Brightness cannot separate a lit bulb
from a sunlit window — both saturate the sensor — so the detector finds windows. On one
real run it also found a candle burning on a desk. It will not stop doing this: the
limitation is in the physics, not the threshold.

So the output is a numbered contact sheet and the question *"which of these are real
fittings?"*. On that run, 24 candidates contained about 18 fittings and the extras were all
glass — obvious to a person, invisible to the detector. What it is genuinely better at than
a person is the tedious part: it found exactly one fitting in each of two 1.6 m² wardrobes
and placed each in the right cupboard.

**One discriminator removes the windows mechanically**, and it is not a cleverer threshold —
it is a second capture. A window is bright in *every* capture, a fitting only when switched
on, so differencing a fixture pass against an ordinary capture of the same rooms isolates
the fittings. Pass `--daylight-mesh` to `placefixtures`; the ordinary capture is the one the
geometry came from, so it already exists for every level with a fixture pass.

There are **three** answers, not two, and that is the part worth knowing. An ordinary
capture photographs ceilings badly — the camera meters for the room, and nobody points a
phone at a dark ceiling — so "nothing there to compare with" is common and means the scan
never looked. It is reported as `unseen`, and it is not evidence either way. Only a positive
`window` is refused, and `lights` counts those in its report rather than dropping them
quietly. The contact sheet sorts the likely windows to the bottom and outlines them, so
review runs top-down and can stop early: that is the point, because a ground-level pass
produced 38 candidates for perhaps 12–14 real fittings and reading all 38 costs more than
the detection saves.

Human review is still the filter. The cutoff is a guess like everything else here, and a
candle or a mirror reflecting a lit fitting is bright in exactly one capture — which is what
a real fitting looks like.

Expect far more fittings than entities. The upstairs of the house this was built for has
roughly 18 fittings and 5 `light.*` entities; the rest are dumb switches. Fittings with no
entity are **reported, never invented** — placing an uncontrollable light would render
prettily and respond to nothing.

**Not every `light.*` entity is a light.** Status LEDs, indicator rings, and controllers
exposing sound channels in the light domain all turn up. They are placed and flagged rather
than dropped, and you exclude them in `project.yaml` — a real fitting that merely looks
like an indicator would otherwise vanish and leave a room dark for no visible reason.

**And not every `light.*` entity is one light.** A group and its members are the same bulbs
counted twice, and because the plugin *sums* sources sharing a name the result is not an
error — it is a room that renders quietly too bright. Groups are found three ways, and the
report always says which found what, because the coverage differs. Home Assistant's own
group helper lists its members, so those are exact. **ZHA hangs a Zigbee group's entity off
the coordinator device** — the radio, not a lamp — and nothing else in the light domain
lives there, which makes that a mechanical test: on this house it finds four groups where
the name heuristic finds one of the same four. Hue rooms and deCONZ groups expose neither,
and are still flagged by name for you to judge. Anything skipped is named, and
`lights.include` puts it back.

Room identity is solved in three pieces, and only the first is automatic. Scanner room names
are guesses and its splits are artefacts — mine confidently labelled an entrance hall "Living
Room" and "Dining Room", and returned one open kitchen as "Kitchen" plus "Office 1".
`rooms.py` renames scanner rooms to HA `area_id`s and unions named groups into one polygon,
dissolving the shared edge rather than taking a bounding box. `seams.py` does the opposite
where the fusion is real: an open plan has no wall to segment on, so **every** capture fuses
the kitchen end and the dining end and no amount of rescanning separates them. That boundary
is a declaration about how you use the house, and it lives in `project.yaml` under `split:` —
either a `seam:` of two points, or a traced `sections:` outline per room when a straight line
will not do. What none of it does is read the area registry: you write the mapping yourself.

Where a boundary *is* built rather than used — a step, a change from wood to carpet — the mesh
can corroborate it, and `split --mesh` says so per boundary. It never moves the line and never
refuses one. The boundary between a sofa end and a table end is real and the floor beneath it
is continuous, so an unsupported boundary is not a wrong boundary. Three answers, not two:
corroborated, declared, or never photographed.

---

## Known limits

- **Registration is the weak point.** A wall-poor open-plan level gives the fitter little
  to hold onto. `registration.py` reports how well-constrained each fit is, and you should
  read that number rather than trusting the result. `textures_tile.py` exists precisely
  because registration wasn't trustworthy enough on my first capture to project photos.
- **Texture detail is capped by the scan.** Polycam's atlas carries roughly 384 px per
  metre of real surface, measured. No export setting produces more; it's what the camera
  saw.
- **Windows are missing.** LiDAR passes through glass. Add them by hand in Sweet Home 3D.
- **Scan every level at least three times.** Captures of *adjacent* spaces share no frame
  and cannot be merged at all; captures of the *same* level can, and `combine` selects the
  best source for each room rather than averaging — two plans of one room disagree by ~17 cm
  and a blend matches neither wall. But a capture that does not overlay is **discarded**,
  not folded in with a caveat, and with only two captures nothing can say which of them is
  at fault. A third identifies the odd one out immediately. On this house that mattered: the
  capture the whole mid-level model had been built from turned out to be the worst of its
  three, and only the third scan revealed it. When you go back to rescan a room, rescan a
  room you already have as well, so the new capture has common ground to overlay onto.
  What `combine` does not yet do is composite textures across captures, which is the case
  for rescanning that pays best. A single fixture pass may span several geometry captures;
  `placefixtures` takes as many models as you give it and sends each fitting to whichever
  one contains it.
- **A capture is only as good as the one you compare it against.** Every per-capture
  error figure is measured against a chosen reference, so a poor reference charges its
  own error to everything else — on one level here the anchor turned out to be the worst
  of three captures, and every number reported against it was wrong in the same
  direction. `combine` also reports how well each capture fits onto *the others*, which
  is the figure that cannot flatter itself, and names the outlier. Read that one.
- **Nothing yet catches a scan placed on the wrong walls.** A small capture fits inside a
  large one wherever you put it, so every point finds a nearby point — just the wrong one. A
  five-wall bedroom landed 65° out on top of a hallway at 100% coverage and 18.9 cm median,
  and no error figure or quantile can see that, because arithmetically nothing is wrong.
  Today's bound discards it for being far out, and `combine` warns that a small capture's
  coverage is unreadable, but neither is the real answer. The real answer is that every
  capture of one building shares a wall grid, so only four rotations between two captures
  are ever valid — that check is not built yet.
- **Voids** — stairwell shafts, double-height spaces — have to be declared by hand, because
  a scanner maps rooms, not the empty space between them.
- **Tuned to one house, one app.** Anything here that looks like a constant is a guess that
  happened to work once.

---

## The thing that will waste your afternoon

If you're here to write `.sh3d` files yourself, read this first. This is the part of the
repo I'd stand behind regardless of the state of everything else.

A `.sh3d` is a ZIP. Since Sweet Home 3D 5.3 it contains a `Home.xml` conforming to
[`SweetHome3D.dtd`][dtd], and the documentation describes that XML as the modern format. So
the obvious move is to write XML, zip it, done.

**Sweet Home 3D will refuse to open it** — *"can't open home"*.

Dump an archive Sweet Home 3D wrote itself:

```console
$ python -c "import zipfile; print(zipfile.ZipFile('saved.sh3d').namelist())"
['Home']
```

One entry. `Home`, containing **Java-serialised objects**. No `Home.xml` at all. The desktop
app reads the serialised entry; the XML is there for other tools to consume.

Java serialisation of Sweet Home 3D's classes can only be produced by those classes. So a
generator has to go *through* Sweet Home 3D, not around it — which is why there is Java in
this repo, and why it compiles against your own installation.

[dtd]: http://www.sweethome3d.com/SweetHome3D.dtd

### Other things that cost a day each

- **`addWall()` / `addRoom()` / `addPieceOfFurniture()` overwrite the object's level** with
  the home's *selected* level. Call `setLevel()` **after** adding, or everything silently
  lands on `level=null` and your plan opens empty.
- **Rooms are not walls.** Four walls enclosing a space do not make a `Room`, and the plugin
  groups lights by `Room`. Room polygons need no enclosing walls, so open-plan works: emit
  the polygon, emit no wall along the open edge.
- **Pick the catalog light by ID.** The alphabetically first `Light` is
  `eTeks#blueLightSource`, and the catalog's `*LightSource` entries are invisible emitters
  whose model is line geometry — they light the scene but render as nothing. Use
  `eTeks#pendantLamp`. `Furniture.jar` must be on the classpath.
- **Light sources must be visible with power > 0** or the plugin ignores them.
- **The plugin only sees lights on the SELECTED level.** Not the viewable ones, not all of
  them — `setViewable(true)` and `setAllLevelsSelection(true)` both make no difference. A
  two-storey house therefore renders with one floor's lights and no cross-floor spill,
  which is the entire reason for using a raytracer. The way through: a light's elevation is
  measured from its own level's floor, so emitting every light against the *lowest* level
  with its own level's elevation added leaves it in the identical place in space and puts
  all of them in the render set. Geometry stays on its proper level; only the lights move.
- **Yaw looks along `(sin yaw, cos yaw)`,** so `yaw = 0` faces *increasing* y. Place the
  camera on the far side of the plan, leave the yaw at zero, and every render comes back a
  uniform white frame — a picture of the sky, produced at full raytracing cost without a
  single warning.
- **The field of view is horizontal.** So on a 16:9 render the *vertical* angle is the
  narrow one, and that is the axis a model gets clipped on. Framing by a bounding sphere
  and a safety multiplier does not account for this: it wastes half the width on a long
  house and clips the height anyway.
- **Rendering needs Sweet Home 3D's own 32-bit JVM.** Java3D and YafaRay ship as 32-bit
  natives, and that runtime has only `javaw.exe` — no console — so the render step logs to
  a file. Writing a `.sh3d` runs fine on a normal JDK. Two JVMs; `javabridge.py` exists to
  keep that fact in one place.
- **So does exporting geometry, which traces nothing.** `ObjExport` only walks the model
  and writes OBJ, but building the scene graph goes through `Object3DBranchFactory`, which
  loads Java3D's natives regardless. On a 64-bit JDK it dies with `Can't load IA 32-bit
  .dll on a AMD 64-bit platform`. "It doesn't render, so it can use the normal JVM" is
  wrong, and the error names a DLL rather than the reason.
- **`-Djava.awt.headless=true` breaks the headless export.** It is the obvious flag for a
  command-line tool with no window, and it is exactly backwards: `VirtualUniverse`'s static
  initialiser wants a display, so setting it throws `HeadlessException` during class
  loading — before `main()`, and so before any handler that would have logged it.
- **Converting OBJ→glTF with trimesh silently discards every object name.** trimesh keys a
  scene by *material*, so six lights that share the `white` material come back as one node
  called `white`. `ObjExport` exists for one reason — to name each group after its entity
  id, because a 3D card binds entities to objects by name — and the conversion that looks
  free destroys precisely that. The resulting GLB is valid, opens fine, and binds nothing.
  `obj2gltf` preserves the names; `glb.py` counts them going in and coming out, every time,
  because the failure is invisible until the card does nothing.
- **`javac` and `java` must come from the same JDK.** Resolving each off `PATH`
  independently gets you a modern compiler and whatever stale JRE is earlier in the path —
  on Windows, typically Oracle's `java8path` shim — and it fails at the point of use with
  `UnsupportedClassVersionError` naming neither.
- **Java3D is not in `SweetHome3D.jar`.** `j3dcore`, `j3dutils` and `vecmath` are separate
  jars beside it, so anything touching `javax.media.j3d` needs the whole lib directory on
  the classpath, not just the main jar.
- **A UTF-8 BOM breaks both sides silently-ish.** In a `.java` source `javac` reports
  `illegal character: '﻿'`; in a scene file it makes the first record `﻿home`,
  which surfaces as the unhelpful "unknown record type". Windows editors add one unasked.
- **Ceiling height is a property of the room, not the level.** One capture has a 2.2 m
  laundry beside a 3.2–4.7 m double-height space; one number per level throws away exactly
  the geometry that makes cross-floor light spill worth rendering.
- **An entity's area is not its device's area.** Home Assistant lets you set the area per
  entity, and that override is how a user says "this gang lights the pantry, that one
  lights the deck". Resolve device-first and every integration-native light group lands
  wherever its coordinator happens to be plugged in.
- **Units are centimetres.** Most scanning apps export millimetres or metres.
- **The plugin's "use existing renders"** reprocesses without re-rendering. It regenerates
  the floor plan and YAML only, so what it lets you change is the dashboard layer -- display
  type, icon, tap action, sensitivity. Not the lighting: that is baked into the frames.
- **One setting changes the render count by five orders of magnitude.** The light mixing
  mode decides how many images get made, and nothing warns you. On one 21-light house at
  640x360, same model and same size:

  | mixing | what it renders | frames | time |
  |---|---|---|---|
  | `CSS` | one per light; the browser adds them | 22 | 5 min |
  | `OVERLAY` | every combination of each room's lights | 65,541 | 9 days |
  | `FULL` | every combination in the house | 2,097,152 | 10 months |

  `getNumberOfTotalRenders()` knows before a pixel is traced, which is why
  `lidar2ha render --list` is free and always worth running first.
- **`Quality.LOW` does not raytrace.** It screenshots the Java3D OpenGL view, and with no
  usable GL context it returns a *blank frame* in about a second without erroring. Seven
  perfectly-generated blank PNGs cost someone an hour here.
- **Rendering is slow and the machine barely matters.** It runs single-process on Sweet
  Home 3D's bundled 32-bit Java 8 runtime, because Java3D and YafaRay are 32-bit natives.
  Measured: 179.7 s for 7 frames at 800x600 on a real model, about 26 s a frame. Scene
  complexity counts as much as pixels -- a near-empty test scene managed 6.5 s a frame.

---

## Contributing

If you try this on a second house I'd genuinely like to hear what broke — that's the single
most useful thing anyone could do with it, and I have no way to find out on my own.

```bash
uv run pytest -q                     # the java-marked tests skip without Sweet Home 3D
uv run ruff check .
uv run mypy                          # clean on src/lidar2ha, and it stays that way
uv run lidar2ha doctor               # the only thing that compiles the Java
```

`uv.lock` and `.python-version` are committed, so everyone resolves the same packages.
Change a dependency with `uv add` or `uv lock --upgrade-package <name>` and commit the
lock in the same commit as the `pyproject.toml` change — CI installs with
`uv sync --locked` and fails when the two have drifted apart.

Contributor tooling lives in a `[dependency-groups]` entry rather than an extra, so it
never reaches the published wheel. uv installs it by default; there is no `[dev]` to ask
for.

## License

MIT — see [LICENSE](LICENSE).

Sweet Home 3D is © eTeks, under the GNU GPL. This project does not bundle or modify it; it
compiles against a local installation. Check your own obligations if you redistribute a
combined work.
