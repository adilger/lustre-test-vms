# IPv6 on ltvm clusters

Every extra (`--nic`) NIC holds an IPv4 address and an IPv6 address.
The management NIC (`eth0`) holds IPv4 only. A cluster's NIDs use one
family at a time, and `cluster deploy --ip-family` picks it.

## Run a cluster on IPv6

IPv6 needs tcp on an extra NIC:

```bash
sudo ltvm cluster create co6 rocky9 mgs+mds:co6-mds:1 oss:co6-oss:2 \
    client:co6-cli --nic tcp
ltvm cluster deploy co6 --build ~/lustre-release --ip-family ipv6
```

`--ip-family ipv6` writes these lines into each node's `cfg/local.sh`:

```bash
NETTYPE=tcp
FORCE_LARGE_NID=true
MGSNID=fd17:2016:1000:f100:f172:f016:f100:f023@tcp
```

The cluster records the family, so a bare `cluster deploy` keeps it.
`--ip-family ipv4` writes `FORCE_LARGE_NID=false` and returns to IPv4.
`ltvm cluster status` shows the family.

The deploy reads the MGS NID from the MGS itself. It takes the first
net in the MGS's `/etc/modprobe.d/lnet.conf` and the first interface of
that net. It refuses `--ip-family ipv6` before the build in two cases:

- The net is not tcp. `test-framework.sh` stops with
  `FORCE_LARGE_NID only supported by tcp`.
- The interface has no global IPv6 address. `eth0` has none, so a
  cluster with no `--nic` cannot run IPv6.

## Why lnet.conf does not change

`lnet.conf` is the same for both families, for example
`options lnet networks="tcp0(eth1)"`. It names interfaces, not
addresses. LNet selects the family when the node configures it.
`lnetctl lnet configure --large` puts each interface's IPv6 address
first. `FORCE_LARGE_NID=true` makes the test framework use `--large`.

## How ltvm derives the addresses

The default prefix is `fd17:2016:1000:f100::/64`. The interface ID
holds the four IPv4 octets. Each octet is `f` and then the octet in
three decimal digits:

```
172.16.100.203  ->  fd17:2016:1000:f100:f172:f016:f100:f203
```

Every hextet is `0x1000` or more, so the address always prints at full
width: 39 characters with no `::`. The resulting 43-character NID tests
the string sizes that a short address does not reach. Set another
prefix with `$LTVM_EXTRA_SUBNET6` or `VM_DIR/extra-subnet6`. ltvm
refuses a prefix that breaks the `0x1000` rule.

`eth0` has no IPv6 address, on purpose. `test-framework.sh` makes NIDs
from the output of `hostname -I`. An IPv6 address on `eth0` could then
become a NID.

## Check that a node is on IPv6

```bash
ssh co6-mds 'modprobe lnet; lnetctl lnet configure --all --large; lctl list_nids'
ssh co6-cli 'modprobe lnet; lnetctl lnet configure --all --large; \
    lctl ping fd17:2016:1000:f100:f172:f016:f100:f023@tcp'
```

`lctl list_nids` on the MGS must print the `MGSNID` from `cfg/local.sh`,
character for character. An IPv4 NID means that LNet did not take the
IPv6 address. A compressed IPv6 NID means that the prefix breaks the
width rule.

## Known limit: no mount over an IPv6 NID

LNet runs over IPv6, but Lustre master cannot mount over it yet
(LU-18041). The limit is 40 for `UUID_MAX`, so an `obd_uuid` holds a
NID of 39 characters at most. The MDT mount fails with `-EINVAL`:

```
LustreError: (ldlm_lib.c:369:client_obd_setup()) target UUID must be 40 characters or less
```

Gerrit change 65491 shortens the string, and it has not landed. Until
then, `cluster llmount` fails on an IPv6 cluster. The filesystem suites
fail too. An LNet suite runs if auster skips its own format and mount:

```bash
ssh co6-cli 'cd /usr/lib64/lustre/tests && ./auster -N -v sanity-lnet'
```
