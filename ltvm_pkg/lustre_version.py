"""The version a Lustre build carries, decided on the host.

Lustre's configure takes its version from LUSTRE-VERSION-GEN, which runs
``git describe`` and falls back to LUSTRE-VERSION-FILE and then to a
hard-coded DEFAULT_VERSION.  Inside the build container that describe
fails for a git worktree (its ``.git`` names a gitdir under the main
repository, which is not mounted) and for a rootful build of a tree the
container's root does not own (git's safe.directory check).  The build
then silently carries DEFAULT_VERSION, and every test gated on a newer
version skips.

So the host runs the same describe and writes LUSTRE-VERSION-FILE, which
LUSTRE-VERSION-GEN reads whenever its own describe fails.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

VERSION_FILE = "LUSTRE-VERSION-FILE"
VERSION_GEN = "LUSTRE-VERSION-GEN"


@dataclass(frozen=True)
class LustreVersion:
    version: str | None
    # "git", "file" (LUSTRE-VERSION-FILE, e.g. a release tarball) or
    # "default" (nothing could tell; the build gets DEFAULT_VERSION).
    source: str
    default: str | None
    reason: str = ""


def _git(tree: Path, *args: str) -> subprocess.CompletedProcess[str]:
    # safe.directory: under sudo the tree belongs to someone else.
    # GIT_OPTIONAL_LOCKS=0: status must not rewrite the index, which
    # would leave it root-owned under sudo.
    return subprocess.run(
        ["git", "-c", "safe.directory=*", "-C", str(tree), *args],
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        check=False,
    )


def git_version(tree: Path) -> tuple[str | None, str]:
    """What LUSTRE-VERSION-GEN derives from git, or (None, why not)."""
    if not (tree / ".git").exists():
        return None, f"{tree} is not a git checkout"
    try:
        r = _git(tree, "describe", "--match", "[0-9]*", "--abbrev=7", "HEAD")
    except OSError as e:
        return None, f"cannot run git: {e}"
    vn = r.stdout.strip()
    if r.returncode != 0 or not vn or "\n" in vn:
        err = r.stderr.strip().splitlines()
        return None, "git describe failed: " + (err[-1] if err else "no output")
    st = _git(tree, "status", "--porcelain", "--untracked-files=no")
    if st.returncode == 0 and st.stdout.strip():
        vn += "-dirty"
    return vn.replace("-", "_"), ""


def default_version(tree: Path) -> str | None:
    try:
        text = (tree / VERSION_GEN).read_text()
    except OSError:
        return None
    m = re.search(r"^DEFAULT_VERSION=(\S+)", text, re.MULTILINE)
    return m.group(1) if m else None


def read_version_file(tree: Path) -> str | None:
    try:
        text = (tree / VERSION_FILE).read_text()
    except OSError:
        return None
    m = re.match(r"LUSTRE_VERSION = (\S+)", text)
    return m.group(1) if m else None


def pin_version(tree: Path) -> LustreVersion:
    """Decide the build's version and write it where configure finds it."""
    default = default_version(tree)
    version, why = git_version(tree)
    if version is not None:
        line = f"LUSTRE_VERSION = {version}\n"
        if read_version_file(tree) != version:
            (tree / VERSION_FILE).write_text(line)
        return LustreVersion(version, "git", default)
    from_file = read_version_file(tree)
    if from_file is not None:
        return LustreVersion(from_file, "file", default)
    return LustreVersion(None, "default", default, why)


def configure_is_stale(tree: Path, pin: LustreVersion) -> bool:
    """Whether <tree>/configure was generated for another version.

    The version reaches configure through m4_esyscmd, which autoconf
    cannot track, so autogen.sh keeps the old one (and autom4te.cache)
    until they are removed.
    """
    try:
        text = (tree / "configure").read_text(errors="replace")
    except OSError:
        return False
    m = re.search(r"^PACKAGE_VERSION='([^']*)'", text, re.MULTILINE)
    return bool(pin.version and m and m.group(1) != pin.version)


def configured_version(tree: Path) -> str | None:
    """The version configure baked into <tree>/config.h, if any."""
    try:
        text = (tree / "config.h").read_text()
    except OSError:
        return None
    m = re.search(r'^#define VERSION "([^"]*)"', text, re.MULTILINE)
    return m.group(1) if m else None


def undetermined_warning(pin: LustreVersion) -> str:
    return (
        f"--- WARNING: cannot determine the Lustre version "
        f"({pin.reason}).\n"
        f"    The build will report LUSTRE-VERSION-GEN's default "
        f"{pin.default or '(unknown)'}, and tests gated on a newer "
        f"version will skip.\n"
        f"    Fix: build from a checkout with its tags (git fetch --tags, "
        f"or deepen a shallow clone)."
    )


def check_configured(tree: Path, pin: LustreVersion) -> list[str]:
    """Lines to print after a build about the version it carries."""
    built = configured_version(tree)
    if built is None:
        return []
    if pin.source == "default":
        return [undetermined_warning(pin)]
    # Editing a file flips _dirty without anyone wanting a reconfigure.
    if built.removesuffix("_dirty") == (pin.version or "").removesuffix(
        "_dirty"
    ):
        return [f"  Lustre version: {built}"]
    if built == pin.default:
        return [
            f"--- WARNING: Lustre was configured as the default version "
            f"{built}, not {pin.version}.\n"
            f"    Tests gated on a newer version will skip.  Rebuild "
            f"with --force."
        ]
    return [
        f"  Lustre version: {built} (HEAD is {pin.version}; configure has "
        f"not re-run since -- rebuild with --force to refresh it)"
    ]
