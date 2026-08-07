import time

import click

from . import deployment
from . import manifest
from . import report
from .instance import Instance


def _is_galera_instance(instance):
    """Checks if an instance has Galera (wsrep) enabled in my.cnf."""
    try:
        content = instance.my_cnf_path.read_text()
        return 'wsrep_on' in content
    except Exception:
        return False


def start_instance(instance_id, bootstrap=False):
    """Starts a MariaDB instance. Raises on start failure/timeout."""
    instance = Instance(instance_id)
    instance._require_exists()
    instance.start(wsrep_new_cluster=bootstrap)

    is_galera = _is_galera_instance(instance)
    is_galera_joiner = is_galera and not bootstrap

    if is_galera_joiner:
        # Galera joiners receive users via SST from the donor node.
        report.log("Galera joiner — waiting for SST and sync...", nl=False)
        for _ in range(300):  # Up to 5 min for SST
            time.sleep(1)
            report.log(".", nl=False)
            if instance.is_socket_ready():
                time.sleep(2)
                report.log(" OK", fg='green')
                report.log("Users received via SST from donor node.")
                return
        report.log("")
        raise click.ClickException(
            f"Galera joiner {instance_id} did not sync within 5 min. "
            f"Check log: mh log {instance_id}"
        )
    else:
        # Bootstrap node or non-Galera: create users after start.
        report.log(f"Waiting for instance {instance_id} to be ready...", nl=False)
        for _ in range(60):
            time.sleep(1)
            report.log(".", nl=False)
            if instance.is_socket_ready():
                time.sleep(2)
                report.log(" OK", fg='green')
                deployment.create_service_users(instance)
                return
        report.log("")
        raise click.ClickException(
            f"Instance {instance_id} did not start within 60s. "
            f"Check log: mh log {instance_id}"
        )


def stop_instance(instance_id):
    """Stops a MariaDB instance."""
    instance = Instance(instance_id)
    instance._require_exists()
    if instance.is_socket_ready():
        deployment.create_service_users(instance, retries=1)
    instance.stop()


def scli_instance(instance_id):
    instance = Instance(instance_id)
    instance._require_exists()
    instance.scli()


def cli_instance(instance_id):
    instance = Instance(instance_id)
    instance._require_exists()
    instance.cli()


# ---------- cluster-level lifecycle (manifest-driven) ----------

def _cluster_node_ids(cluster_id):
    """Returns node ids for a manifest-recorded deployment, bootstrap node first.

    For Galera the first node is the bootstrap/donor; for replication the master
    is first. Raises if the cluster is unknown.
    """
    entry = manifest.get(cluster_id)
    if not entry:
        raise click.ClickException(
            f"No deployment '{cluster_id}' in the manifest. "
            f"(Deploy via mh deploygalera/deployreplication, or list with mh list.)"
        )
    ids = [n['id'] for n in entry.get('nodes', [])]
    return entry.get('topology'), ids


def start_cluster(cluster_id):
    """Starts every node of a deployment in the correct order."""
    topology, ids = _cluster_node_ids(cluster_id)
    if not ids:
        raise click.ClickException(f"Deployment '{cluster_id}' has no nodes.")
    if topology == 'galera':
        report.log(f"Bootstrapping Galera node {ids[0]}...")
        start_instance(ids[0], bootstrap=True)
        for node_id in ids[1:]:
            start_instance(node_id, bootstrap=False)
    else:
        for node_id in ids:  # master first, then slaves
            start_instance(node_id, bootstrap=False)
    report.success(f"Deployment '{cluster_id}' started ({len(ids)} nodes).")


def stop_cluster(cluster_id):
    """Stops every node of a deployment (reverse order)."""
    _topology, ids = _cluster_node_ids(cluster_id)
    for node_id in reversed(ids):
        try:
            stop_instance(node_id)
        except click.ClickException as exc:
            report.warn(str(exc))
    report.success(f"Deployment '{cluster_id}' stopped.")


def erase_cluster(cluster_id, purge=False):
    """Stops and removes every node of a deployment, then drops the manifest entry."""
    _topology, ids = _cluster_node_ids(cluster_id)
    for node_id in ids:
        deployment.teardown_instance(node_id, purge=purge)
    manifest.remove(cluster_id)
    report.success(f"Deployment '{cluster_id}' erased ({len(ids)} nodes).")
