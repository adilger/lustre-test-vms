"""Marks and fixtures for tests that assume a Linux x86_64 host.

The suite also runs on macOS on Apple Silicon, where guest-side shell
(GNU sed), /proc and the host architecture all differ.
"""

from __future__ import annotations

import platform
import subprocess
import sys

import pytest


def _gnu_sed() -> bool:
    try:
        r = subprocess.run(["sed", "--version"], capture_output=True, text=True)
    except OSError:
        return False
    return r.returncode == 0 and "GNU" in r.stdout


needs_gnu_sed = pytest.mark.skipif(
    not _gnu_sed(), reason="runs guest shell that needs GNU sed"
)
needs_proc = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="reads /proc"
)


@pytest.fixture
def x86_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the host x86_64, the arch the default targets build for."""
    monkeypatch.setattr(platform, "machine", lambda: "x86_64")
