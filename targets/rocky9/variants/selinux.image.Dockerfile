ARG BASE_IMAGE_TAG
FROM ${BASE_IMAGE_TAG}

# SELinux enforcing with the targeted policy, for sanity-selinux, which
# skips the whole suite otherwise.  The base image has no policy and its
# root filesystem (mke2fs -d) carries no labels, so the first boot
# relabels it and reboots once.
RUN dnf -y install selinux-policy-targeted policycoreutils \
        policycoreutils-python-utils checkpolicy setools-console \
    && dnf clean all

RUN sed -i -e 's/^SELINUX=.*/SELINUX=enforcing/' \
        -e 's/^SELINUXTYPE=.*/SELINUXTYPE=targeted/' /etc/selinux/config

# sanity-selinux test_4 runcon's from unconfined_t to user_t and guest_t,
# after checking the policy allows it with sesearch (setools-console).
COPY rocky9/variants/selinux-lustre-tests.te /tmp/
RUN cd /tmp \
    && checkmodule -M -m -o lustre-tests.mod selinux-lustre-tests.te \
    && semodule_package -o lustre-tests.pp -m lustre-tests.mod \
    && semodule -i lustre-tests.pp \
    && rm -f selinux-lustre-tests.te lustre-tests.mod lustre-tests.pp

RUN touch /.autorelabel
