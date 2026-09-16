"""A user without sudo gets a report or a clean error, never a traceback."""

from __future__ import annotations

import argparse
import subprocess
from typing import Any
from unittest.mock import patch

import pytest

from ltvm_pkg import priv
from ltvm_pkg.cli import setup as cli_setup
from ltvm_pkg.cli import vm as cli_vm
from ltvm_pkg.cli.util import EXIT_ERROR


@pytest.fixture(autouse=True)
def _reset_latch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(priv, "_sudo_refused", False)


def _completed(cmd: list[str], rc: int) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(cmd, rc, "", "")


def _ns(**kw: Any) -> argparse.Namespace:
    base: dict[str, Any] = {"json": False, "fix": False}
    base.update(kw)
    return argparse.Namespace(**base)


class TestSudoPrime:
    def test_refusal_raises_sudo_unavailable(self) -> None:
        calls: list[list[str]] = []

        def fake_run(cmd, *, check=True, quiet=False):
            calls.append(cmd)
            return _completed(cmd, 1)

        with (
            patch.object(priv.os, "geteuid", return_value=1001),
            patch.object(priv, "_run", side_effect=fake_run),
            pytest.raises(priv.SudoUnavailable, match="sudo refused"),
        ):
            priv.sudo_prime("ltvm start needs root")
        assert ["sudo", "-v"] in calls

    def test_later_sudo_run_never_prompts(self) -> None:
        calls: list[list[str]] = []

        def fake_run(cmd, *, check=True, quiet=False):
            calls.append(cmd)
            return _completed(cmd, 1)

        with (
            patch.object(priv.os, "geteuid", return_value=1001),
            patch.object(priv, "_run", side_effect=fake_run),
        ):
            with pytest.raises(priv.SudoUnavailable):
                priv.sudo_prime("x")
            calls.clear()
            priv.sudo_run(["ip", "link", "del", "tap-x"], check=False)
        assert calls == [["sudo", "-n", "ip", "link", "del", "tap-x"]]

    def test_ready_sudo_does_not_prompt(self) -> None:
        with (
            patch.object(priv, "sudo_ready", return_value=True),
            patch.object(priv, "_run") as run,
        ):
            priv.sudo_prime("x")
        run.assert_not_called()


class TestDoctorWithoutSudo:
    def test_report_does_not_touch_sudo(self) -> None:
        with (
            patch.object(priv, "sudo_prime") as sp,
            patch("ltvm_pkg.vm_commands.cmd_doctor", return_value=0) as doctor,
        ):
            rc = cli_setup.cmd_doctor(_ns())
        sp.assert_not_called()
        doctor.assert_called_once()
        assert rc == 0

    def test_fix_runs_when_sudo_refuses(self) -> None:
        with (
            patch.object(
                priv, "sudo_prime", side_effect=priv.SudoUnavailable("no")
            ),
            patch("ltvm_pkg.vm_commands.cmd_doctor", return_value=1) as doctor,
        ):
            rc = cli_setup.cmd_doctor(_ns(fix=True))
        doctor.assert_called_once()
        assert rc == 1


class TestLifecycleWithoutSudo:
    def test_create_reports_refusal(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with (
            patch.object(
                priv,
                "sudo_prime",
                side_effect=priv.SudoUnavailable(
                    "ltvm create needs root, and sudo refused"
                ),
            ),
            patch("ltvm_pkg.vm_commands.cmd_create") as create,
        ):
            rc = cli_setup.cmd_create(_ns(name="co1-x", dry_run=False))
        create.assert_not_called()
        assert rc == EXIT_ERROR
        assert "sudo refused" in capsys.readouterr().err

    def test_start_reports_refusal(self) -> None:
        with (
            patch.object(
                priv, "sudo_prime", side_effect=priv.SudoUnavailable("no")
            ),
            patch("ltvm_pkg.vm_commands.cmd_start") as start,
        ):
            rc = cli_vm.cmd_vm_start(_ns(names=["co1-x"]))
        start.assert_not_called()
        assert rc == EXIT_ERROR
