#!/usr/bin/env bash
# openSUSE-specific image setup: what the shared setup scripts do with
# RHEL or Debian paths, done with SUSE's.
#
# Runs in place of common/setup-kdump.sh, whose RHEL branch would
# overwrite SUSE's /etc/sysconfig/kdump with variables SUSE's kdump
# does not read.
set -euo pipefail

# systemd-sysvcompat's rc-local.service runs /etc/init.d/boot.local,
# not /etc/rc.local.
mkdir -p /etc/init.d
ln -sf /etc/rc.d/rc.local /etc/init.d/boot.local

# setup-ssh.sh empties root's password and sshd permits it, but
# SUSE's pam_unix, unlike RHEL's, refuses an empty password without
# nullok, and ltvm logs in through that.
pam-config -a --unix-nullok

# SLES refuses to load a module SUSE did not mark supported, unless
# told otherwise, and neither the kernel ltvm builds nor Lustre carries
# that mark.  This is the setting a SLES site running Lustre uses.
mkdir -p /etc/modprobe.d
echo 'allow_unsupported_modules 1' > /etc/modprobe.d/10-unsupported-modules.conf

# kdump: dumps under /var/crash, as on the other targets.  The values
# match common/setup-kdump.sh's RHEL ones: lzo-compressed, dump level
# 1, the same capture kernel arguments.
mkdir -p /var/crash /etc/sysctl.d
echo 'kernel.unknown_nmi_panic = 1' > /etc/sysctl.d/90-ltvm-nmi.conf
sed -i \
	-e 's|^KDUMP_SAVEDIR=.*|KDUMP_SAVEDIR="/var/crash"|' \
	-e 's|^KDUMP_DUMPLEVEL=.*|KDUMP_DUMPLEVEL=1|' \
	-e 's|^KDUMP_DUMPFORMAT=.*|KDUMP_DUMPFORMAT="lzo"|' \
	-e 's|^KDUMP_COMMANDLINE_APPEND=.*|KDUMP_COMMANDLINE_APPEND="irqpoll nr_cpus=1 reset_devices cgroup_disable=memory mce=off numa=off udev.children-max=2 panic=10 rootflags=nofail acpi_no_memhotplug transparent_hugepage=never nokaslr"|' \
	/etc/sysconfig/kdump
systemctl enable kdump

# Leap's bash reads /etc/bash.bashrc, not RHEL's /etc/bashrc
cat /etc/profile.d/lustre-tests-path.sh >> /etc/bash.bashrc

# openmpi4 lives off PATH, where cfg/local.sh's `which mpirun` cannot
# find it, and its MCA defaults are a file in /etc, not the directory
# build-tools.sh looks for: the same settings, for the same reason (no
# RDMA, UCX's memory).
echo 'export PATH="$PATH:/usr/lib64/mpi/gcc/openmpi4/bin"' >> /etc/bash.bashrc
cat >> /etc/openmpi-mca-params.conf <<EOF
pml = ob1
btl = self,vader,tcp
osc = ^ucx
EOF
