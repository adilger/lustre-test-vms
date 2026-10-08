"""The Lustre version is decided on the host, where git works.

The build container cannot run ``git describe`` in a worktree (the
gitdir it names is not mounted), so a worktree build used to carry
LUSTRE-VERSION-GEN's DEFAULT_VERSION and skip every version-gated test.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ltvm_pkg.lustre_build import build_lustre
from ltvm_pkg.lustre_version import (
    VERSION_FILE,
    LustreVersion,
    check_configured,
    pin_version,
)

# The part of Lustre's LUSTRE-VERSION-GEN that picks the version,
# unchanged from 2.12 through master.
VERSION_GEN = """\
#!/bin/bash
DEFAULT_VERSION=2.17.58
LVF=LUSTRE-VERSION-FILE
LF='
'
if test -d ${GIT_DIR:-.git} -o -f .git &&
	VN=$(git describe --match "[0-9]*" --abbrev=7 HEAD 2>/dev/null) &&
	case "$VN" in
	*$LF*) (exit 1) ;;
	[0-9]*)
		git update-index -q --refresh
		test -z "$(git diff-index --name-only HEAD --)" ||
		VN="$VN-dirty" ;;
	esac
then
	VN=$(echo "$VN" | sed -e 's/-/_/g');
elif test -r $LVF
then
	VN=$(sed -e 's/^LUSTRE_VERSION = //' <$LVF)
else
	VN="$DEFAULT_VERSION"
fi
echo $VN
"""

needs_git = pytest.mark.skipif(
    shutil.which("git") is None, reason="needs git on PATH"
)


@pytest.fixture
def git_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
    for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        monkeypatch.delenv(var, raising=False)


def git(*args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def gen(tree: Path) -> str:
    return subprocess.run(
        ["bash", str(tree / "LUSTRE-VERSION-GEN")],
        cwd=tree,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def make_repo(tmp_path: Path, *, tag: bool = True) -> Path:
    main = tmp_path / "main"
    git("init", "-q", str(main))
    (main / "LUSTRE-VERSION-GEN").write_text(VERSION_GEN)
    (main / ".gitignore").write_text("/LUSTRE-VERSION-FILE\n")
    git("-C", str(main), "add", ".")
    git("-C", str(main), "commit", "-q", "-m", "base")
    if tag:
        git("-C", str(main), "tag", "-a", "2.17.58", "-m", "2.17.58")
    for i in range(3):
        git("-C", str(main), "commit", "-q", "--allow-empty", "-m", f"c{i}")
    return main


def worktree(main: Path, tmp_path: Path) -> Path:
    wt = tmp_path / "wt"
    git("-C", str(main), "worktree", "add", "-q", str(wt))
    return wt


@needs_git
@pytest.mark.usefixtures("git_env")
class TestPinVersion:
    def test_worktree_gets_the_real_version(self, tmp_path: Path) -> None:
        wt = worktree(make_repo(tmp_path), tmp_path)
        head = git("-C", str(wt), "rev-parse", "--short=7", "HEAD")
        pin = pin_version(wt)
        assert pin.source == "git"
        assert pin.version == f"2.17.58_3_g{head}"
        assert gen(wt) == pin.version

    def test_container_without_the_gitdir_reads_the_pin(
        self, tmp_path: Path
    ) -> None:
        """The container's view: .git names a gitdir that is not there."""
        main = make_repo(tmp_path)
        wt = worktree(main, tmp_path)
        pin = pin_version(wt)
        (main / ".git").rename(tmp_path / "hidden-git")
        assert gen(wt) == pin.version
        assert gen(wt) != "2.17.58"

    def test_container_without_the_pin_falls_back_to_default(
        self, tmp_path: Path
    ) -> None:
        """The bug this module exists for."""
        main = make_repo(tmp_path)
        wt = worktree(main, tmp_path)
        (main / ".git").rename(tmp_path / "hidden-git")
        assert gen(wt) == "2.17.58"

    def test_dirty_tree(self, tmp_path: Path) -> None:
        wt = worktree(make_repo(tmp_path), tmp_path)
        (wt / "LUSTRE-VERSION-GEN").write_text(VERSION_GEN + "# edit\n")
        pin = pin_version(wt)
        assert pin.version is not None and pin.version.endswith("_dirty")
        assert gen(wt) == pin.version

    def test_untracked_files_are_not_dirty(self, tmp_path: Path) -> None:
        wt = worktree(make_repo(tmp_path), tmp_path)
        (wt / ".ltvm-build-lock").write_text("")
        pin = pin_version(wt)
        assert pin.version is not None
        assert not pin.version.endswith("_dirty")

    def test_no_tags_is_undetermined(self, tmp_path: Path) -> None:
        main = make_repo(tmp_path, tag=False)
        pin = pin_version(main)
        assert pin.source == "default"
        assert pin.version is None
        assert pin.default == "2.17.58"
        assert "git describe failed" in pin.reason
        assert not (main / VERSION_FILE).exists()

    def test_rewrites_a_stale_version_file(self, tmp_path: Path) -> None:
        main = make_repo(tmp_path)
        (main / VERSION_FILE).write_text("LUSTRE_VERSION = 2.17.58\n")
        pin = pin_version(main)
        assert (main / VERSION_FILE).read_text() == (
            f"LUSTRE_VERSION = {pin.version}\n"
        )


class TestNotGit:
    def test_release_tarball_uses_its_version_file(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "LUSTRE-VERSION-GEN").write_text(VERSION_GEN)
        (tmp_path / VERSION_FILE).write_text("LUSTRE_VERSION = 2.15.8\n")
        pin = pin_version(tmp_path)
        assert (pin.source, pin.version) == ("file", "2.15.8")

    def test_nothing_to_go_on_is_undetermined(self, tmp_path: Path) -> None:
        (tmp_path / "LUSTRE-VERSION-GEN").write_text(VERSION_GEN)
        pin = pin_version(tmp_path)
        assert pin.source == "default"
        assert "not a git checkout" in pin.reason


class TestCheckConfigured:
    def _configured(self, tree: Path, version: str) -> None:
        (tree / "config.h").write_text(f'#define VERSION "{version}"\n')

    def test_match(self, tmp_path: Path) -> None:
        self._configured(tmp_path, "2.17.58_3_gabc1234")
        pin = LustreVersion("2.17.58_3_gabc1234", "git", "2.17.58")
        assert check_configured(tmp_path, pin) == [
            "  Lustre version: 2.17.58_3_gabc1234"
        ]

    def test_dirty_alone_is_a_match(self, tmp_path: Path) -> None:
        self._configured(tmp_path, "2.17.58_3_gabc1234")
        pin = LustreVersion("2.17.58_3_gabc1234_dirty", "git", "2.17.58")
        assert "HEAD is" not in check_configured(tmp_path, pin)[0]

    def test_default_when_git_knew_better_warns(self, tmp_path: Path) -> None:
        self._configured(tmp_path, "2.17.58")
        pin = LustreVersion("2.17.58_3_gabc1234", "git", "2.17.58")
        assert "WARNING" in check_configured(tmp_path, pin)[0]

    def test_undetermined_warns(self, tmp_path: Path) -> None:
        self._configured(tmp_path, "2.17.58")
        pin = LustreVersion(None, "default", "2.17.58", "git describe failed")
        out = check_configured(tmp_path, pin)[0]
        assert "WARNING" in out and "2.17.58" in out

    def test_stale_configure_is_named(self, tmp_path: Path) -> None:
        self._configured(tmp_path, "2.17.58_1_g1111111")
        pin = LustreVersion("2.17.58_3_gabc1234", "git", "2.17.58")
        out = check_configured(tmp_path, pin)[0]
        assert "HEAD is 2.17.58_3_gabc1234" in out and "--force" in out


def _build(wt: Path, tmp_path: Path, *, force: bool = False) -> list[str]:
    """Run build_lustre up to the container; return LVF text and script."""
    (wt / "lustre" / "kernel_patches").mkdir(parents=True, exist_ok=True)
    kernel = tmp_path / "kernel"
    (kernel / "include" / "config").mkdir(parents=True, exist_ok=True)
    (kernel / "include" / "config" / "kernel.release").write_text("5.14.0\n")
    (kernel / "Module.symvers").write_text("")
    seen: list[str] = []

    def fake_podman(cmd, *args, **kwargs):
        seen.extend([(wt / VERSION_FILE).read_text(), cmd[-1]])
        r = MagicMock()
        r.returncode = 1
        return r

    with (
        patch(
            "ltvm_pkg.lustre_build.run_podman_with_cleanup",
            side_effect=fake_podman,
        ),
        patch("ltvm_pkg.lustre_build._container_exists", return_value=True),
        patch("ltvm_pkg.target_config.TargetConfig") as mock_tc,
        pytest.raises(RuntimeError),
    ):
        mock_tc.return_value.resolve_kernel.return_value = "5.14-rhel9.7"
        build_lustre(wt, kernel, container_tag="ltvm-build-rocky9", force=force)
    return seen


@needs_git
@pytest.mark.usefixtures("git_env")
class TestBuild:
    def test_pins_the_version_before_the_container_runs(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        wt = worktree(make_repo(tmp_path), tmp_path)
        lvf, _ = _build(wt, tmp_path)
        head = git("-C", str(wt), "rev-parse", "--short=7", "HEAD")
        assert lvf == f"LUSTRE_VERSION = 2.17.58_3_g{head}\n"
        assert f"Lustre version: 2.17.58_3_g{head}" in capsys.readouterr().out

    def test_reconfigure_regenerates_a_stale_configure(
        self, tmp_path: Path
    ) -> None:
        """autoconf cannot see a version change; the cache must go."""
        wt = worktree(make_repo(tmp_path), tmp_path)
        (wt / "configure").write_text("PACKAGE_VERSION='2.17.58'\n")
        _, script = _build(wt, tmp_path, force=True)
        assert script.index("rm -rf autom4te.cache configure") < script.index(
            "bash autogen.sh"
        )

    def test_current_configure_is_kept(self, tmp_path: Path) -> None:
        wt = worktree(make_repo(tmp_path), tmp_path)
        pin = pin_version(wt)
        (wt / "configure").write_text(f"PACKAGE_VERSION='{pin.version}'\n")
        _, script = _build(wt, tmp_path, force=True)
        assert "autom4te.cache" not in script
