"""The Lustre part of `ltvm cluster status`: the targets of each server
with their recovery state, and the imports of each client.

A node that does not answer in a few seconds is reported as unreachable.
A failover test often leaves a node frozen, down or cut off, and status
must not wait for it.
"""

from __future__ import annotations

import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .qemu_run import is_running
from .vm_net import run_ssh
from .vm_state import ClusterInfo, ClusterNode, VMInfo, VMNotFound

_SSH_PROBE = 10
_TARGET_TYPES = ("mgs", "mdt", "obdfilter")
_HEALTHY = ("FULL", "IDLE")
_RECOVERY_KEYS = (
    "status",
    "connected_clients",
    "completed_clients",
    "evicted_clients",
    "time_remaining",
    "recovery_duration",
)


def parse_targets(dl: str, recovery: str) -> list[dict[str, Any]]:
    """Targets set up on a node, from `lctl dl` and recovery_status."""
    status: dict[str, dict[str, str]] = {}
    current = None
    for line in recovery.splitlines():
        # A one-line value follows the "=" (status: INACTIVE).
        m = re.match(r"^\w+\.([^.]+)\.recovery_status=(.*)$", line)
        if m:
            current = status.setdefault(m.group(1), {})
            line = m.group(2)
        key, sep, value = line.strip().partition(": ")
        if current is not None and sep and key in _RECOVERY_KEYS:
            current[key] = value
    targets = []
    for line in dl.splitlines():
        fields = line.split()
        if len(fields) >= 4 and fields[2] in _TARGET_TYPES:
            name = fields[3]
            targets.append({"target": name, **status.get(name, {})})
    return targets


def parse_imports(text: str) -> list[dict[str, str]]:
    """Client imports, from `lctl get_param {mgc,mdc,osc}.*.import`."""
    imports: list[dict[str, str]] = []
    for block in re.split(r"^\S+\.import=$", text, flags=re.M)[1:]:
        row = {}
        for key in ("target", "state", "current_connection"):
            m = re.search(rf"^\s+{key}: (.*)$", block, re.M)
            if m:
                row[key] = m.group(1).strip().strip('"')
        if row:
            imports.append(row)
    return imports


def _probe(node: ClusterNode) -> dict[str, Any]:
    try:
        vm = VMInfo.load(node.name)
    except (VMNotFound, ValueError):
        # The node table already says "missing" or "corrupt".
        return {"node": node.name, "reachable": False}
    if not is_running(vm):
        return {"node": node.name, "reachable": False}
    cmd = (
        "lctl dl 2>/dev/null; echo @@; "
        "lctl get_param '*.*.recovery_status' 2>/dev/null; echo @@; "
        "lctl get_param 'mgc.*.import' 'mdc.*.import' 'osc.*.import' "
        "2>/dev/null"
    )
    try:
        r = run_ssh(vm.ip, cmd, timeout=_SSH_PROBE, retry_connect=False)
    except subprocess.TimeoutExpired:
        return {"node": node.name, "reachable": False}
    # lctl's own status means nothing here: get_param fails on a server
    # with no osc.  Only ssh's 255 says the node did not answer.
    parts = (r.stdout or "").split("@@\n")
    if r.returncode == 255 or len(parts) != 3:
        return {"node": node.name, "reachable": False}
    row: dict[str, Any] = {"node": node.name, "reachable": True}
    if node.is_server:
        row["targets"] = parse_targets(parts[0], parts[1])
    if node.is_client:
        row["imports"] = parse_imports(parts[2])
    return row


def lustre_state(cluster: ClusterInfo) -> list[dict[str, Any]]:
    """Each node's targets and recovery state, or its client imports.

    A node that does not answer in a few seconds (frozen, down, link
    cut) is reported as unreachable, not waited for.
    """
    nodes = cluster.get_nodes()
    with ThreadPoolExecutor(max_workers=len(nodes) or 1) as pool:
        return list(pool.map(_probe, nodes))


# A target whose recovery has ended, or never started.  The MGS has no
# recovery_status, so no status counts as ended too.
_RECOVERY_DONE = ("", "COMPLETE", "INACTIVE")


def recovery_done(rows: list[dict[str, Any]]) -> bool:
    """True when no target on a reachable node is still recovering."""
    return all(
        t.get("status", "") in _RECOVERY_DONE
        for row in rows
        for t in row.get("targets", [])
    )


def wait_recovery(
    cluster: ClusterInfo, timeout: int, interval: int = 5
) -> tuple[list[dict[str, Any]], bool]:
    """Probe until recovery_done() or *timeout* seconds pass.

    Returns the last probe and whether recovery ended.  Only a caller
    that asks waits: in a test, recovery may never end.
    """
    deadline = time.monotonic() + timeout
    while True:
        rows = lustre_state(cluster)
        if recovery_done(rows):
            return rows, True
        left = deadline - time.monotonic()
        if left <= 0:
            return rows, False
        time.sleep(min(interval, left))


def print_lustre_state(rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    print()
    print("lustre:")
    for row in rows:
        if not row["reachable"]:
            print(f"  {row['node']:<20} unreachable")
            continue
        if row.get("targets") == []:
            print(f"  {row['node']:<20} no targets")
        for t in row.get("targets", []):
            state = t.get("status", "")
            done = t.get("completed_clients")
            if state == "RECOVERING":
                state += (
                    f" {done or '?'} done, {t.get('connected_clients', '?')}"
                    f" connected, {t.get('time_remaining', '?')}s left"
                )
            elif done:
                state += f" {done} clients"
            print(f"  {row['node']:<20} {t['target']:<20} {state}")
        imports = row.get("imports", [])
        if imports:
            counts: dict[str, int] = {}
            for i in imports:
                counts[i.get("state", "?")] = (
                    counts.get(i.get("state", "?"), 0) + 1
                )
            summary = ", ".join(f"{n} {s}" for s, n in sorted(counts.items()))
            print(f"  {row['node']:<20} {len(imports)} imports: {summary}")
            for i in imports:
                # IDLE is an idle OSC that disconnected on purpose.
                if i.get("state") not in _HEALTHY:
                    print(
                        f"  {'':<20}   {i.get('target', '?')} "
                        f"{i.get('state', '?')} via "
                        f"{i.get('current_connection', '?')}"
                    )
