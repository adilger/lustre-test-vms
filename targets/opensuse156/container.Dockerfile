ARG BASE_IMAGE=registry.opensuse.org/opensuse/leap:15.6
FROM ${BASE_IMAGE}

# openSUSE Leap 15.6 build container for SLES 15 kernel and Lustre
# client builds.  GCC 7 is what SUSE builds the SLE 15 kernels with
# (rpm/config.sh in SUSE/kernel-source names no other).

# Install build packages from the shared common/packages-dev.txt,
# translating RHEL names through package-map.txt ("-" means skip).
# --no-recommends: crash and friends recommend a kernel-default, which
# would put a distro kernel in a container that builds its own.
COPY common/packages-dev.txt      /tmp/packages-dev.txt
COPY opensuse156/package-map.txt /tmp/package-map.txt
# The base image can carry a library newer than the -devel package the
# repos have for it (libncurses6 vs ncurses-devel), so the install must
# be allowed to downgrade: --force-resolution, then check that zypper
# kept every package rather than dropping one to resolve.
RUN zypper -q --gpg-auto-import-keys refresh \
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
        | sort -u > /tmp/packages.txt \
    && echo zstd >> /tmp/packages.txt \
    && xargs zypper -n install --no-recommends --force-resolution \
        < /tmp/packages.txt \
    && xargs rpm -q --whatprovides < /tmp/packages.txt > /dev/null \
    && zypper clean -a \
    && rm -f /tmp/packages-dev.txt /tmp/package-map.txt /tmp/packages.txt

# Whamcloud-patched e2fsprogs (needed for Lustre userspace tools).
COPY common/build-e2fsprogs.sh /tmp/build-e2fsprogs.sh
RUN bash /tmp/build-e2fsprogs.sh && rm /tmp/build-e2fsprogs.sh

# configure's default libdir is ${exec_prefix}/lib.  EL's
# /usr/share/config.site makes it lib64 for prefix /usr, which is where
# SLES's Lustre packages install too, and a cluster's rocky and SUSE
# nodes share one cfg/local.sh naming it.  SUSE's equivalent is in
# site-config, which only a login shell's CONFIG_SITE points at.  Not
# at /usr/share/config.site: the SUSE file sources that one itself.
RUN zypper -n install --no-recommends site-config \
    && ln -s "$(uname -m)-unknown-linux-gnu" /usr/share/site/native \
    && zypper clean -a
ENV CONFIG_SITE=/usr/share/site/native

# Cross toolchain for aarch64 builds on an x86_64 host, under the
# aarch64-linux-gnu- names the cross scripts use.  Leap has GCC 7 for
# it, the compiler SUSE builds these kernels with; lustre_build.py adds
# the target's libgcc and crt files from an aarch64 sysroot.
RUN if [ "$(uname -m)" = x86_64 ]; then \
        zypper -n install --no-recommends \
            cross-aarch64-gcc7 cross-aarch64-binutils \
        && for t in /usr/bin/aarch64-suse-linux-*; do \
               ln -s "$t" "/usr/bin/aarch64-linux-gnu-${t##*/aarch64-suse-linux-}"; \
           done \
        && zypper clean -a; \
    fi

ENV PATH="/usr/lib64/ccache:${PATH}"
ENV CCACHE_DIR="/ccache"

WORKDIR /build
ENTRYPOINT ["/bin/bash"]
