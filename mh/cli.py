import click

from . import config

@click.group()
def main():
    """MyHarem: A tool for managing local MariaDB instances."""
    config.setup_myharem_dirs()

from . import deployment

from . import service

@main.command()
@click.argument('tarball')
@click.argument('instance_id')
def deploy(tarball, instance_id):
    """Deploys a new MariaDB instance."""
    click.echo(f"Deploying {tarball} as instance {instance_id}")
    deployment.deploy_instance(tarball, instance_id)

@main.group()
def service_group():
    """Manages MariaDB services."""
    pass

@service_group.command()
@click.argument('instance_id')
def start(instance_id):
    """Starts a MariaDB instance."""
    service.start_instance(instance_id)

@service_group.command()
@click.argument('instance_id')
def stop(instance_id):
    """Stops a MariaDB instance."""
    service.stop_instance(instance_id)

@service_group.command(name='status')
def status_command():
    """Shows the status of all instances."""
    from .instance import Instance
    instances = Instance.get_all_instances()
    if not instances:
        click.echo("No instances found.")
        return

    for instance in instances:
        click.echo(f"Instance {instance.id}: {instance.get_status()}")

main.add_command(service_group, name='service')

@main.command()
@click.argument('instance_id')
def scli(instance_id):
    """Connects to a MariaDB instance using a socket."""
    service.scli_instance(instance_id)

@main.command()
@click.argument('instance_id')
@click.option('--lines', default=20, help='Number of lines to show.')
@click.option('--level', help='Filter by log level (e.g., ERROR, Warning).')
def log(instance_id, lines, level):
    """Shows the latest log entries for an instance."""
    from .instance import Instance
    instance = Instance(instance_id)
    if not instance.exists():
        click.echo(f"Instance {instance_id} not found.")
        return

    entries = instance.get_log_entries(num_lines=lines, level=level)
    for entry in entries:
        click.echo(entry)

@main.command()
@click.argument('variable_name')
def var(variable_name):
    """Extracts a variable from all running instances."""
    from .instance import Instance
    instances = Instance.get_all_instances()
    if not instances:
        click.echo("No instances found.")
        return

    click.echo(f"Extracting variable '{variable_name}':")
    for instance in instances:
        value = instance.get_variable(variable_name)
        click.echo(f"  - Instance {instance.id}: {value}")

from . import galera

@main.command()
@click.argument('tarball')
@click.argument('first_instance_id')
def deploygalera(tarball, first_instance_id):
    """Deploys a 3-node Galera cluster."""
    galera.deploy_cluster(tarball, first_instance_id)
