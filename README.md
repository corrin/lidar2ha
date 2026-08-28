# lidar2ha

Turn a phone LiDAR scan of your house into a 3D floorplan in Home Assistant that
lights up when you tap a light — raytraced, with light spilling through stairwells
and across open-plan volumes.

Built for one house: mine. Every threshold and default here is tuned to one
building scanned with one app. Claude wrote essentially all of the code; I
supplied the house, the scans, and the judgement about whether each output was
right.

## What you need

- An iPhone with LiDAR (12 Pro or later), or an iPad Pro (2020 or later)
- Polycam, on a tier that exports floor plans — around $1,000/year, or the 7-day trial
- Home Assistant, with SSH to `/config` ([Terminal & SSH add-on][ssh])
- A long-lived access token from an admin account ([your profile page][token])
- [Sweet Home 3D][sh3d] and the [floor-plan plugin][plugin]
- A JDK 17 or later ([Temurin][temurin])
- Three scans of each storey, plus a fixture pass per storey
- A week of Polycam, and a couple of weekends

Most people do this on the trial. Everything that needs Polycam — scanning *and*
exporting — happens inside those seven days. Everything after runs offline
forever, so prove the toolchain works before you start the clock.

[ssh]: https://github.com/home-assistant/addons/blob/master/ssh/DOCS.md
[token]: https://www.home-assistant.io/docs/authentication/#your-account-profile
[sh3d]: https://www.sweethome3d.com/
[plugin]: https://github.com/shmuelzon/home-assistant-floor-plan
[temurin]: https://adoptium.net/

## What works

| Stage | State |
|---|---|
| Parse Polycam floor-plan DXF/CSV → JSON model | works (`polycam.py`) |
| Recover floor elevations from the mesh | works (`mesh.py`) |
| Register DXF floors onto the mesh | works, and the weak link — see limits |
| Rectify per-wall textures from the photo atlas | works (`textures_project.py`) |
| Tiled textures by surface class (fallback) | works (`textures_tile.py`) |
| Write a real `.sh3d` | works (`Sh3dWriter.java`) |
| Headless raytraced render + `floorplan.yaml` | works (`HeadlessRender.java`), all levels in one pass |
| Frame the camera so the house fits | works (`camera.py`), solved rather than guessed |
| Rename rooms to HA areas, merge open-plan splits | works (`rooms.py`), mapping written by hand |
| Merge several captures of one level | works (`combine.py`), align-or-discard, needs 3+ scans |
| Read your HA area/entity registry | works (`ha.py`), over the WebSocket API |
| Place every `light.*` entity in its room | works (`lights.py`), positions are a guess |
| Find real fittings in the scan | works (`fixtures.py`, `placefixtures.py`), needs human review |
| Separate windows from fittings mechanically | works (`daylight.py`), differences two captures |
| Review sheet for the fittings found | works (`contactsheet.py`), windows sorted last |
| Export a named GLB for a real-time 3D card | works (`ObjExport.java`, `glb.py`) |
| Cut an open-plan room into the rooms it is used as | works (`seams.py`), boundary declared by you |
| Corroborate a declared boundary against the floor | works (`thresholds.py`), reports, never decides |
| A demo house, to run all of it before you scan | works (`demo.py`) |
| `lidar2ha add-capture` | **not implemented** (exits saying so) |

The geometry and rendering half is solid. The Home Assistant half is manual:
you write the area mapping, you review the fittings, you paste the card.

### Why raytraced, and not a WebGL card

`floor3d-card` and Floorplan 3D render in real time, so there is no raytracing
and no cross-floor light spill. For a house of sealed boxes that costs nothing.
For a stairwell, a double-height space, or open-plan living, it is the reason to
do this at all.

They are not exclusive. `export-glb` emits `.obj` and `.glb` from the same
`.sh3d`, each object named after its entity id, so one model drives both.

## Install

```bash
git clone https://github.com/corrin/lidar2ha && cd lidar2ha
uv sync --all-extras
uv run lidar2ha doctor
```

`--all-extras` is not optional: `paramiko` and `websockets` are extras, and a
bare `uv sync` removes them along with `deploy` and the Home Assistant registry.

`doctor` locates Sweet Home 3D, the plugin and your JDK, then compiles the Java
against your own installation and reports the compiler's errors. It also checks
your installed packages against `uv.lock`.

Then run the whole pipeline on a house that does not exist:

```bash
uv run lidar2ha demo ~/demo-house
```

Eight captures of a two-storey building with a stairwell, packaged the way
Polycam packages yours, one of them wrong about where the kitchen is. Do this
before you pay Polycam anything.

## How it goes

Scan a room, run it through, put it on your dashboard, and look at it. What you
see tells you what to fix next: a name in `project.yaml`, a boundary you need to
declare, or another scan. Then round again.

`project.yaml` is the control file, and it accumulates. Every section of it is an
answer to something a render got wrong.

Home Assistant receives pre-rendered overlay images and a `picture-elements`
card, which `deploy` copies to `/config/www/floorplan/`. The model stays on your
desktop.

**[docs/TUTORIAL.md](docs/TUTORIAL.md)** walks the loop from an empty directory to
a lit dashboard, with what you should see at each step and what it looks like
when it is wrong.

## Known limits

- **Registration is the weak point.** A wall-poor open-plan level gives the
  fitter little to hold onto. `registration.py` reports how well-constrained each
  fit is; read that number rather than trusting the result.
- **A capture placed on the wrong walls is not caught.** A five-wall bedroom
  landed 65° out on top of a hallway at 100% coverage and 18.9 cm median error.
  No quantile sees this, because arithmetically nothing is wrong.
- **Windows are missing.** LiDAR passes through glass. Add them by hand in Sweet
  Home 3D.
- **Texture detail is capped by the scan.** Polycam's atlas carries roughly
  384 px per metre of real surface, measured. No export setting improves it.
- **Voids** — stairwell shafts, double-height spaces — are declared by hand. A
  scanner maps rooms, not the space between them.
- **Staging captures is manual**, and you pay it on every pass. `add-capture`
  would fix it and does not exist.
- **Every constant here is a guess** that worked once, on one house, in one app.

## When the toolchain bites

Sweet Home 3D, Java3D and the plugin fail with messages that name a DLL, a class
version, or nothing at all. **[docs/SH3D-NOTES.md](docs/SH3D-NOTES.md)** indexes
those errors, and explains why a `.sh3d` holds Java-serialised objects rather
than the documented `Home.xml` — which is why there is Java in this repo.

## Contributing

If you try this on a second house I'd like to hear what broke. That is the most
useful thing anyone could do with it, and I have no way to find out on my own.

```bash
uv run pytest -q                     # java-marked tests skip without Sweet Home 3D
uv run pytest -q -m "not tutorial"   # skip the demo pipeline run, ~25s
uv run ruff check .
uv run mypy                          # clean on src/lidar2ha, and it stays that way
uv run lidar2ha doctor               # the only thing that compiles the Java
```

Change a stage's interface and change [docs/TUTORIAL.md](docs/TUTORIAL.md) in the
same commit. `tests/test_tutorial.py` holds it to the claims it can check.

`uv.lock` and `.python-version` are committed. Change a dependency with `uv add`
or `uv lock --upgrade-package <name>` and commit the lock alongside the
`pyproject.toml` change; CI installs with `uv sync --locked`.

Contributor tooling lives in `[dependency-groups]` rather than an extra, so it
never reaches the published wheel. uv installs it by default.

## License

MIT — see [LICENSE](LICENSE).

Sweet Home 3D is © eTeks, under the GNU GPL. This project does not bundle or
modify it; it compiles against a local installation. Check your own obligations
if you redistribute a combined work.
