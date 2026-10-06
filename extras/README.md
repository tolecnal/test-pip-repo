# extras

## `check_devpi.py`: Nagios / Icinga 2 check

`pipcheck` answers "does the repository work end to end?" by publishing and installing a
real package, which is too heavy and too invasive to run every minute. `check_devpi.py`
is the lightweight counterpart for continuous monitoring. It makes one or two GET
requests and changes nothing on the server.

It reads devpi-server's JSON status endpoint, `GET /+status`. devpi-web uses the same
data for the ok / degraded / fatal banner on its pages, but that verdict only exists in
HTML. This plugin applies the same tests to the JSON, with **devpi-web's thresholds as
its defaults**, so an alert means what the banner would have said.

| Area | Check | WARNING | CRITICAL |
|---|---|---|---|
| reachability | `/+status` answers with devpi's JSON | — | unreachable, TLS failure, HTTP error |
| | response time (`-w`/`-c`) | > 2 s | > 10 s |
| event processing | hooks feeding devpi-web's search (and other plugins) keep up with commits | out of sync > 1 h; nothing processed > 5 min | out of sync > 6 h; nothing processed > 30 min; never started |
| replica only | contact with the primary | no update > 1 min | no update > 5 min |
| | serial vs. the primary's | behind > 5 min | behind > 60 min (only WARNING while updates still arrive) |
| | replication errors | — | any |
| devpi-web search | whoosh index queue | > 10 items, or any indexing error | — |
| `--index` | the index exists | — | 404, auth failure, not devpi JSON |

Timestamps are aged against the **server's** clock (its HTTP `Date` header), not the
monitoring host's. Clock skew between the two cannot cause a false alert.

Exit codes follow the plugin API: `0` OK, `1` WARNING, `2` CRITICAL, `3` UNKNOWN (bad
arguments, or something answered that is not devpi). A `401`/`403` from `/+status` is
UNKNOWN, because it means the plugin needs credentials, not that devpi is unhealthy.

```
$ ./check_devpi.py --url https://devpi.internal --index root/pypi --no-metrics
DEVPI OK - devpi-server 6.20.3, devpi-web 5.1.1, master, serial 4521 | 'time'=0.0068s;2;10;0 'serial'=4521c;;;0 'event_lag'=0;;;0
index root/pypi: type=mirror, bases=-
```

### Perfdata

`time`, `serial`, `event_lag` (serials not yet processed by plugins) and, on a replica,
`replica_lag` (serials behind the primary). Unless `--no-metrics` is given, every metric
devpi reports is included too: storage/changelog/relpath cache hits, misses and sizes,
and the search index queue lengths. Counters carry the `c` unit, so graphers can
derive rates from them.

### Requirements

Python 3.8+ and nothing else: standard library only, a single file. It does not import
the harness, so it can be copied onto a monitoring host by itself. CI lints and
type-checks it against 3.8.

### Options

```
-u/--url URL          devpi-server root (required), e.g. https://devpi.internal:3141
-i/--index USER/NAME  also check that this index exists
-t/--timeout SECONDS  per HTTP request (default 10)
--ca-file PEM         trust an internal CA     -k/--insecure   skip TLS verification
--user USER           basic auth, if /+status is protected; password from
--password-file FILE  or the DEVPI_PASSWORD environment variable (never argv)
--no-metrics          leave devpi's own metrics out of the perfdata
-w/-c, --primary-warn/-crit, --replica-warn/-crit, --sync-warn/-crit,
--processing-warn/-crit, --queue-warn     thresholds; see --help for defaults
```

`/+status` is readable without credentials on a default devpi. Only pass `--user` if
yours is locked down. The credentials are not carried along if a proxy redirects the
request elsewhere.

### Icinga 2

[`icinga2/devpi.conf`](icinga2/devpi.conf) defines a `devpi` CheckCommand, with a
`devpi_*` custom variable for every option, and an apply rule that creates the service on
any host with `vars.devpi_url` set:

```
install -m 0755 extras/check_devpi.py /usr/lib/nagios/plugins/check_devpi.py
cp extras/icinga2/devpi.conf /etc/icinga2/conf.d/
icinga2 daemon -C && systemctl reload icinga2
```

```
object Host "devpi.internal" {
  address = "devpi.internal"
  check_command = "hostalive"
  vars.devpi_url = "https://devpi.internal"
  vars.devpi_index = "root/pypi"
  vars.devpi_ca_file = "/etc/ssl/certs/internal-ca.pem"
}
```

`PluginDir` is `/usr/lib/nagios/plugins` on Debian/Ubuntu and `/usr/lib64/nagios/plugins`
on RHEL; install the script wherever your `constants.conf` points.

### Nagios / Naemon

[`nagios/devpi.cfg`](nagios/devpi.cfg) has a `check_devpi` command (`$ARG1$` = URL,
`$ARG2$` = further options) and an example service.

### Replicas

Point a check at each replica as well as at the primary. On a replica, the replication
checks are where most real failures show up: the primary is unreachable, the replica is
falling behind, or it has replication errors.
