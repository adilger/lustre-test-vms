"""macOS build volumes: what is mounted, and when it falls back.

Nothing here runs podman: build_volumes._podman / _probe / _populate
are replaced, and conftest turns the volumes off for every other test.
"""

from __future__ import annotations

import contextlib
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from ltvm_pkg import build_volumes
from ltvm_pkg.lustre_build import build_lustre


def _cp(rc: int = 0, out: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], rc, out, "")


def _kernel_artifact(
    tmp_path: Path, kver: str = "5.14-rhel9.7-5.14.0-611"
) -> Path:
    kdir = tmp_path / "artifacts" / "rocky9" / "aarch64" / "kernels" / kver
    bt = kdir / "build-tree"
    (bt / "include" / "config").mkdir(parents=True)
    (bt / "include" / "config" / "kernel.release").write_text("5.14.0-611\n")
    (bt / "Module.symvers").write_text("")
    (kdir / "meta.json").write_text('{"input_hash": "aaaa"}')
    return bt


@pytest.fixture
def on_mac(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(build_volumes, "is_macos", lambda: True)
    monkeypatch.delenv("LTVM_MACOS_BUILD_VOLUMES", raising=False)
    # The real lock is a file in the system temp dir, one per volume
    # name -- and every test's tmp_path makes a new name.
    monkeypatch.setattr(
        build_volumes, "_lock", lambda name: contextlib.nullcontext()
    )


class TestEnabled:
    def test_off_on_linux(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(build_volumes, "is_macos", lambda: False)
        monkeypatch.delenv("LTVM_MACOS_BUILD_VOLUMES", raising=False)
        assert not build_volumes.enabled()

    def test_on_on_macos(self, on_mac: None) -> None:
        assert build_volumes.enabled()

    def test_env_turns_it_off(
        self, on_mac: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LTVM_MACOS_BUILD_VOLUMES", "0")
        assert not build_volumes.enabled()

    def test_linux_mounts_are_the_bind_mounts_and_run_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(build_volumes, "is_macos", lambda: False)
        monkeypatch.delenv("LTVM_MACOS_BUILD_VOLUMES", raising=False)
        bt = _kernel_artifact(tmp_path)
        podman = MagicMock()
        monkeypatch.setattr(build_volumes, "_podman", podman)
        assert build_volumes.kernel_tree_mount(bt, "img") == str(bt)
        assert build_volumes.ccache_mount(
            "ltvm-build-x", tmp_path / "cc"
        ) == str(tmp_path / "cc")
        podman.assert_not_called()


class TestKernelVolumeName:
    def test_names_the_kernel_and_is_stable(self, tmp_path: Path) -> None:
        bt = _kernel_artifact(tmp_path)
        name = build_volumes.kernel_volume_name(bt)
        assert name.startswith(
            "ltvm-kernel-rocky9-aarch64-5.14-rhel9.7-5.14.0-611-"
        )
        assert name == build_volumes.kernel_volume_name(bt)

    def test_a_rebuilt_kernel_gets_another_volume(self, tmp_path: Path) -> None:
        bt = _kernel_artifact(tmp_path)
        before = build_volumes.kernel_volume_name(bt)
        (bt.parent / "meta.json").write_text('{"input_hash": "bbbb"}')
        assert build_volumes.kernel_volume_name(bt) != before

    def test_a_changed_symvers_gets_another_volume(
        self, tmp_path: Path
    ) -> None:
        bt = _kernel_artifact(tmp_path)
        before = build_volumes.kernel_volume_name(bt)
        (bt / "Module.symvers").write_text("0x1\tsym\tvmlinux\tEXPORT_SYMBOL\n")
        assert build_volumes.kernel_volume_name(bt) != before

    def test_only_valid_volume_characters(self, tmp_path: Path) -> None:
        bt = _kernel_artifact(tmp_path, kver="5.14+odd name")
        name = build_volumes.kernel_volume_name(bt)
        assert all(c.isalnum() or c in "_.-" for c in name)


class TestKernelTreeMount:
    def test_ready_volume_is_used_without_copying(
        self, tmp_path: Path, on_mac: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bt = _kernel_artifact(tmp_path)
        monkeypatch.setattr(build_volumes, "_ready", lambda v, i: True)
        populate = MagicMock()
        monkeypatch.setattr(build_volumes, "_populate", populate)
        vol = build_volumes.kernel_tree_mount(bt, "img")
        assert vol == build_volumes.kernel_volume_name(bt)
        populate.assert_not_called()

    def test_missing_volume_is_copied_and_older_ones_removed(
        self, tmp_path: Path, on_mac: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bt = _kernel_artifact(tmp_path)
        name = build_volumes.kernel_volume_name(bt)
        stem = name.rsplit("-", 1)[0]
        other_kernel = "ltvm-kernel-rocky9-aarch64-5.14-rhel9.6-x-0123456789ab"
        calls: list[tuple[str, ...]] = []

        def podman(*args: str, timeout: float | None = None) -> Any:
            calls.append(args)
            if args[:2] == ("volume", "ls"):
                return _cp(out=f"{stem}-oldoldoldold\n{name}\n{other_kernel}\n")
            return _cp()

        monkeypatch.setattr(build_volumes, "_podman", podman)
        monkeypatch.setattr(build_volumes, "_ready", lambda v, i: False)
        populate = MagicMock()
        monkeypatch.setattr(build_volumes, "_populate", populate)
        assert build_volumes.kernel_tree_mount(bt, "img") == name
        populate.assert_called_once_with(name, bt, "img")
        removed = [a[-1] for a in calls if a[:2] == ("volume", "rm")]
        assert removed == [f"{stem}-oldoldoldold"]

    def test_a_failed_copy_falls_back_to_the_bind_mount(
        self, tmp_path: Path, on_mac: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bt = _kernel_artifact(tmp_path)
        calls: list[tuple[str, ...]] = []
        monkeypatch.setattr(
            build_volumes,
            "_podman",
            lambda *a, timeout=None: calls.append(a) or _cp(),
        )
        monkeypatch.setattr(build_volumes, "_ready", lambda v, i: False)

        def boom(*a: Any) -> None:
            raise RuntimeError("import failed")

        monkeypatch.setattr(build_volumes, "_populate", boom)
        assert build_volumes.kernel_tree_mount(bt, "img") == str(bt)
        # The partial volume is not left behind.
        assert (
            "volume",
            "rm",
            build_volumes.kernel_volume_name(bt),
        ) in calls


class TestReady:
    def test_needs_the_marker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            build_volumes, "_podman", lambda *a, timeout=None: _cp()
        )
        monkeypatch.setattr(build_volumes, "_probe", lambda i, m, s: "\n")
        assert not build_volumes._ready("v", "img")
        monkeypatch.setattr(build_volumes, "_probe", lambda i, m, s: "ready\n")
        assert build_volumes._ready("v", "img")

    def test_a_missing_volume_is_not_ready(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            build_volumes, "_podman", lambda *a, timeout=None: _cp(rc=1)
        )
        assert not build_volumes._ready("v", "img")


class TestCcacheMount:
    def test_writable_volume_is_used(
        self, tmp_path: Path, on_mac: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            build_volumes, "_podman", lambda *a, timeout=None: _cp()
        )
        monkeypatch.setattr(build_volumes, "_probe", lambda i, m, s: "ok\n")
        assert (
            build_volumes.ccache_mount("ltvm-build-rocky9-aarch64", tmp_path)
            == "ltvm-build-ccache-rocky9-aarch64"
        )

    def test_unwritable_volume_falls_back(
        self, tmp_path: Path, on_mac: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """What podman machine 5.x did, and why 0e4c1c9 left volumes."""
        monkeypatch.setattr(
            build_volumes, "_podman", lambda *a, timeout=None: _cp()
        )
        monkeypatch.setattr(build_volumes, "_probe", lambda i, m, s: None)
        assert build_volumes.ccache_mount("ltvm-build-x", tmp_path) == str(
            tmp_path
        )

    def test_does_not_reuse_the_old_ccache_volume_name(self) -> None:
        assert not build_volumes.ccache_volume_name("ltvm-build-x").startswith(
            "ltvm-ccache-"
        )


class TestBuildLustreMounts:
    """The mounts build_lustre hands podman, with and without volumes."""

    def _tree(self, tmp_path: Path) -> Path:
        lustre = tmp_path / "lustre"
        (lustre / "lustre" / "kernel_patches").mkdir(parents=True)
        (lustre / "configure").write_text("#!/bin/sh\n")
        return lustre

    def _mounts(self, tmp_path: Path) -> tuple[list[str], Path]:
        lustre = self._tree(tmp_path)
        bt = _kernel_artifact(tmp_path)
        captured: list[list[str]] = []

        def run(cmd: list[str], *a: Any, **kw: Any) -> Any:
            if cmd[0] == "podman":
                captured.append(cmd)
            r = MagicMock()
            r.returncode = 0
            return r

        with (
            patch("ltvm_pkg.lustre_build.subprocess.run", side_effect=run),
            patch(
                "ltvm_pkg.lustre_build.run_podman_with_cleanup", side_effect=run
            ),
            patch("ltvm_pkg.lustre_build._container_exists", return_value=True),
        ):
            try:
                build_lustre(
                    lustre,
                    bt,
                    container_tag="ltvm-build-rocky9",
                    target="rocky9",
                )
            except Exception:
                pass
        assert captured, "podman run was not called"
        cmd = captured[0]
        return [cmd[i + 1] for i, a in enumerate(cmd) if a == "-v"], bt

    def test_bind_mounts_when_volumes_are_off(self, tmp_path: Path) -> None:
        mounts, bt = self._mounts(tmp_path)
        assert f"{bt}:/kernel:ro" in mounts
        ccache = [m for m in mounts if m.endswith(":/ccache")]
        assert ccache and ccache[0].startswith(str(tmp_path))

    def test_volumes_on_macos(
        self, tmp_path: Path, on_mac: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(build_volumes, "_ready", lambda v, i: True)
        monkeypatch.setattr(
            build_volumes, "_podman", lambda *a, timeout=None: _cp()
        )
        monkeypatch.setattr(build_volumes, "_probe", lambda i, m, s: "ok\n")
        mounts, bt = self._mounts(tmp_path)
        assert f"{build_volumes.kernel_volume_name(bt)}:/kernel:ro" in mounts
        assert "ltvm-build-ccache-rocky9:/ccache" in mounts
        # The tree itself stays a bind mount: it is edited on macOS.
        assert f"{tmp_path / 'lustre'}:/lustre" in mounts


class TestDeadKernelVolumes:
    def test_lists_only_kernel_volumes_without_an_artifact(
        self, tmp_path: Path, on_mac: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bt = _kernel_artifact(tmp_path)
        live = build_volumes.kernel_volume_name(bt)
        gone = "ltvm-kernel-rocky9-aarch64-5.14-rhel9.4-x-0123456789ab"
        listing = f"{live}\n{gone}\nltvm-build-ccache-rocky9\nsomeone-elses\n"
        monkeypatch.setattr(
            build_volumes, "_podman", lambda *a, timeout=None: _cp(out=listing)
        )
        assert build_volumes.dead_kernel_volumes(tmp_path / "artifacts") == [
            gone
        ]

    def test_nothing_where_volumes_are_off(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(build_volumes, "is_macos", lambda: False)
        podman = MagicMock()
        monkeypatch.setattr(build_volumes, "_podman", podman)
        assert build_volumes.dead_kernel_volumes(tmp_path) == []
        podman.assert_not_called()


def test_another_checkouts_volume_is_not_superseded(
    tmp_path: Path, on_mac: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two ltvm checkouts building one kernel keep a volume each."""
    mine = _kernel_artifact(tmp_path / "a")
    theirs = build_volumes.kernel_volume_name(_kernel_artifact(tmp_path / "b"))
    name = build_volumes.kernel_volume_name(mine)
    assert name != theirs
    removed: list[str] = []

    def podman(*args: str, timeout: float | None = None) -> Any:
        if args[:2] == ("volume", "ls"):
            return _cp(out=f"{name}\n{theirs}\n")
        if args[:2] == ("volume", "rm"):
            removed.append(args[-1])
        return _cp()

    monkeypatch.setattr(build_volumes, "_podman", podman)
    build_volumes._remove_superseded(name)
    assert removed == []


def test_no_volume_is_ever_force_removed(
    tmp_path: Path, on_mac: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`podman volume rm -f` also removes the containers using it, which
    would kill a build still running against an older kernel."""
    bt = _kernel_artifact(tmp_path)
    name = build_volumes.kernel_volume_name(bt)
    stem = name.rsplit("-", 1)[0]
    calls: list[tuple[str, ...]] = []

    def podman(*args: str, timeout: float | None = None) -> Any:
        calls.append(args)
        if args[:2] == ("volume", "ls"):
            return _cp(out=f"{stem}-oldoldoldold\n{name}\n")
        if args[:2] == ("volume", "rm"):
            return _cp(rc=2)  # in use
        return _cp()

    monkeypatch.setattr(build_volumes, "_podman", podman)
    build_volumes._remove_superseded(name)  # in use: kept, no error
    monkeypatch.setattr(build_volumes, "_ready", lambda v, i: False)

    def boom(*a: Any) -> None:
        raise RuntimeError("import failed")

    monkeypatch.setattr(build_volumes, "_populate", boom)
    build_volumes.kernel_tree_mount(bt, "img")
    rms = [a for a in calls if a[:2] == ("volume", "rm")]
    assert rms and not any("-f" in a or "--force" in a for a in rms)


def test_the_copy_uses_macos_tar_not_whatever_is_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its flags are bsdtar's; a GNU tar first on PATH refuses them."""
    bt = _kernel_artifact(tmp_path)
    monkeypatch.setattr(
        build_volumes, "_podman", lambda *a, timeout=None: _cp()
    )
    monkeypatch.setattr(build_volumes, "_probe", lambda i, m, s: "")
    popen = MagicMock()
    popen.return_value.wait.return_value = 0
    monkeypatch.setattr(build_volumes.subprocess, "Popen", popen)
    monkeypatch.setattr(build_volumes.subprocess, "run", lambda *a, **k: _cp())
    build_volumes._populate("v", bt, "img")
    assert popen.call_args.args[0][0] == "/usr/bin/tar"
