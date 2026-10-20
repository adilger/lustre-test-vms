"""Podman volumes that keep macOS builds off virtiofs.

On macOS podman runs its containers in a Linux VM (``podman machine``)
and reaches every bind mount over virtiofs, which is slow for the
access pattern of a kernel-module build: each compile opens and stats
thousands of kernel headers, and ccache writes a file per object.  A
Lustre build against a kernel build-tree on virtiofs spends more time
waiting on it than compiling -- 332 s, where the same build in a Linux
VM takes 137 s, and doubling the machine's CPUs gains nothing.

Two inputs are moved onto the machine's own disk, as named volumes:

- the kernel build-tree, which is read-only and changes only when the
  kernel artifact does: copied in once per kernel (~1.5 min for
  1.8 GB), keyed on the artifact so a rebuilt or re-fetched kernel gets
  a new volume and the old one is removed;
- ccache, which is a cache, so losing or not sharing it is harmless.

Together they bring that build to 160 s.  The Lustre tree itself stays
a bind mount: the user edits it on macOS and deploy reads its staging
dir there.

Every step falls back to the bind mount it replaces, with a warning,
so a podman that cannot do volumes builds as before.
``LTVM_MACOS_BUILD_VOLUMES=0`` turns the volumes off.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import logging
import os
import re
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

from .host_setup import is_macos

log = logging.getLogger(__name__)

# Written into the kernel volume after the copy has finished; a volume
# without it is a copy that was interrupted, and is rebuilt.
READY_MARKER = ".ltvm-volume-ready"
KERNEL_PREFIX = "ltvm-kernel-"
# Not "ltvm-ccache-": volumes of that name were ltvm's ccache before
# 0e4c1c9 and may still be lying around with the unreadable root that
# made it switch to a bind mount.
CCACHE_PREFIX = "ltvm-build-ccache-"


def enabled() -> bool:
    """True when builds should use volumes: macOS, not turned off."""
    return is_macos() and os.environ.get("LTVM_MACOS_BUILD_VOLUMES") != "0"


def _volume_name(*parts: str) -> str:
    # podman volume names: [a-zA-Z0-9][a-zA-Z0-9_.-]*
    return re.sub(r"[^a-zA-Z0-9_.-]", "_", "-".join(parts))


def kernel_volume_name(build_tree: Path) -> str:
    """The volume for this kernel artifact's build-tree.

    ``ltvm-kernel-<target>-<arch>-<kver>-<where>-<what>``: *where* hashes
    the artifact's path, so two ltvm checkouts on one Mac keep a volume
    each; *what* hashes its meta.json (which a rebuild or a fetch
    rewrites) and the kernel.release and Module.symvers that Lustre
    builds against, so a changed kernel never reads an old copy.  Only
    *what* may differ between a volume and the one that supersedes it.
    """
    build_tree = build_tree.resolve()
    kdir = build_tree.parent
    where = hashlib.sha256(str(build_tree).encode()).hexdigest()[:8]
    what = hashlib.sha256()
    for f in (kdir / "meta.json", build_tree / "include/config/kernel.release"):
        with contextlib.suppress(OSError):
            what.update(f.read_bytes())
    with contextlib.suppress(OSError):
        st = (build_tree / "Module.symvers").stat()
        what.update(f"{st.st_size}:{st.st_mtime_ns}".encode())
    # .../artifacts/<target>/<arch>/kernels/<kver>/build-tree
    parts = kdir.parts
    layout = (
        list(parts[-4:-2]) if len(parts) >= 4 and parts[-2] == "kernels" else []
    )
    return _volume_name(
        KERNEL_PREFIX.rstrip("-"),
        *layout,
        kdir.name,
        where,
        what.hexdigest()[:12],
    )


def ccache_volume_name(container_tag: str) -> str:
    return _volume_name(
        CCACHE_PREFIX.rstrip("-"), container_tag.removeprefix("ltvm-build-")
    )


def _podman(
    *args: str, timeout: float | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["podman", *args], capture_output=True, text=True, timeout=timeout
    )


@contextlib.contextmanager
def _lock(name: str) -> Iterator[None]:
    """Serialise ltvm processes populating the same volume."""
    path = (
        Path(tempfile.gettempdir()) / f"ltvm-volume-{os.getuid()}-{name}.lock"
    )
    with path.open("a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _probe(image: str, mounts: list[str], script: str) -> str | None:
    """Run *script* in *image* with *mounts*; its stdout, or None."""
    cmd = ["run", "--rm", "--security-opt", "label=disable"]
    for m in mounts:
        cmd += ["-v", m]
    r = _podman(*cmd, "--entrypoint", "sh", image, "-c", script, timeout=120)
    return r.stdout if r.returncode == 0 else None


def _ready(volume: str, image: str) -> bool:
    if _podman("volume", "exists", volume).returncode != 0:
        return False
    out = _probe(
        image,
        [f"{volume}:/v:ro"],
        f"test -f /v/{READY_MARKER} && echo ready || true",
    )
    return out is not None and out.strip() == "ready"


def _populate(volume: str, build_tree: Path, image: str) -> None:
    """Copy *build_tree* into a fresh *volume*, the marker last."""
    _podman("volume", "rm", "-f", volume)
    r = _podman("volume", "create", volume)
    if r.returncode != 0:
        raise RuntimeError(f"podman volume create {volume}: {r.stderr.strip()}")
    # Through podman's API rather than virtiofs: 92 s for a 1.8 GB tree,
    # against 254 s for a cp across the bind mount.  bsdtar's own uid
    # and AppleDouble entries are not mappable in the machine, so
    # everything goes in as root (the build container's own uid).
    env = {**os.environ, "COPYFILE_DISABLE": "1"}
    tar = subprocess.Popen(
        [
            "tar",
            "--uid",
            "0",
            "--gid",
            "0",
            "--no-mac-metadata",
            "--no-xattrs",
            "-C",
            str(build_tree),
            "-cf",
            "-",
            ".",
        ],
        stdout=subprocess.PIPE,
        env=env,
    )
    imp = subprocess.run(
        ["podman", "volume", "import", volume, "-"],
        stdin=tar.stdout,
        capture_output=True,
        text=True,
    )
    assert tar.stdout is not None
    tar.stdout.close()
    tar_rc = tar.wait()
    if tar_rc != 0 or imp.returncode != 0:
        raise RuntimeError(
            f"copying {build_tree} into {volume} failed "
            f"(tar rc={tar_rc}, import rc={imp.returncode}): {imp.stderr.strip()}"
        )
    if _probe(image, [f"{volume}:/v"], f": > /v/{READY_MARKER}") is None:
        raise RuntimeError(f"could not mark {volume} complete")


def _remove_superseded(keep: str) -> None:
    """Remove other volumes for the same kernel (an older artifact)."""
    stem = keep.rsplit("-", 1)[0]
    r = _podman("volume", "ls", "--format", "{{.Name}}")
    for name in (r.stdout or "").split():
        if name != keep and name.rsplit("-", 1)[0] == stem:
            log.info("removing superseded kernel volume %s", name)
            _podman("volume", "rm", "-f", name)


def kernel_tree_mount(build_tree: Path, image: str) -> str:
    """The ``-v`` source for /kernel: a volume when possible.

    *image* is the build container, used to check and mark the volume.

    Returns the volume name, or *build_tree* itself (the bind mount)
    when volumes are off or anything about them fails.
    """
    if not enabled():
        return str(build_tree)
    volume = kernel_volume_name(build_tree)
    try:
        with _lock(volume):
            if not _ready(volume, image):
                print(
                    f"--- Copying the kernel build-tree into podman volume "
                    f"{volume} (once per kernel, about a minute or two)...",
                    flush=True,
                )
                _populate(volume, build_tree, image)
                _remove_superseded(volume)
    except (OSError, RuntimeError, subprocess.SubprocessError) as e:
        log.warning(
            "kernel build-tree volume unavailable (%s); building against "
            "the bind mount, which is slower on macOS",
            e,
        )
        _podman("volume", "rm", "-f", volume)
        return str(build_tree)
    return volume


def ccache_mount(container_tag: str, fallback: Path) -> str:
    """The ``-v`` source for /ccache: a volume when it is writable.

    A volume whose root the container cannot write -- what podman
    machine 5.x did in 0e4c1c9 -- falls back to *fallback*, the host
    directory ccache used before.
    """
    if not enabled():
        return str(fallback)
    volume = ccache_volume_name(container_tag)
    try:
        with _lock(volume):
            if _podman("volume", "exists", volume).returncode != 0:
                r = _podman("volume", "create", volume)
                if r.returncode != 0:
                    raise RuntimeError(r.stderr.strip())
            out = _probe(
                container_tag,
                [f"{volume}:/ccache"],
                "mkdir -p /ccache/tmp && : > /ccache/tmp/.ltvm-w && echo ok",
            )
            if out is None or out.strip() != "ok":
                raise RuntimeError(f"{volume} is not writable in the container")
    except (OSError, RuntimeError, subprocess.SubprocessError) as e:
        log.warning("ccache volume unavailable (%s); using %s", e, fallback)
        return str(fallback)
    return volume


def dead_kernel_volumes(artifacts_dir: Path | None = None) -> list[str]:
    """Kernel volumes no kernel artifact on this host would mount.

    What is left once `ltvm clean` (or a hand rm) takes a kernel away;
    a rebuilt kernel's old volume is removed when its new one is made.
    Only where volumes are in use: none are made anywhere else.
    """
    if not enabled():
        return []
    if artifacts_dir is None:
        from .target_config import ARTIFACTS_DIR

        artifacts_dir = ARTIFACTS_DIR
    try:
        r = _podman("volume", "ls", "--format", "{{.Name}}", timeout=30)
    except (OSError, subprocess.SubprocessError):
        return []
    if r.returncode != 0:
        return []
    live = {
        kernel_volume_name(bt)
        for bt in artifacts_dir.glob("*/*/kernels/*/build-tree")
        if bt.is_dir()
    }
    return sorted(
        n
        for n in r.stdout.split()
        if n.startswith(KERNEL_PREFIX) and n not in live
    )


def remove_volume(name: str) -> bool:
    try:
        return _podman("volume", "rm", name, timeout=60).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
