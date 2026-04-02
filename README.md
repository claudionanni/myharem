# MyHarem

MyHarem is a Python-based CLI tool for managing local MariaDB instances deployed from tarballs. It supports single instances, GTID-based async replication (master/slave), and multi-node Galera clusters.

## Installation

```bash
sudo pip install .
```

This installs the `mh` command-line tool. All `mh` commands that manage instances require `sudo` (root access for socket auth and file ownership).

> **Note:** When updating from a local checkout, pip may not detect changes if the version number hasn't changed. Use `--force-reinstall`:
> ```bash
> sudo pip install --force-reinstall --no-deps .
> ```

Or use the built-in update command:

```bash
sudo mh update
```

## Configuration

MyHarem uses a configuration file to specify the base directory and database user. The config path is resolved in this order:

1. `MYHAREM_CONF` environment variable
2. `/etc/myharem.conf`

Example `myharem.conf`:

```ini
basedir=/var/opt/myharem
dbuser=mysql
```

The first time you run `mh`, it creates the directory structure under `basedir`:

```
basedir/
├── instances/    # Deployed MariaDB instances
├── local/        # Place tarballs here for auto-discovery
├── remote/       # Remote tarball list cache
└── erased/       # Erased instances (safety net)
```

## Service Users

MyHarem automatically creates three service users on first instance start:

| User | Purpose | Auth |
|------|---------|------|
| `myharem` | Admin — used by all `mh` commands | No password (socket) |
| `mh_repl` | Async replication slave IO thread | No password |
| `mh_sst` | Galera SST with mariabackup | Password: `sstpwd` |

Users are created by connecting as `root` via socket (requires `sudo`). On Galera joiner nodes, users are received automatically via SST from the donor.

## Tarball Auto-Discovery

All deploy commands accept a tarball path or just a filename. If the file isn't found at the given path, MyHarem searches in `basedir/local/` automatically.

```bash
# These are equivalent:
sudo mh deploy /var/opt/myharem/local/mariadb-11.8.6-linux-systemd-x86_64.tar.gz 18000
sudo mh deploy mariadb-11.8.6-linux-systemd-x86_64.tar.gz 18000
```

## Commands

### `mh deploy` — Interactive Wizard

When called without arguments, launches an interactive deployment wizard:

1. Pick a tarball from `local/`
2. Choose deployment type: single, replica, or galera
3. Enter an instance ID (base port)
4. Validates that the ID (and dependent IDs) don't already exist
5. Confirms and deploys

```bash
sudo mh deploy
```

### `mh deploy <tarball> <instance_id>`

Deploys a single MariaDB instance directly (non-interactive).

```bash
sudo mh deploy mariadb-11.8.6-linux-systemd-x86_64.tar.gz 18000
```

### `mh deployreplication <tarball> <instance_id>`

Deploys a master/slave async replication pair using GTID.

The slave ID is master ID + 10000. Both instances are started automatically and replication is configured via `CHANGE MASTER TO ... MASTER_USE_GTID=slave_pos`.

```bash
sudo mh deployreplication mariadb-11.8.6-linux-systemd-x86_64.tar.gz 18000
# Deploys master: 18000, slave: 28000
```

### `mh deploygalera <tarball> <first_instance_id>`

Deploys a 3-node Galera cluster. Node IDs are incremented by 10000.

```bash
sudo mh deploygalera mariadb-11.8.6-linux-systemd-x86_64.tar.gz 12022
# Deploys nodes: 12022, 22022, 32022
```

After deploying, start the cluster in order:

```bash
sudo mh service start --bootstrap 12022   # Bootstrap (new cluster)
sudo mh service start 22022               # Joins cluster
sudo mh service start 32022               # Joins cluster
```

> **Important:** Only use `--bootstrap` when creating a **new** cluster. On restart, use plain `mh service start` for all nodes.

### `mh service start [--bootstrap] <instance_id>`

Starts a MariaDB instance. Use `--bootstrap` for the first node of a new Galera cluster.

Service users are created automatically on first start. Galera joiners skip user creation (users arrive via SST).

### `mh service stop <instance_id>`

Stops a MariaDB instance.

### `mh service status`

Shows the status of all deployed instances (Running/Stopped).

### `mh list`

Lists all deployed instances grouped by tarball version, sorted by ID.

### `mh scli <instance_id>`

Opens a MariaDB client connected via socket (using the `myharem` admin user).

### `mh cli <instance_id>`

Opens a MariaDB client connected via TCP (using the `myharem` admin user).

### `mh log <instance_id>`

Shows the latest log entries for an instance.

Options:
*   `--lines <n>`: Number of lines to show (default: 20).
*   `--level <level>`: Filter by log level (e.g., `ERROR`, `Warning`).

### `mh var <variable_name>`

Extracts a server variable from all running instances.

```bash
sudo mh var version
# [18000] 11.8.6-MariaDB
# [28000] 11.8.6-MariaDB
```

### `mh erase <instance_id>`

Removes an instance completely. Requires double confirmation. Stops the instance if running and moves it to `erased/`.

### `mh show local`

Lists tarballs available in the `local/` directory.

### `mh show remote`

Lists remotely available tarballs from a previously fetched list.

### `mh update`

Updates MyHarem to the latest version from the GitHub repository.

```bash
sudo mh update
```
