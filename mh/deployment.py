import os
import subprocess
import time
from pathlib import Path

import click

from . import config


def resolve_tarball(tarball_path):
    """Resolves a tarball path, searching the local directory as fallback.

    If the path doesn't exist as given, looks in basedir/local/.

    Returns:
        Resolved Path object.
    """
    path = Path(tarball_path)
    if path.exists():
        return path

    local_path = config.get_basedir() / 'local' / path.name
    if local_path.exists():
        click.echo(f"Found tarball in local: {local_path}")
        return local_path

    raise click.ClickException(
        f"Tarball not found: {tarball_path}\n"
        f"Also checked: {local_path}"
    )


def deploy_instance(tarball_path, instance_id, init_db=True):
    """Deploys a new MariaDB instance.

    Args:
        tarball_path: The path to the MariaDB tarball.
        instance_id: The ID for the new instance.
        init_db: Whether to initialize the database. Defaults to True.

    Returns:
        The path to the new instance directory (Path object).
    """
    t0 = time.time()
    tarball_path = resolve_tarball(tarball_path)

    basedir = config.get_basedir()
    instances_dir = basedir / 'instances'

    dirname = tarball_path.name.replace('.tar.gz', '')
    instance_name = f"{dirname}.{instance_id}"
    instance_path = instances_dir / instance_name

    click.echo(f"[1/4] Creating instance directory: {instance_name}")
    instance_path.mkdir(parents=True, exist_ok=True)

    click.echo(f"[2/4] Extracting {tarball_path.name}...")
    t1 = time.time()
    process = subprocess.run(
        ['tar', '-zxf', str(tarball_path), '--strip-components=1',
         '-C', str(instance_path)],
        capture_output=True, text=True,
    )
    if process.returncode != 0:
        raise click.ClickException(
            f"Failed to extract tarball:\n{process.stderr}"
        )
    click.echo(f"       Extracted in {time.time() - t1:.1f}s")

    if init_db:
        click.echo(f"[3/4] Generating my.cnf...")
        _generate_my_cnf(instance_id, instance_path)
        click.echo(f"[4/4] Initializing database...")
        initialize_database(instance_path)
    else:
        click.echo(f"[3/4] Skipping my.cnf (custom config will be applied)")
        click.echo(f"[4/4] Skipping database init (will be done after config)")

    config.chown_instance(instance_path)
    click.echo(f"  Instance {instance_id} deployed in {time.time() - t0:.1f}s")
    return instance_path


def _generate_my_cnf(instance_id, instance_path, extra_config=None):
    """Generates a my.cnf for a standalone instance.

    Args:
        instance_id: The instance ID (used as port, server_id, etc.).
        instance_path: Path to the instance directory.
        extra_config: Optional dict of additional config lines to append.
    """
    dbuser = config.get_dbuser()
    instance_path = Path(instance_path)
    my_cnf_path = instance_path / 'my.cnf'

    lines = [
        "[mariadbd]",
        f"port={instance_id}",
        f"socket={instance_path / f'{instance_id}.sock'}",
        f"basedir={instance_path}",
        f"datadir={instance_path / 'data'}",
        f"server_id={instance_id}",
        f"user={dbuser}",
        "innodb_file_per_table",
        "log_bin",
        f"log_error={instance_path / f'error.{instance_id}.log'}",
        "binlog_format=ROW",
    ]

    if extra_config:
        lines.append("")
        for key, value in extra_config.items():
            if value is True:
                lines.append(key)
            else:
                lines.append(f"{key}={value}")

    my_cnf_path.write_text('\n'.join(lines) + '\n')
    click.echo(f"Generated my.cnf at {my_cnf_path}")


def initialize_database(instance_path):
    """Initializes the MariaDB data directory using mariadb-install-db."""
    instance_path = Path(instance_path)
    install_db_script = instance_path / 'scripts' / 'mariadb-install-db'
    my_cnf_path = instance_path / 'my.cnf'
    datadir = instance_path / 'data'

    if not install_db_script.exists():
        raise click.ClickException(
            f"mariadb-install-db not found at {install_db_script}"
        )

    click.echo("Initializing the database...")
    cmd = [
        str(install_db_script),
        f"--defaults-file={my_cnf_path}",
        f"--basedir={instance_path}",
        f"--datadir={datadir}",
    ]

    process = subprocess.run(cmd, capture_output=True, text=True)

    if process.returncode != 0:
        raise click.ClickException(
            f"Error initializing the database:\n{process.stderr}"
        )

    click.secho("Database initialized successfully.", fg='green')
    if process.stdout.strip():
        click.echo(process.stdout)


ADMIN_USER = 'myharem'
REPL_USER = 'mh_repl'
SST_USER = 'mh_sst'
SST_PASSWORD = 'sstpwd'

# All user creation SQL. Executed as root on a running instance.
CREATE_USERS_SQL = (
    f"CREATE USER IF NOT EXISTS '{ADMIN_USER}'@'localhost';\n"
    f"GRANT ALL PRIVILEGES ON *.* TO '{ADMIN_USER}'@'localhost' "
    f"WITH GRANT OPTION;\n"
    f"CREATE USER IF NOT EXISTS '{REPL_USER}'@'localhost';\n"
    f"GRANT REPLICATION SLAVE ON *.* TO '{REPL_USER}'@'localhost';\n"
    f"CREATE USER IF NOT EXISTS '{SST_USER}'@'localhost' "
    f"IDENTIFIED BY '{SST_PASSWORD}';\n"
    f"GRANT RELOAD, PROCESS, LOCK TABLES, REPLICATION CLIENT "
    f"ON *.* TO '{SST_USER}'@'localhost';\n"
    f"FLUSH PRIVILEGES;\n"
)

# Extra grants for MariaDB 10.11+ (separated from ALL PRIVILEGES).
ADMIN_EXTRA_GRANTS = (
    f"GRANT SUPER, SHUTDOWN, REPLICATION SLAVE ADMIN "
    f"ON *.* TO '{ADMIN_USER}'@'localhost';"
)


def create_service_users(instance):
    """Creates service users on a running instance by connecting as root.

    Connects via socket as the OS root user (requires sudo) and creates:
    - myharem: full admin, no password (for mh commands)
    - mh_repl: REPLICATION SLAVE privilege (for async replication)
    - mh_sst: SST privileges with password (for Galera mariabackup)

    Idempotent — skips silently if users already exist.

    Args:
        instance: A running Instance object.
    """
    mariadb = instance.path / 'bin' / 'mariadb'
    if not mariadb.exists():
        mariadb = instance.path / 'bin' / 'mysql'

    # Check if myharem user already exists (skip if so)
    check_cmd = [
        str(mariadb), '-uroot',
        f'--socket={instance.socket_path}',
        '-B', '-N', '-e',
        f"SELECT 1 FROM mysql.user WHERE User='{ADMIN_USER}' LIMIT 1",
    ]
    check = subprocess.run(check_cmd, capture_output=True, text=True)
    if check.returncode == 0 and check.stdout.strip() == '1':
        return  # Users already exist

    click.echo("Creating service users (myharem, mh_repl, mh_sst)...")

    # Connect as root via socket (works because mh runs with sudo)
    cmd = [
        str(mariadb), '-uroot',
        f'--socket={instance.socket_path}',
        '-B', '-e', CREATE_USERS_SQL,
    ]
    process = subprocess.run(cmd, capture_output=True, text=True)

    if process.returncode != 0:
        output = (process.stderr or process.stdout or '(no output)').strip()
        click.secho(f"Warning: user creation failed:\n{output}", fg='yellow')
        return

    # Apply extra admin grants for MariaDB 10.11+
    cmd_extra = [
        str(mariadb), '-uroot',
        f'--socket={instance.socket_path}',
        '-B', '-e', ADMIN_EXTRA_GRANTS,
    ]
    subprocess.run(cmd_extra, capture_output=True, text=True)
    # Ignore errors — older versions don't have these privilege names

    click.secho("Service users created.", fg='green')
