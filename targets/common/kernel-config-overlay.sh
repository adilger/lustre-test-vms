# Sourced by every kernel-build-inner*.sh.
#
# The shared config overlay (targets/common/kernel-config.overlay, less
# what the target forces or whites out) is best effort: it goes in
# ahead of the forced fragment and is never forced itself, so a symbol
# this kernel cannot take (an unmet dependency, a select, a symbol the
# kernel does not have) is reported after olddefconfig rather than
# fought over.  The report lands in /output/config-overlay-missed,
# which kernel_build records in the kernel's meta.json.

KCONFIG_OVERLAY=/input/staging/config.overlay

overlay_apply() {
	local line key
	[[ -s $KCONFIG_OVERLAY ]] || return 0
	echo "--- Applying the shared config overlay..."
	while IFS= read -r line; do
		[[ $line == CONFIG_*=* ]] || continue
		key=${line%%=*}
		sed -i -e "/^${key}=/d" -e "/^# ${key} is not set\$/d" .config
		echo "$line" >> .config
	done < "$KCONFIG_OVERLAY"
}

# Run after olddefconfig.  "=n" is satisfied by a symbol that is unset
# or absent, which is how kconfig writes n.
overlay_report() {
	local line key want got total=0
	: > /output/config-overlay-missed
	[[ -s $KCONFIG_OVERLAY ]] || return 0
	while IFS= read -r line; do
		[[ $line == CONFIG_*=* ]] || continue
		key=${line%%=*}
		want=${line#*=}
		got=$(sed -n "s/^${key}=//p" .config)
		total=$((total + 1))
		[[ $want == "${got:-n}" ]] && continue
		echo "$line (got ${got:-n})" >> /output/config-overlay-missed
	done < "$KCONFIG_OVERLAY"
	echo "--- Shared config overlay: $((total -
		$(wc -l < /output/config-overlay-missed))) of $total taken"
	sed 's/^/    not taken: /' /output/config-overlay-missed
}
