"""ufw and firewalld: trusting fcbr0 when one of them is running."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from ltvm_pkg import host_firewall as fw


def _done(
    rc: int = 0, out: str = "", err: str = ""
) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], rc, out, err)


class _Host:
    def __init__(
        self,
        firewalld: bool = False,
        added: str | None = "",
        zone: str | None = None,
    ) -> None:
        self.firewalld = firewalld
        self.added = added
        self.zone = zone
        self.cmds: list[list[str]] = []

    def __call__(self, cmd: list[str]) -> subprocess.CompletedProcess:
        self.cmds.append(cmd)
        if cmd[:2] == ["systemctl", "is-active"]:
            return _done(0 if self.firewalld else 3)
        if cmd[:3] == ["ufw", "show", "added"]:
            if self.added is None:
                return _done(1, err="ERROR: You need to be root")
            return _done(0, "Added user rules:\n" + self.added)
        if cmd[0] == "firewall-cmd" and cmd[1].startswith("--get-zone"):
            return _done(0, f"{self.zone}\n") if self.zone else _done(2)
        return _done(0)


@pytest.fixture
def ufw_conf(tmp_path: Path):
    conf = tmp_path / "ufw.conf"
    with (
        patch.object(fw, "UFW_CONF", conf),
        patch(
            "ltvm_pkg.host_firewall.shutil.which", return_value="/usr/sbin/x"
        ),
    ):
        yield conf


class TestActive:
    def test_installed_but_disabled_ufw_is_not_a_firewall(
        self, ufw_conf
    ) -> None:
        """Ubuntu's default.  Its unit reads "active" all the same,
        which is why that is not what is asked."""
        ufw_conf.write_text("ENABLED=no\nLOGLEVEL=low\n")
        with patch.object(fw, "_run", _Host()):
            assert fw.active() is None

    def test_enabled_ufw(self, ufw_conf) -> None:
        ufw_conf.write_text("# comment\nENABLED=yes\n")
        with patch.object(fw, "_run", _Host()):
            assert fw.active() == "ufw"

    def test_firewalld(self, ufw_conf) -> None:
        with patch.object(fw, "_run", _Host(firewalld=True)):
            assert fw.active() == "firewalld"


class TestTrustBridge:
    def test_ufw_adds_only_what_is_missing(self, ufw_conf) -> None:
        ufw_conf.write_text("ENABLED=yes\n")
        host = _Host(added="ufw allow in on fcbr0\nufw allow 22/tcp\n")
        with patch.object(fw, "_run", host):
            fw.trust_bridge()
        assert [c for c in host.cmds if c[0] == "ufw" and c[1] != "show"] == [
            ["ufw", "route", "allow", "in", "on", "fcbr0"],
            ["ufw", "route", "allow", "out", "on", "fcbr0"],
        ]

    def test_ufw_unreadable_adds_all(self, ufw_conf) -> None:
        """ufw skips a rule it already has, so adding all is safe."""
        ufw_conf.write_text("ENABLED=yes\n")
        host = _Host(added=None)
        with patch.object(fw, "_run", host):
            fw.trust_bridge()
        added = [c[1:] for c in host.cmds if c[0] == "ufw" and c[1] != "show"]
        assert added == [list(r) for r in fw.UFW_RULES]

    def test_firewalld_moves_the_bridge_now_and_for_good(
        self, ufw_conf
    ) -> None:
        host = _Host(firewalld=True, zone="public")
        with patch.object(fw, "_run", host):
            fw.trust_bridge()
        changes = [
            c for c in host.cmds if any("--change-interface" in a for a in c)
        ]
        assert ["--permanent" in c for c in changes] == [False, True]
        assert all("--zone=trusted" in c for c in changes)

    def test_firewalld_already_trusting_is_left_alone(self, ufw_conf) -> None:
        host = _Host(firewalld=True, zone="trusted")
        with patch.object(fw, "_run", host):
            fw.trust_bridge()
        assert not any("--change-interface=fcbr0" in c for c in host.cmds)

    def test_no_firewall_does_nothing(self, ufw_conf) -> None:
        ufw_conf.write_text("ENABLED=no\n")
        host = _Host()
        with patch.object(fw, "_run", host):
            fw.trust_bridge()
        assert all(c[0] == "systemctl" for c in host.cmds)


class TestStatus:
    def test_untrusted_firewalld(self, ufw_conf) -> None:
        with patch.object(fw, "_run", _Host(firewalld=True, zone="public")):
            assert fw.status() == {"active": "firewalld", "trusted": False}

    def test_ufw_as_non_root_cannot_tell(self, ufw_conf) -> None:
        ufw_conf.write_text("ENABLED=yes\n")
        with (
            patch.object(fw, "_run", _Host()),
            patch("ltvm_pkg.host_firewall.os.geteuid", return_value=1000),
        ):
            assert fw.status() == {"active": "ufw", "trusted": None}

    def test_ufw_as_root_with_all_rules(self, ufw_conf) -> None:
        ufw_conf.write_text("ENABLED=yes\n")
        added = "".join("ufw " + " ".join(r) + "\n" for r in fw.UFW_RULES)
        with (
            patch.object(fw, "_run", _Host(added=added)),
            patch("ltvm_pkg.host_firewall.os.geteuid", return_value=0),
        ):
            assert fw.status() == {"active": "ufw", "trusted": True}
