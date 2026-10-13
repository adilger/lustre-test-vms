"""Simulated NUMA nodes (--numa-nodes) for testing Lustre CPTs."""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import patch

import pytest

from ltvm_pkg import qemu_run, vm_commands
from ltvm_pkg.vm_state import VMInfo
from tests.test_qemu_run import (  # noqa: F401
    _force_linux_host,
    _LaunchHarness,
    _make_vm,
    _run_launch,
    tmp_vmdir,
)


class TestSmpArgs:
    def test_none_is_plain_smp(self) -> None:
        assert qemu_run.smp_args(4, 4096, 0) == ["-smp", "4", "-m", "4096"]
        assert qemu_run.smp_args(4, 4096, 1) == ["-smp", "4", "-m", "4096"]

    def test_one_cpu_per_node(self) -> None:
        args = qemu_run.smp_args(4, 4096, 4)
        assert args[:4] == [
            "-smp",
            "4,sockets=4,cores=1,threads=1",
            "-m",
            "4096",
        ]
        joined = " ".join(args)
        for i in range(4):
            assert f"memory-backend-ram,id=numa{i},size=1024M" in joined
            assert f"node,nodeid={i},cpus={i},memdev=numa{i}" in joined

    def test_cpu_ranges_and_memory_add_up(self) -> None:
        args = qemu_run.smp_args(8, 3000, 2)
        joined = " ".join(args)
        assert "cpus=0-3," in joined
        assert "cpus=4-7," in joined
        sizes = [
            int(a.split("size=")[1].rstrip("M"))
            for a in args
            if a.startswith("memory-backend-ram")
        ]
        assert sum(sizes) == 3000
        assert all(s % 2 == 0 for s in sizes[:-1])


class TestNumaProblem:
    def test_ok(self) -> None:
        assert qemu_run.numa_problem(4, 4096, 4) is None
        assert qemu_run.numa_problem(4, 512, 0) is None

    def test_must_divide_vcpus(self) -> None:
        assert "divide the 4 vCPUs" in (qemu_run.numa_problem(4, 4096, 3) or "")

    def test_memory_floor(self) -> None:
        assert "under 128 MB" in (qemu_run.numa_problem(4, 256, 4) or "")

    def test_negative(self) -> None:
        assert ">= 0" in (qemu_run.numa_problem(4, 4096, -1) or "")


def test_info_round_trip(tmp_vmdir: Path) -> None:  # noqa: F811
    vm = _make_vm(tmp_vmdir)
    vm.numa_nodes = 2
    vm.save()
    assert VMInfo.load(vm.name).numa_nodes == 2


def test_old_info_has_none(tmp_vmdir: Path) -> None:  # noqa: F811
    vm = _make_vm(tmp_vmdir)
    vm.save()
    text = vm.info_path.read_text()
    vm.info_path.write_text(
        "".join(ln for ln in text.splitlines(True) if "NUMA" not in ln)
    )
    assert VMInfo.load(vm.name).numa_nodes == 0


def test_launch_passes_the_layout(tmp_vmdir: Path) -> None:  # noqa: F811
    vm = _make_vm(tmp_vmdir)
    vm.vcpus = 4
    vm.numa_nodes = 4
    h = _LaunchHarness()
    _run_launch(vm, h)
    joined = " ".join(h.qemu_argv or [])
    assert "-smp 4,sockets=4,cores=1,threads=1" in joined
    assert joined.count("-numa node,") == 4


class TestVmSet:
    def _set(self, tmp_vmdir: Path, **kw: object) -> VMInfo:  # noqa: F811
        vm = _make_vm(tmp_vmdir)
        vm.vcpus = 4
        vm.mem = 4096
        vm.save()
        ns = argparse.Namespace(name=vm.name, vcpus=None, mem=None, **kw)
        with patch.object(vm_commands, "is_running", return_value=False):
            vm_commands.cmd_set(ns)
        return VMInfo.load(vm.name)

    def test_sets_nodes(self, tmp_vmdir: Path) -> None:  # noqa: F811
        assert self._set(tmp_vmdir, numa_nodes=2).numa_nodes == 2

    def test_refuses_an_uneven_split(self, tmp_vmdir: Path) -> None:  # noqa: F811
        with pytest.raises(SystemExit):
            self._set(tmp_vmdir, numa_nodes=3)


def test_create_flag_parses() -> None:
    import importlib.machinery
    import importlib.util

    loader = importlib.machinery.SourceFileLoader(
        "ltvm_cli_numa", str(Path(__file__).resolve().parent.parent / "ltvm")
    )
    spec = importlib.util.spec_from_loader("ltvm_cli_numa", loader)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    p = mod.build_parser()
    assert (
        p.parse_args(["create", "co1-x", "--numa-nodes", "4"]).numa_nodes == 4
    )
    assert (
        p.parse_args(["vm", "set", "co1-x", "--numa-nodes", "0"]).numa_nodes
        == 0
    )
