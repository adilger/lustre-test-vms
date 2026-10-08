"""ltvm cluster krb5: which node is the KDC, and what each node is sent."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from ltvm_pkg import cluster_krb5
from ltvm_pkg.vm_state import ClusterInfo


def _cluster(*nodes: tuple[str, list[str]]) -> ClusterInfo:
    return ClusterInfo(
        name="co9",
        nodes=[{"name": n, "roles": r} for n, r in nodes],
    )


def test_kdc_is_first_mds() -> None:
    c = _cluster(
        ("co9-mgs", ["mgs"]),
        ("co9-mds1", ["mds"]),
        ("co9-mds2", ["mds"]),
        ("co9-c1", ["client"]),
    )
    assert cluster_krb5.kdc_node(c).name == "co9-mds1"


def test_kdc_falls_back_to_mgs() -> None:
    c = _cluster(("co9-mgs", ["mgs", "oss"]), ("co9-c1", ["client"]))
    assert cluster_krb5.kdc_node(c).name == "co9-mgs"


def test_setup_configures_every_node_then_the_kdc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = _cluster(
        ("co9-mds", ["mgs", "mds"]),
        ("co9-oss", ["oss"]),
        ("co9-c1", ["client"]),
    )
    ips = {"co9-mds": "10.0.0.1", "co9-oss": "10.0.0.2", "co9-c1": "10.0.0.3"}
    monkeypatch.setattr(
        cluster_krb5.VMInfo, "load", lambda name: SimpleNamespace(ip=ips[name])
    )
    calls: list[tuple[str, str, object]] = []

    def fake_ssh(ip, command, *, stdin=None, timeout):
        calls.append((ip, command, stdin))
        out = (
            b"KEYTAB" if command.startswith("cat /root/krb5-keytabs/") else b""
        )
        return subprocess.CompletedProcess([], 0, out, b"")

    monkeypatch.setattr(cluster_krb5, "_ssh", fake_ssh)
    cluster_krb5.setup(c, realm="EX.TEST", users=("u1",))

    node_runs = [
        x for x in calls if x[2] == cluster_krb5.NODE_SCRIPT.read_bytes()
    ]
    assert [ip for ip, _, _ in node_runs] == list(ips.values())
    assert all(cmd == "bash -s -- EX.TEST co9-mds" for _, cmd, _ in node_runs)

    kdc_runs = [
        x for x in calls if x[2] == cluster_krb5.KDC_SCRIPT.read_bytes()
    ]
    assert len(kdc_runs) == 1
    ip, cmd, _ = kdc_runs[0]
    assert ip == "10.0.0.1"
    assert cmd == "bash -s -- EX.TEST u1 co9-mds co9-oss co9-c1"

    # each node's keytab comes from the KDC and goes to that node
    installs = [x for x in calls if x[2] == b"KEYTAB"]
    assert [ip for ip, _, _ in installs] == list(ips.values())
    for (ip, cmd, _), name in zip(installs, ips):
        assert f"lustre_root/{name}@EX.TEST" in cmd
        assert "su - u1" in cmd


def test_setup_stops_on_a_failing_node(monkeypatch: pytest.MonkeyPatch) -> None:
    c = _cluster(("co9-mds", ["mgs", "mds"]), ("co9-c1", ["client"]))
    monkeypatch.setattr(
        cluster_krb5.VMInfo, "load", lambda name: SimpleNamespace(ip=name)
    )

    def fake_ssh(ip, command, *, stdin=None, timeout):
        rc = 1 if ip == "co9-c1" else 0
        return subprocess.CompletedProcess([], rc, b"", b"reverse lookup")

    monkeypatch.setattr(cluster_krb5, "_ssh", fake_ssh)
    with pytest.raises(SystemExit):
        cluster_krb5.setup(c)
