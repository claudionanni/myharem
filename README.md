# MyHarem

MyHarem is a Python CLI for deploying and managing multiple **MariaDB instances
from tarballs on a single host**. Each instance is fully isolated (its own home,
data directory, `my.cnf`, port, and socket under a dedicated folder), so many
instances — including an entire Galera cluster or a replication set — coexist on
one machine without conflicting.

It supports single instances, GTID-based async replication (one master + N
slaves), and N-node Galera clusters. As of v0.2.0 it is also **scriptable**: a
`--json` mode and a persisted manifest let automation drive it as a deployment
backend (e.g. the Simulacro/MSRS control plane), while it remains a first-class
standalone tool.

## Installation

```bash
sudo pip install .
```

Installs the `mh` command. Instance-managing commands require `sudo` (root, for
socket auth and file ownership). Requires Python 3.10+.

## Configuration

Config is resolved from `MYHAREM_CONF`, then `/etc/myharem.conf`:

```ini
basedir=/var/opt/myharem
dbuser=mysql
# optional credentials (see Service Users)
admin_password=
sst_password=sstpwd
# optional: Galera provider for tarballs that don't bundle it (see Galera notes)
# wsrep_provider=/path/to/libgalera_smm.so
# optional: customer token for `mh download --edition ES` (see Download)
# es_token=
```

The first run creates the directory tree under `basedir`:

```
basedir/
├── instances/       # Deployed MariaDB instances
├── local/           # Place tarballs here for auto-discovery
├── erased/          # Erased instances (safety net)
└── manifest.json    # Registry of deployments (topology/nodes/ports/roles)
```

## Output convention (automation-friendly)

**stdout is the result; stderr is progress.** Human progress, warnings, and
status go to stderr; the command's result goes to stdout. With `--json`, the
result on stdout is a single machine-readable JSON object:

```bash
sudo mh --json deploygalera mariadb-11.8.6-linux-x86_64.tar.gz 12000 --nodes 3
# stdout: {"topology": "galera", "cluster_id": "12000", "nodes": [...], ...}
```

Deployments are also recorded in `manifest.json`, so state is authoritative
rather than scraped. `mh --json list` returns instances plus the manifest.

## Service Users

Created automatically on first instance start (by connecting as `root` via
socket; Galera joiners receive them via SST from the donor):

| User | Purpose | Auth |
|------|---------|------|
| `myharem` | Admin — used by all `mh` commands | Socket; password optional via `MYHAREM_ADMIN_PASSWORD` |
| `mh_repl` | Async replication slave | No password |
| `mh_sst` | Galera SST (mariabackup) | `MYHAREM_SST_PASSWORD` (default `sstpwd`) |

Set `MYHAREM_ADMIN_PASSWORD` / `MYHAREM_SST_PASSWORD` (or `admin_password` /
`sst_password` in the config) to avoid the defaults. The admin password is
passed to clients via `MYSQL_PWD`, never on the command line.

## Tarball auto-discovery

Deploy commands accept a full path or a bare filename; if not found as given,
MyHarem looks in `basedir/local/`:

```bash
sudo mh deploy mariadb-11.8.6-linux-x86_64.tar.gz 18000   # found in local/
```

`mh download` puts tarballs there for you — see below.

## Commands

### Download

Fetches a tarball from the official download sites into `local/`, where every
deploy command looks for it.

- `mh download` — interactive: pick edition, series and release. The release
  menu shows the newest 10 with a `[0] show all` entry, so an older release in a
  long series (10.6 has 28) is still two keystrokes away.
- `mh download --edition CS|ES --version <series|version>` — non-interactive.
  A series (`11.4`) takes the newest release in it; a version (`11.4.13`, or an
  Enterprise build `11.4.13-10`) takes exactly that one.
- `mh download --list --edition CS [--version <series>]` — what is published,
  without downloading anything. Honours `--json`.
- `mh download ... --target <bintar>` — Enterprise only; which distribution the
  tarball is for. The wizard asks, defaulting to this host; detection is a
  default, not a restriction, because the tarball is usually for other machines.
- `mh download ... --arch <arch>` — Community architecture (default: this
  machine's). For Enterprise the architecture is part of `--target`.
- `mh download ... --no-verify` — skip the published sha256 check.
- `mh fetch-tarball <url> [--name FILENAME]` — download from a URL you already
  have, for anything the catalogue does not cover.
- `mh token` — where to get the Enterprise download token, and whether one is
  already configured (never the value). Opens the page in a browser on a desktop
  session; `--no-open` just prints the address.

Both are idempotent: a file already staged under that name is left alone.

```bash
sudo mh download                                     # pick edition, series, release
sudo mh download --edition CS --version 11.4         # newest release in the series
sudo mh download --edition CS --version 11.4.13      # exactly that one
sudo mh download --edition CS --version 10.6.18      # an older release
sudo mh download --list --edition CS                 # the series
sudo mh download --list --edition CS --version 10.6  # the releases in one
```

**Community** comes from `downloads.mariadb.org` and needs no credentials. The
published **sha256 is verified** before the file is put in place, and the build
chosen is always the one that bundles the Galera provider — so a downloaded
tarball never hits the problem described under *Galera provider* below.

#### Enterprise downloads and the token

Enterprise comes from `dlm.mariadb.com` and needs a customer token — run
`mh token` to get to the page (<https://customers.mariadb.com/downloads/token/>,
MariaDB ID login), or to check whether one is already set. Without a token
the command refuses, though it still lists the published Enterprise series so
you can see what you are missing. Enterprise publishes **one tarball per
distribution**, not just per architecture. The wizard asks which one, defaulting
to this host when it recognises it; `--target rhel-9-x86_64` sets it directly.
Detection is only a default — you are usually downloading on one machine for
nodes that run somewhere else.

```bash
export MYHAREM_ES_TOKEN=...
sudo -E mh download --edition ES --version 11.4     # note the -E
```

**`sudo` resets the environment**, so a plain `sudo mh download` does not see
`MYHAREM_ES_TOKEN` and refuses as if it were never set. Either pass `-E`, add
`Defaults env_keep += "MYHAREM_ES_TOKEN"` to sudoers, or keep the token in the
config file instead:

```bash
printf 'es_token=...\n' | sudo tee -a /etc/myharem.conf > /dev/null
sudo chmod 600 /etc/myharem.conf        # it is installed world-readable
```

There is deliberately no `--token` flag, and you should not write
`sudo MYHAREM_ES_TOKEN=... mh download` either: the token is a path segment of
the download URL, so either would put a working credential in your shell history
and in `ps`, where every user on the host can read it.

### Repository files

Generates a repository file for a given MariaDB version and distribution, by
running **MariaDB's own repo-setup script** in its generate-only mode. Nothing is
installed, nothing needs root, and the target can be any supported distro — not
just this machine. Useful for answering "what should this customer's
`mariadb.repo` actually say?" without touching a system.

- `mh repo` — interactive: edition, version, distribution, architecture.
- `mh repo --edition CS|ES --version <v> --os <type> --os-version <v>` —
  non-interactive. `--arch`, `--out`, `--stdout` as expected.
- `mh repo ... --with-token` — Enterprise: write the real token into the file
  instead of a placeholder (the result is a credential, written mode 600).

```bash
mh repo --edition CS --version 11.4.5 --os rhel --os-version 9
# Wrote mariadb-11.4.5-rhel-9.repo
# Install as /etc/yum.repos.d/mariadb.repo on the target host.
```

`--os-version` takes what the official script takes, which is not always what a
human would say: `8`/`9`/`10` for rhel and sles, but the **codename** for Ubuntu
(`jammy`, `noble`) and Debian (`bullseye`, `bookworm`, `trixie`). The wizard's
menu removes the guesswork.

An Enterprise file is written with `__MARIADB_ES_TOKEN__` in place of the token
by default, so it is safe to paste into a ticket — substitute the real one on the
target, or pass `--with-token`. Note the repository carries a **smaller set of
versions than the tarballs do**: the script refuses a version that has no
repository for that distribution, and says which ones exist.

### Deploy

- `mh deploy` — interactive wizard (pick tarball, type, IDs).
- `mh deploy <tarball> <id>` — single instance (non-interactive).
- `mh deploygalera <tarball> <first_id> [--nodes N] [--wsrep-provider PATH]` —
  N-node Galera cluster (default 3). Nodes are placed at `first_id`,
  `first_id+10`, … Start the whole cluster with `mh cluster start <first_id>`.
  See **Galera provider** below for `--wsrep-provider`.
- `mh deployreplication <tarball> <master_id> [--slaves N]` — master + N GTID
  slaves (default 1). Slaves at `master_id + i*10`.

```bash
sudo mh deploygalera mariadb-11.8.6-linux-systemd-x86_64.tar.gz 12000 --nodes 5
sudo mh deployreplication mariadb-11.8.6-linux-systemd-x86_64.tar.gz 18000 --slaves 2
```

#### Galera provider (`libgalera_smm.so`)

Galera needs the provider library. **Which tarballs bundle it:**

| Tarball flavor | Bundles Galera? |
|---|---|
| `mariadb-*-linux-systemd-x86_64` (CS binary) | ✅ `lib/galera/libgalera_smm.so` |
| `mariadb-enterprise-*-rhel-*` (ES binary) | ✅ `lib/libgalera_enterprise_smm.so` |
| `mariadb-*-linux-x86_64` (generic glibc) | ❌ **not bundled** |
| source / RPM-bundle tarballs | ❌ not a bindist |

`mh download --edition CS` always picks the `linux-systemd` build, so a tarball
obtained that way bundles the provider and none of this applies.

MyHarem auto-detects the library inside the tarball. If it isn't there, the
deploy **fails immediately with a clear message** (rather than a cryptic
start-up crash). For a tarball that doesn't bundle it, point at one yourself —
e.g. from a matching `linux-systemd` tarball or a system `galera-4` install:

```bash
sudo mh deploygalera mariadb-11.8.6-linux-x86_64.tar.gz 12000 \
    --wsrep-provider /usr/lib64/galera-4/libgalera_smm.so
```

Or set it once via `wsrep_provider` in the config or `MYHAREM_WSREP_PROVIDER`.
The provider's Galera version must be compatible with the server.

### Cluster lifecycle (whole deployment)

Operates on every node of a deployment, in the right order, using the manifest:

```bash
sudo mh cluster start 12000     # Galera: bootstraps node 0, then joins the rest
sudo mh cluster stop 12000
sudo mh cluster erase 12000 --yes [--purge]
```

### Per-instance service

- `mh service start [--bootstrap] <id>` — start one instance (`--bootstrap` =
  first node of a **new** Galera cluster only). Fails (non-zero) if the instance
  doesn't come up in time.
- `mh service stop <id>` — stop one instance.
- `mh service status` — status of all instances (`--json` supported).

### Inspect

- `mh list` — instances grouped by version (`--json` adds the manifest).
- `mh var <name>` — a server variable across running instances (`--json`
  supported; e.g. `mh --json var wsrep_cluster_size`).
- `mh log <id> [--lines N] [--level LEVEL]` — tail an instance's error log.
- `mh cd <id> [--shell]` — print (or `--shell` into) an instance's directory.
- `mh scli <id>` / `mh cli <id>` — open a client via socket / TCP.
- `mh show local` — list tarballs in `local/`.

### Remove

- `mh erase <id> [--yes] [--purge]` — stop and remove a single instance. Without
  `--purge` it is moved to `erased/`; `--yes` skips the confirmation.

### Maintenance

- `mh update` — reinstall MyHarem from the GitHub repository.

## Development

```bash
python -m pytest tests/
```

Tests are MariaDB-free: the tar-extract, DB-init, process-start, and SQL steps
are stubbed, so they validate port math, the result model, manifest recording,
deploy orchestration, and rollback without a live server.

## License

MIT — see [LICENSE](LICENSE).
