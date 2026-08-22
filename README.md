# scan2ha

**Scan your house with a phone. Get an interactive 3D floorplan in Home Assistant.**

Tap a light on your dashboard and watch the room actually illuminate — raytraced,
with light spilling through stairwells and across open-plan volumes.

> **Status: early.** The pipeline works end to end, but it has been exercised on one
> house. Expect sharp edges, and read [Known limits](#known-limits) before starting.

No hand-drawn walls. You hold the phone; the tool builds the model.

---

## Why this exists

Home Assistant has no native 3D floorplan. The thing that produces a clickable one is
[home-assistant-floor-plan][plugin], which is a **Sweet Home 3D plugin**: it raytraces
your model once per light state, emits overlay images and a `picture-elements` card,
and Home Assistant swaps overlays as entity states change.

So you need a Sweet Home 3D model of your house. Everyone builds that by hand.

As of August 2026 there is no public tool that *writes* a `.sh3d` — there are readers
([sh3d.py][sh3dpy], FreeCAD's importer, [sh3dtoblender][blender]) and headless
renderers ([SH3D-ConsolePhotoGenerator][console]), but nothing that generates one. And
there is no published phone-scan → Home Assistant workflow at all.

This is that missing piece, plus everything either side of it.

[plugin]: https://github.com/shmuelzon/home-assistant-floor-plan
[sh3dpy]: https://pypi.org/project/sh3d.py/
[blender]: https://github.com/lcgamboa/sh3dtoblender
[console]: https://github.com/AnimMouse/SH3D-ConsolePhotoGenerator

### Why not a WebGL floorplan card?

`floor3d-card` and Floorplan 3D render in real time, which means no raytracing and
**no cross-floor light spill**. If your house is a set of sealed boxes that may not
matter. If it has a stairwell, a double-height space, or open-plan living, it is the
whole point.

---

## From phone to dashboard

Steps marked *(human)* need you. Everything else runs unattended.

### 1. Prepare the house — *(human, 10 min)*

- **Cover every mirror.** This matters more than anything else here. A scanner cannot
  tell a reflection from a room, so it builds a phantom copy of the space *behind the
  wall*. In our test capture a 2.2 m room produced a 5.55 m tall mesh in 882
  disconnected pieces.
- Open interior doors, turn the lights on.
- Glass is invisible to LiDAR. Windows will be missing and get added by hand later.

### 2. Scan — *(human, ~20 min)*

Polycam, LiDAR mode. **One continuous capture covering every level you want modelled**
— walk the stairs, don't stop and restart.

This is a hard requirement, not advice. The mesh is the world coordinate frame; a
second capture has a different, unrelated frame, and nothing can reliably merge them.

### 3. Export — *(human, 5 min)*

Two separate exports (the picker is single-select):

| | |
|---|---|
| Floor Plan → **Zip (all)** | DXF + CSV |
| Mesh → **OBJ** | the textured mesh |

In export settings: **Metric / Meters**, point density **High**, and **Mesh up axis:
Z** — the last one matters, several stages assume the mesh shares the plan's ground
plane.

> Floor-plan exports are a paid Polycam tier. Also: don't open a capture URL with an
> `?invitation=` parameter — it crashes their web app. Strip it.

### 4. Install — *(human, once)*

```bash
pipx install scan2ha
```

You also need [Sweet Home 3D](https://www.sweethome3d.com/), the
[floor-plan plugin][plugin], and a **JDK** 17+ ([Temurin](https://adoptium.net/) —
Sweet Home 3D bundles a runtime but no compiler). Then:

```bash
scan2ha doctor
```

which tells you precisely what is missing and where to get it.

### 5. Build the model

```bash
scan2ha init myhouse
scan2ha add-capture house --floorplan floorplan.zip --mesh mesh.zip
scan2ha build
```

This imports the DXF, registers the plan against the mesh, recovers floor elevations,
rectifies wall textures from the photo atlas, and writes a verified `.sh3d`.

It then **stops and asks you to check it**, showing an overlay of the plan on the
scan. See [The human's job](#the-humans-job).

### 6. Name your rooms — *(human, 15 min, once)*

Scanner room names are guesses and its room splits are artefacts — ours confidently
labelled an entrance hall "Living Room" and "Dining Room". Identity comes from **your
Home Assistant area registry** instead.

For closed rooms this is automatic. For open-plan volumes, cut the floor plate with a
couple of **seams** and drop a **seed** in each region naming its HA area:

```yaml
areas:
  Ground:
    seams:
      - [[1240, 60], [1240, 610]]
    seeds:
      kitchen: [1420, 300]
      dining:  [1000, 800]
```

Coordinates are read off the preview SVG. Two to four seams usually covers a house.
Because seeds are typed HA `area_id`s, no name-matching happens anywhere.

### 7. Place the lights

```bash
scan2ha lights --refresh --report
```

Reads your HA registry and puts every `light.*` entity in its area. The plugin matches
objects **by `name == entity_id`** and sums multiple light sources sharing a name — so
a switch driving six bulbs is six placements with one name.

Fix what it got wrong with room-relative overrides (pendant drops, multi-bulb
switches, entities whose area is where the *switch* is rather than the fitting).

### 8. Render and deploy

```bash
scan2ha render --preview   # cheap draft: is the framing right?
scan2ha render
scan2ha deploy
```

Rendering is fully headless. `deploy` pushes the renders and generated
`floorplan.yaml` to `/config/www/` over SSH and prints the dashboard card to paste in.

Tap a light on your phone.

---

## The human's job

**Automation proposes; you validate.** Not "you do the fiddly bits" — every stage runs
unattended, then shows you something and asks one question.

| Stage | You see | You're asked |
|---|---|---|
| `import` | plan SVG per fragment | "is each fragment on the right level?" |
| `register` | plan overlaid on the scan | "does the outline sit on the scan?" |
| `areas` | coloured faces + names | "are these boundaries where *you* would draw them?" |
| `lights` | plan with bulb positions | "is each light in the right room?" |
| `textures` | contact sheet of wall images | "any wall showing obvious garbage?" |
| `render` | draft frame, then all renders | "does this look right?" |
| `deploy` | dry-run manifest | "shall I push this?" |

Approvals are recorded against a hash of each stage's *inputs*, so you are never asked
twice — but changing a seam reopens that question and everything downstream.

---

## Known limits

- **Texture detail is capped by the scan.** Polycam's atlas carries roughly 384 px per
  metre of real surface. No export setting produces more; it is what the camera saw.
- **Windows are missing.** LiDAR passes through glass. Add them by hand in Sweet Home
  3D if you want them.
- **Open-plan registration is the weak point.** A wall-poor level gives the fitter
  little to hold onto. `scan2ha` grades how well-constrained each fit is and refuses to
  claim success on a degenerate one, but you may need to supply two anchor points.
- **One capture per project.** See step 2.
- Voids — stairwell shafts, double-height spaces — are declared in config, because a
  scanner maps rooms, not the empty space between them.

---

## The thing that will waste your afternoon

If you are here to write `.sh3d` files yourself, read this first.

A `.sh3d` is a ZIP. Since Sweet Home 3D 5.3 it contains a `Home.xml` conforming to
[`SweetHome3D.dtd`][dtd], and the documentation describes that XML as the modern
format. So the obvious move is to write XML, zip it, done.

**Sweet Home 3D will refuse to open it** — *"can't open home"*.

Dump an archive Sweet Home 3D wrote itself:

```console
$ python -c "import zipfile; print(zipfile.ZipFile('saved.sh3d').namelist())"
['Home']
```

One entry. `Home`, containing **Java-serialised objects**. No `Home.xml` at all. The
desktop app reads the serialised entry; the XML exists for other tools to consume.

Java serialisation of Sweet Home 3D's classes can only be produced by those classes.
So a generator has to go *through* Sweet Home 3D, not around it — which is why there
is Java in this repo, and why it compiles against your own installation.

[dtd]: http://www.sweethome3d.com/SweetHome3D.dtd

### Other things that cost us a day each

- **`addWall()` / `addRoom()` / `addPieceOfFurniture()` overwrite the object's level**
  with the home's *selected* level. Call `setLevel()` **after** adding, or everything
  silently lands on `level=null` and your plan opens empty.
- **Rooms are not walls.** Four walls enclosing a space do not make a `Room`, and the
  plugin groups lights by `Room`. Room polygons need no enclosing walls, so open-plan
  works: emit the polygon, emit no wall along the open edge.
- **Pick the catalog light by ID.** The alphabetically first `Light` is
  `eTeks#blueLightSource`, and the catalog's `*LightSource` entries are invisible
  emitters whose model is line geometry — they light the scene but render as nothing.
  Use `eTeks#pendantLamp`. `Furniture.jar` must be on the classpath.
- **Light sources must be visible with power > 0** or the plugin ignores them.
- **Rendering needs Sweet Home 3D's own 32-bit JVM.** Java3D and YafaRay ship as
  32-bit natives, and that runtime has only `javaw.exe` — no console — so the render
  step logs to a file. Writing a `.sh3d` runs fine on a normal JDK. Two JVMs.
- **Units are centimetres.** Most scanning apps export millimetres or metres.
- **The plugin's "use existing renders"** reprocesses without re-rendering, so
  iterating on layout and YAML after the first pass is free.

---

## License

MIT — see [LICENSE](LICENSE).

Sweet Home 3D is © eTeks, under the GNU GPL. This project does not bundle or modify
it; it compiles against a local installation. Check your own obligations if you
redistribute a combined work.
