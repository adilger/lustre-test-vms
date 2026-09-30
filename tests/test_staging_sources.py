"""A deploy must not ship a staged copy of a script that was since edited.

Reported from a Patch Watcher run: `deploy-lustre --userspace-only` (and
a full deploy) said "Deployed" while the VM got the previous
sanity-quota.sh, because nothing but a rebuild ever rewrote staging.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from ltvm_pkg import staging_sources as ss
from tests.test_deploy import (
    _deploy_args,
    _make_vm,
    _mark_staging_fresh,
    _stub_tc,
)

_TESTS = "usr/lib64/lustre/tests"


@pytest.fixture
def tmp_sockets(tmp_path: Path):
    with patch("ltvm_pkg.vm_state.SOCKETS", tmp_path):
        yield tmp_path


def _tree_and_staging(root: Path) -> tuple[Path, Path]:
    """A Lustre tree with a staging that `make install` just filled."""
    tree = root / "lustre-release"
    for d in ("lustre/tests/cfg", "lustre/utils", "lustre/doc", "lnet"):
        (tree / d).mkdir(parents=True)
    (tree / "configure.ac").write_text("")
    (tree / "lustre/tests/sanity-quota.sh").write_text("echo v1\n")
    (tree / "lustre/tests/cfg/local.sh").write_text("FSNAME=lustre\n")
    (tree / "lustre/doc/lfs.1").write_text(".TH LFS 1\n")
    (tree / "lustre/utils/lfs.c").write_text("int main(void){}\n")
    # A build product that happens to be ELF, like the real lfs.
    (tree / "lustre/utils/lfs").write_bytes(b"\x7fELF-built")

    from ltvm_pkg.lustre_build import staging_path

    staging = staging_path(tree, "rocky9", arch="x86_64", kernel="5.14-rhel9.7")
    (staging / _TESTS / "cfg").mkdir(parents=True)
    (staging / "usr/share/man/man1").mkdir(parents=True)
    (staging / "usr/bin").mkdir(parents=True)
    (staging / "lib/modules/5.14/extra").mkdir(parents=True)
    (staging / _TESTS / "sanity-quota.sh").write_text("echo v1\n")
    (staging / _TESTS / "sanity-quota.sh").chmod(0o755)
    (staging / _TESTS / "cfg/local.sh").write_text("FSNAME=lustre\n")
    (staging / "usr/share/man/man1/lfs.1").write_text(".TH LFS 1\n")
    (staging / "usr/bin/lfs").write_bytes(b"\x7fELF-built")
    (staging / "lib/modules/5.14/extra/lustre.ko").write_text("")
    (staging / ".ltvm-staging-stamp").write_text("5.14\n")
    return tree, staging


def _edit_keeping_old_mtime(path: Path, text: str) -> None:
    """An edit a `find -newer stamp` cannot see (cp -p, rsync -a, tar)."""
    st = path.stat()
    path.write_text(text)
    os.utime(path, (st.st_atime - 3600, st.st_mtime - 3600))


class TestManifest:
    def test_maps_verbatim_installs_to_their_sources(
        self, tmp_path: Path
    ) -> None:
        tree, staging = _tree_and_staging(tmp_path)
        m = ss.build_manifest(staging, tree)
        assert m[f"{_TESTS}/sanity-quota.sh"] == "lustre/tests/sanity-quota.sh"
        assert m[f"{_TESTS}/cfg/local.sh"] == "lustre/tests/cfg/local.sh"
        assert m["usr/share/man/man1/lfs.1"] == "lustre/doc/lfs.1"

    def test_leaves_out_compiled_files_and_bookkeeping(
        self, tmp_path: Path
    ) -> None:
        tree, staging = _tree_and_staging(tmp_path)
        m = ss.build_manifest(staging, tree)
        assert "usr/bin/lfs" not in m
        assert not any(k.startswith("lib/modules") for k in m)
        assert not any(k.startswith(".ltvm-") for k in m)

    def test_content_must_match(self, tmp_path: Path) -> None:
        tree, staging = _tree_and_staging(tmp_path)
        (tree / "lustre/doc/lfs.1").write_text(".TH LFS 1 changed\n")
        assert "usr/share/man/man1/lfs.1" not in ss.build_manifest(
            staging, tree
        )

    def test_an_ambiguous_source_maps_nothing(self, tmp_path: Path) -> None:
        tree, staging = _tree_and_staging(tmp_path)
        (tree / "lustre/utils/lfs.1").write_text(".TH LFS 1\n")
        (tree / "lustre/doc/x").mkdir()
        # Two identical candidates, neither sharing more of the path.
        assert "usr/share/man/man1/lfs.1" not in ss.build_manifest(
            staging, tree
        )

    def test_nearer_path_wins(self, tmp_path: Path) -> None:
        tree, staging = _tree_and_staging(tmp_path)
        (tree / "lustre/tests/local.sh").write_text("FSNAME=lustre\n")
        m = ss.build_manifest(staging, tree)
        assert m[f"{_TESTS}/cfg/local.sh"] == "lustre/tests/cfg/local.sh"

    def test_round_trip(self, tmp_path: Path) -> None:
        tree, staging = _tree_and_staging(tmp_path)
        n = ss.write_manifest(staging, tree)
        assert n == len(ss.read_manifest(staging) or {})
        assert (staging / ss.MANIFEST_NAME).name.startswith(".ltvm-")

    def test_build_lustre_writes_it(self) -> None:
        src = Path("ltvm_pkg/lustre_build.py").read_text()
        assert "write_manifest(host_staging, lustre_tree)" in src


class TestRefresh:
    def test_edited_script_is_copied_in(self, tmp_path: Path) -> None:
        tree, staging = _tree_and_staging(tmp_path)
        ss.write_manifest(staging, tree)
        _edit_keeping_old_mtime(
            tree / "lustre/tests/sanity-quota.sh", "echo v2\n"
        )
        got = ss.refresh(staging, tree)
        assert got == ["lustre/tests/sanity-quota.sh"]
        staged = staging / _TESTS / "sanity-quota.sh"
        assert staged.read_text() == "echo v2\n"
        assert staged.stat().st_mode & 0o777 == 0o755

    def test_any_manifest_file_not_just_tests(self, tmp_path: Path) -> None:
        tree, staging = _tree_and_staging(tmp_path)
        ss.write_manifest(staging, tree)
        (tree / "lustre/doc/lfs.1").write_text(".TH LFS 1 new\n")
        assert ss.refresh(staging, tree) == ["lustre/doc/lfs.1"]

    def test_nothing_to_do_when_in_step(self, tmp_path: Path) -> None:
        tree, staging = _tree_and_staging(tmp_path)
        ss.write_manifest(staging, tree)
        assert ss.refresh(staging, tree) == []

    def test_same_size_same_mtime_edit_is_seen(self, tmp_path: Path) -> None:
        tree, staging = _tree_and_staging(tmp_path)
        ss.write_manifest(staging, tree)
        src = tree / "lustre/tests/sanity-quota.sh"
        st = src.stat()
        src.write_text("echo v9\n")
        os.utime(src, ns=(st.st_atime_ns, st.st_mtime_ns))
        assert ss.refresh(staging, tree) == ["lustre/tests/sanity-quota.sh"]

    def test_tests_dir_checked_without_a_manifest(self, tmp_path: Path) -> None:
        """Staging built before the manifest existed."""
        tree, staging = _tree_and_staging(tmp_path)
        (tree / "lustre/tests/cfg/local.sh").write_text("FSNAME=other\n")
        (tree / "lustre/doc/lfs.1").write_text(".TH changed\n")
        assert ss.refresh(staging, tree) == ["lustre/tests/cfg/local.sh"]

    def test_a_vanished_source_is_left_alone(self, tmp_path: Path) -> None:
        """It may be a generated file `make clean` removed."""
        tree, staging = _tree_and_staging(tmp_path)
        ss.write_manifest(staging, tree)
        (tree / "lustre/doc/lfs.1").unlink()
        assert ss.refresh(staging, tree) == []
        assert (staging / "usr/share/man/man1/lfs.1").is_file()


class TestDeployRefreshes:
    """cmd_deploy puts the edited file in staging before streaming it."""

    def _run(
        self, tree: Path, staging: Path, *, userspace_only: bool
    ) -> tuple[int, dict]:
        from ltvm_pkg import cli as cli_mod

        vm = _make_vm(name="co1-refresh", ip="10.0.0.40")
        vm.os_id = "rocky9"
        vm.save()
        seen: dict = {}

        def fake_deploy_to_vm(vm_arg, staging_arg, **kwargs):
            p = Path(staging_arg) / _TESTS / "sanity-quota.sh"
            seen["script"] = p.read_text()

        with (
            patch.object(cli_mod, "TargetConfig", return_value=_stub_tc()),
            patch("ltvm_pkg.cli.deploy_to_vm", side_effect=fake_deploy_to_vm),
        ):
            rc = cli_mod.cmd_deploy(
                _deploy_args(
                    vm="co1-refresh",
                    lustre_tree=str(tree),
                    userspace_only=userspace_only,
                )
            )
        return rc, seen

    def test_userspace_only_ships_the_edit(
        self, tmp_sockets: Path, tmp_path: Path, capsys
    ) -> None:
        tree, staging = _tree_and_staging(tmp_path)
        ss.write_manifest(staging, tree)
        _mark_staging_fresh(staging, tree, _stub_tc())
        (tree / "lustre/tests/sanity-quota.sh").write_text("echo v2\n")

        rc, seen = self._run(tree, staging, userspace_only=True)

        assert rc == 0
        assert seen["script"] == "echo v2\n"
        out = capsys.readouterr()
        assert "Refreshed 1 staged file(s)" in out.out
        # The script is handled; nothing else changed, so no warning.
        assert "does not rebuild" not in out.err

    def test_userspace_only_warns_about_compiled_sources(
        self, tmp_sockets: Path, tmp_path: Path, capsys
    ) -> None:
        tree, staging = _tree_and_staging(tmp_path)
        ss.write_manifest(staging, tree)
        _mark_staging_fresh(staging, tree, _stub_tc())
        stamp = staging / ".ltvm-staging-stamp"
        old = time.time() - 60
        os.utime(stamp, (old, old))
        (tree / "lustre/utils/lfs.c").write_text("int main(void){return 1;}\n")

        rc, _seen = self._run(tree, staging, userspace_only=True)

        assert rc == 0
        err = capsys.readouterr().err
        assert "--userspace-only does not rebuild" in err
        assert "lustre/utils/lfs.c" in err

    def test_full_deploy_fast_path_ships_the_edit(
        self, tmp_sockets: Path, tmp_path: Path
    ) -> None:
        """An mtime-preserving edit leaves the fast path calling staging
        up to date; the content check still catches it."""
        tree, staging = _tree_and_staging(tmp_path)
        ss.write_manifest(staging, tree)
        _mark_staging_fresh(staging, tree, _stub_tc())
        _edit_keeping_old_mtime(
            tree / "lustre/tests/sanity-quota.sh", "echo v2\n"
        )

        rc, seen = self._run(tree, staging, userspace_only=False)

        assert rc == 0
        assert seen["script"] == "echo v2\n"

    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores modes")
    def test_a_refresh_that_fails_fails_the_deploy(
        self, tmp_sockets: Path, tmp_path: Path
    ) -> None:
        tree, staging = _tree_and_staging(tmp_path)
        ss.write_manifest(staging, tree)
        _mark_staging_fresh(staging, tree, _stub_tc())
        (tree / "lustre/tests/sanity-quota.sh").write_text("echo v2\n")
        tests_dir = staging / _TESTS
        tests_dir.chmod(0o555)
        try:
            rc, seen = self._run(tree, staging, userspace_only=True)
        finally:
            tests_dir.chmod(0o755)
        assert rc != 0
        assert "script" not in seen
