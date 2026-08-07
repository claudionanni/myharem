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

## Commands

### Deploy

- `mh deploy` — interactive wizard (pick tarball, type, IDs).
- `mh deploy <tarball> <id>` — single instance (non-interactive).
- `mh deploygalera <tarball> <first_id> [--nodes N]` — N-node Galera cluster
  (default 3). Nodes are placed at `first_id`, `first_id+10000`, … Start the
  whole cluster with `mh cluster start <first_id>`.
- `mh deployreplication <tarball> <master_id> [--slaves N]` — master + N GTID
  slaves (default 1). Slaves at `master_id + i*10000`.

```bash
sudo mh deploygalera mariadb-11.8.6-linux-x86_64.tar.gz 12000 --nodes 5
sudo mh deployreplication mariadb-11.8.6-linux-x86_64.tar.gz 18000 --slaves 2
```

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
