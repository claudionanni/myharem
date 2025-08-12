# MyHarem

MyHarem is a Python-based tool for managing local MariaDB instances. It allows you to easily deploy single instances or multi-node Galera clusters from tarball archives.

## Installation

To install MyHarem, simply use `pip`:

```bash
pip install .
```

This will install the `mh` command-line tool.

## Configuration

MyHarem uses a configuration file located at `/etc/myharem.conf`. This file specifies the base directory where MyHarem will store its data.

Here's an example `myharem.conf`:

```ini
[DEFAULT]
basedir = /var/opt/myharem
dbuser = mysql
```

The first time you run `mh`, it will automatically create the necessary directory structure under the specified `basedir`.

## Commands

MyHarem provides a simple and powerful command-line interface.

### `mh deploy <tarball> <instance_id>`

Deploys a new single MariaDB instance.

*   `<tarball>`: The path to the MariaDB tarball archive.
*   `<instance_id>`: A unique ID for the instance (e.g., a port number).

Example:
```bash
mh deploy /path/to/mariadb-10.5.9-linux-x86_64.tar.gz 10509
```

### `mh deploygalera <tarball> <first_instance_id>`

Deploys a 3-node Galera cluster.

*   `<tarball>`: The path to the MariaDB tarball archive.
*   `<first_instance_id>`: The ID for the first node in the cluster. The other nodes will be deployed with IDs incremented by 10000.

Example:
```bash
mh deploygalera /path/to/mariadb-10.5.9-linux-x86_64.tar.gz 10509
```

### `mh service start <instance_id>`

Starts a MariaDB instance.

### `mh service stop <instance_id>`

Stops a MariaDB instance.

### `mh service status`

Shows the status of all deployed instances.

### `mh scli <instance_id>`

Connects to a MariaDB instance using a socket.

### `mh log <instance_id>`

Shows the latest log entries for an instance.

Options:
*   `--lines <n>`: The number of lines to show (default: 20).
*   `--level <level>`: Filter by log level (e.g., `ERROR`, `Warning`).

### `mh var <variable_name>`

Extracts a server variable from all running instances.
