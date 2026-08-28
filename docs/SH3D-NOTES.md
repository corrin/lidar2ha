# Writing .sh3d files

Notes from building a generator for Sweet Home 3D's file format, and for the
[home-assistant-floor-plan plugin][plugin] that renders from it.

You do not need the rest of this repo to use these.

Prior art, all of it read-side: [sh3d.py][sh3dpy], FreeCAD's importer and
[sh3dtoblender][blender] parse `.sh3d`; [SH3D-ConsolePhotoGenerator][console]
renders one headlessly. I know of nothing else that writes one.

[plugin]: https://github.com/shmuelzon/home-assistant-floor-plan
[sh3dpy]: https://pypi.org/project/sh3d.py/
[blender]: https://github.com/lcgamboa/sh3dtoblender
[console]: https://github.com/AnimMouse/SH3D-ConsolePhotoGenerator

---

## The format itself

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

## Things that cost a day each

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
