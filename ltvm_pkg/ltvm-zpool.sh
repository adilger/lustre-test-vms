#!/bin/bash
# The test framework's $ZPOOL on an ltvm cluster, installed on every
# node by `ltvm cluster deploy`.
#
# A failover partner attaches the disks of its primary.  mkfs.lustre
# partitions a disk when it makes the pool, after the partner booted,
# and the partner's kernel does not see the new partitions.  Its
# `zpool import` then finds no labels.  Thus reread the partition table
# of a data disk that shows no partitions, before an import.
#
# A reread removes the partition nodes for a moment, and the framework
# imports the pools of one node in parallel.  Thus the lock, the reread
# of only the disks that need it, and the wait for udev to make the
# nodes again.
if [[ ${1:-} == import ]]; then
	(
		flock 9
		for d in /sys/block/vd[b-z]; do
			[[ -e $d ]] || continue
			disk=${d##*/}
			compgen -G "$d/${disk}[0-9]*" > /dev/null && continue
			blockdev --rereadpt "/dev/$disk" 2> /dev/null
		done
		udevadm settle
	) 9> /run/ltvm-zpool.lock
fi
exec zpool "$@"
