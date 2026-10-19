"""Failover pairs: two VMs that open one set of data disks.

A VM made with --share-disks lets one partner (--disks-from) attach its
data disks.  None of it may change a VM that does not ask for it.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from ltvm_pkg import qemu_run, vm_commands
from ltvm_pkg.vm_state import VMInfo


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
