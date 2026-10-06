export PATH="$PATH:/usr/lib64/lustre/tests:/usr/lib/lustre/tests"
# EL installs openmpi off PATH; cfg/local.sh finds mpirun with which.
[ -d /usr/lib64/openmpi/bin ] && export PATH="$PATH:/usr/lib64/openmpi/bin"
