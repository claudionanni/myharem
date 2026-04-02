import os
import shutil

import click

from . import config
from . import deployment
from . import galera
from . import service
from .instance import Instance


@click.group()
def main():
    """MyHarem: A tool for managing local MariaDB instances."""
    config.setup_myharem_dirs()


# ---------- deploy ----------

@main.command()
@click.argument('tarball', required=False)
@click.argument('instance_id', required=False)
def deploy(tarball, instance_id):
    """Deploys a new MariaDB instance (interactive if no args given)."""
    if tarball and instance_id:
        click.echo(f"Deploying {tarball} as instance {instance_id}")
        deployment.deploy_instance(tarball, instance_id)
        return

    # --- Interactive wizard ---
    _deploy_wizard()


def _deploy_wizard():
    """Interactive deployment wizard: pick tarball, type, and instance ID."""
    from . import replication

    # 1. Pick tarball from local/
    local_dir = config.get_basedir() / 'local'
    if not local_dir.exists():
        raise click.ClickException(
            f"No local tarball directory found at {local_dir}"
        )
    tarballs = sorted(local_dir.glob('*.tar.gz'))
    if not tarballs:
        raise click.ClickException("No tarballs found in local/")

    click.echo("\nAvailable tarballs:")
    for i, t in enumerate(tarballs, 1):
        click.echo(f"  [{i}] {t.name}")

    choice = click.prompt("\nSelect tarball", type=click.IntRange(1, len(tarballs)))
    tarball = tarballs[choice - 1]
    click.echo(f"  → {tarball.name}")

    # 2. Pick deployment type
    deploy_types = ['single', 'replica', 'galera']
    click.echo("\nDeployment type:")
    for i, dt in enumerate(deploy_types, 1):
        if dt == 'single':
            click.echo(f"  [{i}] Single instance")
        elif dt == 'replica':
            click.echo(f"  [{i}] Async replication (master + slave)")
        elif dt == 'galera':
            click.echo(f"  [{i}] Galera cluster (3 nodes)")

    dtype_choice = click.prompt(
        "\nSelect type", type=click.IntRange(1, len(deploy_types))
    )
    deploy_type = deploy_types[dtype_choice - 1]

    # 3. Pick instance ID
    instance_id = click.prompt("\nInstance ID (base port)", type=int)

    # 4. Check for ID conflicts
    existing = {inst.id for inst in Instance.get_all_instances()}

    if deploy_type == 'single':
        needed_ids = [instance_id]
    elif deploy_type == 'replica':
        needed_ids = [instance_id, instance_id + 10000]
    elif deploy_type == 'galera':
        needed_ids = [instance_id, instance_id + 10000, instance_id + 20000]

    conflicts = [str(nid) for nid in needed_ids if str(nid) in existing]
    if conflicts:
        raise click.ClickException(
            f"Instance ID(s) already exist: {', '.join(conflicts)}"
        )

    # 5. Confirm
    click.echo(f"\n  Tarball: {tarball.name}")
    click.echo(f"  Type:    {deploy_type}")
    click.echo(f"  IDs:     {', '.join(str(i) for i in needed_ids)}")
    if not click.confirm("\nProceed?", default=True):
        click.echo("Aborted.")
        return

    # 6. Deploy
    if deploy_type == 'single':
        deployment.deploy_instance(str(tarball), str(instance_id))
    elif deploy_type == 'replica':
        replication.deploy_replication(str(tarball), str(instance_id))
    elif deploy_type == 'galera':
        galera.deploy_cluster(str(tarball), str(instance_id))


# ---------- deploygalera ----------

@main.command()
@click.argument('tarball')
@click.argument('first_instance_id')
def deploygalera(tarball, first_instance_id):
    """Deploys a 3-node Galera cluster."""
    galera.deploy_cluster(tarball, first_instance_id)


# ---------- deployreplication ----------

@main.command()
@click.argument('tarball')
@click.argument('instance_id')
def deployreplication(tarball, instance_id):
    """Deploys a master/slave async replication pair (GTID)."""
    from . import replication
    replication.deploy_replication(tarball, instance_id)


# ---------- list ----------

@main.command(name='list')
def list_command():
    """Lists all deployed instances and their status."""
    _list_instances()


# ---------- service ----------

@main.group(name='service')
def service_group():
    """Manages MariaDB services."""
    pass


@service_group.command()
@click.argument('instance_id')
@click.option('--bootstrap', is_flag=True,
              help='Galera: bootstrap a new cluster from this node.')
def start(instance_id, bootstrap):
    """Starts a MariaDB instance."""
    service.start_instance(instance_id, bootstrap=bootstrap)


@service_group.command()
@click.argument('instance_id')
def stop(instance_id):
    """Stops a MariaDB instance."""
    service.stop_instance(instance_id)


def _list_instances():
    """Prints a table of all deployed instances grouped by tarball version."""
    instances = Instance.get_all_instances()
    if not instances:
        click.echo("No instances found.")
        return

    # Group by base directory name (tarball version)
    from collections import defaultdict
    groups = defaultdict(list)
    for inst in instances:
        dirname = os.path.basename(inst.path) if inst.path else "unknown"
        # Base name is everything before the last dot (instance ID)
        base = dirname.rsplit('.', 1)[0] if '.' in dirname else dirname
        groups[base].append(inst)

    # Sort groups by name, instances within each group by ID
    for base in sorted(groups):
        click.secho(f"\n  {base}", bold=True)
        for inst in sorted(groups[base], key=lambda i: int(i.id)):
            status = inst.get_status()
            color = 'green' if status == 'Running' else 'red'
            click.echo(f"    {inst.id:<10} ", nl=False)
            click.secho(status, fg=color)


@service_group.command(name='status')
def status_command():
    """Shows the status of all deployed instances."""
    _list_instances()


# ---------- scli / cli ----------

@main.command()
@click.argument('instance_id')
def scli(instance_id):
    """Connects to a MariaDB instance via socket (root)."""
    service.scli_instance(instance_id)


@main.command()
@click.argument('instance_id')
def cli(instance_id):
    """Connects to a MariaDB instance via TCP (root)."""
    service.cli_instance(instance_id)


# ---------- log ----------

@main.command()
@click.argument('instance_id')
@click.option('--lines', default=20, help='Number of lines to show.')
@click.option('--level', help='Filter by log level (e.g., ERROR, Warning).')
def log(instance_id, lines, level):
    """Shows the latest log entries for an instance."""
    instance = Instance(instance_id)
    instance._require_exists()

    entries = instance.get_log_entries(num_lines=lines, level=level)
    for entry in entries:
        click.echo(entry)


# ---------- var ----------

@main.command()
@click.argument('variable_name')
def var(variable_name):
    """Extracts a server variable from all running instances."""
    instances = Instance.get_all_instances()
    if not instances:
        click.echo("No instances found.")
        return

    click.echo(f"Variable '{variable_name}':")
    for inst in instances:
        value = inst.get_variable(variable_name)
        click.echo(f"  [{inst.id}] {value}")


# ---------- erase ----------

@main.command()
@click.argument('instance_id')
def erase(instance_id):
    """Removes an instance completely. ALL DATA WILL BE LOST."""
    instance = Instance(instance_id)
    instance._require_exists()

    basedir = config.get_basedir()
    erased_dir = basedir / 'erased'

    click.secho(f"WARNING: Instance {instance_id} will be erased!", fg='red',
                bold=True)
    click.echo(f"  Path: {instance.path}")

    # Show datadir size if possible
    datadir = instance.path / 'data'
    if datadir.exists():
        total = sum(
            f.stat().st_size for f in datadir.rglob('*') if f.is_file()
        )
        click.echo(f"  Data size: {total / 1024 / 1024:.1f} MB")

    if not click.confirm("Are you sure?"):
        click.echo("Aborted.")
        return

    if not click.confirm("Are you REALLY sure? All data will be wiped!"):
        click.echo("Aborted.")
        return

    # Stop instance if running
    if instance.is_running():
        click.echo("Stopping instance first...")
        instance.stop()

    # Move to erased directory
    dest = erased_dir / os.path.basename(instance.path)
    click.echo(f"Moving to {dest}...")
    shutil.move(str(instance.path), str(dest))

    click.secho(f"Instance {instance_id} erased.", fg='green')


# ---------- show ----------

@main.group()
def show():
    """Lists available tarballs (local or remote)."""
    pass


# ---------- update ----------

@main.command()
def update():
    """Updates MyHarem from the GitHub repository."""
    import subprocess
    import tempfile

    repo_url = "https://github.com/claudionanni/myharem.git"
    branch = "feature/python-refactor-and-new-features"

    click.echo(f"Updating MyHarem from {repo_url} ({branch})...")

    with tempfile.TemporaryDirectory() as tmpdir:
        # Clone the repo
        click.echo("Cloning repository...")
        result = subprocess.run(
            ['git', 'clone', '--depth=1', '--branch', branch,
             repo_url, tmpdir],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise click.ClickException(
                f"Failed to clone repository:\n{result.stderr}"
            )

        # Install with --force-reinstall
        click.echo("Installing...")
        result = subprocess.run(
            ['pip', 'install', '--force-reinstall', '--no-deps', '.'],
            capture_output=True, text=True, cwd=tmpdir,
        )
        if result.returncode != 0:
            raise click.ClickException(
                f"Failed to install:\n{result.stderr}"
            )

    click.secho("MyHarem updated successfully!", fg='green')


@show.command(name='local')
def show_local():
    """Lists locally available tarballs."""
    local_dir = config.get_basedir() / 'local'
    if not local_dir.exists():
        click.echo("No local tarball directory found.")
        return

    tarballs = sorted(local_dir.glob('*.tar.gz'))
    if not tarballs:
        click.echo("No local tarballs found.")
        return

    click.echo("Available local tarballs:")
    click.echo("=" * 40)
    for i, t in enumerate(tarballs, 1):
        click.echo(f"  [L{i}] {t.name}")


@show.command(name='remote')
def show_remote():
    """Lists remotely available tarballs."""
    remote_list = config.get_basedir() / 'remote' / 'list'
    if not remote_list.exists():
        click.echo("No remote list found. Run 'mh tarballs' to fetch it.")
        return

    click.echo("Available remote tarballs:")
    click.echo("=" * 40)
    with open(remote_list) as f:
        for line in f:
            line = line.strip()
            if '|' in line:
                parts = line.split('|')
                if len(parts) >= 3:
                    click.echo(f"  [{parts[0]}] {parts[1]}")
                    click.echo(f"         {parts[2]}")
