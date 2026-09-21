"""/etc/ltvm.conf: settings an admin makes once for every user on a host."""

from __future__ import annotations

import os
from pathlib import Path


def path() -> Path:
    return Path(os.environ.get("LTVM_SITE_CONFIG") or "/etc/ltvm.conf")
