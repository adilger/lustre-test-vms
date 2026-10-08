"""Kerberos for a cluster: the setup Lustre's sanity-krb5 needs.

The KDC runs on the cluster's first MDS node (the MGS node when it has
none).  Every node gets krb5.conf naming it, its own lustre_{mgs,mds,
oss,root}/<node> and host/<node> keys in /etc/krb5.keytab, and the
request-key line for lgss_keyring; the test users get password
principals.  The guest-side work is krb5-node.sh and krb5-kdc.sh beside
this module.

A single VM cannot run sanity-krb5 at all: every connection there is
over 0@lo, and sptlrpc applies no flavor on 0@lo (LU-13343).
"""

from __future__ import annotations

import argparse
import shlex
import subprocess
from pathlib import Path

from . import vm_claim
from .qemu_run import die
from .vm_net import sshpass_ssh_argv
from .vm_state import ClusterInfo, ClusterNode, VMInfo

NODE_SCRIPT = Path(__file__).parent / "krb5-node.sh"
KDC_SCRIPT = Path(__file__).parent / "krb5-kdc.sh"

DEFAULT_REALM = "LTVM.TEST"
# sanity-krb5's runas users (uid 500/501 in the image)
DEFAULT_USERS = ("sanityusr", "sanityusr1")


def _ssh(
    ip: str, command: str, *, stdin: str | bytes | None = None, timeout: int
) -> subprocess.CompletedProcess:
    return subprocess.run(
        sshpass_ssh_argv(ip, command),
        input=stdin,
        capture_output=True,
        timeout=timeout,
    )


def _run_script(
    node: ClusterNode, ip: str, script: Path, args: list[str], timeout: int
) -> None:
    cmd = "bash -s -- " + " ".join(shlex.quote(a) for a in args)
    r = _ssh(ip, cmd, stdin=script.read_bytes(), timeout=timeout)
    if r.returncode != 0:
        out = (r.stdout + r.stderr).decode(errors="replace").strip()
        die(f"{script.name} failed on {node.name} (rc={r.returncode}):\n{out}")


def kdc_node(cluster: ClusterInfo) -> ClusterNode:
    mds = cluster.mds_nodes()
    return mds[0] if mds else cluster.mgs_node()


def setup(
    cluster: ClusterInfo,
    *,
    realm: str = DEFAULT_REALM,
    users: tuple[str, ...] = DEFAULT_USERS,
    timeout: int = 600,
) -> None:
    nodes = cluster.get_nodes()
    ips = {n.name: VMInfo.load(n.name).ip for n in nodes}
    kdc = kdc_node(cluster)

    for n in nodes:
        print(f"{n.name}: krb5 client configuration", flush=True)
        _run_script(n, ips[n.name], NODE_SCRIPT, [realm, kdc.name], timeout)

    print(f"{kdc.name}: KDC for {realm}", flush=True)
    _run_script(
        kdc,
        ips[kdc.name],
        KDC_SCRIPT,
        [realm, " ".join(users), *(n.name for n in nodes)],
        timeout,
    )

    for n in nodes:
        r = _ssh(
            ips[kdc.name], f"cat /root/krb5-keytabs/{n.name}.keytab", timeout=60
        )
        if r.returncode != 0 or not r.stdout:
            die(f"cannot read {n.name}'s keytab from {kdc.name}")
        smoke = (
            "cat > /etc/krb5.keytab && chmod 600 /etc/krb5.keytab && "
            f"kinit -k -t /etc/krb5.keytab lustre_root/{n.name}@{realm} && "
            "kdestroy"
        )
        for u in users:
            smoke += (
                f" && su - {u} -c 'echo {u} | kinit > /dev/null && "
                "klist -s && kdestroy'"
            )
        r = _ssh(ips[n.name], smoke, stdin=r.stdout, timeout=120)
        if r.returncode != 0:
            out = (r.stdout + r.stderr).decode(errors="replace").strip()
            die(f"{n.name}: keytab or kinit check failed:\n{out}")
        print(f"{n.name}: keytab installed, kinit OK", flush=True)

    print(f"krb5 ready: realm {realm}, KDC {kdc.name}")


def cmd_cluster_krb5(args: argparse.Namespace) -> None:
    from .vm_cluster import _node_state

    cluster = ClusterInfo.load(args.name)
    names = [n.name for n in cluster.get_nodes()]
    vm_claim.require_all(names, "krb5")
    down = [n for n in names if _node_state(n) != "up"]
    if down:
        die(
            f"cluster '{cluster.name}' has nodes not running: "
            f"{', '.join(down)}\n"
            f"  start them with: ltvm cluster start {cluster.name}"
        )
    from .vm_commands import _os_family_for_vm

    family = _os_family_for_vm(VMInfo.load(names[0]), "krb5")
    if family != "rhel":
        die(f"cluster krb5 supports rhel-family targets only, not {family}")
    setup(cluster, realm=args.realm, timeout=args.timeout)
