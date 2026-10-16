ARG BASE_IMAGE=debian:13
FROM ${BASE_IMAGE}

# Debian 13 (trixie) build container for kernel and Lustre builds.
#
# KERNEL_DEB_SOURCE comes from targets.yaml (kernel_deb_source field)
# via --build-arg so the apt-installed kernel source package matches
# the kernel ltvm builds against.  It is held: apt upgrades every
# package built from one source together, so a later install that
# brings in a newer linux-libc-dev would take the kernel source along.
ARG KERNEL_DEB_SOURCE=linux-source-6.12

ENV DEBIAN_FRONTEND=noninteractive

# Install build packages from the same shared common/packages-dev.txt
# the rocky containers use.  RHEL package names get translated to
# Debian via package-map.txt; "-" means skip.
COPY common/packages-dev.txt   /tmp/packages-dev.txt
COPY debian13/package-map.txt /tmp/package-map.txt
RUN apt-get update \
    && cat /tmp/packages-dev.txt \
        | grep -v '^\s*#' | grep -v '^\s*$' \
        | sort -u \
        | awk 'NR==FNR { \
                 if ($0 ~ /^[[:space:]]*#/ || $0 ~ /^[[:space:]]*$/) next; \
                 rhel=$1; \
                 sub(/^[^[:space:]]+[[:space:]]+/, ""); \
                 map[rhel]=$0; \
                 next \
               } \
               { if ($1 in map) { if (map[$1] != "-") print map[$1] } \
                 else print $1 }' \
            /tmp/package-map.txt - \
        | tr ' ' '\n' \
        | sort -u \
        | xargs apt-get install -y --no-install-recommends \
    && apt-get install -y --no-install-recommends "${KERNEL_DEB_SOURCE}" \
    && apt-mark hold "${KERNEL_DEB_SOURCE%%=*}" \
    && rm -f /tmp/packages-dev.txt /tmp/package-map.txt \
    && rm -rf /var/lib/apt/lists/*

# Debian's own kernel config, from the linux-image packages of the same
# upload as the source, for both arches: the container is the host's
# arch whichever arch the kernel is built for.  The kernel build uses
# $KERNEL_DEB_CONFIG_DIR/config-<deb arch> instead of defconfig.
# Debian's x86_64 config compresses the bzImage with zstd.
RUN ver=$(dpkg-query -W -f '${Version}' "${KERNEL_DEB_SOURCE%%=*}") \
    && host=$(dpkg --print-architecture) \
    && for a in amd64 arm64; do \
           [ "$a" = "$host" ] || dpkg --add-architecture "$a"; \
       done \
    && apt-get update \
    && apt-get install -y --no-install-recommends zstd \
    && mkdir -p /usr/src/debian-config /tmp/kimg \
    && cd /tmp/kimg \
    && for a in amd64 arm64; do \
           apt-get download "linux-image-${ver%-*}+deb13-${a}:${a}=${ver}" \
           && dpkg-deb -x linux-image-*_"${a}".deb "x-${a}" \
           && cp x-"${a}"/boot/config-* "/usr/src/debian-config/config-${a}" \
           || exit 1; \
       done \
    && cd / && rm -rf /tmp/kimg \
    && for a in amd64 arm64; do \
           [ "$a" = "$host" ] || dpkg --remove-architecture "$a"; \
       done \
    && rm -rf /var/lib/apt/lists/*
ENV KERNEL_DEB_CONFIG_DIR=/usr/src/debian-config

# Whamcloud-patched e2fsprogs (needed for Lustre userspace tools).
# Pinned via build-e2fsprogs.sh's DEFAULT_E2FS_TAG.
COPY common/build-e2fsprogs.sh /tmp/build-e2fsprogs.sh
RUN apt-get update && apt-get install -y git ca-certificates \
    && bash /tmp/build-e2fsprogs.sh \
    && rm -f /tmp/build-e2fsprogs.sh \
    && rm -rf /var/lib/apt/lists/*

# Cross-compilers for both arches.  Debian names gcc for the target's
# GNU triple with hyphens (gcc-x86-64-linux-gnu), unlike RHEL (gcc-
# x86_64-linux-gnu).  Both directions best-effort because the exact
# set installed at container build time depends on the building host's
# arch; the inner build script falls back to a runtime install if the
# cross toolchain is missing.
RUN apt-get update \
    && (apt-get install -y --no-install-recommends \
          gcc-aarch64-linux-gnu g++-aarch64-linux-gnu 2>/dev/null || true) \
    && (apt-get install -y --no-install-recommends \
          gcc-x86-64-linux-gnu g++-x86-64-linux-gnu 2>/dev/null || true) \
    && rm -rf /var/lib/apt/lists/*

ENV PATH="/usr/lib/ccache:${PATH}"
ENV CCACHE_DIR="/ccache"

WORKDIR /build
ENTRYPOINT ["/bin/bash"]
