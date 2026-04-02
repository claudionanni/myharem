import time

import click

from . import deployment
from .instance import Instance


def start_instance(instance_id):
    """Starts a MariaDB instance."""
    instance = Instance(instance_id)
    instance._require_exists()
    instance.start()
    # Wait for socket to appear, then give server a moment to finish
    # initialization before creating service users.
    click.echo(f"Waiting for instance {instance_id} to be ready...", nl=False)
    for _ in range(60):
        time.sleep(1)
        click.echo(".", nl=False)
        if instance.is_socket_ready():
            time.sleep(2)  # Extra time for server to finish init
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
