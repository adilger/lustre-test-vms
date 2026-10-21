"""The socket_vmnet launchd plist ltvm installs on macOS."""

from __future__ import annotations

import grp
from pathlib import Path

import pytest

from ltvm_pkg import host_setup


@pytest.fixture
def rendered(monkeypatch: pytest.MonkeyPatch) -> object:
    monkeypatch.setattr(
        host_setup, "socket_vmnet_path", lambda: Path("/brew/bin/socket_vmnet")
    )
    monkeypatch.setattr(
        host_setup, "socket_vmnet_socket_path", lambda: Path("/brew/run/sock")
    )
    return host_setup._render_socket_vmnet_plist


def _group_exists(monkeypatch: pytest.MonkeyPatch, exists: bool) -> None:
    real = grp.getgrnam

    def getgrnam(name: str) -> grp.struct_group:
        if name == "ltvm":
            if not exists:
                raise KeyError(name)
            return grp.struct_group(("ltvm", "*", 501, []))
        return real(name)

    monkeypatch.setattr(grp, "getgrnam", getgrnam)


def test_the_socket_is_the_ltvm_groups(
    rendered: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not staff, socket_vmnet's default: a directory-service account on
    a Mac bound to one is not in staff, and could never use the socket."""
    _group_exists(monkeypatch, True)
    plist = rendered()  # type: ignore[operator]
    assert "<string>--socket-group=ltvm</string>" in plist
    assert "@" not in plist


def test_staff_until_the_group_exists(
    rendered: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """socket_vmnet refuses to start with a group it cannot find."""
    _group_exists(monkeypatch, False)
    assert "<string>--socket-group=staff</string>" in rendered()  # type: ignore[operator]


def test_replace_waits_for_the_old_job_to_go(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """bootout returns before launchd lets go of the job.  A rewritten
    plist used to leave socket_vmnet unloaded: still "loaded" right after
    the bootout, so it was never bootstrapped again."""
    events: list[str] = []
    still_loaded = iter([True, True, True, False])  # before, then 2 polls

    monkeypatch.setattr(
        host_setup, "_launchd_loaded", lambda label: next(still_loaded)
    )
    monkeypatch.setattr(
        host_setup, "_sudo_run", lambda cmd, **kw: events.append(cmd[1])
    )
    monkeypatch.setattr("time.sleep", lambda s: events.append("wait"))
    host_setup._launchd_replace("job", Path("/L/job.plist"))
    assert events == ["bootout", "wait", "wait", "bootstrap"]


def test_replace_loads_a_job_that_is_not_loaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    monkeypatch.setattr(host_setup, "_launchd_loaded", lambda label: False)
    monkeypatch.setattr(
        host_setup, "_sudo_run", lambda cmd, **kw: events.append(cmd[1])
    )
    host_setup._launchd_replace("job", Path("/L/job.plist"))
    assert events == ["bootstrap"]
