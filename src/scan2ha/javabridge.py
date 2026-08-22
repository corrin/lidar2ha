"""Locate the Java pieces, compile on first use, and run them.

There are TWO JVMs in this pipeline and they are not interchangeable. This module
exists mainly to keep that fact in one place.

**Writing a .sh3d** needs only Sweet Home 3D's model classes, so it runs on an
ordinary 64-bit JDK. A JDK, not a JRE: Sweet Home 3D bundles a runtime but no
compiler.

**Rendering** does not. Sweet Home 3D ships a 32-bit runtime, and Java3D plus
YafaRay are 32-bit native libraries, so the raytracer must be driven by *that*
JVM. It ships only `javaw.exe`, which has no console attached -- so the render
step cannot log to stdout, and instead writes to a file we tail. Any design that
assumes a single `java` on PATH is wrong, which is why this is not inlined at the
call sites.

Nothing is committed as a binary: `.java` sources are compiled on first use into a
per-user cache keyed by a hash of the sources plus the jars they compile against,
so a Sweet Home 3D upgrade or an edit to our Java transparently triggers a rebuild.
"""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

# Inside the package, so an installed wheel actually carries the sources it
# compiles. Pointing outside src/scan2ha ships a package that cannot build.
JAVA_SRC = Path(__file__).resolve().parent / "java"

# Where Sweet Home 3D installs itself, per platform. First hit wins.
SH3D_LIB_CANDIDATES = [
    r"C:\Program Files (x86)\Sweet Home 3D\lib",
    r"C:\Program Files\Sweet Home 3D\lib",
    "/Applications/Sweet Home 3D.app/Contents/Java",
    "/usr/share/sweethome3d",
    "/usr/lib/sweethome3d",
    str(Path.home() / ".local/share/sweethome3d"),
]

# The plugin is installed by Sweet Home 3D into the user's profile.
PLUGIN_DIR_CANDIDATES = [
    Path(os.environ.get("APPDATA", "")) / "eTeks" / "Sweet Home 3D" / "plugins",
    Path.home() / "Library" / "Application Support" / "eTeks" / "Sweet Home 3D" / "plugins",
    Path.home() / ".eteks" / "sweethome3d" / "plugins",
]


class ToolchainError(RuntimeError):
    """Raised with an actionable message -- `doctor` prints these verbatim."""


@dataclass
class Toolchain:
    sh3d_lib: Path
    sweethome_jar: Path
    furniture_jar: Path | None
    plugin_jar: Path | None
    javac: Path
    java: Path
    render_java: Path | None
    # Every jar Sweet Home 3D ships. Java3D (j3dcore, j3dutils, vecmath) lives
    # here, and ObjExport and CatalogProbe import javax.media.j3d directly, so
    # a classpath of just SweetHome3D.jar cannot compile them. Globbing the
    # directory rather than naming jars keeps this working across versions.
    lib_jars: tuple[Path, ...] = ()

    @property
    def classpath_sep(self) -> str:
        return ";" if platform.system() == "Windows" else ":"

    def writer_classpath(self, classes: Path) -> str:
        parts = [str(self.sweethome_jar)]
        if self.furniture_jar:
            parts.append(str(self.furniture_jar))
        parts.append(str(classes))
        return self.classpath_sep.join(parts)

    def render_classpath(self, classes: Path) -> str:
        """Everything: Sweet Home 3D, Java3D, the plugin, and our classes.

        This is also the compile classpath, because our Java spans both the
        model-only tools and the ones that touch Java3D.
        """
        parts = [str(jar) for jar in self.lib_jars] or [str(self.sweethome_jar)]
        if self.plugin_jar:
            parts.append(str(self.plugin_jar))
        parts.append(str(classes))
        return self.classpath_sep.join(parts)


def _find_sh3d_lib(override: str | Path | None = None) -> Path:
    if override:
        p = Path(override)
        p = p.parent if p.is_file() else p
        if (p / "SweetHome3D.jar").exists():
            return p
        raise ToolchainError(f"No SweetHome3D.jar in {p}")
    for cand in SH3D_LIB_CANDIDATES:
        p = Path(cand)
        if (p / "SweetHome3D.jar").exists():
            return p
    raise ToolchainError(
        "Sweet Home 3D not found. Install it from https://www.sweethome3d.com/ "
        "or set sweethome3d_jar in project.yaml (or $SCAN2HA_SH3D_JAR)."
    )


def _find_jdk() -> tuple[Path, Path]:
    """javac and java from a real JDK. Sweet Home 3D's bundled runtime has no javac.

    They must come from the SAME JDK. Resolving each with which() independently
    is how you end up compiling with a modern javac and running on whatever
    stale JRE is earlier on PATH -- on Windows, typically Oracle's java8path
    shim -- which fails at the point of use with UnsupportedClassVersionError
    and no hint that two different toolchains were involved.
    """
    exe = ".exe" if platform.system() == "Windows" else ""
    java_home = os.environ.get("JAVA_HOME")
    if java_home:
        javac = Path(java_home) / "bin" / f"javac{exe}"
        java = Path(java_home) / "bin" / f"java{exe}"
        if javac.exists() and java.exists():
            return javac, java

    found = shutil.which("javac")
    if found:
        javac = Path(found)
        # Prefer this JDK's own java; only fall back to PATH if it has none.
        java = javac.with_name(f"java{exe}")
        if not java.exists():
            on_path = shutil.which("java")
            if not on_path:
                raise ToolchainError(f"Found {javac} but no java alongside it or on PATH.")
            java = Path(on_path)
        return javac, java

    raise ToolchainError(
        "No JDK found (need javac, not just a JRE). Install Temurin 17+ from "
        "https://adoptium.net/ or set JAVA_HOME."
    )


def _find_render_jvm(sh3d_lib: Path) -> Path | None:
    """Sweet Home 3D's own JVM -- the only one that can drive Java3D/YafaRay.

    It ships javaw.exe only (no console), so callers must pass -DlogFile and read
    the log rather than expecting stdout.
    """
    root = sh3d_lib.parent
    for rel in ("runtime/bin/javaw.exe", "runtime/bin/java", "jre/bin/javaw.exe", "jre/bin/java"):
        p = root / rel
        if p.exists():
            return p
    return None


def _find_plugin() -> Path | None:
    for d in PLUGIN_DIR_CANDIDATES:
        if not d or not d.exists():
            continue
        hits = sorted(d.glob("*FloorPlan*.sh3p")) or sorted(d.glob("*.sh3p"))
        if hits:
            return hits[-1]
    return None


def detect(sh3d_jar: str | Path | None = None) -> Toolchain:
    """Locate everything. Raises ToolchainError with an actionable message."""
    sh3d_jar = sh3d_jar or os.environ.get("SCAN2HA_SH3D_JAR")
    lib = _find_sh3d_lib(sh3d_jar)
    javac, java = _find_jdk()
    furniture = lib / "Furniture.jar"
    return Toolchain(
        sh3d_lib=lib,
        sweethome_jar=lib / "SweetHome3D.jar",
        # Lights are built from catalog pieces, which live in Furniture.jar.
        furniture_jar=furniture if furniture.exists() else None,
        plugin_jar=_find_plugin(),
        javac=javac,
        java=java,
        render_java=_find_render_jvm(lib),
        lib_jars=tuple(sorted(lib.glob("*.jar"))),
    )


def _cache_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CACHE_HOME")
    root = Path(base) if base else Path.home() / ".cache"
    return root / "scan2ha" / "jclasses"


def compile_java(tc: Toolchain, sources: list[str] | None = None) -> Path:
    """Compile our Java against the user's jars, cached by content hash.

    The hash covers both our sources and the jars, so upgrading Sweet Home 3D
    rebuilds automatically instead of failing at runtime with a linkage error.
    """
    srcs = sorted(JAVA_SRC.glob("*.java")) if sources is None else [JAVA_SRC / s for s in sources]
    srcs = [s for s in srcs if not s.name.startswith("_")]
    if not srcs:
        raise ToolchainError(f"No Java sources in {JAVA_SRC}")

    h = hashlib.sha256()
    for s in srcs:
        h.update(s.name.encode())
        h.update(s.read_bytes())
    for jar in (*tc.lib_jars, tc.plugin_jar):
        if jar and jar.exists():
            h.update(str(jar).encode())
            h.update(str(jar.stat().st_mtime_ns).encode())

    out = _cache_dir() / h.hexdigest()[:16]
    if (out / ".ok").exists():
        return out

    out.mkdir(parents=True, exist_ok=True)
    cp = tc.render_classpath(out)
    cmd = [str(tc.javac), "-cp", cp, "-d", str(out), *(str(s) for s in srcs)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise ToolchainError(
            "Java compilation failed. This usually means the installed Sweet Home 3D "
            "or plugin version differs from the API we target.\n\n" + proc.stderr
        )
    (out / ".ok").write_text("")
    return out


def run_writer(tc: Toolchain, classes: Path, main: str, *args: str) -> subprocess.CompletedProcess:
    """Run a model-only tool (writer, verifier) on the 64-bit JDK."""
    cmd = [str(tc.java), "-cp", tc.writer_classpath(classes), main, *args]
    return subprocess.run(cmd, capture_output=True, text=True)


def run_render(tc: Toolchain, classes: Path, log: Path, *args: str) -> subprocess.CompletedProcess:
    """Run HeadlessRender on Sweet Home 3D's bundled 32-bit JVM.

    That JVM has no console, so `log` is where output goes; callers tail it for
    progress. Falls back to the JDK only when no bundled runtime is found, which
    will fail on Java3D -- but failing with a clear log beats failing silently.
    """
    jvm = tc.render_java or tc.java
    log.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(jvm),
        f"-DlogFile={log}",
        "-cp",
        tc.render_classpath(classes),
        "HeadlessRender",
        *args,
    ]
    return subprocess.run(cmd, capture_output=True, text=True)
