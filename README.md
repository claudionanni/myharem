# MyHarem

MyHarem is a Python-based tool for managing local MariaDB instances. It allows you to easily deploy single instances, multi-node Galera clusters, or master/slave async replication pairs from tarball archives.

## Installation

To install MyHarem, simply use `pip`:

```bash
pip install .
```

This will install the `mh` command-line tool.

## Configuration

MyHarem uses a configuration file to specify the base directory and database user. The config path is resolved in this order:

1. `MYHAREM_CONF` environment variable
2. `/etc/myharem.conf`

Here's an example `myharem.conf`:

```ini
basedir=/var/opt/myharem
dbuser=mysql
```

The first time you run `mh`, it will automatically create the necessary directory structure under the specified `basedir` and set ownership to `dbuser`.

## Commands

### `mh deploy <tarball> <instance_id>`

Deploys a new single MariaDB instance.

*   `<tarball>`: The path to the MariaDB tarball archive.
*   `<instance_id>`: A unique ID for the instance (e.g., a port number).

```bash
mh deploy /path/to/mariadb-10.5.9-linux-x86_64.tar.gz 10509
```

### `mh deploygalera <tarball> <first_instance_id>`

Deploys a 3-node Galera cluster.

*   `<tarball>`: The path to the MariaDB tarball archive.
*   `<first_instance_id>`: The ID for the first node. Other nodes get IDs incremented by 10000.

```bash
mh deploygalera /path/to/mariadb-10.5.9-linux-x86_64.tar.gz 10509
# Deploys nodes: 10509, 20509, 30509
```

### `mh deployreplication <tarball> <instance_id>`

Deploys a master/slave async replication pair using GTID.

*   `<tarball>`: The path to the MariaDB tarball archive.
*   `<instance_id>`: The ID for the master instance. The slave gets ID + 10000.

Both instances are automatically started and replication is configured via `CHANGE MASTER TO ... MASTER_USE_GTID=slave_pos`.

```bash
mh deployreplication /path/to/mariadb-10.5.9-linux-x86_64.tar.gz 10509
# Deploys master: 10509, slave: 20509
```

### `mh service start <instance_id>`

Starts a MariaDB instance.

### `mh service stop <instance_id>`

Stops a MariaDB instance.

### `mh service status`

Shows the status of all deployed instances (Running/Stopped).

### `mh scli <instance_id>`

Connects to a MariaDB instance using a socket (root user).

### `mh cli <instance_id>`

Connects to a MariaDB instance using TCP (root user).

### `mh log <instance_id>`

Shows the latest log entries for an instance.

Options:
*   `--lines <n>`: The number of lines to show (default: 20).
*   `--level <level>`: Filter by log level (e.g., `ERROR`, `Warning`).

### `mh var <variable_name>`

Extracts a server variable from all running instances.

### `mh erase <instance_id>`

Removes an instance completely. Requires double confirmation. The instance is stopped (if running) and moved to the `erased/` directory.

### `mh show local`

Lists locally available tarballs in the `local/` directory.

### `mh show remote`

Lists remotely available tarballs from a previously fetched list.
