# scan2ha

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
| Headless raytraced render + `floorplan.yaml` | works (`HeadlessRender.java`) |
| Rename rooms to HA areas, merge open-plan splits | works (`rooms.py`), mapping written by hand |
| `scan2ha doctor` and `scan2ha build` | works (`cli.py`) |
| **Reading your HA area/entity registry** | **does not exist** |
| **Placing lights automatically** | **does not exist** |
| **Seam/seed open-plan splitting** | **does not exist** — `rooms.py` merges named rooms instead |
| **`scan2ha add-capture` / `lights` / `render` / `deploy`** | **does not exist** (they exit saying so) |

`pip install .` now gives you a working `scan2ha` with `doctor` and `build`; the other four
subcommands are registered so `--help` matches this table, and each exits telling you what
to run instead. The package is not on PyPI. The remaining stages are still scripts you run
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
modelled. Walk the stairs; don't stop and restart. This is a hard requirement, not a
preference: the mesh is the world coordinate frame, a second capture has a different and
unrelated frame, and nothing here can merge them.

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

You need Python 3.11+, [Sweet Home 3D](https://www.sweethome3d.com/), the
[floor-plan plugin][plugin], and a **JDK** 17+ ([Temurin](https://adoptium.net/) — Sweet
Home 3D bundles a runtime but no compiler).

```bash
git clone https://github.com/<you>/scan2ha && cd scan2ha
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
scan2ha doctor
```

`doctor` finds Sweet Home 3D, the plugin and your JDK, then **compiles the Java against
your own installation** and reports the compiler's own errors. That last step is the point:
a version check that only looked at paths passed happily while the sources would not build.

Each stage is a module you run by hand, inspecting the output before moving on. Run them
with `python -m` — they import their shared model from `scan2ha.schema`, so running the
files by path fails on the relative import. Paths below are illustrative; substitute your
own.

```bash
# 1. floor plan -> intermediate model
python -m scan2ha.polycam floorplan.dxf --csv rooms.csv -o home.json

# 2. what elevation is each floor at?
python -m scan2ha.mesh mesh.obj

# 3. put the plan and the mesh in the same coordinate frame
python -m scan2ha.registration home.json mesh.obj -o registered.json

# 4. one rectified photo per wall (or textures_tile.py for the cheap fallback)
python -m scan2ha.textures_project registered.json mesh.obj -o walltex

# 5. give rooms their HA area names, merging open-plan splits
python -m scan2ha.rooms registered.json project.yaml -o named.json --capture midlevel

# 6. scene file -> .sh3d: compiles and runs the Java for you, then reopens the
#    result through Sweet Home 3D's own reader
scan2ha build named.json -o house.sh3d \
    --walltex walltex/manifest.json --lights lights.json --elevation 'Upper=262'
```

`build` names any level whose elevation the mesh could not recover instead of quietly
defaulting it to zero, and refuses to report success on a `.sh3d` that will not reopen.

Rendering has no subcommand yet, so drive it through `javabridge`, which locates Sweet Home
3D, compiles into a per-user cache keyed by a hash over sources and jars, and knows which of
the **two JVMs** each program needs:

```python
from scan2ha import javabridge
tc = javabridge.detect()
classes = javabridge.compile_java(tc)
javabridge.run_render(tc, classes, "render.log", "house.sh3d", "render_out")
```

`examples/minimal.tsv` is a complete four-wall, one-light scene — the smallest thing that
exercises the writer end to end, and the right place to start if you only care about
generating `.sh3d` files. `examples/twolevel.tsv` is the one that matters if you change the
writer: a single-level scene cannot catch a level-assignment bug, because with one level
every wrong answer is also the right one.

At this point `render_out/` holds the overlay images and a generated `floorplan.yaml`. You
copy those to `/config/www/` yourself and paste the card in yourself. That's the missing
`deploy`.

### Lights, and why they aren't automatic yet

The plugin matches furniture **by `name == entity_id`** and sums multiple light sources
sharing a name — so a switch driving six bulbs is six placements carrying one name. Right
now you write those `light` lines into the scene file yourself (see `examples/minimal.tsv`)
or place them in Sweet Home 3D. Reading them out of your HA area registry and positioning
them automatically is the obvious next thing to build, and is not built.

Room identity is half-solved. Scanner room names are guesses and its splits are artefacts —
mine confidently labelled an entrance hall "Living Room" and "Dining Room", and returned one
open kitchen as "Kitchen" plus "Office 1". `rooms.py` fixes both: it renames scanner rooms
to HA `area_id`s and unions named groups into one polygon, dissolving the shared edge rather
than taking a bounding box. What it does *not* do is read the area registry — you write the
mapping into `project.yaml` yourself — nor cut a room the scanner failed to split at all,
which is what the seam/seed idea was for.

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
- **One capture per project.** See above.
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
- **Rendering needs Sweet Home 3D's own 32-bit JVM.** Java3D and YafaRay ship as 32-bit
  natives, and that runtime has only `javaw.exe` — no console — so the render step logs to
  a file. Writing a `.sh3d` runs fine on a normal JDK. Two JVMs; `javabridge.py` exists to
  keep that fact in one place.
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
- **Units are centimetres.** Most scanning apps export millimetres or metres.
- **The plugin's "use existing renders"** reprocesses without re-rendering, so iterating on
  layout and YAML after the first pass is free.

---

## Contributing

If you try this on a second house I'd genuinely like to hear what broke — that's the single
most useful thing anyone could do with it, and I have no way to find out on my own.

## License

MIT — see [LICENSE](LICENSE).

Sweet Home 3D is © eTeks, under the GNU GPL. This project does not bundle or modify it; it
compiles against a local installation. Check your own obligations if you redistribute a
combined work.
