"""CLI layer for ``ltvm suite``: run / status / collect over
``ltvm_pkg.suite_run``."""

from __future__ import annotations

import argparse
from typing import Any

from ltvm_pkg.cli.util import EXIT_ERROR, EXIT_NOT_FOUND, _emit_error, _error


def _call(name: str, args: argparse.Namespace) -> int:
    """Run a suite_run handler, mapping its failures to exit codes.

    Fetched by name at call time so tests can patch
    ``ltvm_pkg.suite_run.cmd_suite_*``.
    """
    from ltvm_pkg import suite_run
    from ltvm_pkg.suite_run import TargetNotFound
    from ltvm_pkg.vm_state import VMNotFound

    fn: Any = getattr(suite_run, name)
    try:
        return int(fn(args))
    except SystemExit as e:
        return int(e.code) if e.code is not None else EXIT_ERROR
    except TargetNotFound as e:
        return _emit_error(str(e), args.json, code=EXIT_NOT_FOUND)
    except (VMNotFound, RuntimeError) as e:
        return _error(str(e), args.json)


def cmd_suite_run(args: argparse.Namespace) -> int:
    """Start a test suite on a VM or cluster, detached."""
    return _call("cmd_suite_run", args)


def cmd_suite_status(args: argparse.Namespace) -> int:
    """Show whether a suite run is going, finished or died."""
    return _call("cmd_suite_status", args)


def cmd_suite_collect(args: argparse.Namespace) -> int:
    """Copy a suite run's logs back and summarize its results."""
    return _call("cmd_suite_collect", args)
