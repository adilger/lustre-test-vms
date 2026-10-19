"""Failover pairs: two VMs that open one set of data disks.

A VM made with --share-disks lets one partner (--disks-from) attach its
data disks, and `cluster create --failover PARTNER=PRIMARY` builds such
a pair and writes the partner into local.sh as each target's failover
host.  None of it may change a VM or cluster that does not ask for it.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from ltvm_pkg import qemu_run, vm_cluster, vm_commands
from ltvm_pkg.vm_state import ClusterInfo, ClusterNode, VMInfo


@pytest.fixture
def vm_dirs(tmp_path: Path) -> Any:
    sockets = tmp_path / "sockets"
    overlays = tmp_path / "overlays"
    sockets.mkdir()
    overlays.mkdir()
    with (
        patch("ltvm_pkg.vm_state.SOCKETS", sockets),
        patch("ltvm_pkg.vm_state.OVERLAYS", overlays),
        patch("ltvm_pkg.vm_commands.SOCKETS", sockets),
        patch("ltvm_pkg.vm_commands.OVERLAYS", overlays),
        patch("ltvm_pkg.vm_cluster.SOCKETS", sockets),
    ):
        yield sockets, overlays


def _vm(name: str, mdt: int = 0, ost: int = 0, **kw: Any) -> VMInfo:
    vm = VMInfo(name=name, ip="192.0.2.10", mdt_disks=mdt, ost_disks=ost, **kw)
    vm.save()
    for n in range(1, mdt + ost + 1):
        vm.disk_path(n).write_text("")
    return vm


# ── VMInfo ───────────────────────────────────────────────


class TestVMInfoFields:
    def test_round_trip(self, vm_dirs: Any) -> None:
        _vm("g-a", share_disks=True)
        _vm("g-b", disks_from="g-a")
        assert VMInfo.load("g-a").share_disks is True
        assert VMInfo.load("g-a").disks_from == ""
        assert VMInfo.load("g-b").disks_from == "g-a"
        assert VMInfo.load("g-b").share_disks is False

    def test_old_info_file_loads_as_neither(self, vm_dirs: Any) -> None:
        sockets, _ = vm_dirs
        (sockets / "g-old.info").write_text("NAME=g-old\nIP=192.0.2.9\n")
        vm = VMInfo.load("g-old")
        assert vm.share_disks is False
        assert vm.disks_from == ""


# ── QEMU disk arguments ──────────────────────────────────


class TestDataDiskArgs:
    def test_plain_vm_is_unchanged(self, vm_dirs: Any) -> None:
        vm = _vm("g-a", mdt=1, ost=1)
        args = qemu_run.data_disk_args(vm)
        assert args == [
            "-device",
            "virtio-blk-pci,drive=disk1",
            "-drive",
            f"id=disk1,file={vm.disk_path(1)},format=raw,if=none,"
            "discard=unmap,detect-zeroes=unmap",
            "-device",
            "virtio-blk-pci,drive=disk2",
            "-drive",
            f"id=disk2,file={vm.disk_path(2)},format=raw,if=none,"
            "discard=unmap,detect-zeroes=unmap",
        ]

    def test_shared_disks_allow_a_second_writer(self, vm_dirs: Any) -> None:
        vm = _vm("g-a", mdt=1, share_disks=True)
        dev, drive = qemu_run.data_disk_args(vm)[1::2]
        assert dev.endswith(",share-rw=on")
        assert drive.endswith(",cache=none")

    def test_partner_attaches_the_primary_disks_in_order(
        self, vm_dirs: Any
    ) -> None:
        a = _vm("g-a", mdt=1, ost=2, share_disks=True)
        b = _vm("g-b", disks_from="g-a")
        args = qemu_run.data_disk_args(b)
        drives = args[3::4]
        assert [d.split(",")[1] for d in drives] == [
            f"file={a.disk_path(n)}" for n in (1, 2, 3)
        ]
        # Both sides must agree, or QEMU's lock refuses the second.
        assert args[1::2] == qemu_run.data_disk_args(a)[1::2]
        assert drives == qemu_run.data_disk_args(a)[3::4]

    def test_partner_of_an_unshared_vm_is_refused(self, vm_dirs: Any) -> None:
        _vm("g-a", mdt=1)
        b = _vm("g-b", disks_from="g-a")
        with pytest.raises(SystemExit):
            qemu_run.data_disk_args(b)

    def test_partner_of_a_missing_vm_is_refused(self, vm_dirs: Any) -> None:
        b = _vm("g-b", disks_from="g-gone")
        with pytest.raises(SystemExit):
            qemu_run.data_disk_args(b)


# ── create --share-disks / --disks-from ──────────────────


def _create_args(**kw: Any) -> argparse.Namespace:
    values: dict[str, Any] = {
        "name": "g-b",
        "mdt_disks": 1,
        "ost_disks": 2,
        "share_disks": False,
        "disks_from": None,
    }
    values.update(kw)
    return argparse.Namespace(**values)


class TestDestroyKeepsPartnerDisks:
    def test_primary_alone_is_refused(self, vm_dirs: Any) -> None:
        _vm("g-a", mdt=1, share_disks=True)
        _vm("g-b", disks_from="g-a")
        with pytest.raises(SystemExit):
            vm_commands._refuse_orphaned_partners(["g-a"])

    def test_pair_together_is_allowed(self, vm_dirs: Any) -> None:
        _vm("g-a", mdt=1, share_disks=True)
        _vm("g-b", disks_from="g-a")
        vm_commands._refuse_orphaned_partners(["g-a", "g-b"])
        vm_commands._refuse_orphaned_partners(["g-b"])


class TestValidateFailoverDisks:
    def _check(self, args: argparse.Namespace, typed: set[str] = set()) -> Any:
        with patch.object(vm_commands, "_explicit_flags", return_value=typed):
            return vm_commands._validate_failover_disks(args)

    def test_no_flags_change_nothing(self, vm_dirs: Any) -> None:
        args = _create_args()
        assert self._check(args) is None
        assert (args.mdt_disks, args.ost_disks) == (1, 2)

    def test_partner_gets_no_disks_of_its_own(self, vm_dirs: Any) -> None:
        _vm("g-a", mdt=1, share_disks=True)
        args = _create_args(disks_from="g-a")
        owner = self._check(args)
        assert owner.name == "g-a"
        assert (args.mdt_disks, args.ost_disks) == (0, 0)

    def test_typed_disk_count_is_refused(self, vm_dirs: Any) -> None:
        _vm("g-a", mdt=1, share_disks=True)
        with pytest.raises(SystemExit):
            self._check(_create_args(disks_from="g-a"), {"ost_disks"})

    def test_typed_zero_is_accepted(self, vm_dirs: Any) -> None:
        """cluster create passes --mdt-disks 0 --ost-disks 0 always."""
        _vm("g-a", mdt=1, share_disks=True)
        args = _create_args(disks_from="g-a", mdt_disks=0, ost_disks=0)
        assert self._check(args, {"mdt_disks", "ost_disks"}) is not None

    @pytest.mark.parametrize(
        "owner_kw",
        [
            {"mdt": 1},  # not --share-disks
            {"share_disks": True},  # no data disks
            {"mdt": 1, "share_disks": True, "disks_from": "g-x"},  # chain
        ],
    )
    def test_unusable_owner_is_refused(
        self, vm_dirs: Any, owner_kw: dict[str, Any]
    ) -> None:
        _vm("g-a", **owner_kw)
        with pytest.raises(SystemExit):
            self._check(_create_args(disks_from="g-a"))

    def test_missing_owner_is_refused(self, vm_dirs: Any) -> None:
        with pytest.raises(SystemExit):
            self._check(_create_args(disks_from="g-gone"))

    def test_own_disks_are_refused(self, vm_dirs: Any) -> None:
        with pytest.raises(SystemExit):
            self._check(_create_args(name="g-a", disks_from="g-a"))

    def test_a_third_vm_is_refused(self, vm_dirs: Any) -> None:
        _vm("g-a", mdt=1, share_disks=True)
        _vm("g-b", disks_from="g-a")
        with pytest.raises(SystemExit):
            self._check(_create_args(name="g-c", disks_from="g-a"))

    def test_both_flags_are_refused(self, vm_dirs: Any) -> None:
        _vm("g-a", mdt=1, share_disks=True)
        with pytest.raises(SystemExit):
            self._check(_create_args(disks_from="g-a", share_disks=True))


# ── cluster create --failover ────────────────────────────


class TestFailoverPartners:
    def _specs(self) -> list[ClusterNode]:
        return [
            vm_cluster.parse_node_spec(s)
            for s in ("mgs+mds:g-mds1:1", "oss:g-oss:2", "client:g-c")
        ]

    def test_partner_is_a_diskless_server(self) -> None:
        (p,) = vm_cluster.failover_partners(self._specs(), {"g-mds2": "g-mds1"})
        assert p.name == "g-mds2"
        assert p.failover_for == "g-mds1"
        assert (p.mdt_disks, p.ost_disks) == (0, 0)
        assert p.is_server
        assert not (p.is_mgs or p.is_mds or p.is_oss or p.is_client)

    def test_none_asked_for_adds_none(self) -> None:
        assert vm_cluster.failover_partners(self._specs(), {}) == []

    @pytest.mark.parametrize(
        "failover",
        [
            {"g-oss": "g-mds1"},  # partner is a node spec
            {"g-x": "g-nope"},  # unknown primary
            {"g-x": "g-c"},  # a client has no targets
            {"g-x": "g-mds1", "g-y": "g-mds1"},  # two partners
            {"bad name": "g-mds1"},  # invalid VM name
        ],
    )
    def test_bad_pairs_are_refused(self, failover: dict[str, str]) -> None:
        with pytest.raises(SystemExit):
            vm_cluster.failover_partners(self._specs(), failover)

    def test_partner_create_attaches_and_primary_shares(self) -> None:
        primary, partner = (
            vm_cluster.parse_node_spec("mgs+mds:g-mds1:1"),
            ClusterNode(
                name="g-mds2", roles=["failover"], failover_for="g-mds1"
            ),
        )
        argvs = []
        with patch.object(vm_cluster, "_run_prefixed") as run:
            run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            vm_cluster._create_one_node(primary, 2, None, share_disks=True)
            argvs.append(run.call_args.args[0])
            vm_cluster._create_one_node(partner, 2, None)
            argvs.append(run.call_args.args[0])
        assert "--share-disks" in argvs[0]
        assert "--disks-from" not in argvs[0]
        i = argvs[1].index("--disks-from")
        assert argvs[1][i + 1] == "g-mds1"
        assert argvs[1][argvs[1].index("--mdt-disks") + 1] == "0"
        assert argvs[1][argvs[1].index("--ost-disks") + 1] == "0"

    def test_plain_node_create_is_unchanged(self) -> None:
        node = vm_cluster.parse_node_spec("oss:g-oss:2")
        with patch.object(vm_cluster, "_run_prefixed") as run:
            run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            vm_cluster._create_one_node(node, 2, None)
        argv = run.call_args.args[0]
        assert "--share-disks" not in argv
        assert "--disks-from" not in argv

    def test_cluster_creates_partners_after_primaries(
        self, vm_dirs: Any
    ) -> None:
        order: list[tuple[str, bool]] = []

        def fake_create(node: ClusterNode, *a: Any, **kw: Any) -> Any:
            order.append((node.name, kw.get("share_disks", False)))
            VMInfo(name=node.name, ip="192.0.2.30").save()
            return node.name, 0, ""

        args = argparse.Namespace(
            name="g",
            nodes=["mgs+mds:g-mds1:1", "client:g-c"],
            failover={"g-mds2": "g-mds1"},
            vcpus=2,
            mem=2048,
            os=None,
            arch=None,
            disk_size=None,
            nic=[],
            owner_id=None,
        )
        with (
            patch.object(
                vm_cluster, "_create_one_node", side_effect=fake_create
            ),
            patch.object(
                vm_cluster, "_distribute_cluster_hosts", return_value=[]
            ),
        ):
            vm_cluster.cmd_cluster_create(args)
        assert order[-1] == ("g-mds2", False)
        assert ("g-mds1", True) in order[:-1]
        assert ("g-c", False) in order[:-1]
        nodes = {n.name: n for n in ClusterInfo.load("g").get_nodes()}
        assert nodes["g-mds2"].failover_for == "g-mds1"
        assert nodes["g-mds2"].roles == ["failover"]
        assert ClusterInfo.load("g").failover_partners() == {"g-mds1": "g-mds2"}

    def test_record_without_failover_has_no_new_key(self, vm_dirs: Any) -> None:
        def fake_create(node: ClusterNode, *a: Any, **kw: Any) -> Any:
            VMInfo(name=node.name, ip="192.0.2.30").save()
            return node.name, 0, ""

        args = argparse.Namespace(
            name="g",
            nodes=["mgs+mds:g-mds1:1"],
            vcpus=2,
            mem=2048,
            os=None,
            arch=None,
            disk_size=None,
            nic=[],
            owner_id=None,
        )
        with (
            patch.object(
                vm_cluster, "_create_one_node", side_effect=fake_create
            ),
            patch.object(
                vm_cluster, "_distribute_cluster_hosts", return_value=[]
            ),
        ):
            vm_cluster.cmd_cluster_create(args)
        assert "failover_for" not in ClusterInfo.load("g").path.read_text()


# ── cfg/local.sh ─────────────────────────────────────────


def _cluster(*nodes: tuple) -> ClusterInfo:
    """(name, roles, mdt, ost, ip[, failover_for]) tuples."""
    return ClusterInfo(
        name="g",
        nodes=[
            {
                "name": n[0],
                "roles": list(n[1]),
                "mdt_disks": n[2],
                "ost_disks": n[3],
                "ip": n[4],
                **({"failover_for": n[5]} if len(n) > 5 else {}),
            }
            for n in nodes
        ],
    )


class TestLocalShFailover:
    def test_mds_pair_names_the_partner_for_mdt_and_mgs(self) -> None:
        c = _cluster(
            ("g-mds1", ["mgs", "mds"], 1, 0, "10.0.0.10"),
            ("g-oss", ["oss"], 0, 2, "10.0.0.11"),
            ("g-c", ["client"], 0, 0, "10.0.0.12"),
            ("g-mds2", ["failover"], 0, 0, "10.0.0.13", "g-mds1"),
        )
        text = vm_cluster.generate_local_sh(c)
        assert "mds1_HOST=g-mds1" in text
        assert "mds1failover_HOST=g-mds2" in text
        assert "mgsfailover_HOST=g-mds2" in text
        assert "MDSDEV1=/dev/vdb" in text
        assert "ostfailover" not in text
        # The partner is no MDS of its own: still one MDT.
        assert "MDSCOUNT=${_ltvm_env_MDSCOUNT:-1}" in text

    def test_oss_pair_names_the_partner_for_each_ost(self) -> None:
        c = _cluster(
            ("g-mds", ["mgs", "mds"], 1, 0, "10.0.0.10"),
            ("g-oss1", ["oss"], 0, 2, "10.0.0.11"),
            ("g-oss2", ["failover"], 0, 0, "10.0.0.12", "g-oss1"),
        )
        text = vm_cluster.generate_local_sh(c)
        assert "ost1failover_HOST=g-oss2" in text
        assert "ost2failover_HOST=g-oss2" in text
        assert "mds1failover_HOST" not in text
        assert "mgsfailover_HOST" not in text
        assert "OSTCOUNT=${_ltvm_env_OSTCOUNT:-2}" in text

    def test_no_partner_writes_no_failover_line(self) -> None:
        c = _cluster(
            ("g-mds", ["mgs", "mds"], 1, 0, "10.0.0.10"),
            ("g-oss", ["oss"], 0, 2, "10.0.0.11"),
        )
        assert "failover" not in vm_cluster.generate_local_sh(c)

    def test_mgs_partner_adds_its_nid(self) -> None:
        c = _cluster(
            ("g-mds1", ["mgs", "mds"], 1, 0, "10.0.0.10"),
            ("g-mds2", ["failover"], 0, 0, "10.0.0.13", "g-mds1"),
        )
        probes = {
            "10.0.0.10": "--\n2: eth0    inet 10.0.0.10/24 brd x\n",
            "10.0.0.13": "--\n2: eth0    inet 10.0.0.13/24 brd x\n",
        }

        def fake_ssh(ip: str, *a: Any, **kw: Any) -> Any:
            return MagicMock(returncode=0, stdout=probes[ip], stderr="")

        with patch.object(vm_cluster, "run_ssh", side_effect=fake_ssh):
            lnet = vm_cluster.probe_mgs_lnet(c)
        assert lnet.mgsnid == "10.0.0.10@tcp:10.0.0.13@tcp"
        text = vm_cluster.generate_local_sh(c, lnet=lnet)
        assert "MGSNID=10.0.0.10@tcp:10.0.0.13@tcp" in text

    def test_oss_partner_leaves_the_mgs_nid_alone(self) -> None:
        c = _cluster(
            ("g-mds", ["mgs", "mds"], 1, 0, "10.0.0.10"),
            ("g-oss1", ["oss"], 0, 2, "10.0.0.11"),
            ("g-oss2", ["failover"], 0, 0, "10.0.0.12", "g-oss1"),
        )
        with patch.object(vm_cluster, "run_ssh") as ssh:
            ssh.return_value = MagicMock(
                returncode=0,
                stdout="--\n2: eth0    inet 10.0.0.10/24 brd x\n",
                stderr="",
            )
            lnet = vm_cluster.probe_mgs_lnet(c)
        assert lnet.mgsnid == "10.0.0.10@tcp"
        assert ssh.call_count == 1
