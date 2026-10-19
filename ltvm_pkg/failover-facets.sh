#!/bin/bash
# Move Lustre facets between the nodes of a failover pair, run as root
# on the first client by `ltvm cluster failover` and `ltvm cluster
# failback`.  Usage: failover-facets.sh LUSTRE_DIR ACTION FACET...
#
#   active  print "FACET HOST STATE": the active node of each facet by
#           the framework's record, and mounted/unmounted/unreachable
#   stop    unmount each facet on its active node
#   move    make the other node of the pair active, and mount there
#   start   mount each facet on its active node
#   drop    on the passive node, drop what a lost facet left: a mount,
#           an imported zpool, a dm-flakey device
#
# The script uses the test framework's own functions.  Thus the record
# of the active node ($TMP/<facet>active) stays correct for a later
# suite, and a mount on the partner goes through the same dm-flakey
# device or zpool import that `fail <facet>` uses.  No `set -u`:
# test-framework.sh reads unset variables.

LUSTRE=$1
action=$2
shift 2

cd "$LUSTRE/tests" || exit 1
. ./test-framework.sh
# As rpc.sh does.  Without RPC_MODE, init_test_env deletes the active
# records, and it asks each facet's node for its version: the node
# that just failed, too.
RPC_MODE=true init_test_env > /dev/null
trap - ERR

start_facet() {
	local facet=$1

	case $facet in
	mgs) start mgs "$(mgsdevname)" $MGS_MOUNT_OPTS ;;
	mds*) start "$facet" "$(mdsdevname "${facet#mds}")" $MDS_MOUNT_OPTS ;;
	ost*) start "$facet" "$(ostdevname "${facet#ost}")" $OST_MOUNT_OPTS ;;
	*) echo "unknown facet $facet"; return 1 ;;
	esac
}

# mounted, unmounted or unreachable: what HOST says of FACET.
mount_state() {
	local facet=$1 host=$2 rc

	do_node --quiet "$host" "grep -q ' $(facet_mntpt "$facet") ' /proc/mounts"
	rc=$?
	case $rc in
	0) echo mounted ;;
	255) echo unreachable ;;
	*) echo unmounted ;;
	esac
}

# Start the facets on their active nodes: the MGS first, since the other
# targets register with it, then the rest in parallel.  On a failure,
# run the function named by $2 with the facet.
start_all() {
	local on_fail=$1 facet p rc=0
	local pids=()

	shift
	for facet; do
		if [[ $facet == mgs ]]; then
			start_facet mgs || { rc=1; $on_fail mgs; }
			continue
		fi
		start_facet "$facet" &
		pids+=("$!:$facet")
	done
	for p in "${pids[@]}"; do
		wait "${p%%:*}" && continue
		rc=1
		$on_fail "${p#*:}"
	done
	return $rc
}

declare -A old_host

# A failed move: when the old node still holds the facet, or cannot
# say, the record goes back to it.  Otherwise the record keeps the new
# node, and a `start` there mounts it.
undo_move() {
	local facet=$1 host=${old_host[$1]}

	if [[ $(mount_state "$facet" "$host") == unmounted ]]; then
		echo "mount of $facet failed; it is mounted nowhere"
	else
		echo "mount of $facet failed; it stays on $host"
		change_active "$facet"
	fi
}

report_failure() {
	echo "mount of $1 failed"
}

case $action in
active)
	for facet; do
		host=$(facet_active_host "$facet")
		echo "$facet $host $(mount_state "$facet" "$host")"
	done
	;;
stop)
	rc=0
	for facet; do
		stop "$facet" || rc=1
	done
	exit $rc
	;;
move)
	for facet; do
		old_host[$facet]=$(facet_active_host "$facet")
		change_active "$facet"
	done
	start_all undo_move "$@"
	;;
start)
	start_all report_failure "$@"
	;;
drop)
	# A node that froze or lost its link still holds what it had.  Its
	# mount would find the pool imported, or the device busy.  The
	# framework's own names, since a pool takes the target's name
	# (lustre-mdt1), not the facet's.
	rc=0
	for facet; do
		host=$(facet_passive_host "$facet")
		[[ -n $host ]] || continue
		mnt=$(facet_mntpt "$facet")
		cmd="grep -q ' $mnt ' /proc/mounts && umount -f $mnt"
		if [[ $(facet_fstype "$facet") == zfs ]]; then
			cmd+="; zpool export -f $(zpool_name "$facet") 2>/dev/null"
		fi
		cmd+="; dmsetup remove $(dm_facet_devname "$facet") 2>/dev/null; true"
		do_node "$host" "$cmd" || rc=1
	done
	exit $rc
	;;
*)
	echo "usage: failover-facets.sh LUSTRE_DIR active|stop|move|start|drop FACET..."
	exit 2
	;;
esac
