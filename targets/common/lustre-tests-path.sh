# Not the Lustre tests directory: test-framework puts it first in PATH
# only when it is absent, and behind /usr/bin its truncate and memhog
# lose to coreutils' and numactl's (sanityn 121).  auster is wrapped in
# /usr/local/bin instead.
# EL installs openmpi off PATH; cfg/local.sh finds mpirun with which.
[ -d /usr/lib64/openmpi/bin ] && export PATH="$PATH:/usr/lib64/openmpi/bin"
# parallel-scale finds compilebench and connectathon only through these.
[ -d /opt/compilebench ] && export cbench_DIR=${cbench_DIR:-/opt/compilebench}
[ -d /opt/connectathon ] && export cnt_DIR=${cnt_DIR:-/opt/connectathon}
