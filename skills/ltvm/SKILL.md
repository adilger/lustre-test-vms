---
name: ltvm
description: This skill should be used to get a Lustre test VM and put a build on it with ltvm - "create a test VM", "spin up a Lustre VM", "deploy my tree to a VM", "build Lustre for the VM", "mount Lustre and run sanity", "make a two-node cluster", "my VM is hung", "collect the crash from that VM", "the VM kernel does not match my build". Covers VM and cluster lifecycle, deploying a Lustre tree, artifacts, and the root rules.
version: 0.1.0
---

# ltvm: Lustre test VMs

`ltvm` builds and runs QEMU microVMs for Lustre development. Four
cacheable artifacts per target OS -- build container, kernel, VM base
image, and Lustre staging -- each rebuilt only when its inputs change.

This is a test environment built for the purpose. Creating, destroying,
building and mounting are ordinary operations: do them rather than asking
whether to.

## First use on a host

```bash
ltvm doctor                     # is the host set up?
sudo ./ltvm install             # QEMU, bridge, dnsmasq, SSH, ltvm on PATH
ltvm target fetch rocky9        # pre-built artifacts -- much faster than building
ltvm build status               # what is stale
```

On macOS it is `./ltvm install` with no `sudo`: it refuses to run as
root there and prompts for sudo itself where it must (README.md,
"Running on macOS").

`install` also sets up tab completion for the human's shell (bash, zsh,
fish), which needs a new shell before it works -- worth saying when you
walk someone through setup, since nothing else announces it. `ltvm
doctor --fix` installs it on a host that predates it.

`ltvm target fetch` is the fast path, and a fetched image already has
Lustre baked in -- `ltvm create` then `ltvm llmount` gives a mounted
filesystem with no build at all. Build locally only when a target has no
release, or when the kernel or image genuinely needs to change:
`ltvm build all rocky9 --lustre-tree <tree>` rebuilds only what is stale,
`--force` rebuilds everything, and `ltvm build status` shows what is.

Images are per-kernel. A target with two kernels has an image for each,
so `--kernel <name>` on `build image` and on `create` picks which one,
and creating against a kernel with no matching image fails rather than
booting the wrong modules. A Lustre build lands in the tree's
`.ltvm-staging/<target>/<arch>/<kernel>/`.

## A single VM, from nothing to a mounted filesystem

```bash
ltvm create co1-single --vcpus 2 --mem 4096 --mdt-disks 1 --ost-disks 3
ltvm deploy-lustre co1-single --lustre-tree <tree> --mount
ssh co1-single 'lctl dl'
```

```bash
ltvm list                       # running and stopped
ltvm start|stop|destroy co1-single
ltvm doctor [--fix]             # host infrastructure health
```

Every VM command also answers under `vm` -- `ltvm vm create` is `ltvm
create`, and the same for `destroy`, `start`, `stop`, `list`,
`deploy-lustre`, `llmount`, `llumount` and `doctor`.

`--disk-size` sizes the MDT/OST scratch disks; `--root-size` sizes the
VM's own OS disk (default 8G). Both are fixed at create time, so a VM
that needs room for a debug build or a vmcore wants `--root-size 20G`
when it is created, not after.

vCPUs and memory can change later without recreating the VM: stop it,
then `ltvm vm set <name> --vcpus 2 --mem 4096`, and the new size applies
at the next start. Use it to shrink idle VMs on a crowded host rather
than destroying them.

Running guests may commit at most the host's RAM less a reserve.
`create` and `start` refuse a VM that would go over it and list the
guests using it; with `--wait SECONDS` they wait up to that long for
the memory to free instead, and `cluster create --wait` gives every
node the same wait. On a host other sessions share, where their guests
are not yours to stop, that is how to wait for room -- not a retry loop:

```bash
ltvm create co1-single --mem 4096 --wait 1800
```

A host running KSM holds far less than that, since guests booted from
one image share most of their pages, so an admin may have set
`[memory] overcommit = N` in `/etc/ltvm.conf`. The running guests may
then commit N times the budget, though one VM must still fit the budget
on its own, and a refusal prints the ratio in force. It is off by
default and it is the user's setting for the whole host: do not raise
it to get past a refusal -- wait.

`--kernel-args` adds kernel boot parameters, also fixed at create time.
The target kernels have no KASAN, but they build in SLUB debugging (and
the Rocky ones page owner tracking), off until booted with them:

```bash
ltvm create co1-single --kernel-args 'slub_debug=FZPU page_owner=on'
```

`slub_debug=FZPU` turns on sanity checks, red zones, poisoning and
allocation tracking in every slab cache. A use-after-free or overrun in
Lustre code is then reported when the object is freed or reused, with
its allocation and free stacks, rather than surfacing as unrelated
corruption.

ltvm refuses `root=`, `console=` and `fc_*`, which it sets itself.
`cluster create` takes the same flag for every node.

`create` is idempotent: it starts a stopped VM and no-ops on a running
one. Name VMs `co<N>-<role>` after the checkout they serve -- `co1-single`,
`co2-mds`, `co5-ec-dom`. Never a bare `testvm`: the number is what tells a
later session which tree the VM belongs to.

`deploy-lustre` is idempotent: it unmounts and unloads first, builds the
tree if the staging is stale, pushes it over ssh, and mounts with
`--mount`. The unload takes down every Lustre mount on the VM, however it
was made -- llmount.sh or a hand `mount -t lustre` -- and if Lustre will
not unload, the deploy stops before copying anything rather than leave
the old modules running; `cluster deploy` does the same on every node.
`--userspace-only` leaves a running Lustre alone and never rebuilds, but
edited test scripts, cfg files and other uncompiled sources are copied
into the staging before every deploy, so a script edit needs no rebuild;
it warns when compiled sources changed. `cluster deploy --userspace-only`
does the same on every node, from the staging an earlier `cluster deploy`
built. **The tree flag is never positional** -- `--lustre-tree <path>`
(`cluster deploy` also accepts `--build`).

Do not use host `make` or `fullbuild` to produce something for a VM. The
host kernel is not the VM kernel; `ltvm build lustre <target>
--lustre-tree <tree>` builds inside the target's build container, which is
what `deploy-lustre` invokes.

Configure flags belong to the command that builds. A deploy that rebuilds
uses the target's defaults, so a tree built with `build lustre
--configure=...` loses those flags at its next deploy: pass the same
`--configure` to `deploy-lustre` or `cluster deploy`.

ltvm's build path detects two staleness traps that a host build still
walks into, and both look like something else:

- `modpost: "ldiskfs_<sym>" [osd_ldiskfs.ko] undefined!` is stale ldiskfs
  staging. The tree's `sources` stamp depends on the series file but not
  on the individual patches under `ldiskfs/kernel_patches/`, so an edited
  patch never restages the generated `ldiskfs/*.c`. Fix it with
  `rm -f <tree>/ldiskfs/sources` and rebuild -- it is not a
  kernel/target mismatch, so do not bump the target or re-fetch
  artifacts over it.
- `configure: error: newly created file is older than distributed
  files!` is the host clock stepping backwards mid-build. Retry; any
  residual `make: Clock skew detected` warnings are harmless.

VMs are disposable. Destroying and recreating takes about 15-20 seconds
and is usually faster than unpicking a broken mount -- use
`ltvm llmount <vm> --cleanup` only when the logs or crash dumps on that VM
still matter.

## Update notices

ltvm checks weekly for a newer version and, when one is available,
prints one line to stderr on any command:

```
ltvm: update available (0.5.1.abc1234 -> def5678).  Run: sudo ltvm update
```

Surface it to the user and offer to run `sudo ltvm update`. Do not run
it mid-task on your own initiative: the update replaces `targets.yaml`,
the host-config templates and the kernel build scripts under a process
that has already imported the old code, so the rest of the task then
reads new files with old logic. Finish what you are doing, update
between tasks, and re-run.

The notice repeats at most once a day while an update is pending, so
seeing it once in a session is the expected behaviour -- not a signal
that a previous update attempt failed.

## Telemetry notice

On its first run ltvm prints a short notice to stderr saying it sends
an anonymous weekly check-in. That is expected output, not an error and
not something that failed -- do not try to fix it, and do not run
`ltvm telemetry off` on the user's behalf unless they ask. It prints
once per install.

If they ask what is collected, `ltvm telemetry show` prints the literal
payload: a random install ID, the ltvm version, a description of the
host (OS, arch, WSL, bucketed CPU/RAM), and counts of which commands
and targets were used. No paths, names or error text.

## Root

- **None, on a host set up for it.** Members of the `ltvm` group run
  `create`, `start`, `stop`, `destroy`, `doctor` and `cluster
  create/destroy` as themselves: QEMU runs as the user and joins the
  bridge through QEMU's bridge helper. `ltvm doctor` says whether the
  host is ready and, if not, what is missing. There, `sudo ltvm create`
  runs as the user who typed it, and `sudo ltvm start` as the VM's
  owner.
- **Otherwise** the single-VM lifecycle runs as the invoking user and
  prompts once for sudo, and `cluster create/destroy` need root.
- **Always root:** `install`, `update`, and any VM with a `passthrough`
  NIC.
- **Never:** `list`, `build *`, `target *`, `deploy-lustre`, `llmount`,
  `vm *`, `cluster deploy/exec/status/ssh`.

If `doctor` reports that this user is not in the `ltvm` group, that is
for the human to fix (`sudo usermod -aG ltvm <user>`, then a new login);
do not try to work around it.

**macOS** has the shared layout too, once `./ltvm install` has set it
up (`ltvm doctor` says whether it has): QEMU runs as the user and reaches
the network through socket_vmnet, and VM names go to `hosts.d/`, which
ltvm's dnsmasq is reloaded from by launchd. Without it, the lifecycle
commands elevate individual steps through sudo: `create` and `start`
launch QEMU as root, `stop` and `destroy` signal it as root, and each
`create` updates `/etc/hosts`. A VM started as root before the host was
set up still needs sudo to stop; once restarted it is the user's. You
cannot answer a sudo password prompt, so when one of these fails for want
of a password, ask the human to run that same command in their own
terminal -- where sudo can prompt and then remember the password for a
few minutes -- rather than working around it. Never edit sudoers.

## Talking to a VM

Every VM gets an entry in your `~/.ssh/config`, so plain `ssh` and
`scp` work by name:

```bash
ssh co1-single 'lctl dl'
scp co1-single:/tmp/out.txt .
ssh -o ConnectTimeout=10 -o ServerAliveInterval=5 \
    -o ServerAliveCountMax=3 co1-single uptime   # when it may be hung
```

Bound anything that might hang. The ssh options above give up on a VM
that stops answering within about 15 seconds, on any host. For a long
command, set a timeout on your own command runner, or use `timeout` --
which macOS lacks; Homebrew's coreutils installs it as `gtimeout`. A VM
that stops answering is a candidate for `ltvm vm console-log`, not for a
longer wait.

Do not use `ltvm vm console-log -f`: it streams until Ctrl-C, which is
useful to a human watching a boot and a way to hang yourself. Take
another `console-log` snapshot instead -- the log is a file, and re-reading
it costs nothing.

## Clusters

```bash
ltvm cluster create co2 mgs+mds:co2-mds:1 oss:co2-oss:3 client:co2-client
ltvm cluster deploy co2 --build <tree> --mount
ltvm cluster exec co2 oss 'lctl dl'        # every node with the role
ltvm cluster exec co2 co2-oss2 'lctl dl'   # one node by name
ltvm cluster status co2
ltvm cluster llumount co2                  # llmount to mount it again
ltvm cluster stop co2                      # start to bring it back
ltvm cluster destroy co2
```

`cluster create` and `cluster destroy` need `sudo` in front on a host
that is not set up for unprivileged VMs (see Root).

The cluster commands take the single-VM flags that carry over.
`cluster create --kernel <name>` boots every node on that kernel, and
`cluster deploy` then builds Lustre for it -- the way to run an interop
cluster on a kernel an older branch still supports:

```bash
ltvm cluster create co2 --kernel 5.14-rhel9.3 mgs+mds:co2-mds:1 oss:co2-oss:2
```

`--variant` and `--wait` work the same way. `cluster start` takes
`--wait`, and `cluster destroy` accepts `--force`/`--yes` though it
never prompts.

Create and start wait up to 120 seconds for a guest to answer ssh, and
stop sooner if its QEMU exits. A host busy enough to run out of that
("SSH not ready after 120s") wants `LTVM_SSH_TIMEOUT=<seconds>` exported,
not a retry. That error ends with the last thing ssh said: read it
before blaming a slow boot. An ssh that fails on the host itself, such
as a config file with bad owner or permissions, stops the wait at once
with "fails on this host, not in the guest"; `ltvm doctor` checks the
ssh client config.

Options can go anywhere after `cluster create <name>`, between the
target and the node specs included.

Every node's `/etc/hosts` names every member of its cluster, written by
`cluster create` and again by each `cluster deploy`, so the nodes find
each other without the host's DNS. A cluster created by an older ltvm
gets it at its next `cluster deploy`.

A cluster record whose VMs are all gone, because they were destroyed one
by one, is removed with the last of them, and `cluster create` replaces
one it finds.

`cluster exec <role>` fans out and exits non-zero if any node did.
`cluster ssh <role>` is interactive and lands on the first node.

Security suites need a cluster, not one VM: on a single node every
connection is over `0@lo`, which Lustre never gives a security flavor
(LU-13343), so sanity-krb5 and sanity-sec's SSK runs (`SHARED_KEY=true`)
cannot pass there.  SSK needs nothing more.  sanity-krb5 needs a KDC and
keytabs, which one command sets up (KDC on the first MDS, realm
LTVM.TEST; rerunning keeps the keys, recreating the cluster loses them):

```bash
ltvm cluster krb5 co2
```

## Running tests

For a whole suite, or a long `--only` list, use `ltvm suite`.  It starts
the suite the way CI does -- unmounted, `CLEANUP_DM_DEV=false`, `auster
-k -v -r -H`, detached under its own session keyring -- each of which
was a wrong result when left out (the paragraphs below say why):

```bash
id=$(ltvm suite run co1-single sanity)          # returns at once
ltvm suite run co1-single sanity --only '39r 42a'
ltvm suite run co2 sanity-sec --env SHARED_KEY=true --wait  # cluster
ltvm suite status co1-single [$id]   # running / done rc=N / died
ltvm suite collect co1-single $id    # logs to ./ltvm-suite/..., summary
```

A cluster runs it from its first client.  `--wait` collects at the end
and exits non-zero on any failure; `status` flags a node that rebooted
during the run (a crash: look in `/var/crash`).  One suite at a time per
VM or cluster; `run` refuses a second.

For a single subtest by hand:

```bash
ssh co1-single 'sudo -E ONLY=42a bash /usr/lib64/lustre/tests/sanity.sh'
ssh co1-single 'sudo -E auster sanity --only 42a'
```

auster's `-s` is `SLOW=yes`, not "suite": CI runs `SLOW=no`, so leave it off
unless the slow tests are the point.

Mount first. Only llmount.sh formats the targets, so on a VM that has
never been mounted a bare `sanity.sh` fails setup with "has not been
formatted with mkfs.lustre". `deploy-lustre --mount` or `ltvm llmount
<vm>` does it (`ltvm cluster llmount <cluster>` for a cluster), and
`deploy-lustre` without `--mount` says so when the targets are blank.
`REFORMAT=yes` on the suite, or `auster -r`, formats as well.

For a whole suite, start unmounted, as CI does, and let auster format
and mount:

```bash
ssh co1-single 'cd /usr/lib64/lustre/tests && bash llmountcleanup.sh &&
    auster -k -r -H sanity'
```

A suite that finds Lustre already mounted -- by `deploy-lustre --mount`,
`llmount`, or an earlier suite in another shell -- takes the raw disks
(`/dev/vdb`...) as each target's device instead of the dm-flakey mapper
it is mounted through, so tests that read the device behind Lustre's back
see stale data: sanity 39r fails every time.  `-H` makes `--only` honour
the suite's `ALWAYS_EXCEPT` list; without it, a test named in `--only`
runs even when CI skips it as known-broken.

A suite left running after the ssh that started it has gone needs a
session keyring of its own:

```bash
ssh co1-single 'cd /usr/lib64/lustre/tests &&
    setsid nohup keyctl session - ./auster -k -r -H sanity-sec \
    > /tmp/sec.out 2>&1 < /dev/null &'
```

Without `keyctl session`, the job inherits the ssh login's keyring,
which is revoked at logout, and every encryption test then runs
without encryption: mount reports "could not insert dummy encryption
key into session keyring" and sanity-sec fails a dozen tests.

Auster logs land in `/tmp/test_logs/YYYY-MM-DD/HHMMSS/` inside the VM.
Redirect long runs to a file and grep the file afterwards rather than
piping a slow command through `grep`.

The VM's shape is in `cfg/local.sh`: deploy appends blocks setting
`MDSCOUNT`, `OSTCOUNT`, `MDSDEV*`/`OSTDEV*`, the sizes and `FSTYPE` (and
on a cluster which node holds what) after the stock `${VAR:-default}`
lines. The two counts still take the environment's value, as on a stock
tree, so `MDSCOUNT=1` or `OSTCOUNT=1` runs fewer targets than the VM has
(never more: there are no devices past its disks). Reformat with the
same count:

```bash
ssh co1-single 'cd /usr/lib64/lustre/tests && MDSCOUNT=1 bash llmount.sh'
ssh co1-single 'MDSCOUNT=1 ONLY=42a bash /usr/lib64/lustre/tests/sanity.sh'
```

The rest -- devices, sizes, `FSTYPE` -- are plain assignments and win
over the environment. A VM deployed by an older ltvm takes the counts
from the environment only after its next deploy.

On a cluster the servers run parts of a suite (`rpc.sh`) from their own
`local.sh`, without the client's environment. There, point `CONFIG` at a
file that sources local.sh and then sets the count, on every node
(`cluster exec`):

```bash
printf '. /usr/lib64/lustre/tests/cfg/local.sh\nMDSCOUNT=1\n' > /root/one-mdt.sh
CONFIG=/root/one-mdt.sh ONLY=42a bash /usr/lib64/lustre/tests/sanity.sh
```

## Crashes and hangs

VMs boot with `crashkernel=512M`, and the image ships kexec-tools, crash
and drgn with the kernel and initramfs pre-baked. After a panic kdump
writes a vmcore to `/var/crash/<ip>-<date>/` and reboots, about 15
seconds. `ltvm vm nmi` triggers one from the host; `echo c >
/proc/sysrq-trigger` from inside the VM does the same.

```bash
ltvm vm console-log co1-single
ltvm vm snapshot co1-single before-test
ltvm vm restore co1-single [tag]
ltvm vm exec co1-single lctl dl               # run a command; exit = its rc
ltvm vm nmi co1-single                        # NMI -> panic + kdump
ltvm vm crash-collect co1-single --mod-dir <build>
ltvm vm crash-collect co1-single --trigger --mod-dir <build>
```

`crash-collect` resolves the matching vmlinux itself and verifies its ELF
build-id against the running kernel, exiting non-zero when it cannot find
one. Pass the `.ko` directory of the build that **crashed** -- a
`git checkout` does not rebuild, so the modules on disk are whatever was
built last. For reading the dump afterwards, see the crash triage skill.

## Compatibility gate

Before any Lustre-involving build, ltvm checks the tree against the
target's `lustre.mode`:

```bash
ltvm target validate rocky9 --lustre-tree <tree>   # 0 ok/warn, 1 refused, 2 error
ltvm build all rocky9 --lustre-tree <tree> --force-compat
```

The gate reads `lustre/kernel_patches/which_patch` for `server_ldiskfs`
and `lustre/ChangeLog` for `server_zfs`, and refuses combinations Lustre
upstream does not declare supported. `--force-compat` silences refusals,
not hard errors, and is for known work-in-progress branches only. It is
accepted by the `build` commands, `target build`, `target validate`,
`deploy-lustre`, `cluster deploy` and `make-install`/`make-reinstall`;
`target publish` runs no gate and does not take it.

## Sharing what was built

```bash
ltvm target publish rocky9                # bundle + upload a GitHub release
ltvm target publish rocky9 --no-upload    # build the tarballs only
```

`ltvm target fetch` finds the latest release for a target and downloads
it; re-running when the local tree is current finishes in under a second.

## Inside a VM rather than on the host

`deploy-lustre` pushes Lustre into a VM from the host. The `make-*`
commands are the other direction -- ltvm running inside a machine it
built, installing onto that machine's own root:

```bash
ltvm make-install --lustre-tree <tree>
ltvm make-uninstall
```

Both refuse unless the machine is one ltvm built and the working
directory is a Lustre tree. On EL8/EL9 images run them with
`python3.11`, and install podman first.

## For agent controllers

Export `LTVM_OWNER_ID=<durable-session-id>` before creating VMs so the
owner metadata identifies the session rather than a pid; read it back with
`ltvm list --json`. Its `clusters` list gives each cluster's owner and
member VMs, and each VM row names its `cluster`; destroy a cluster with
`ltvm cluster destroy`, not node by node.

## Sharing VMs with other sessions

Other sessions on the host see the same VMs, and one deploying over
another's test run wrecks both.  Claim a VM before using it:

```bash
ltvm claim co1-single --tree ~/src/lustre-release   # or: ltvm claim (list)
ltvm release co1-single                              # when done with it
```

`deploy-lustre` claims an unclaimed VM for you.  `deploy-lustre`,
`llmount`, `start`/`stop`/`destroy`, `vm snapshot/restore/nmi/crash-collect/set`
and the cluster commands refuse a VM another live session has claimed, and
`ltvm list` shows `claimed=<owner>`.  When refused, use another VM or ask
the user -- do not `ltvm release --force` someone else's claim on your own.
Plain `ssh` is not gated, so claim before ssh-only work too.

A Claude Code session is recognised by itself (`claude:<session-id>`), and
its claims end when it exits.  Another agent exports `LTVM_OWNER_ID` and
`LTVM_OWNER_PID` (the agent's own long-lived process: each command it runs
is a fresh shell), or passes `--owner`/`--pid`; `--ttl 4h` ends a claim
after a while regardless.  A human's claims (`user:<name>`) last until
released, and a hand deploy never claims.

## Flags that apply everywhere

`--json` for machine-readable output, `--verbose`, `--arch <arch>` to
override the target's configured architecture, `--kernel <name>` on the
commands that act on one kernel, and `--force-compat` on the build,
validate and deploy commands.

`--json` is accepted everywhere but only some commands have anything
structured to say.  The ones worth parsing: `list`, `build status`,
`target show/validate/fetch/delete`, `create`, `deploy-lustre`, and
`cluster status/list/exec`.  The other `cluster` actions stream
human progress under `--json` too, and `cluster ssh` execs an
interactive session, so don't parse those.

## Where the detail lives

This skill covers using ltvm. For target configuration, package lists,
adding a new target OS, variants, artifact layout, the release manifest
schema and the export formats, read `CLAUDE.md` in the lustre-test-vms-v2
checkout and the operator notes under `docs/`.
