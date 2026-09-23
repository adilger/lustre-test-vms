"""Privileged-operation helpers.

ltvm runs as the invoking user and elevates only the specific
operations that require root (bridge/tap setup, /etc/hosts edits,
qemu launch, losetup/mount, etc.).  This module exposes the
helpers that make that uniform across host_setup, vm_commands,
vm_net, qemu_run, image_export, and vm_cluster: ``sudo_run()``
prefixes a command with ``sudo`` when not already root, and
``sudo_prime()`` warms the sudo timestamp upfront so later
``sudo_run()`` calls don't surprise the user with a mid-flow
password prompt.  ``atomic_write()`` writes a file atomically,
falling back to a ``sudo install`` when the destination dir
isn't user-writable (e.g. ``/etc/hosts`` or ``/opt/qemu-vms/``).

Commands documented as never needing root (deploy-lustre, cluster
deploy) pass ``noninteractive=True``: sudo is then used only with
``-n`` -- a cached timestamp or NOPASSWD rule -- and a write that
would need a password raises ``PermissionError`` for the caller to
warn about instead of stalling an unattended run at a prompt.

These helpers are deliberately dependency-free (stdlib only) so
any module can import them without risking a circular import.
"""

from __future__ import annotations

import errno
import logging
import os
import stat
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

# Set once `sudo -v` has been refused.  Every later sudo_run() in the
# process then uses `sudo -n`, so a user without sudo is told no once
# rather than prompted again for each host operation.
_sudo_refused = False


class SudoUnavailable(RuntimeError):
    """sudo would not grant root to this user."""


def _run(
    cmd: list[str],
    *,
    check: bool = True,
    quiet: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run a command, optionally capturing output, raising on non-zero."""
    log.debug("run: %s", " ".join(str(c) for c in cmd))
    r = subprocess.run(cmd, capture_output=quiet, text=True)
    if check and r.returncode != 0:
        # Include the captured stderr: with quiet=True the child's
        # output goes nowhere else, so without this an atomic_write
        # sudo-fallback failure reports only an argv and an rc, and
        # the actual reason (ENOSPC, read-only fs, sudo policy) is
        # lost entirely.
        detail = ""
        if quiet:
            err = (r.stderr or r.stdout or "").strip()
            if err:
                detail = f": {err}"
        raise RuntimeError(
            f"Command failed (rc={r.returncode}): "
            f"{' '.join(str(c) for c in cmd)}{detail}"
        )
    return r


def sudo_run(
    cmd: list[str],
    *,
    check: bool = True,
    quiet: bool = False,
    noninteractive: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run a command under sudo (no-op prefix if already root).

    ``noninteractive`` adds ``-n``: use a credential that is already
    there (cached timestamp, NOPASSWD) and fail rather than prompt.
    """
    if os.geteuid() == 0:
        return _run(cmd, check=check, quiet=quiet)
    if noninteractive or _sudo_refused:
        return _run(["sudo", "-n", *cmd], check=check, quiet=quiet)
    return _run(["sudo", *cmd], check=check, quiet=quiet)


def sudo_ready() -> bool:
    """Can we sudo right now without being asked for a password?

    True when already root, or when ``sudo -n true`` succeeds -- an
    unexpired timestamp or a NOPASSWD rule.
    """
    if os.geteuid() == 0:
        return True
    return _run(["sudo", "-n", "true"], check=False, quiet=True).returncode == 0


def sudo_prime(reason: str) -> None:
    """Prompt for sudo credentials up front so later ``sudo_run()``
    calls don't interrupt with a surprise password prompt mid-flow.

    Skips the prompt entirely when ``sudo -n true`` succeeds, which
    covers both an unexpired sudo timestamp and ``NOPASSWD`` rules --
    in those cases ``sudo -v`` would still try to authenticate and
    fail in non-tty contexts (subshells, hooks, CI), aborting even
    though every later ``sudo`` would have worked.

    Raises ``SudoUnavailable`` when sudo refuses.
    """
    global _sudo_refused
    if sudo_ready():
        return
    log.info("%s -- prompting for sudo credentials now.", reason)
    r = _run(["sudo", "-v"], check=False)
    if r.returncode != 0:
        _sudo_refused = True
        raise SudoUnavailable(f"{reason}, and sudo refused")


def invoking_user() -> tuple[str, str] | None:
    """(user, group) of the human behind this invocation, or None.

    Files ltvm writes into root-owned dirs like /opt/qemu-vms/sockets
    go through a ``sudo install``, so without an explicit owner they
    land root-owned and the next unprivileged ltvm command can't touch
    them.  Under sudo the human is $SUDO_USER; otherwise it is just us.
    None means we genuinely are root (a real root login) and there is
    no better owner to pick.
    """
    import grp
    import pwd

    name = os.environ.get("SUDO_USER")
    if not name and os.geteuid() != 0:
        try:
            name = pwd.getpwuid(os.geteuid()).pw_name
        except KeyError:
            return None
    if not name or name == "root":
        return None
    try:
        pw = pwd.getpwnam(name)
        return name, grp.getgrgid(pw.pw_gid).gr_name
    except KeyError:
        return None


def _ltvm_owned(path: Path) -> bool:
    """Is *path* a file ltvm creates and must hand back to the human?

    ltvm writes two very different kinds of file through
    ``atomic_write()``: its own state (``/opt/qemu-vms/sockets/*.info``,
    lock files, overlays) which a later *unprivileged* ltvm has to
    rewrite, and pre-existing system files (``/etc/hosts``) which it
    only edits a line of.  Only the first kind may be chowned to the
    invoking user.

    Handing ``/etc/hosts`` to the invoking user -- which is what this
    function exists to prevent -- means any user who can run a single
    ``ltvm create`` owns it from then on and can rewrite it at will
    with no privilege at all.
    """
    vm_dir = Path(os.environ.get("LTVM_VM_DIR", "/opt/qemu-vms"))
    roots = [vm_dir]
    owner = invoking_user()
    if owner is not None:
        import pwd

        try:
            roots.append(Path(pwd.getpwnam(owner[0]).pw_dir))
        except KeyError:
            pass
    for root in roots:
        try:
            path.resolve().relative_to(root.resolve())
            return True
        except (ValueError, OSError):
            continue
    return False


def chown_to_invoking_user(path: Path) -> None:
    """Give *path* back to the human when we hold it as root.

    A no-op when not root or when there is no better owner.  Failures
    are ignored: ownership is a convenience, not correctness, and the
    caller has already written the file.
    """
    if os.geteuid() != 0:
        return
    owner = invoking_user()
    if owner is None:
        return
    import pwd

    try:
        pw = pwd.getpwnam(owner[0])
        os.chown(path, pw.pw_uid, pw.pw_gid)
    except (KeyError, OSError):
        pass


def chmod_regular(path: Path, mode: int) -> None:
    """chmod *path* only if it is a regular file, never through a link.

    For root working in a directory other users can write to.
    """
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "not a regular file", str(path))
        os.fchmod(fd, mode)
    finally:
        os.close(fd)


def ensure_dir(path: Path, *, noninteractive: bool = False) -> None:
    """Create directory *path* (and parents), escalating when it must.

    /opt/qemu-vms is root-owned, and on macOS nothing creates it before
    the first `ltvm create` -- the Linux `ltvm install` makes it while
    setting up the bridge, which macOS has no equivalent of -- so a bare
    ``mkdir`` from the unprivileged create died with PermissionError
    before the VM got its IP.  Made under sudo, the directory is
    root-owned 0755 as `ltvm install` would have left it.
    """
    if path.is_dir():
        return
    try:
        path.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        if noninteractive and not sudo_ready():
            raise
        sudo_run(
            ["mkdir", "-p", str(path)],
            quiet=True,
            noninteractive=noninteractive,
        )


def ensure_lock_file(path: Path, *, noninteractive: bool = False) -> None:
    """Create *path* as a 0666 lock file if it is not there yet.

    Deliberately NOT ``atomic_write``: that finishes with a rename, and
    a rename is exactly wrong for a lock file.  Two processes racing the
    first acquisition each wrote their own temp file and renamed it into
    place, so each ended up holding ``flock`` on a different inode --
    the loser's inode already unlinked -- and both entered the critical
    section.  That defeated both locks that guard a first create:
    ``.ip-alloc.lock`` (two VMs handed the same 192.168.100.x) and
    ``.hosts.lock`` (an unsynchronised /etc/hosts read-modify-write
    dropping an entry).  The sudo branch made the window tens of
    milliseconds wide, since it spans two sudo spawns.

    ``open(O_CREAT)`` and ``touch`` both attach to whichever inode
    exists, which is the property a lock file needs.  The sudo branch
    exists because /opt/qemu-vms is root-owned 0755, so an
    unprivileged create cannot make a file there at all.
    """
    if path.exists():
        return
    chmod_as_root = ["chmod", "666", str(path)]
    # O_NOFOLLOW: in a group-writable VM_DIR a planted symlink would
    # otherwise have a root ltvm chmod its target 0666.
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o666)
    except PermissionError:
        if noninteractive and not sudo_ready():
            raise
        sudo_run(
            ["touch", str(path)], quiet=True, noninteractive=noninteractive
        )
        sudo_run(
            chmod_as_root,
            check=False,
            quiet=True,
            noninteractive=noninteractive,
        )
        return
    except OSError:
        return
    # O_CREAT's mode is masked by umask, and the point of 0666 is that
    # the *other* uid can open it later.  chmod can fail when the file
    # is already there and owned by root; that is fine, it means some
    # earlier call already set the mode.
    try:
        os.fchmod(fd, 0o666)
    except OSError:
        sudo_run(
            chmod_as_root,
            check=False,
            quiet=True,
            noninteractive=noninteractive,
        )
    finally:
        os.close(fd)


def atomic_write(
    path: Path,
    text: str,
    mode: int = 0o644,
    *,
    noninteractive: bool = False,
) -> None:
    """Write *text* to *path* atomically, falling back to sudo when
    the destination dir isn't user-writable.

    With ``noninteractive`` the fallback runs ``sudo -n`` only, and
    raises ``PermissionError`` up front when that would need a
    password -- for callers that must never block on a prompt.

    User-writable case: tempfile + rename in the same directory --
    a true atomic swap on the destination filesystem.

    Sudo fallback: write the tempfile under /tmp, install it to a
    temporary name in the destination directory, then rename it into
    place. This preserves the same-filesystem atomic replacement and
    avoids trying to mkstemp inside e.g. ``/etc/`` as a normal user.

    Creates parent directories as needed (sudo if required).
    """
    parent = path.parent
    ensure_dir(parent, noninteractive=noninteractive)

    owns = _ltvm_owned(path)
    prev_owner: tuple[int, int] | None = None
    try:
        st = path.stat()
        prev_owner = (st.st_uid, st.st_gid)
    except OSError:
        prev_owner = None
    # System files keep their owner.  So does ltvm state that already
    # belongs to a user: root working on someone's VM must not take it
    # over.  New or root-owned state goes to the invoking user.
    keep = prev_owner
    if owns and (prev_owner is None or prev_owner[0] == 0):
        keep = None

    if os.access(str(parent), os.W_OK):
        fd, tmp = tempfile.mkstemp(dir=str(parent), prefix=f".{path.name}.")
        replaced = False
        try:
            with os.fdopen(fd, "w") as f:
                f.write(text)
            os.chmod(tmp, mode)
            # The tempfile we are about to rename over the destination
            # is ours, so without this a root-run ltvm would turn
            # /etc/hosts into whatever we happen to be.
            if keep is not None:
                try:
                    os.chown(tmp, keep[0], keep[1])
                except OSError:
                    pass
            try:
                os.rename(tmp, str(path))
                replaced = True
            except PermissionError:
                # A sticky shared directory: only the file's owner or
                # root may replace it, so this goes through sudo below.
                if os.geteuid() == 0:
                    raise
            if replaced and owns and keep is None:
                chown_to_invoking_user(path)
        finally:
            if not replaced:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        if replaced:
            return

    if noninteractive and not sudo_ready():
        raise PermissionError(
            errno.EACCES,
            f"{parent} is not writable and sudo would need a password",
            str(path),
        )

    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir="/tmp")
    dest_tmp = parent / f".{path.name}.{os.getpid()}.tmp"
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        owner = invoking_user() if owns else None
        own_args = ["-o", owner[0], "-g", owner[1]] if owner is not None else []
        if keep is not None:
            # Rather than letting `install` default to whoever sudo runs as.
            own_args = ["-o", str(keep[0]), "-g", str(keep[1])]
        sudo_run(
            [
                "install",
                "-m",
                f"{mode & 0o777:o}",
                *own_args,
                tmp,
                str(dest_tmp),
            ],
            quiet=True,
            noninteractive=noninteractive,
        )
        sudo_run(
            ["mv", "-f", str(dest_tmp), str(path)],
            quiet=True,
            noninteractive=noninteractive,
        )
    finally:
        sudo_run(
            ["rm", "-f", str(dest_tmp)],
            check=False,
            quiet=True,
            noninteractive=noninteractive,
        )
        try:
            os.unlink(tmp)
        except OSError:
            pass
