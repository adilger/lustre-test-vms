#!/usr/bin/env bash
# Disable slow or unnecessary services and kernel modules.
#
# Pass additional service names to mask as arguments, e.g.:
#   setup-services.sh dnf-makecache.timer
set -euo pipefail

# Always mask these in every VM image
MASK_ALWAYS=(
    systemd-hwdb-update
    firewalld
)

# Caller can pass extra services to mask (distro-specific timers, etc.)
MASK_EXTRA=("$@")

systemctl mask "${MASK_ALWAYS[@]}" "${MASK_EXTRA[@]}" 2>/dev/null || true

# Blacklist DRM -- no display hardware in microvm
mkdir -p /etc/modprobe.d
cat > /etc/modprobe.d/no-drm.conf <<'EOF'
blacklist drm
EOF

# sssd-kcm's drop-in makes KCM: every user's default ccache, which needs
# its socket running; keep the distro's keyring default.
if [[ -f /etc/krb5.conf.d/kcm_default_ccache ]]; then
	sed -i 's/^[[:space:]]*default_ccache_name/#&/' \
		/etc/krb5.conf.d/kcm_default_ccache
fi
