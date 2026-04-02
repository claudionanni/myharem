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
