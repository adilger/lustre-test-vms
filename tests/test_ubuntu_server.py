"""ubuntu2604-server: an Ubuntu kernel Lustre builds ldiskfs for.

Lustre's configure takes a kernel for Ubuntu's only when utsrelease.h
defines UTS_UBUNTU_RELEASE_ABI, and then picks the ldiskfs series from
the release string's base version and ABI.  The gate has to reproduce
that choice, and the kernel build has to produce a build-tree that
configure reads as Ubuntu's -- otherwise Lustre silently builds the
vanilla series (ldiskfs-7.0-ml) instead.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ltvm_pkg.kernel_build import _mark_ubuntu_abi, deb_abi
from ltvm_pkg.lustre_compat import ubuntu_ldiskfs_series, validate_target
from ltvm_pkg.target_config import LustreMode, TargetConfig


class TestUbuntuLadder:
    """Cases from LDISKFS_LINUX_SERIES's Ubuntu branch in
    config/lustre-build-ldiskfs.m4."""

    @pytest.mark.parametrize(
        "release,stem",
        [
            ("7.0.0-14-ubuntu26", "7.0.0-14-ubuntu26"),
            ("7.0.0-14-lustre", "7.0.0-14-ubuntu26"),
            ("7.0.0-39-generic", "7.0.0-14-ubuntu26"),
            ("7.0.0-13-generic", "7.0-ml"),
            ("7.1.0-2-generic", "7.0-ml"),
            ("6.17.0-5-generic", "7.0.0-14-ubuntu26"),
            ("6.11.0-9-generic", "6.11-ml"),
            ("6.10.0-15-generic", "6.10-ml"),
            ("6.8.0-106-generic", "6.8.0-106-ubuntu24"),
            ("6.8.0-101-generic", "6.8.0-100-ubuntu24"),
            ("6.8.0-90-generic", "6.8.0-90-ubuntu24"),
            ("6.8.0-45-generic", "6.8.0-45-ubuntu24"),
            ("6.8.0-44-generic", "6.8.0-45-ubuntu24"),
            ("6.8.0-43-generic", "6.7-ml"),
            ("6.9.0-1-generic", "6.7-ml"),
            ("5.15.0-106-generic", "5.15.0-106-ubuntu20"),
            ("5.15.0-105-generic", "5.15.0-83-ubuntu20"),
            ("5.4.0-1007-aws", "5.4.0-42-ubuntu20"),
            ("5.4.0-91-generic", "5.4.0-90-ubuntu20"),
            ("5.4.0-48-generic", "5.4.0-42-ubuntu20"),
            ("5.6.0-1-generic", "5.4.0-ml"),
            ("4.15.0-20-generic", "4.15.0-20-ubuntu18"),
        ],
    )
    def test_series(self, release: str, stem: str) -> None:
        assert ubuntu_ldiskfs_series(release) == f"ldiskfs-{stem}"

    def test_no_abi_assumes_the_latest(self) -> None:
        """configure warns and takes the band's newest series."""
        assert ubuntu_ldiskfs_series("7.0.0") == "ldiskfs-7.0.0-14-ubuntu26"
        assert ubuntu_ldiskfs_series("6.8.0") == "ldiskfs-6.8.0-106-ubuntu24"

    def test_below_any_series(self) -> None:
        assert ubuntu_ldiskfs_series("4.14.0-1-generic") is None
        assert ubuntu_ldiskfs_series("4.15.0-19-generic") is None


def _tree(tmp_path: Path, stems: tuple[str, ...]) -> Path:
    tree = tmp_path / "lustre-release"
    kp = tree / "lustre" / "kernel_patches"
    kp.mkdir(parents=True)
    (kp / "which_patch").write_text(
        "PATCH SERIES FOR SERVER KERNELS:\n"
        "5.14-rhel9.7.series     5.14.0-611.55.1.el9  (RHEL 9.7)\n"
    )
    series = tree / "ldiskfs" / "kernel_patches" / "series"
    series.mkdir(parents=True)
    for stem in stems:
        (series / f"{stem}.series").write_text("")
    return tree


class TestGate:
    @pytest.fixture()
    def tc(self) -> TargetConfig:
        tc = TargetConfig("ubuntu2604-server")
        assert tc.lustre_mode == LustreMode.SERVER_LDISKFS
        return tc

    def test_series_from_the_ubuntu_ladder(
        self, tc: TargetConfig, tmp_path: Path
    ) -> None:
        """Not the filename heuristic, which takes the newest 7.0 one."""
        tree = _tree(
            tmp_path,
            (
                "ldiskfs-7.0-ml",
                "ldiskfs-7.0.0-14-ubuntu26",
                "ldiskfs-7.0.0-99-ubuntu26",
            ),
        )
        r = validate_target(tc, tree, kernel="7.0.0-14-ubuntu26")
        assert r.status == "ok", r.message
        assert "'ldiskfs-7.0.0-14-ubuntu26'" in r.message
        assert "Ubuntu version/ABI ladder" in r.message

    def test_tree_without_the_series_is_refused(
        self, tc: TargetConfig, tmp_path: Path
    ) -> None:
        tree = _tree(tmp_path, ("ldiskfs-7.0-ml",))
        r = validate_target(tc, tree, kernel="7.0.0-14-ubuntu26")
        assert r.status == "refuse"
        assert "ldiskfs-7.0.0-14-ubuntu26" in r.message
        assert "does not ship" in r.message


class TestTarget:
    def test_pinned_to_the_exact_upload(self) -> None:
        tc = TargetConfig("ubuntu2604-server")
        assert tc.kernel_deb_source == "linux-source-7.0.0=7.0.0-14.14"
        assert deb_abi(tc.kernel_deb_source) == "14"

    def test_localversion_carries_the_abi(self) -> None:
        cfg = TargetConfig("ubuntu2604-server").kernel_config_overrides
        assert cfg["CONFIG_LOCALVERSION"] == '"-14-lustre"'
        assert cfg["CONFIG_LOCALVERSION_AUTO"] == "n"
        # ubuntu2604's own options come along through the YAML merge.
        assert cfg["CONFIG_QFMT_V2"] == "y"
        assert cfg["CONFIG_DM_FLAKEY"] == "m"

    def test_client_target_is_unchanged(self) -> None:
        tc = TargetConfig("ubuntu2604")
        assert tc.lustre_mode == LustreMode.CLIENT
        assert deb_abi(tc.kernel_deb_source or "") is None
        assert "CONFIG_LOCALVERSION" not in tc.kernel_config_overrides


class TestDebAbi:
    @pytest.mark.parametrize(
        "source,abi",
        [
            ("linux-source-7.0.0=7.0.0-14.14", "14"),
            ("linux-source-6.8.0=6.8.0-106.106~22.04.1", "106"),
            ("linux-source-7.0.0", None),
            ("linux-source-7.0.0=7.0.0", None),
        ],
    )
    def test_parse(self, source: str, abi: str | None) -> None:
        assert deb_abi(source) == abi


class TestMarkUbuntuAbi:
    def _tree(self, tmp_path: Path, release: str) -> Path:
        bt = tmp_path / "build-tree"
        (bt / "include" / "generated").mkdir(parents=True)
        (bt / "include" / "generated" / "utsrelease.h").write_text(
            f'#define UTS_RELEASE "{release}"\n'
        )
        (bt / "kernel-version").write_text(release + "\n")
        return bt

    def test_adds_the_line_configure_looks_for(self, tmp_path: Path) -> None:
        bt = self._tree(tmp_path, "7.0.0-14-lustre")
        _mark_ubuntu_abi(bt, "14")
        _mark_ubuntu_abi(bt, "14")  # idempotent
        text = (bt / "include" / "generated" / "utsrelease.h").read_text()
        assert text.count("#define UTS_UBUNTU_RELEASE_ABI 14\n") == 1

    def test_release_without_the_abi_is_an_error(self, tmp_path: Path) -> None:
        """configure would read KPLEV from a release that lacks it."""
        bt = self._tree(tmp_path, "7.0.0-lustre")
        with pytest.raises(RuntimeError, match="CONFIG_LOCALVERSION"):
            _mark_ubuntu_abi(bt, "14")


class TestDryApplyIsSequential:
    """A series' patches build on each other, as the ldiskfs build
    applies them: the second here only applies after the first."""

    FIRST = (
        "--- a/fs/ext4/f.c\n+++ b/fs/ext4/f.c\n"
        "@@ -1,3 +1,4 @@\n a\n+b\n c\n d\n"
    )
    SECOND = (
        "--- a/fs/ext4/f.c\n+++ b/fs/ext4/f.c\n"
        "@@ -1,4 +1,5 @@\n a\n b\n+x\n c\n d\n"
    )

    def test_patch_on_patch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ltvm_pkg import lustre_compat

        monkeypatch.setattr(lustre_compat, "_patch_program", lambda: "patch")
        src = tmp_path / "src"
        (src / "fs" / "ext4").mkdir(parents=True)
        (src / "fs" / "ext4" / "f.c").write_text("a\nc\nd\n")
        p1 = tmp_path / "1.patch"
        p1.write_text(self.FIRST)
        p2 = tmp_path / "2.patch"
        p2.write_text(self.SECOND)
        ok, failures = lustre_compat.dry_apply_patches([p1, p2], src)
        assert ok, failures
        # The kernel tree itself is never touched.
        assert (src / "fs" / "ext4" / "f.c").read_text() == "a\nc\nd\n"

    def test_without_gnu_patch_the_series_is_not_judged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ltvm_pkg import lustre_compat

        monkeypatch.setattr(lustre_compat, "_patch_program", lambda: None)
        tree = _tree(tmp_path, ("ldiskfs-7.0.0-14-ubuntu26",))
        bt = tmp_path / "bt"
        (bt / "fs" / "ext4").mkdir(parents=True)
        (bt / "fs" / "ext4" / "sysfs.c").write_text("")
        r = validate_target(
            TargetConfig("ubuntu2604-server"),
            tree,
            kernel="7.0.0-14-ubuntu26",
            kernel_build_tree=bt,
        )
        assert r.status == "ok", r.message
        assert "no GNU patch" in r.message
