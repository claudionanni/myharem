import os
import subprocess
import tarfile
from pathlib import Path

import click

from . import config


def deploy_instance(tarball_path, instance_id, init_db=True):
    """Deploys a new MariaDB instance.

    Args:
        tarball_path: The path to the MariaDB tarball.
        instance_id: The ID for the new instance.
        init_db: Whether to initialize the database. Defaults to True.

    Returns:
        The path to the new instance directory (Path object).
    """
    tarball_path = Path(tarball_path)
    if not tarball_path.exists():
        raise click.ClickException(f"Tarball not found: {tarball_path}")

    basedir = config.get_basedir()
    instances_dir = basedir / 'instances'

    dirname = tarball_path.name.replace('.tar.gz', '')
    instance_name = f"{dirname}.{instance_id}"
    instance_path = instances_dir / instance_name

    click.echo(f"Creating instance directory: {instance_path}")
    instance_path.mkdir(parents=True, exist_ok=True)

    click.echo(f"Extracting {tarball_path} to {instance_path}")
    with tarfile.open(tarball_path, 'r:gz') as tar:
        # Strip the top-level directory (equivalent to tar --strip-components=1)
        members = tar.getmembers()
        root_dir = members[0].name.split('/')[0]
        for member in members:
            if member.path.startswith(root_dir + '/'):
                member.path = member.path[len(root_dir) + 1:]
                if member.path:
                    tar.extract(member, path=instance_path)

    if init_db:
        _generate_my_cnf(instance_id, instance_path)
        initialize_database(instance_path)

    config._chown_tree(instance_path)
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
