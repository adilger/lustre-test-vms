"""Tests for the telemetry client.

The network is never exercised: what matters here is the opt-out
precedence, the schedule, and the promise about what the payload
does and does not contain.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated XDG config + state, and no site config by default."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("LTVM_SITE_CONFIG", str(tmp_path / "absent.conf"))
    monkeypatch.delenv("LTVM_TELEMETRY", raising=False)
    # No module reload needed: telemetry resolves its paths per call, so
    # the env above is enough.  A reload would also quietly undo any
    # patching conftest has done.
    return tmp_path


# ---------------------------------------------------------------------------
# Opt-out precedence
# ---------------------------------------------------------------------------


def test_enabled_by_default(_home: Path) -> None:
    import ltvm_pkg.telemetry as t

    assert t.is_enabled() is True


def test_env_kill_switch(_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import ltvm_pkg.telemetry as t

    for value in ("0", "no", "false"):
        monkeypatch.setenv("LTVM_TELEMETRY", value)
        assert t.is_enabled() is False, value


def test_user_opt_out(_home: Path) -> None:
    import ltvm_pkg.telemetry as t

    t.set_enabled(False)
    assert t.is_enabled() is False
    t.set_enabled(True)
    assert t.is_enabled() is True


def test_site_config_disables(_home: Path) -> None:
    import ltvm_pkg.telemetry as t

    t._site_config().write_text("[telemetry]\nenabled = false\n")
    assert t.is_enabled() is False


def test_site_opt_out_beats_the_user(_home: Path) -> None:
    """A site opt-out a user could silently undo would not be one."""
    import ltvm_pkg.telemetry as t

    t._site_config().write_text("[telemetry]\nenabled = false\n")
    t.set_enabled(True)
    assert t.is_enabled() is False
    assert "site-wide" in (t.status()["disabled_by"] or "")


def test_unreadable_site_config_is_ignored(_home: Path) -> None:
    """A broken /etc/ltvm.conf must not be a broken ltvm."""
    import ltvm_pkg.telemetry as t

    t._site_config().write_text("this is not ini [[[\n")
    assert t.is_enabled() is True


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


def test_install_id_is_stable_and_random(_home: Path) -> None:
    import ltvm_pkg.telemetry as t

    first = t.install_id()
    assert t.install_id() == first
    # Not derived from anything about the host.
    import socket
    import uuid as _uuid

    assert socket.gethostname() not in first
    assert _uuid.UUID(first).version == 4


def test_config_write_preserves_update_check(_home: Path) -> None:
    """telemetry and update_check share config.json.

    Clobbering someone's update mode to record an install ID would be
    a memorable bug; this is the test that stops it.
    """
    import ltvm_pkg.telemetry as t

    t._config_dir().mkdir(parents=True, exist_ok=True)
    t._config_file().write_text(json.dumps({"update_check": {"mode": "never"}}))
    t.install_id()
    data = json.loads(t._config_file().read_text())
    assert data["update_check"]["mode"] == "never"
    assert data["telemetry"]["install_id"]


# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------


def test_due_when_never_sent(_home: Path) -> None:
    import ltvm_pkg.telemetry as t

    assert t._due_for_send({"last_send_iso": None}, "abc") is True


def test_not_due_the_next_day(_home: Path) -> None:
    import ltvm_pkg.telemetry as t

    state = {
        "last_send_iso": (
            datetime.now(timezone.utc) - timedelta(days=1)
        ).isoformat()
    }
    assert t._due_for_send(state, "abc") is False


def test_due_after_the_interval_plus_jitter(_home: Path) -> None:
    import ltvm_pkg.telemetry as t

    state = {
        "last_send_iso": (
            datetime.now(timezone.utc) - timedelta(days=9)
        ).isoformat()
    }
    assert t._due_for_send(state, "abc") is True


def test_jitter_is_stable_and_bounded(_home: Path) -> None:
    """Stable per install, so it does not move between runs."""
    import ltvm_pkg.telemetry as t

    assert t._jitter("abc") == t._jitter("abc")
    assert t._jitter("abc") != t._jitter("xyz")
    for uid in ("a", "b", "c", "d", "e"):
        assert abs(t._jitter(uid)) <= timedelta(hours=12)


def test_corrupt_stamp_is_treated_as_due(_home: Path) -> None:
    import ltvm_pkg.telemetry as t

    assert t._due_for_send({"last_send_iso": "not a date"}, "abc") is True


# ---------------------------------------------------------------------------
# The payload promise
# ---------------------------------------------------------------------------


def test_payload_is_a_closed_list(_home: Path) -> None:
    """The set of fields is the product promise, so pin it.

    A new field here is a deliberate act that has to update this test
    and the docstring's never-send list -- not something that arrives
    with an unrelated change.
    """
    import ltvm_pkg.telemetry as t

    assert set(t._payload()) == {
        "schema",
        "install_id",
        "sent_at",
        "ltvm_version",
        "host",
        "since",
        "targets",
        "commands",
        "options",
        "callers",
    }
    assert set(t._payload()["host"]) == {
        "os",
        "os_version",
        "arch",
        "wsl",
        "python",
        "qemu",
        "ram_gb",
    }


def _payload_strings(value: object) -> Iterator[str]:
    """Every string *value* in a payload, recursively."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _payload_strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _payload_strings(v)


# Distinctive enough that they cannot collide with anything the payload
# legitimately carries, which is the whole point of using them instead
# of the machine's real name -- see the test below.
SENTINEL_HOST = "ZZ-SENTINEL-HOSTNAME-ZZ"
SENTINEL_USER = "ZZ-SENTINEL-USERNAME-ZZ"
SENTINEL_HOME = "/ZZ-SENTINEL-HOME-ZZ"


def _mask_machine_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every way of asking "who and where am I" return a sentinel."""
    import getpass
    import platform
    import socket

    monkeypatch.setattr(socket, "gethostname", lambda: SENTINEL_HOST)
    monkeypatch.setattr(socket, "getfqdn", lambda *a: SENTINEL_HOST)
    monkeypatch.setattr(platform, "node", lambda: SENTINEL_HOST)
    monkeypatch.setattr(getpass, "getuser", lambda: SENTINEL_USER)
    monkeypatch.setenv("HOSTNAME", SENTINEL_HOST)
    monkeypatch.setenv("USER", SENTINEL_USER)
    monkeypatch.setenv("LOGNAME", SENTINEL_USER)
    # Path.home() and expanduser("~") both prefer $HOME on POSIX.  The
    # _home fixture has already pointed XDG_* at a tmpdir, so nothing
    # resolves state through this.
    monkeypatch.setenv("HOME", SENTINEL_HOME)


def test_payload_leaks_nothing_identifying(
    _home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing that names the machine or its user may leave it.

    The machine's identity is replaced with sentinels first, rather than
    searching the payload for whatever this host happens to be called.
    Comparing against the real name made the test's behaviour depend on
    the name, and it failed both ways: a host named `vm` failed with no
    leak present ("vm" is inside the key "ltvm_version"), and narrowing
    the match to dodge that left it blind to a genuine leak on the same
    host, because a two-character name cannot be matched as a substring
    without noise.  A host named `ubuntu` or `x86_64` -- both plausible
    -- would have failed too, by colliding with a legitimate value.

    A sentinel collides with nothing, so the whole serialized blob can
    be searched, keys included, and the result is the same on every
    machine.
    """
    import ltvm_pkg.telemetry as t

    _mask_machine_identity(monkeypatch)
    blob = json.dumps(t._payload())
    for what, sentinel in (
        ("hostname", SENTINEL_HOST),
        ("username", SENTINEL_USER),
        ("home directory", SENTINEL_HOME),
    ):
        assert sentinel not in blob, f"payload carries the {what}"


def test_payload_carries_no_filesystem_paths(_home: Path) -> None:
    """Not one payload string may look like a path.

    The sentinels above only catch a leak that went through an accessor
    they patch.  This catches the rest of the class -- a cwd, a Lustre
    tree, an artifacts dir -- on the invariant instead: nothing ltvm
    legitimately sends contains a slash (versions, an ISO timestamp, a
    uuid, os/arch names and a RAM bucket all do without one), and error
    sites are exactly where paths come from.
    """
    import ltvm_pkg.telemetry as t

    for s in _payload_strings(t._payload()):
        assert "/" not in s, f"payload carries a path: {s!r}"


def test_payload_does_not_depend_on_the_machine_name(
    _home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two machines differing only in identity must send the same thing.

    The strongest form of the property, and the one that cannot give a
    false answer either way: it never consults this host's real name, so
    a host called `ubuntu` (which equals the `os` value) or `x86_64`
    (the `arch` value) cannot fail it for a leak it does not have --
    which is what used to happen.
    """
    import getpass
    import platform
    import socket

    import ltvm_pkg.telemetry as t

    def render(host: str, user: str, home: str) -> dict:
        monkeypatch.setattr(socket, "gethostname", lambda: host)
        monkeypatch.setattr(socket, "getfqdn", lambda *a: host)
        monkeypatch.setattr(platform, "node", lambda: host)
        monkeypatch.setattr(getpass, "getuser", lambda: user)
        monkeypatch.setenv("HOSTNAME", host)
        monkeypatch.setenv("USER", user)
        monkeypatch.setenv("LOGNAME", user)
        monkeypatch.setenv("HOME", home)
        payload = t._payload()
        # Clocks, not identity: both are read from time.now() on a call
        # with no counters file yet, so they differ by microseconds
        # between the two renders.  install_id is minted once into the
        # fixture's tmpdir config, so it is stable across both -- which
        # is itself worth having in the comparison.
        payload.pop("sent_at")
        payload.pop("since")
        return payload

    assert render("ubuntu", "alice", "/home/alice") == render(
        "x86_64", "bob", "/var/home/bob"
    )


def test_preview_matches_what_send_would_post(_home: Path) -> None:
    import ltvm_pkg.telemetry as t

    with patch.object(t, "_post", return_value=True) as mock_post:
        t.send_now()
    posted = mock_post.call_args[0][0]
    assert set(posted) == set(t.preview())


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------


def test_stamp_moves_even_when_the_send_fails(_home: Path) -> None:
    """No retry storms: a missed week is just a missed week."""
    import ltvm_pkg.telemetry as t

    with patch.object(t, "_post", return_value=False):
        assert t.send_now() is False
    assert t._load_state()["last_send_iso"] is not None


def test_first_run_notices_and_sends(
    _home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Opt-out: the notice does not hold the first check-in back.

    Delaying it a week would be opt-in behaviour, and would lose every
    install used for a few days and dropped.
    """
    import ltvm_pkg.telemetry as t

    with patch.object(t, "_spawn_detached_send") as mock_spawn:
        t.maybe_send()
    mock_spawn.assert_called_once()
    assert "anonymous usage telemetry" in capsys.readouterr().err


def test_notice_prints_once_only(
    _home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import ltvm_pkg.telemetry as t

    with patch.object(t, "_spawn_detached_send"):
        t.maybe_send()
        capsys.readouterr()
        for _ in range(3):
            t.maybe_send()
    assert capsys.readouterr().err == ""


def test_sends_once_due(_home: Path) -> None:
    """And exactly once: the parent marks the attempt before spawning,
    so a loop of ltvm invocations does not spawn a sender each time."""
    import ltvm_pkg.telemetry as t

    with patch.object(t, "_spawn_detached_send") as mock_spawn:
        t.maybe_send()  # first run: notice + send
        t.maybe_send()  # too soon, nothing
        assert mock_spawn.call_count == 1
        t._save_state(
            {
                "last_send_iso": (
                    datetime.now(timezone.utc) - timedelta(days=9)
                ).isoformat()
            }
        )
        t.maybe_send()
    assert mock_spawn.call_count == 2


def test_disabled_never_spawns(_home: Path) -> None:
    import ltvm_pkg.telemetry as t

    t.set_enabled(False)
    with patch.object(t, "_spawn_detached_send") as mock_spawn:
        t.maybe_send()
    mock_spawn.assert_not_called()


def test_never_raises_when_the_config_dir_is_unwritable(
    _home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A telemetry failure must never be able to fail a command."""
    import ltvm_pkg.telemetry as t

    def _boom(*a: object, **kw: object) -> None:
        raise OSError("read-only filesystem")

    monkeypatch.setattr(Path, "write_text", _boom)
    monkeypatch.setattr(Path, "mkdir", _boom)
    t._save_config({"enabled": True})
    t._save_state({"last_send_iso": None})


# ---------------------------------------------------------------------------
# Counters
# ---------------------------------------------------------------------------


class _Args:
    """Stand-in for the argparse namespace main() hands to record()."""

    def __init__(self, func_name: str = "cmd_list", **kw: object) -> None:
        def handler() -> None:
            pass

        handler.__name__ = func_name
        self.func = handler
        for k, v in kw.items():
            setattr(self, k, v)


def test_command_label_comes_from_the_handler(_home: Path) -> None:
    import ltvm_pkg.telemetry as t

    # A handler already named for its command path is used as-is.
    assert (
        t._command_label(_Args("cmd_build_lustre", command="build"))
        == "build_lustre"
    )
    # A shared handler gets its group prefixed, so `build status` does
    # not land on the dashboard as a bare "status".
    assert (
        t._command_label(_Args("cmd_status", command="build")) == "build.status"
    )
    # Anything not one of our handlers is not counted at all.
    assert t._command_label(_Args("something_else")) is None


def test_record_counts_commands_and_targets(_home: Path) -> None:
    import ltvm_pkg.telemetry as t

    t.record(_Args("cmd_deploy_lustre", target="rocky9"), 0)
    t.record(_Args("cmd_deploy_lustre", target="rocky9"), 1)
    t.record(_Args("cmd_build_lustre", target="rocky10"), 0)
    counters = t._load_counters()
    assert counters["commands"] == {
        "deploy_lustre": {"ok": 1, "fail": 1},
        "build_lustre": {"ok": 1, "fail": 0},
    }
    assert counters["targets"] == {"rocky9": 2, "rocky10": 1}


def test_unknown_target_name_is_not_transmitted(_home: Path) -> None:
    """A target someone added themselves names their site, not ours."""
    import ltvm_pkg.telemetry as t

    t.record(_Args("cmd_build_lustre", target="acme-internal-fs"), 0)
    counters = t._load_counters()
    assert counters["targets"] == {"other": 1}
    assert "acme" not in json.dumps(counters)


def test_unknown_variant_is_not_transmitted(_home: Path) -> None:
    import ltvm_pkg.telemetry as t

    t.record(_Args("cmd_build_lustre", variant="customer-x"), 0)
    counters = t._load_counters()
    assert counters["options"] == {"variant:other": 1}
    assert "customer" not in json.dumps(counters)


def test_record_counts_options(_home: Path) -> None:
    import ltvm_pkg.telemetry as t

    t.record(_Args("cmd_build_lustre", zfs=True, variant="mofed"), 0)
    counters = t._load_counters()
    assert counters["options"] == {"zfs": 1, "variant:mofed": 1}


def test_counter_keys_are_bounded(_home: Path) -> None:
    import ltvm_pkg.telemetry as t

    for i in range(t.MAX_COUNTER_KEYS + 20):
        t._bump(t._load_counters()["targets"], f"k{i}")
    table: dict[str, object] = {}
    for i in range(t.MAX_COUNTER_KEYS + 20):
        t._bump(table, f"k{i}")
    assert len(table) == t.MAX_COUNTER_KEYS


def test_failures_are_counted_but_not_explained(_home: Path) -> None:
    """A rate says where to look.  A reason would carry a path."""
    import ltvm_pkg.telemetry as t

    t.record(_Args("cmd_deploy_lustre", target="rocky9"), 1)
    blob = json.dumps(t._load_counters())
    assert '"fail": 1' in blob
    for leaky in ("Traceback", "/home/", "Error", "errno"):
        assert leaky not in blob


def test_record_is_a_noop_when_disabled(_home: Path) -> None:
    import ltvm_pkg.telemetry as t

    t.set_enabled(False)
    t.record(_Args("cmd_list", target="rocky9"), 0)
    assert t._load_counters()["commands"] == {}


def test_counters_flush_only_on_a_successful_send(_home: Path) -> None:
    """A failed send keeps the week rather than losing it."""
    import ltvm_pkg.telemetry as t

    t.record(_Args("cmd_deploy_lustre", target="rocky9"), 0)

    with patch.object(t, "_post", return_value=False):
        t.send_now()
    assert t._load_counters()["commands"] == {
        "deploy_lustre": {"ok": 1, "fail": 0}
    }

    with patch.object(t, "_post", return_value=True):
        t.send_now()
    assert t._load_counters()["commands"] == {}


def test_payload_carries_the_counters(_home: Path) -> None:
    import ltvm_pkg.telemetry as t

    t.record(_Args("cmd_deploy_lustre", target="rocky9", zfs=True), 0)
    payload = t._payload()
    assert payload["commands"] == {"deploy_lustre": {"ok": 1, "fail": 0}}
    assert payload["targets"] == {"rocky9": 1}
    assert payload["options"] == {"zfs": 1}
    assert payload["since"]


# ---------------------------------------------------------------------------
# Caller
# ---------------------------------------------------------------------------


class _Stream:
    def __init__(self, tty: bool) -> None:
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


@pytest.fixture
def _no_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    """The test suite itself is often run by an agent."""
    import ltvm_pkg.telemetry as t

    for name in t.AGENT_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(t, "_agent_above_sudo", lambda: False)


def _streams(monkeypatch: pytest.MonkeyPatch, tty: bool) -> None:
    for name in ("stdin", "stdout", "stderr"):
        monkeypatch.setattr(sys, name, _Stream(tty))


def test_terminal_is_a_human(
    _home: Path, _no_agent: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ltvm_pkg.telemetry as t

    _streams(monkeypatch, True)
    assert t._caller() == "human"
    # `ltvm list --json | jq` at a prompt still has a terminal on stdin.
    monkeypatch.setattr(sys, "stdout", _Stream(False))
    assert t._caller() == "human"


def test_no_terminal_is_a_script(
    _home: Path, _no_agent: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ltvm_pkg.telemetry as t

    _streams(monkeypatch, False)
    assert t._caller() == "script"
    monkeypatch.setattr(sys, "stdin", None)
    assert t._caller() == "script"


def test_agent_wins_over_a_terminal(
    _home: Path, _no_agent: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex and Gemini CLI give their commands a pty."""
    import ltvm_pkg.telemetry as t

    _streams(monkeypatch, True)
    monkeypatch.setenv("CODEX_CI", "1")
    assert t._caller() == "agent"


def test_empty_agent_marker_is_not_an_agent(
    _home: Path, _no_agent: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ltvm_pkg.telemetry as t

    _streams(monkeypatch, False)
    monkeypatch.setenv("AI_AGENT", "")
    assert t._caller() == "script"


@pytest.mark.skipif(
    not Path("/proc/self/environ").exists(), reason="needs /proc"
)
@pytest.mark.parametrize(
    ("parent_name", "parent_agent", "expected"),
    [
        ("sudo", True, "agent"),
        ("sudo", False, "script"),
        ("python3", True, "script"),
    ],
)
def test_agent_is_found_above_sudo(
    _home: Path,
    tmp_path: Path,
    parent_name: str,
    parent_agent: bool,
    expected: str,
) -> None:
    """sudo clears the agent's variables; its own process keeps them.

    A real process chain stands in for agent -> sudo -> ltvm: a parent
    run through a link named `sudo` (so that is its comm) carries the
    marker, and the child gets a scrubbed environment, as sudo would
    give it.  The pytest process above them may well be under an agent
    itself, which is what the second case checks is not looked at.
    """
    repo = Path(__file__).resolve().parent.parent
    link = tmp_path / parent_name
    link.symlink_to(sys.executable)
    child_env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(repo)}
    parent_env = dict(child_env)
    if parent_agent:
        parent_env["CLAUDECODE"] = "1"
    child = "import ltvm_pkg.telemetry as t; print(t._caller())"
    parent = (
        "import json, subprocess, sys;"
        "r = subprocess.run([sys.argv[1], '-c', sys.argv[2]],"
        " env=json.loads(sys.argv[3]), stdin=subprocess.DEVNULL,"
        " capture_output=True, text=True);"
        "print(r.stdout.strip() or r.stderr)"
    )
    out = subprocess.run(
        [
            str(link),
            "-c",
            parent,
            sys.executable,
            child,
            json.dumps(child_env),
        ],
        env=parent_env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.stdout.strip() == expected, out.stdout + out.stderr


def test_record_counts_callers(_home: Path) -> None:
    import ltvm_pkg.telemetry as t

    # As an ltvm from before the split wrote it.
    t._save_counters(
        {
            "since": t._now_iso(),
            "targets": {},
            "commands": {"list": {"ok": 3, "fail": 0}},
            "options": {},
        }
    )
    for who in ("agent", "agent", "human"):
        with patch.object(t, "_caller", return_value=who):
            t.record(_Args("cmd_list"), 0)
    assert t._load_counters()["callers"] == {"agent": 2, "human": 1}
    assert t._payload()["callers"] == {"agent": 2, "human": 1}


def test_caller_carries_no_environment_value(
    _home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ltvm_pkg.telemetry as t

    monkeypatch.setenv("AI_AGENT", "ZZ-SENTINEL-AGENT-ZZ")
    t.record(_Args("cmd_list"), 0)
    blob = json.dumps(t._payload())
    assert "ZZ-SENTINEL-AGENT-ZZ" not in blob
    assert t._payload()["callers"] == {"agent": 1}


# ---------------------------------------------------------------------------
# Host facts
# ---------------------------------------------------------------------------


def test_buckets_never_report_an_exact_value(_home: Path) -> None:
    import ltvm_pkg.telemetry as t

    assert t._bucket(2, (4, 8, 16)) == "<4"
    assert t._bucket(12, (4, 8, 16)) == "8-15"
    assert t._bucket(64, (4, 8, 16)) == "16+"
    assert t._bucket(None, (4, 8)) is None


def test_host_info_leaks_nothing_identifying(_home: Path) -> None:
    import getpass
    import socket

    import ltvm_pkg.telemetry as t

    blob = json.dumps(t._host_info())
    for secret in (socket.gethostname(), getpass.getuser(), str(Path.home())):
        if secret:
            assert secret not in blob


def test_legacy_counter_shape_is_repaired(_home: Path) -> None:
    """A counters file from another ltvm version must not wedge a key.

    An earlier build counted commands as a bare integer.  Declining to
    touch an entry of the wrong shape meant that command never being
    counted again -- silently, and for as long as the file lived.
    """
    import ltvm_pkg.telemetry as t

    t._save_counters(
        {
            "since": t._now_iso(),
            "targets": {},
            "commands": {"list": 86},
            "options": {},
        }
    )
    t.record(_Args("cmd_list"), 0)
    assert t._load_counters()["commands"]["list"] == {"ok": 1, "fail": 0}
