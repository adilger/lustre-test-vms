"""Running VMs without root on a shared Linux host.

`ltvm install` prepares a host so that members of the ``ltvm`` group
need no root for the VM lifecycle:

* VM_DIR, its ``overlays/`` and ``sockets/``, and ``hosts.d/`` are
  ``root:ltvm`` with mode 3775.  The sticky bit stops one member from
  deleting or replacing another member's files.
* QEMU's bridge helper is setuid root and its ACL allows the ltvm
  bridge, so a QEMU running as the user can put a NIC on it.
* dnsmasq serves the VM names in ``hosts.d/``, which it watches, so
  registering a VM needs neither /etc/hosts nor a reload signal.

``ready()`` says whether this process can work that way.  When it
cannot, callers keep elevating each host operation with sudo.
"""

from __future__ import annotations

import os
import platform
import pwd
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from .vm_state import BRIDGE, OVERLAYS, SOCKETS, VM_DIR

GROUP = "ltvm"
SHARED_DIR_MODE = 0o3775
HOSTS_DIR = VM_DIR / "hosts.d"
KVM_DEVICE = Path("/dev/kvm")
BRIDGE_ACL = Path("/etc/qemu/bridge.conf")

# Debian/Ubuntu, then Fedora/RHEL.  ltvm's prebuilt QEMU ships no helper.
HELPER_CANDIDATES = (
    Path("/usr/lib/qemu/qemu-bridge-helper"),
    Path("/usr/libexec/qemu-bridge-helper"),
)


def shared_dirs() -> tuple[Path, ...]:
    return (VM_DIR, OVERLAYS, SOCKETS, HOSTS_DIR)


def is_setuid_root(path: Path) -> bool:
    try:
        st = path.stat()
    except OSError:
        return False
    return (
        stat.S_ISREG(st.st_mode)
        and st.st_uid == 0
        and bool(st.st_mode & stat.S_ISUID)
        and bool(st.st_mode & stat.S_IXOTH)
    )


def installed_helper() -> Path | None:
    """The bridge helper the distro installed, setuid or not."""
    for path in HELPER_CANDIDATES:
        if path.is_file():
            return path
    return None


def bridge_helper() -> Path | None:
    """A bridge helper an unprivileged QEMU can use, or None."""
    for path in HELPER_CANDIDATES:
        if is_setuid_root(path):
            return path
    return None


def _acl_rules(path: Path, depth: int = 0) -> list[tuple[str, str]]:
    if depth > 4:
        return []
    try:
        text = path.read_text()
    except OSError:
        return []
    rules: list[tuple[str, str]] = []
    for raw in text.splitlines():
        words = raw.split("#", 1)[0].split()
        if len(words) != 2:
            continue
        verb, arg = words
        if verb == "include":
            rules += _acl_rules(Path(arg), depth + 1)
        elif verb in ("allow", "deny"):
            rules.append((verb, arg))
    return rules


def bridge_denied(bridge: str = BRIDGE, acl: Path = BRIDGE_ACL) -> bool:
    """Does a deny rule in the helper's ACL match *bridge*?

    The helper refuses on any matching deny, whatever the order.
    """
    return any(v == "deny" and a in (bridge, "all") for v, a in _acl_rules(acl))


def bridge_allowed(bridge: str = BRIDGE, acl: Path = BRIDGE_ACL) -> bool:
    """Does the helper's ACL let a user attach to *bridge*?"""
    if bridge_denied(bridge, acl):
        return False
    return any(
        v == "allow" and a in (bridge, "all") for v, a in _acl_rules(acl)
    )


def _accessible(path: Path, mode: int) -> bool:
    if os.access in os.supports_effective_ids:
        return os.access(path, mode, effective_ids=True)
    return os.access(path, mode)


@dataclass
class Readiness:
    helper: Path | None = None
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def readiness() -> Readiness:
    """Can this process run VMs with no root at all?"""
    r = Readiness()
    if platform.system() != "Linux":
        r.problems.append("unprivileged VMs are Linux-only")
        return r
    r.helper = bridge_helper()
    if r.helper is None:
        found = installed_helper()
        if found is None:
            r.problems.append("no qemu-bridge-helper is installed")
        else:
            r.problems.append(f"{found} is not setuid root")
    elif not bridge_allowed():
        r.problems.append(f"{BRIDGE_ACL} does not allow {BRIDGE}")
    for d in shared_dirs():
        if not _accessible(d, os.W_OK | os.X_OK):
            r.problems.append(
                f"{d} is not writable -- the user must be in the "
                f"'{GROUP}' group"
            )
            break
    if KVM_DEVICE.exists() and not _accessible(KVM_DEVICE, os.R_OK | os.W_OK):
        r.problems.append(f"{KVM_DEVICE} is not accessible -- join 'kvm'")
    return r


def ready() -> bool:
    """True when this unprivileged process can run VMs without sudo."""
    return os.geteuid() != 0 and readiness().ok


@contextmanager
def _acting_as(user: pwd.struct_passwd) -> Iterator[None]:
    """Take *user*'s effective ids for access checks, then restore."""
    saved_groups = os.getgroups()
    saved_gid = os.getegid()
    os.setgroups(os.getgrouplist(user.pw_name, user.pw_gid))
    os.setegid(user.pw_gid)
    os.seteuid(user.pw_uid)
    try:
        yield
    finally:
        os.seteuid(0)
        os.setegid(saved_gid)
        os.setgroups(saved_groups)


def sudo_user() -> pwd.struct_passwd | None:
    """The account behind `sudo ltvm`, when root came from sudo."""
    name = os.environ.get("SUDO_USER")
    if os.geteuid() != 0 or not name or name == "root":
        return None
    try:
        return pwd.getpwnam(name)
    except KeyError:
        return None


def drop_to(user: pwd.struct_passwd) -> bool:
    """Run the rest of this root process as *user*.

    Root has no business creating files in the shared VM directories:
    every member of the group can plant a symlink there, and root
    follows it.  Dropping is permanent, so it happens only when *user*
    can do the whole job unprivileged.  Returns True when the process is
    now *user*.
    """
    with _acting_as(user):
        if not readiness().ok:
            return False
    os.setgroups(os.getgrouplist(user.pw_name, user.pw_gid))
    os.setgid(user.pw_gid)
    os.setuid(user.pw_uid)
    os.environ.update(HOME=user.pw_dir, USER=user.pw_name, LOGNAME=user.pw_name)
    for var in ("SUDO_USER", "SUDO_UID", "SUDO_GID", "SUDO_COMMAND"):
        os.environ.pop(var, None)
    return True


def drop_to_sudo_user() -> bool:
    """Run the rest of `sudo ltvm ...` as the user who typed it."""
    user = sudo_user()
    return user is not None and drop_to(user)


def drop_to_owner(paths: list[Path]) -> bool:
    """Run as the one non-root user who owns every existing *paths*.

    Starting a VM as root would run its QEMU as root, writing where the
    VM's owner can plant links; as the owner it is that user's process.
    """
    if os.geteuid() != 0:
        return False
    uids = set()
    for p in paths:
        try:
            uids.add(p.stat().st_uid)
        except OSError:
            continue
    if len(uids) != 1 or 0 in uids:
        return False
    try:
        return drop_to(pwd.getpwuid(uids.pop()))
    except KeyError:
        return False


# ── VM name registry ─────────────────────────────────────


def hosts_file(name: str) -> Path:
    return HOSTS_DIR / name


def hosts_dir_writable() -> bool:
    return HOSTS_DIR.is_dir() and _accessible(HOSTS_DIR, os.W_OK | os.X_OK)


def write_hosts_entry(name: str, ip: str, marker: str) -> None:
    """Publish *name* to dnsmasq through ``hosts.d``."""
    path = hosts_file(name)
    # Written beside hosts.d, not in it: dnsmasq should only ever see the
    # finished file arrive.
    tmp = VM_DIR / f".hosts.{name}.{os.getpid()}.tmp"
    fd = os.open(
        tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644
    )
    try:
        with os.fdopen(fd, "w") as f:
            # dnsmasq reads this as its own user, whatever our umask.
            os.fchmod(f.fileno(), 0o644)
            f.write(f"{ip}\t{name} {marker}:{name}\n")
        os.rename(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_hosts_ip(name: str) -> str | None:
    try:
        words = hosts_file(name).read_text().split()
    except OSError:
        return None
    return words[0] if words else None


def remove_hosts_entry(name: str) -> None:
    try:
        hosts_file(name).unlink()
    except FileNotFoundError:
        pass


def hosts_entries() -> list[str]:
    """VM names that have a ``hosts.d`` entry."""
    try:
        return sorted(
            p.name
            for p in HOSTS_DIR.iterdir()
            if p.is_file() and not p.name.startswith(".")
        )
    except OSError:
        return []
