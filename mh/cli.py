import json
import os

import click

from . import config
from . import deployment
from . import galera
from . import manifest
from . import report
from . import service
from .instance import Instance


@click.group()
@click.option('--json', 'json_out', is_flag=True,
              help='Emit machine-readable JSON results on stdout.')
@click.pass_context
def main(ctx, json_out):
    """MyHarem: A tool for managing local MariaDB instances."""
    ctx.ensure_object(dict)
    ctx.obj['json'] = json_out
    report.set_json_mode(json_out)
    config.setup_myharem_dirs()


def _emit_deploy(ctx, result):
    """Writes a deploy result to stdout: JSON when --json, else a summary."""
    if ctx.obj.get('json'):
        click.echo(json.dumps(result.to_dict()))
        return
    click.echo(f"{result.topology} deployment '{result.cluster_id}' ready:")
    for node in result.nodes:
        click.echo(
            f"  {node.id:<8} {node.role:<8} port={node.port} "
            f"socket={node.socket}"
        )


def _emit_action(ctx, payload, human):
    if ctx.obj.get('json'):
        click.echo(json.dumps(payload))
    else:
        click.echo(human)


# ---------- deploy ----------

@main.command()
@click.argument('tarball', required=False)
@click.argument('instance_id', required=False)
@click.pass_context
def deploy(ctx, tarball, instance_id):
    """Deploys a single MariaDB instance (interactive if no args given)."""
    if tarball and instance_id:
        result = deployment.deploy_single(tarball, instance_id)
        _emit_deploy(ctx, result)
        return
    _deploy_wizard(ctx)


def _deploy_wizard(ctx):
    """Interactive deployment wizard: pick tarball, type, and instance ID."""
    from . import replication

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

    deploy_types = ['single', 'replica', 'galera']
    click.echo("\nDeployment type:")
    click.echo("  [1] Single instance")
    click.echo("  [2] Async replication (master + slaves)")
    click.echo("  [3] Galera cluster")
    dtype_choice = click.prompt(
        "\nSelect type", type=click.IntRange(1, len(deploy_types))
    )
    deploy_type = deploy_types[dtype_choice - 1]

    instance_id = click.prompt("\nInstance ID (base port)", type=int)

    count = 1
    if deploy_type == 'galera':
        count = click.prompt("Number of Galera nodes", type=int, default=3)
    elif deploy_type == 'replica':
        count = click.prompt("Number of slaves", type=int, default=1)

    existing = {inst.id for inst in Instance.get_all_instances()}
    if deploy_type == 'single':
        needed_ids = [instance_id]
    elif deploy_type == 'replica':
        needed_ids = [instance_id] + [instance_id + 10000 * i
                                      for i in range(1, count + 1)]
    else:  # galera
        needed_ids = [instance_id + 10000 * i for i in range(count)]

    conflicts = [str(nid) for nid in needed_ids if str(nid) in existing]
    if conflicts:
        raise click.ClickException(
            f"Instance ID(s) already exist: {', '.join(conflicts)}"
        )

    click.echo(f"\n  Tarball: {tarball.name}")
    click.echo(f"  Type:    {deploy_type}")
    click.echo(f"  IDs:     {', '.join(str(i) for i in needed_ids)}")
    if not click.confirm("\nProceed?", default=True):
        click.echo("Aborted.")
        return

    if deploy_type == 'single':
        result = deployment.deploy_single(str(tarball), str(instance_id))
    elif deploy_type == 'replica':
        result = replication.deploy_replication(
            str(tarball), str(instance_id), slaves=count
        )
    else:
        result = galera.deploy_cluster(str(tarball), str(instance_id), nodes=count)
    _emit_deploy(ctx, result)


# ---------- deploygalera ----------

@main.command()
@click.argument('tarball')
@click.argument('first_instance_id')
@click.option('--nodes', default=3, type=int, show_default=True,
              help='Number of Galera nodes.')
@click.pass_context
def deploygalera(ctx, tarball, first_instance_id, nodes):
    """Deploys an N-node Galera cluster (default 3)."""
    result = galera.deploy_cluster(tarball, first_instance_id, nodes=nodes)
    _emit_deploy(ctx, result)


# ---------- deployreplication ----------

@main.command()
@click.argument('tarball')
@click.argument('instance_id')
@click.option('--slaves', default=1, type=int, show_default=True,
              help='Number of slaves.')
@click.pass_context
def deployreplication(ctx, tarball, instance_id, slaves):
    """Deploys a master + N async (GTID) slaves."""
    from . import replication
    result = replication.deploy_replication(tarball, instance_id, slaves=slaves)
    _emit_deploy(ctx, result)


# ---------- list / status ----------

def _collect_status():
    rows = []
    for inst in Instance.get_all_instances():
        rows.append({
            'id': inst.id,
            'status': inst.get_status(),
            'path': str(inst.path) if inst.path else None,
        })
    return sorted(rows, key=lambda r: int(r['id']) if r['id'].isdigit() else 0)


def _print_status_human(rows):
    if not rows:
        click.echo("No instances found.")
        return
    from collections import defaultdict
    groups = defaultdict(list)
    for row in rows:
        dirname = os.path.basename(row['path']) if row['path'] else "unknown"
        base = dirname.rsplit('.', 1)[0] if '.' in dirname else dirname
        groups[base].append(row)
    for base in sorted(groups):
        click.secho(f"\n  {base}", bold=True)
        for row in groups[base]:
            color = 'green' if row['status'] == 'Running' else 'red'
            click.echo(f"    {row['id']:<10} ", nl=False)
            click.secho(row['status'], fg=color)


@main.command(name='list')
@click.pass_context
def list_command(ctx):
    """Lists all deployed instances and their status."""
    rows = _collect_status()
    if ctx.obj.get('json'):
        click.echo(json.dumps({
            'instances': rows,
            'deployments': manifest.all_deployments(),
        }))
    else:
        _print_status_human(rows)


# ---------- service ----------

@main.group(name='service')
def service_group():
    """Manages MariaDB services (single instances)."""


@service_group.command()
@click.argument('instance_id')
@click.option('--bootstrap', is_flag=True,
              help='Galera: bootstrap a new cluster from this node.')
@click.pass_context
def start(ctx, instance_id, bootstrap):
    """Starts a MariaDB instance."""
    service.start_instance(instance_id, bootstrap=bootstrap)
    _emit_action(ctx, {'instance': instance_id, 'action': 'start', 'ok': True},
                 f"Instance {instance_id} started.")


@service_group.command()
@click.argument('instance_id')
@click.pass_context
def stop(ctx, instance_id):
    """Stops a MariaDB instance."""
    service.stop_instance(instance_id)
    _emit_action(ctx, {'instance': instance_id, 'action': 'stop', 'ok': True},
                 f"Instance {instance_id} stopped.")


@service_group.command(name='status')
@click.pass_context
def status_command(ctx):
    """Shows the status of all deployed instances."""
    rows = _collect_status()
    if ctx.obj.get('json'):
        click.echo(json.dumps({'instances': rows}))
    else:
        _print_status_human(rows)


# ---------- cluster (whole-deployment lifecycle) ----------

@main.group(name='cluster')
def cluster_group():
    """Manage a whole deployment (all nodes) by its cluster id."""


@cluster_group.command(name='start')
@click.argument('cluster_id')
def cluster_start(cluster_id):
    """Starts every node of a deployment in the correct order."""
    service.start_cluster(cluster_id)


@cluster_group.command(name='stop')
@click.argument('cluster_id')
def cluster_stop(cluster_id):
    """Stops every node of a deployment."""
    service.stop_cluster(cluster_id)


@cluster_group.command(name='erase')
@click.argument('cluster_id')
@click.option('--yes', is_flag=True, help='Skip the confirmation prompt.')
@click.option('--purge', is_flag=True,
              help='Delete data instead of moving to erased/.')
def cluster_erase(cluster_id, yes, purge):
    """Stops and removes every node of a deployment."""
    if not yes:
        entry = manifest.get(cluster_id)
        count = len(entry.get('nodes', [])) if entry else 0
        if not click.confirm(
            f"Erase deployment '{cluster_id}' ({count} nodes)? Data will be "
            f"{'deleted' if purge else 'moved to erased/'}."
        ):
            click.echo("Aborted.")
            return
    service.erase_cluster(cluster_id, purge=purge)


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


# ---------- cd / chdir ----------

def _go_to_instance_dir(instance_id, open_shell=False):
    instance = Instance(instance_id)
    instance._require_exists()
    instance_path = str(instance.path)
    if open_shell:
        shell = os.environ.get('SHELL', '/bin/bash')
        os.chdir(instance_path)
        os.execv(shell, [shell])
    else:
        click.echo(instance_path)


@main.command(name='cd')
@click.argument('instance_id')
@click.option('--shell', 'open_shell', is_flag=True,
              help='Open a subshell in the instance directory.')
def cd_command(instance_id, open_shell):
    """Prints instance path (use with: cd \"$(mh cd <instance_id>)\")."""
    _go_to_instance_dir(instance_id, open_shell=open_shell)


@main.command(name='chdir', hidden=True)
@click.argument('instance_id')
@click.option('--shell', 'open_shell', is_flag=True)
def chdir_command(instance_id, open_shell):
    """Alias for 'mh cd'."""
    _go_to_instance_dir(instance_id, open_shell=open_shell)


# ---------- log ----------

@main.command()
@click.argument('instance_id')
@click.option('--lines', default=20, help='Number of lines to show.')
@click.option('--level', help='Filter by log level (e.g., ERROR, Warning).')
def log(instance_id, lines, level):
    """Shows the latest log entries for an instance."""
    instance = Instance(instance_id)
    instance._require_exists()
    for entry in instance.get_log_entries(num_lines=lines, level=level):
        click.echo(entry)


# ---------- var ----------

@main.command()
@click.argument('variable_name')
@click.pass_context
def var(ctx, variable_name):
    """Extracts a server variable from all running instances."""
    instances = Instance.get_all_instances()
    values = {inst.id: inst.get_variable(variable_name) for inst in instances}
    if ctx.obj.get('json'):
        click.echo(json.dumps({'variable': variable_name, 'values': values}))
        return
    if not values:
        click.echo("No instances found.")
        return
    click.echo(f"Variable '{variable_name}':")
    for inst_id, value in values.items():
        click.echo(f"  [{inst_id}] {value}")


# ---------- erase ----------

@main.command()
@click.argument('instance_id')
@click.option('--yes', is_flag=True, help='Skip the confirmation prompt.')
@click.option('--purge', is_flag=True,
              help='Delete data instead of moving to erased/.')
def erase(instance_id, yes, purge):
    """Removes a single instance (moves to erased/, or --purge to delete)."""
    instance = Instance(instance_id)
    instance._require_exists()

    if not yes:
        click.secho(f"WARNING: Instance {instance_id} will be erased!",
                    fg='red', bold=True)
        click.echo(f"  Path: {instance.path}")
        datadir = instance.path / 'data'
        if datadir.exists():
            total = sum(f.stat().st_size for f in datadir.rglob('*')
                        if f.is_file())
            click.echo(f"  Data size: {total / 1024 / 1024:.1f} MB")
        if not click.confirm("Are you sure?"):
            click.echo("Aborted.")
            return

    deployment.teardown_instance(instance_id, purge=purge)
    report.success(f"Instance {instance_id} erased.")


# ---------- show ----------

@main.group()
def show():
    """Lists available tarballs (local or remote)."""


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
        click.echo("Cloning repository...")
        result = subprocess.run(
            ['git', 'clone', '--depth=1', '--branch', branch, repo_url, tmpdir],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise click.ClickException(
                f"Failed to clone repository:\n{result.stderr}"
            )
        click.echo("Installing...")
        result = subprocess.run(
            ['pip', 'install', '--force-reinstall', '--no-deps', '.'],
            capture_output=True, text=True, cwd=tmpdir,
        )
        if result.returncode != 0:
            raise click.ClickException(f"Failed to install:\n{result.stderr}")

    click.secho("MyHarem updated successfully!", fg='green')
