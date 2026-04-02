import time

import click

from . import deployment
from .instance import Instance


def start_instance(instance_id):
    """Starts a MariaDB instance."""
    instance = Instance(instance_id)
    instance._require_exists()
    instance.start()
    # Wait for instance to accept connections, then clean up init-file
    # and apply extra admin grants (needed for MariaDB 10.11+).
    for _ in range(15):
        time.sleep(1)
        if instance.is_running():
            deployment.cleanup_init_file(instance)
            deployment.grant_admin_extras(instance)
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
