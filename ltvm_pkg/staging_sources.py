"""Keep a staging tree's uncompiled files in step with their sources.

`make install` copies scripts, test-framework files, cfg/*.sh, headers
and man pages into staging verbatim.  Nothing about deploy's rebuild
decision can be trusted to notice when one of them is edited: a
`--userspace-only` deploy never rebuilds, and the full deploy's fast
path compares source mtimes against a stamp, which an mtime-preserving
copy (`cp -p`, `rsync -a`, `tar x`) or an edit that races the build
walks straight past.  Either way the old file is what reaches the VM.

So a build records which source file each uncompiled staged file came
from -- proven by identical content at install time -- and every deploy
compares the pairs by content and copies across whatever moved.  That
is cheap (a few thousand small files) and does not depend on mtimes.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

MANIFEST_NAME = ".ltvm-staging-sources.json"
_MANIFEST_VERSION = 1

# Directories in a Lustre tree that never hold an installed source.
_PRUNE_DIRS = frozenset(
    {".git", ".ltvm-staging", "autom4te.cache", "_lpb", "kconftest.dir"}
)

# Where `make install` puts lustre/tests, for either libdir.
_STAGED_TEST_DIRS = ("usr/lib64/lustre/tests", "usr/lib/lustre/tests")


def _same_content(a: Path, b: Path) -> bool:
    """Byte comparison; filecmp's cache is keyed on stat, not content."""
    if a.stat().st_size != b.stat().st_size:
        return False
    with a.open("rb") as fa, b.open("rb") as fb:
        while True:
            ca, cb = fa.read(65536), fb.read(65536)
            if ca != cb:
                return False
            if not ca:
                return True


def _is_elf(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            return f.read(4) == b"\x7fELF"
    except OSError:
        return False


def _staged_candidates(staging: Path) -> list[Path]:
    """Staged regular files that could have been installed verbatim."""
    out = []
    for dirpath, dirnames, filenames in os.walk(staging):
        d = Path(dirpath)
        rel_dir = d.relative_to(staging)
        if rel_dir == Path("lib"):
            dirnames[:] = [n for n in dirnames if n != "modules"]
        for name in filenames:
            p = d / name
            if rel_dir == Path(".") and name.startswith(".ltvm-"):
                continue
            if p.is_symlink() or not p.is_file() or name.endswith(".ko"):
                continue
            if _is_elf(p):
                continue
            out.append(p)
    return out


def _tree_index(tree: Path) -> dict[str, list[Path]]:
    index: dict[str, list[Path]] = {}
    for dirpath, dirnames, filenames in os.walk(tree):
        dirnames[:] = [n for n in dirnames if n not in _PRUNE_DIRS]
        d = Path(dirpath)
        for name in filenames:
            index.setdefault(name, []).append(d / name)
    return index


def _suffix_score(a: Path, b: Path) -> int:
    """How many trailing path components a and b share."""
    n = 0
    for x, y in zip(reversed(a.parts), reversed(b.parts)):
        if x != y:
            break
        n += 1
    return n


def build_manifest(staging: Path, tree: Path) -> dict[str, str]:
    """Map each verbatim-installed staged file to its source in tree.

    A staged file maps to the tree file with the same name and identical
    content; among several, the one sharing the most trailing path
    components wins, and a tie maps nothing, since which copy a later
    edit would land in cannot be known.  ELF files and modules are left
    out: they are compiled, and the build owns them.
    """
    index = _tree_index(tree)
    manifest: dict[str, str] = {}
    for staged in _staged_candidates(staging):
        cands = index.get(staged.name)
        if not cands:
            continue
        same = []
        for c in cands:
            try:
                if not c.is_symlink() and _same_content(staged, c):
                    same.append(c)
            except OSError:
                continue
        if not same:
            continue
        rel = staged.relative_to(staging)
        scored = sorted(
            ((_suffix_score(rel, c.relative_to(tree)), c) for c in same),
            key=lambda t: t[0],
            reverse=True,
        )
        if len(scored) > 1 and scored[0][0] == scored[1][0]:
            continue
        manifest[str(rel)] = str(scored[0][1].relative_to(tree))
    return manifest


def write_manifest(staging: Path, tree: Path) -> int:
    """Record the staged-file -> source map; returns how many it holds."""
    files = build_manifest(staging, tree)
    payload = {"version": _MANIFEST_VERSION, "files": files}
    (staging / MANIFEST_NAME).write_text(
        json.dumps(payload, indent=1, sort_keys=True) + "\n"
    )
    return len(files)


def read_manifest(staging: Path) -> dict[str, str] | None:
    try:
        data = json.loads((staging / MANIFEST_NAME).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != _MANIFEST_VERSION:
        return None
    files = data.get("files")
    if not isinstance(files, dict):
        return None
    return {str(k): str(v) for k, v in files.items()}


def _test_dir_pairs(staging: Path, tree: Path) -> dict[str, str]:
    """lustre/tests files paired by path, for staging with no manifest.

    A staging tree built before the manifest existed still gets its test
    scripts and cfg files checked, which is where edits between deploys
    mostly land.
    """
    pairs: dict[str, str] = {}
    src_root = tree / "lustre" / "tests"
    for rel_dir in _STAGED_TEST_DIRS:
        staged_root = staging / rel_dir
        if not staged_root.is_dir():
            continue
        for dirpath, _dirnames, filenames in os.walk(staged_root):
            d = Path(dirpath)
            for name in filenames:
                staged = d / name
                rel = staged.relative_to(staged_root)
                src = src_root / rel
                if (
                    staged.is_symlink()
                    or src.is_symlink()
                    or not src.is_file()
                    or _is_elf(staged)
                    or _is_elf(src)
                ):
                    continue
                pairs[str(staged.relative_to(staging))] = str(
                    src.relative_to(tree)
                )
    return pairs


def source_pairs(staging: Path, tree: Path) -> dict[str, str]:
    """Every staged file -> source pair a deploy should keep in step."""
    pairs = _test_dir_pairs(staging, tree)
    manifest = read_manifest(staging)
    if manifest:
        pairs.update(manifest)
    return pairs


def _replace_from(src: Path, dest: Path) -> None:
    mode = dest.stat().st_mode & 0o7777
    fd, tmp_str = tempfile.mkstemp(dir=str(dest.parent), prefix=".ltvm-")
    os.close(fd)
    tmp = Path(tmp_str)
    try:
        shutil.copyfile(src, tmp)
        os.chmod(tmp, mode)
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)


def refresh(staging: Path, tree: Path) -> list[str]:
    """Bring staged copies up to date with their sources, by content.

    Returns the source-relative paths copied over their staged copy.  A
    source that is gone is left alone: it may be a generated file that
    `make clean` removed.  Raises OSError if a staged file cannot be
    rewritten, since carrying on would deploy the stale copy.
    """
    refreshed: list[str] = []
    for rel_staged, rel_src in sorted(source_pairs(staging, tree).items()):
        staged = staging / rel_staged
        src = tree / rel_src
        if not staged.is_file() or staged.is_symlink():
            continue
        if not src.is_file() or src.is_symlink():
            continue
        if _same_content(staged, src):
            continue
        _replace_from(src, staged)
        refreshed.append(rel_src)
    return refreshed


def describe(refreshed: list[str]) -> str:
    """Human line for what :func:`refresh` did."""
    return (
        f"  Refreshed {len(refreshed)} staged file(s) from source: "
        + sample(refreshed)
    )


def sample(paths: list[str], n: int = 5) -> str:
    shown = ", ".join(paths[:n])
    return shown if len(paths) <= n else f"{shown} (+{len(paths) - n} more)"
