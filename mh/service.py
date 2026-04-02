import click

from .instance import Instance


def start_instance(instance_id):
    """Starts a MariaDB instance."""
    instance = Instance(instance_id)
    instance._require_exists()
    instance.start()


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
