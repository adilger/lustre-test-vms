#!/bin/bash
# The KDC for a Lustre cluster, run as root on one node by `ltvm cluster
# krb5`.  Usage: krb5-kdc.sh REALM "USER..." NODE...
#
# Creates the realm once, then for every node the lustre_{mgs,mds,oss,
# root}/<node> and host/<node> principals, exported to
# /root/krb5-keytabs/<node>.keytab, and a password principal for each
# user (krb5_login.sh kinit's with password = username).  Reruns keep
# the realm and its keys: -norandkey exports the current ones.
set -euo pipefail

REALM=$1
USERS=$2
shift 2
NODES="$*"
D=/var/kerberos/krb5kdc

dnf install -y -q krb5-server

cat > $D/kdc.conf <<EOF
[kdcdefaults]
 kdc_ports = 88
 kdc_tcp_ports = 88

[realms]
 $REALM = {
  acl_file = $D/kadm5.acl
  admin_keytab = $D/kadm5.keytab
  max_life = 24h
  max_renewable_life = 7d
 }
EOF
echo "*/admin@$REALM *" > $D/kadm5.acl

[[ -f $D/principal ]] ||
	kdb5_util create -s -r "$REALM" -P "$(head -c 32 /dev/urandom | base64)"
systemctl enable -q krb5kdc
systemctl restart krb5kdc

has() {
	kadmin.local -q "getprinc $1" 2>/dev/null | grep -q '^Principal:'
}

mkdir -p /root/krb5-keytabs
for n in $NODES; do
	ps="lustre_mgs/$n lustre_mds/$n lustre_oss/$n lustre_root/$n host/$n"
	for p in $ps; do
		has "$p@$REALM" || kadmin.local -q "addprinc -randkey $p@$REALM"
	done
	rm -f /root/krb5-keytabs/$n.keytab
	for p in $ps; do
		kadmin.local -q \
			"ktadd -norandkey -k /root/krb5-keytabs/$n.keytab $p@$REALM"
	done
done
for u in $USERS; do
	has "$u@$REALM" || kadmin.local -q "addprinc -pw $u $u@$REALM"
done
