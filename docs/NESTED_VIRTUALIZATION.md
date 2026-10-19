# Enabling Nested Virtualization

ltvm runs QEMU/KVM virtual machines. If your host is itself a VM
(cloud instance, Hyper-V, VMware, etc.), you need **nested
virtualization** enabled so the inner VMs can use hardware
acceleration.

Without KVM, QEMU falls back to software emulation which is
~100x slower and not supported by ltvm.

## Quick Diagnosis

```bash
ls /dev/kvm          # should exist
lscpu | grep -i virt # should show VT-x or AMD-V
```

If `/dev/kvm` is missing, follow the instructions for your
platform below.

---

## Linux on Bare Metal

KVM should work out of the box. If `/dev/kvm` is missing:

```bash
# Check BIOS/UEFI settings
grep -E 'vmx|svm' /proc/cpuinfo

# Load the KVM module
sudo modprobe kvm_intel   # Intel
sudo modprobe kvm_amd     # AMD

# If modprobe fails, enable VT-x/AMD-V in BIOS
```

## WSL2 (Windows)

From an **elevated PowerShell** on the Windows host:

```powershell
# Find your WSL VM
wsl -l -v

# Enable nested virt (requires Hyper-V)
Set-VMProcessor -VMName "WSL" -ExposeVirtualizationExtensions $true

# Restart WSL
wsl --shutdown
```

Then verify inside WSL: `ls /dev/kvm`

**Note:** WSL2 nested virt requires Windows 11 or Windows 10
21H2+ with Hyper-V enabled. Older Windows versions do not
support this.

## VMware (ESXi / Workstation / Fusion)

### ESXi (vSphere)

Edit the VM settings:
1. VM > Edit Settings > CPU
2. Check "Expose hardware-assisted virtualization to the guest OS"
3. Power cycle the VM (not just reboot)

Or via CLI:
```
vhv.enable = "TRUE"
```

### VMware Workstation / Fusion

VM Settings > Processors > "Virtualize Intel VT-x/EPT or AMD-V/RVI"

## KVM (libvirt / virsh)

Nested virt must be enabled in the host's KVM module:

```bash
# Check current setting on the HOST
cat /sys/module/kvm_intel/parameters/nested    # Intel
cat /sys/module/kvm_amd/parameters/nested      # AMD

# Enable (temporary)
sudo modprobe -r kvm_intel
sudo modprobe kvm_intel nested=1

# Enable (permanent)
echo 'options kvm_intel nested=1' | sudo tee /etc/modprobe.d/kvm-nested.conf
```

Then ensure the guest VM's XML has:
```xml
<cpu mode='host-passthrough'/>
```

Or via virsh:
```bash
virsh edit <vm-name>
# Change <cpu> to: <cpu mode='host-passthrough'/>
# Then: virsh destroy <vm> && virsh start <vm>
```

## Hyper-V (Azure / on-premises)

### Azure VMs

Use a **v5 or newer** VM size that supports nested virt:
- Dv5, Dsv5, Ev5, etc.
- **Not supported:** Dv2, Dv3, Dv4, Bv1, Fsv2

Check: `az vm list-skus --location <region> --query "[?capabilities[?name=='HyperVGenerations' && value=='V2']]"`

Nested virt is automatic on supported sizes -- no config needed.

### On-premises Hyper-V

From **elevated PowerShell** on the Hyper-V host:

```powershell
Set-VMProcessor -VMName <name> -ExposeVirtualizationExtensions $true
# Power cycle the VM
```

## AWS EC2

Use a **metal** instance type (e.g. `m5.metal`, `c5.metal`).
Regular EC2 instances do not support nested KVM.

Alternatively, `.metal` instances have direct hardware access
and KVM works natively (not technically "nested").

## Google Cloud (GCE)

Nested virt is supported on Haswell+ CPUs. Enable it when
creating the VM:

```bash
gcloud compute instances create <name> \
    --enable-nested-virtualization \
    --min-cpu-platform="Intel Haswell"
```

Or for an existing instance (must be stopped):
```bash
gcloud compute instances update <name> \
    --enable-nested-virtualization
```

## macOS (Apple Silicon / Intel)

### Apple Silicon -- aarch64

ltvm runs on macOS itself, with HVF.  To run it inside a Linux VM on
the Mac instead -- so an agent driving ltvm is confined to that VM --
the Linux VM needs nested virtualization, and that needs all of:

- an **M3 or later** chip (M1 and M2 have no nested virtualization);
- **macOS 15** or later;
- **UTM 4.6** or later, with the VM on the **Apple Virtualization**
  backend (not QEMU) and its nested virtualization option turned on;
- an **aarch64** Linux guest, running **aarch64** ltvm targets.  An
  x86_64 target can only be emulated (TCG) on Apple Silicon.

Check from inside the Linux VM:

```bash
sudo dmesg | grep -e 'started at EL2' -e 'kvm \['   # "All CPU(s) started at EL2"
ls -l /dev/kvm                                      # present
sudo usermod -aG kvm $USER                          # if you are not in the kvm group
```

`ltvm doctor` then reports KVM as available, and `ltvm create` uses
it: `accel=kvm` and `-cpu host`.  `ps -o args -C qemu-system-aarch64`
shows what a running VM got.

**ltvm turns off the GIC ITS here.**  On an Apple CPU (`CPU
implementer : 0x61` in `/proc/cpuinfo`), ltvm launches KVM guests with
`virt,...,its=off`.  With the ITS, virtio-pci MSIs lose wakeups under
this nesting: the guest boots past init, then sits in `cpu_do_idle`
for good while its vCPU thread shows 100% CPU.  Without the ITS,
virtio falls back to wired INTx interrupts, which work.  Trapping WFI
in the L1 KVM does not help, so it is the MSI delivery itself.
`LTVM_FORCE_TCG=1` still forces emulation, if you ever need it.

Measured on an M3 Pro, macOS 26.5, UTM 4.7.5, Ubuntu 26.04 (kernel
7.0) as L1, QEMU 10.2.1, rocky9 aarch64 guest, 2 vCPUs:

| | TCG | nested KVM |
|---|---|---|
| boot to root prompt | 23-25 s | 9 s |
| `ltvm create` to ssh | -- | 20 s |
| `dd \| sha256sum` of 512 MiB | 4.6 s | 0.7 s |
| 2000 fork+exec | 20 s | 0.5 s |
| `xz -3` of 64 MiB | 62-70 s | 20 s (same as the L1 itself) |
| `find /usr` | 7.5-12 s | 2.1 s |
| 256 MiB direct read | 1.9-2.9 s | 1.0 s |

CPU-bound work runs at the L1's own speed; what nesting costs is
exits, so interrupt-bound I/O gains least (4 KiB direct reads are
about even).

A short Lustre run on the same setup -- the rocky9 rhel9.7 bundle,
3 GB guest, 1 MDT + 2 OSTs of 2 GB, L1 with 5 vCPUs and 8 GB:

| step | time |
|---|---|
| `ltvm create` to ssh | 20 s |
| `deploy-lustre --mount` | 31 s |
| sanity 0a-24b (34 tests) | 5 s, all pass |
| createmany / `ls -l` / unlinkmany, 5000 files | 2 s / 1 s / 1 s |
| 1 GiB write (striped, fsync) / read | 9 s / 2 s |
| `llumount` | 37 s |

#### Compared with ltvm on macOS directly

The same image and kernel, 2 vCPUs, nested KVM in the UTM VM against
HVF on the Mac itself:

| | nested KVM | HVF | |
|---|---|---|---|
| boot to root prompt | 8.3-8.8 s | 11 s | |
| openssl sha256, 1 / 2 processes | 2.69 / 2.57 GB/s | 2.90 / 2.60 GB/s | ~1x |
| pipe between processes, one vCPU | 0.31 s | 0.29 s | ~1x |
| pipe between processes, two vCPUs | 0.47 s | 0.20 s | 2.3x |
| 2000 fork+exec | 0.5-0.8 s | 0.4-0.5 s | 1.1-2x |
| `xz -3` of 64 MiB | 20-21 s | 15-16 s | 1.3x |
| 256 MiB direct write / read | 1-1.9 / 1-1.7 s | 0.7-0.8 / 0.5 s | ~2x |
| 4 KiB direct reads x 20000 | 4.8-5.1 s | 2.5-2.8 s | 1.9x |

Where the difference comes from:

- **Compute and syscalls cost nothing extra.**  Code that does not
  leave the guest runs at native speed at any nesting depth.
- **Exits cost about 2x.**  A cross-vCPU wakeup (an IPI and a WFI), an
  interrupt or an I/O request exits from the guest to Apple's
  hypervisor, which hands it to the L1's KVM.  The L1's KVM runs in
  nVHE mode (Apple's virtual EL2 offers no VHE), whose world switch is
  the expensive kind, and `its=off` adds the exits that wired
  interrupts take to deassert.  ltvm can change neither.
- **Memory-heavy work loses ~22% in the L1 itself, not in the
  nesting.**  `xz` takes 16.2 s on the Mac, 15.9 s in an HVF guest,
  19.8 s in the UTM VM and 20 s in a guest nested in it.  Whether the
  UTM VM's nested virtualization option causes it has not been tested.

In this layout builds are not nested: `ltvm build` runs podman
directly in the L1, against a Lustre tree on its own disk, and only
the test VMs nest.  On the Mac, podman runs in a VM of its own
(`podman machine`) and reaches the tree over a shared folder.

An earlier attempt on this setup concluded TCG was faster, from boot
time alone (~60 s under KVM, with `nohz=off` and the ITS off).  That
no longer reproduces: `nohz=off` makes no measurable difference, and
the boot is 9 s.

Parallels Desktop and VMware Fusion have not been tested with ltvm.

If the Mac cannot nest, run ltvm on macOS directly, or on a remote
x86_64 or aarch64 Linux machine.

### Intel Mac

Use VMware Fusion, Parallels, or UTM with an x86_64 Linux guest.
Enable nested virtualization in the VM settings (same as VMware
Workstation instructions above).

## Troubleshooting

### `/dev/kvm` exists but permission denied

```bash
sudo chmod 666 /dev/kvm
# Or add your user to the kvm group:
sudo usermod -aG kvm $USER
# Then log out and back in
```

### Module loads but `/dev/kvm` not created

```bash
# Check for errors
dmesg | grep -i kvm
# Common: BIOS has VT-x disabled even though CPU supports it
```

### Everything looks right but VMs won't start

```bash
# Test KVM directly
/opt/qemu/bin/qemu-system-x86_64 -machine accel=kvm -nographic -no-reboot \
    -kernel /boot/vmlinuz-$(uname -r) -append "console=ttyS0 panic=1" &
# Should boot briefly and panic (no rootfs) -- proves KVM works
# Kill it: kill %1
```

If this fails with "Could not access KVM kernel module", the
issue is in the hypervisor configuration, not the guest.
