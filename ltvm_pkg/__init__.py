"""ltvm -- Lustre test VM infrastructure package.

``ltvm --version`` reports the version from ``git describe`` against the
newest ``vMAJOR.MINOR`` tag, e.g. ``0.5.680+71992dd888ab``: commits since
the tag, plus the commit hash.  See ltvm_pkg/version_info.py.

It is normally baked into ``ltvm_pkg/_build_info.py`` by the
``.githooks/post-commit`` hook and by ``ltvm update`` (so we avoid shelling
out to git on every import).  When that file is missing -- fresh clone,
hook not installed -- we ask git directly, and without git we report
just BASE_VERSION.
"""

from __future__ import annotations

import importlib
from pathlib import Path

from .version_info import BASE_VERSION, git_version, read_baked_version

__all__ = ["BASE_VERSION", "__version__"]


def _compute_version(refresh: bool = False) -> str:
    # Prefer the version baked by the post-commit hook so importing this
    # package stays cheap.  The module is gitignored and may not exist
    # (fresh clone, hook not installed); use importlib so mypy doesn't
    # try to type-check a path that's missing on disk at install time.
    version: str | None = None
    baked = Path(__file__).with_name("_build_info.py")
    try:
        if refresh:
            # Read the file rather than importing it.  ltvm_pkg
            # imports _build_info at startup to compute __version__,
            # so import_module() hands back the already-loaded module
            # -- and cmd_update calls this right after rewriting
            # _build_info.py with the post-pull version, so `ltvm update`
            # reported "Already up to date at <old>" after a real
            # fast-forward and --json reported changed=false.
            # Re-importing is not enough either: pre- and post-update
            # files can have identical sizes, and a .pyc is revalidated
            # on (mtime, size) -- two writes within the same second keep
            # serving the old version out of __pycache__.
            version = read_baked_version(baked)
        else:
            bi = importlib.import_module("ltvm_pkg._build_info")
            version = getattr(bi, "VERSION", None)
            build_hash = getattr(bi, "BUILD_HASH", None)
            if not version and build_hash:
                # Baked before VERSION existed.
                version = f"{BASE_VERSION}+{build_hash}"
    except ImportError:
        pass
    if version:
        return version
    got = git_version(Path(__file__).resolve().parent.parent)
    return got[0] if got else BASE_VERSION


__version__ = _compute_version()
