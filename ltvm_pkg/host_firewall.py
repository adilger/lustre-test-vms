"""The host firewall, when one runs: have it trust the VM bridge.

qemu-bridge.service adds plain iptables rules for fcbr0, which are the
whole story only on a host with no firewall manager.  With one running:

* ufw's INPUT policy is DROP, so what the guests send the host itself --
  DNS and DHCP to ltvm-dnsmasq -- is dropped, and it rebuilds its chains
  from its own configuration on every reload and boot;
* firewalld filters from its own nftables table, and a packet must pass
  every table's chain, so an iptables ACCEPT does not overrule its zones.

So when one is active, install tells it, in its own persistent
configuration, to trust fcbr0.  The NAT rule stays in qemu-bridge.service:
neither manager touches a nat rule it did not add.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

log = logging.getLogger("ltvm")

BRIDGE = "fcbr0"
UFW_CONF = Path("/etc/ufw/ufw.conf")
# ``ufw show added`` lists rules as the commands that made them.
UFW_RULES = (
    ("allow", "in", "on", BRIDGE),
    ("route", "allow", "in", "on", BRIDGE),
    ("route", "allow", "out", "on", BRIDGE),
)
FIREWALLD_ZONE = "trusted"


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(cmd, capture_output=True, text=True)
    except OSError as e:
        return subprocess.CompletedProcess(cmd, 127, "", str(e))


def _ufw_enabled() -> bool:
    # Not `systemctl is-active ufw`: the unit is a oneshot, "active" once
    # it has run whether or not the firewall is on.
    try:
        text = UFW_CONF.read_text()
    except OSError:
        return False
    for line in text.splitlines():
        key, _, value = line.partition("=")
        if key.strip() == "ENABLED":
            return value.strip().strip("\"'").lower() == "yes"
    return False


def active() -> str | None:
    """``"firewalld"``, ``"ufw"``, or None when neither is running."""
    if (
        shutil.which("firewall-cmd")
        and _run(["systemctl", "is-active", "--quiet", "firewalld"]).returncode
        == 0
    ):
        return "firewalld"
    if shutil.which("ufw") and _ufw_enabled():
        return "ufw"
    return None


def _firewalld_zone() -> str | None:
    r = _run(["firewall-cmd", f"--get-zone-of-interface={BRIDGE}"])
    return r.stdout.strip() if r.returncode == 0 else None


def _ufw_missing() -> list[tuple[str, ...]] | None:
    """The ufw rules ltvm wants that are not there; None if unreadable."""
    r = _run(["ufw", "show", "added"])
    if r.returncode != 0:
        return None
    have = {" ".join(line.split()) for line in r.stdout.splitlines()}
    return [rule for rule in UFW_RULES if "ufw " + " ".join(rule) not in have]


def trust_bridge() -> None:
    """Have the running firewall, if any, let fcbr0's traffic through."""
    kind = active()
    if kind == "ufw":
        missing = _ufw_missing()
        if missing is None:
            missing = list(UFW_RULES)
        for rule in missing:
            r = _run(["ufw", *rule])
            if r.returncode != 0:
                raise RuntimeError(
                    f"ufw {' '.join(rule)} failed: {r.stderr.strip()}"
                )
        if missing:
            log.info("ufw: allowed traffic in on and routed through %s", BRIDGE)
    elif kind == "firewalld":
        if _firewalld_zone() == FIREWALLD_ZONE:
            return
        # Runtime and permanent both: the permanent one is what a
        # reload or a boot restores, and it does not apply until then.
        for extra in ([], ["--permanent"]):
            r = _run(
                [
                    "firewall-cmd",
                    *extra,
                    f"--zone={FIREWALLD_ZONE}",
                    f"--change-interface={BRIDGE}",
                ]
            )
            if r.returncode != 0:
                raise RuntimeError(
                    f"firewall-cmd {' '.join(extra)} --zone={FIREWALLD_ZONE} "
                    f"--change-interface={BRIDGE} failed: {r.stderr.strip()}"
                )
        log.info("firewalld: put %s in the %s zone", BRIDGE, FIREWALLD_ZONE)


def status() -> dict[str, Any]:
    """For ``install --verify``.  ``trusted`` is None when it cannot be
    read as this user (ufw lists its rules only to root)."""
    kind = active()
    trusted: bool | None = True
    if kind == "ufw":
        missing = _ufw_missing() if os.geteuid() == 0 else None
        trusted = None if missing is None else not missing
    elif kind == "firewalld":
        trusted = _firewalld_zone() == FIREWALLD_ZONE
    return {"active": kind, "trusted": trusted}
