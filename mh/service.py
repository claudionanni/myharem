import time

import click

from . import deployment
from .instance import Instance


def _is_galera_instance(instance):
    """Checks if an instance has Galera (wsrep) enabled in my.cnf."""
    try:
        content = instance.my_cnf_path.read_text()
        return 'wsrep_on' in content
    except Exception:
        return False


def start_instance(instance_id, bootstrap=False):
    """Starts a MariaDB instance.

    Args:
        instance_id: The instance ID.
        bootstrap: If True, starts with --wsrep-new-cluster (Galera bootstrap).
    """
    instance = Instance(instance_id)
    instance._require_exists()
    instance.start(wsrep_new_cluster=bootstrap)

    is_galera = _is_galera_instance(instance)
    is_galera_joiner = is_galera and not bootstrap

    if is_galera_joiner:
        # Galera joiners receive users via SST from the donor node.
        # Don't try to create users — just wait for the node to be synced.
        click.echo(
            f"Galera joiner — waiting for SST and sync...", nl=False
        )
        for _ in range(300):  # Up to 5 min for SST
            time.sleep(1)
            click.echo(".", nl=False)
            if instance.is_socket_ready():
                time.sleep(2)
                click.secho(" OK", fg='green')
                click.echo("Users received via SST from donor node.")
                return
        click.secho(" timeout", fg='yellow')
    else:
        # Bootstrap node or non-Galera: create users after start.
        click.echo(
            f"Waiting for instance {instance_id} to be ready...", nl=False
        )
        for _ in range(60):
            time.sleep(1)
            click.echo(".", nl=False)
            if instance.is_socket_ready():
                time.sleep(2)
                click.secho(" OK", fg='green')
                deployment.create_service_users(instance)
                return
        click.secho(" timeout", fg='yellow')


def stop_instance(instance_id):
    """Stops a MariaDB instance."""
    instance = Instance(instance_id)
    instance._require_exists()
    instance.stop()


def scli_instance(instance_id):
    """Connects to an instance via socket."""
    instance = Instance(instance_id)
    instance._require_exists()
    instance.scli()


def cli_instance(instance_id):
    """Connects to an instance via TCP."""
    instance = Instance(instance_id)
    instance._require_exists()
    instance.cli()
