# lidar2ha

Turn a phone LiDAR scan of your house into a 3D floorplan in Home Assistant that
lights up when you tap a light. It's raytraced, so light spills through
stairwells and across open plan volumes.

The raytracing and the dashboard card are [home-assistant-floor-plan][plugin]'s,
and it does that part well. What it needs from you is a Sweet Home 3D model of
your house, and everybody builds that by hand. That is the bit that stops people,
and it's the bit this does.

As far as I can find, nothing else writes a `.sh3d` at all. There are readers and
a headless renderer, listed in [docs/SH3D-NOTES.md](docs/SH3D-NOTES.md), but no
generator, and no published phone-scan-to-Home-Assistant workflow.

Whether it's worth it depends on your house. Real-time cards like `floor3d-card`
render without raytracing, so they can't do cross-floor light spill, and that
spill is most of the reason to bother. If you've got a stairwell, a double-height
space or open plan living, this is for you. If your house is a set of sealed
boxes, use one of those and save yourself a fortnight.

I built it because I wanted my house in Home Assistant and I didn't want to draw
it by hand. I've only ever run it on my own house, so every threshold in it is a
guess that happened to work once. Claude wrote essentially all of the code, and I
supplied the house, the scans, and the judgement about whether each output was
actually right.

## What you need

1. An iPhone with LiDAR (12 Pro or later), or an iPad Pro (2020 or later)
2. Polycam, on a tier that exports floor plans
3. Home Assistant, with SSH to `/config` ([Terminal & SSH add-on][ssh])
4. A long-lived access token from an admin account ([your profile][token])
5. [Sweet Home 3D][sh3d] and the [floor-plan plugin][plugin]
6. A JDK 17 or later ([Temurin][temurin])
7. Three scans of each storey, plus a fixture pass

On (2): floor plan export runs about $1,000/year, so I'm assuming you're on the
7 day trial like a normal person.

That's worth planning around. Everything that needs Polycam, scanning and
exporting both, has to happen inside those seven days. Everything after runs
offline forever. So get the toolchain working first, then start the clock.

[ssh]: https://github.com/home-assistant/addons/blob/master/ssh/DOCS.md
[token]: https://www.home-assistant.io/docs/authentication/#your-account-profile
[sh3d]: https://www.sweethome3d.com/
[plugin]: https://github.com/shmuelzon/home-assistant-floor-plan
[temurin]: https://adoptium.net/

## What works

| Stage | State |
|---|---|
| Parse Polycam floor-plan DXF/CSV into a JSON model | works (`polycam.py`) |
| Recover floor elevations from the mesh | works (`mesh.py`) |
| Register DXF floors onto the mesh | works, and it's the weak link. See limits |
| Rectify per-wall textures from the photo atlas | works (`textures_project.py`) |
| Tiled textures by surface class (fallback) | works (`textures_tile.py`) |
| Write a real `.sh3d` | works (`Sh3dWriter.java`) |
| Headless raytraced render + `floorplan.yaml` | works (`HeadlessRender.java`), all levels in one pass |
| Frame the camera so the house fits | works (`camera.py`), solved rather than guessed |
| Rename rooms to HA areas, merge open-plan splits | works (`rooms.py`), mapping written by hand |
| Merge several captures of one level | works (`combine.py`), align-or-discard, wants 3+ scans |
| Read your HA area/entity registry | works (`ha.py`), over the WebSocket API |
| Place every `light.*` entity in its room | works (`lights.py`), positions are a guess |
| Find real fittings in the scan | works (`fixtures.py`, `placefixtures.py`), needs a human |
| Separate windows from fittings mechanically | works (`daylight.py`), differences two captures |
| Review sheet for the fittings found | works (`contactsheet.py`), windows sorted last |
| Export a named GLB for a real-time 3D card | works (`ObjExport.java`, `glb.py`) |
| Cut an open-plan room into the rooms it's used as | works (`seams.py`), boundary declared by you |
| Corroborate a declared boundary against the floor | works (`thresholds.py`), reports, never decides |
| A demo house, so you can run it all before scanning | works (`demo.py`) |
| `lidar2ha add-capture` | **not implemented** (exits saying so) |

Honest take: the geometry and rendering half is solid. The Home Assistant half
works but is manual. You write the area mapping, you review the fittings, and
you paste the card into your dashboard yourself.

You don't have to pick between this and a real-time card, by the way.
`export-glb` emits `.obj` and `.glb` from the same `.sh3d`, each object named
after its entity id, so one model can drive both.

## Install

```bash
git clone https://github.com/corrin/lidar2ha && cd lidar2ha
uv sync --all-extras
uv run lidar2ha doctor
```

Use `--all-extras`. `paramiko` and `websockets` are extras, so a bare `uv sync`
takes them out again and you lose `deploy` and the Home Assistant registry.

`doctor` finds Sweet Home 3D, the plugin and your JDK, then compiles the Java
against your own installation and shows you the compiler's own errors. It also
checks your installed packages against `uv.lock`.

Then run the whole thing on a house that doesn't exist:

```bash
uv run lidar2ha demo ~/demo-house
```

Eight captures of a two-storey building with a stairwell, packaged the way
Polycam packages yours. One of the eight is wrong about where the kitchen is, so
`combine` has something to catch. Do this before you pay Polycam anything.

## How it goes

It's a loop. Scan a room, run it through, put it on your dashboard, and look at
it. What you see tells you what to do next: a name in `project.yaml`, a boundary
you need to declare, or another scan.

Two questions run through every lap:

1. Is this a bad scan? Then rescan.
2. Is this something true about the house that nothing has been told? Then write
   it in `project.yaml`.

`project.yaml` is where you tell it the things it can't work out on its own.
Mostly that's which scanner room is which Home Assistant area. It grows as you
go.

Worth knowing up front: Home Assistant only ever receives pre-rendered overlay
images and a `picture-elements` card, which `deploy` copies to
`/config/www/floorplan/`. The model itself stays on your desktop.

**[docs/TUTORIAL.md](docs/TUTORIAL.md)** walks the loop from an empty directory
to a lit dashboard, with what you should see at each step and what it looks like
when it's wrong.

## Known limits

- **Registration is the weak point.** A wall-poor open-plan level gives the fitter
  very little to hold onto. `registration.py` reports how well-constrained each
  fit is, and you should read that number rather than trust the result.
- **Nothing catches a scan placed on the wrong walls.** A five-wall bedroom of
  mine landed 65 degrees out on top of a hallway, at 100% coverage and 18.9 cm
  median error. No quantile sees that, because arithmetically nothing is wrong. I
  think I know the fix (every capture of one building shares a wall grid, so only
  four rotations between two captures are ever valid) but it isn't built.
- **Windows are missing.** LiDAR goes straight through glass. Add them by hand in
  Sweet Home 3D.
- **Texture detail is capped by the scan.** Polycam's atlas carries roughly 384 px
  per metre of real surface, measured. No export setting improves it.
- **Voids** (stairwell shafts, double-height spaces) have to be declared by hand.
  A scanner maps rooms, not the space between them.
- **Staging captures is manual**, and you pay it on every lap. `add-capture` would
  fix that and doesn't exist. It's the biggest ergonomic gap in the project.

## When the toolchain bites

Sweet Home 3D, Java3D and the plugin fail with messages that name a DLL, or a
class version, or nothing at all.
**[docs/SH3D-NOTES.md](docs/SH3D-NOTES.md)** indexes those errors, and explains
why a `.sh3d` holds Java-serialised objects rather than the documented
`Home.xml`, which is why there's Java in this repo at all.

## Contributing

If you try this on a second house I'd genuinely like to hear what broke. That's
the most useful thing anyone could do with it, and I've no way to find out on my
own.

```bash
uv run pytest -q                     # java-marked tests skip without Sweet Home 3D
uv run pytest -q -m "not tutorial"   # skip the demo pipeline run, ~25s
uv run ruff check .
uv run mypy                          # clean on src/lidar2ha, and it stays that way
uv run lidar2ha doctor               # the only thing that compiles the Java
```

If you change a stage's interface, change [docs/TUTORIAL.md](docs/TUTORIAL.md) in
the same commit. `tests/test_tutorial.py` holds it to the claims it can check.

`uv.lock` and `.python-version` are committed, so everyone resolves the same
packages. Change a dependency with `uv add` or
`uv lock --upgrade-package <name>`, and commit the lock alongside the
`pyproject.toml` change. CI installs with `uv sync --locked` and fails when the
two have drifted.

Contributor tooling lives in `[dependency-groups]` rather than an extra, so it
never reaches the published wheel. uv installs it by default.

## License

MIT, see [LICENSE](LICENSE).

Sweet Home 3D is (c) eTeks, under the GNU GPL. This project doesn't bundle or
modify it, it compiles against a local installation. Check your own obligations
if you redistribute a combined work.
