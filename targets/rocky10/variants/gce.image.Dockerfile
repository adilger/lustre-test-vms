ARG BASE_IMAGE_TAG
FROM ${BASE_IMAGE_TAG}

# Rocky 10 image overlay for Google Compute Engine: the guest agent, so
# metadata SSH keys and OS Login work and `gcloud compute ssh` reaches
# an exported image.  A variant rather than the base image because the
# base is shared with local VMs, where the agent would wait on a
# metadata server that is not there.
#
# Deliberately not google-compute-engine.  It ships
# /etc/sysctl.d/60-gce-network-security.conf, which sets rp_filter=1 --
# strict, and fatal to LNet multi-rail (see setup-lnet-rp-filter.sh) --
# and kernel.panic=10, which reboots a node before an LBUG can be read.
# Nor gce-disk-expand: its %post runs `dracut --force` against the build
# host's kernel, and `target export` replaces the initramfs regardless.
# Size the root disk with `target export --disk-size-gb` instead.
#
# EL10's rpm verifies signatures with Sequoia.  That rejects Google's
# long-documented rpm-package-key.gpg (no valid binding signature), and
# neither it nor yum-key.gpg is the key these packages are signed with.
# That key, 3156C631B64936F9, is published only as rpm-package-key-v10.
#
# oslogin's %post prints "semodule: Failed!": ltvm images carry no
# SELinux policy, so there is no store to load its module into.  Harmless
# here, and policycoreutils alone does not pull a policy in.
RUN printf '%s\n' \
        '[google-compute-engine]' \
        'name=Google Compute Engine' \
        'baseurl=https://packages.cloud.google.com/yum/repos/google-compute-engine-el10-$basearch-stable' \
        'enabled=1' \
        'gpgcheck=1' \
        'repo_gpgcheck=0' \
        'gpgkey=https://packages.cloud.google.com/yum/doc/rpm-package-key-v10.gpg' \
        > /etc/yum.repos.d/google-compute-engine.repo \
    && dnf -y install --setopt=install_weak_deps=False \
        google-guest-agent google-compute-engine-oslogin \
    && dnf clean all
