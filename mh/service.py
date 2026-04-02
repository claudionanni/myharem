import time

import click

from . import deployment
from .instance import Instance


def start_instance(instance_id):
    """Starts a MariaDB instance."""
    instance = Instance(instance_id)
    instance._require_exists()
    instance.start()
    # Wait for socket, then create service users if first start
    for _ in range(30):
        time.sleep(1)
        if instance.is_socket_ready():
            deployment.create_service_users(instance)
            break


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
