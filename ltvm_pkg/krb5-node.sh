#!/bin/bash
# Kerberos client configuration for one Lustre node, run as root by
# `ltvm cluster krb5`.  Usage: krb5-node.sh REALM KDC_HOST
set -euo pipefail

REALM=$1
KDC=$2

dnf install -y -q krb5-workstation sssd-kcm keyutils

# sssd-kcm's drop-in makes KCM: the default ccache ahead of anything
# krb5.conf says; keep the keyring default (sanity-krb5 test_11 asks for
# KCM itself).
sed -i 's/^[[:space:]]*default_ccache_name/#&/' \
	/etc/krb5.conf.d/kcm_default_ccache
systemctl enable -q --now sssd-kcm.socket

mkdir -p /etc/request-key.d
echo "create lgssc * * $(command -v lgss_keyring) %o %k %t %d %c %u %g %T %P %S" \
	> /etc/request-key.d/lgssc.conf

# Lustre names a node's principals after the reverse lookup of its NID.
ip=$(ip -4 -o route get 1.1.1.1 | sed -n 's/.* src \([0-9.]*\).*/\1/p')
name=$(getent hosts "$ip" | awk '{print $2}')
[[ "$name" == "$(hostname -s)" ]] ||
	{ echo "reverse lookup of $ip is '$name', not $(hostname -s)"; exit 1; }

# One default_realm line, and ticket_lifetime indented: sanity-krb5
# greps the former, and test_200/201 rewrite the latter with
# 's+[^#]ticket_lifetime.*+...+'.
cat > /etc/krb5.conf <<EOF
includedir /etc/krb5.conf.d/

[logging]
 default = FILE:/var/log/krb5libs.log
 kdc = FILE:/var/log/krb5kdc.log
 admin_server = FILE:/var/log/kadmind.log

[libdefaults]
 default_realm = $REALM
 dns_lookup_realm = false
 dns_lookup_kdc = false
 ticket_lifetime = 24h
 renew_lifetime = 7d
 forwardable = true
 rdns = false
 dns_canonicalize_hostname = false
 qualify_shortname = ""
 default_ccache_name = KEYRING:persistent:%{uid}

[realms]
 $REALM = {
  kdc = $KDC
  admin_server = $KDC
 }
EOF
