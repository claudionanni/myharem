import os
from pathlib import Path

import click

from . import config
from . import deployment


INST_STEP = 10000
WSREP_STEP = 1000
SST_STEP = 2000


def deploy_cluster(tarball_path, first_instance_id):
    """Deploys a 3-node Galera cluster.

    Args:
        tarball_path: The path to the MariaDB tarball.
        first_instance_id: The ID for the first node in the cluster.
    """
    first_instance_id = int(first_instance_id)
    node_ids = [
        first_instance_id,
        first_instance_id + INST_STEP,
        first_instance_id + INST_STEP * 2,
    ]

    # Build wsrep_cluster_address with all 3 wsrep ports
    wsrep_ports = [str(nid + WSREP_STEP) for nid in node_ids]
    cluster_address = "gcomm://" + ",".join(
        f"127.0.0.1:{p}" for p in wsrep_ports
    )

    for i, node_id in enumerate(node_ids):
        click.echo(f"Deploying Galera node {i+1} with id {node_id}...")
        instance_path = deployment.deploy_instance(
            tarball_path, str(node_id), init_db=False
        )

        if not instance_path:
            raise click.ClickException(f"Failed to deploy node {i+1}")

        is_bootstrap_node = (i == 0)
        _generate_galera_my_cnf(
            instance_path, str(node_id), cluster_address, is_bootstrap_node
        )

        deployment.initialize_database(instance_path)
        deployment.create_admin_user(instance_path)

    click.secho("Galera cluster deployed successfully.", fg='green')
    click.echo(f"Node IDs: {', '.join(str(n) for n in node_ids)}")
    click.echo(
        "Start the bootstrap node first with: "
        f"mh service start {node_ids[0]}"
    )


def _find_galera_lib(instance_path):
    """Auto-detects the Galera provider library path.

    Searches for libgalera_smm.so or libgalera_enterprise_smm.so
    in common locations within the instance.
    """
    instance_path = Path(instance_path)
    candidates = [
        instance_path / 'lib' / 'galera' / 'libgalera_smm.so',
        instance_path / 'lib' / 'galera' / 'libgalera_enterprise_smm.so',
        instance_path / 'lib' / 'libgalera_smm.so',
        instance_path / 'lib' / 'libgalera_enterprise_smm.so',
    ]
    for path in candidates:
        if path.exists():
            return path
    # Fallback — return the most common path even if not found yet
    return instance_path / 'lib' / 'galera' / 'libgalera_smm.so'


def _generate_galera_my_cnf(instance_path, instance_id, cluster_address,
                             is_bootstrap_node):
    """Generates a Galera-specific my.cnf for a cluster node."""
    instance_path = Path(instance_path)
    wsrep_port = int(instance_id) + WSREP_STEP
    sst_port = int(instance_id) + SST_STEP

    final_cluster_address = "gcomm://" if is_bootstrap_node else cluster_address
    galera_lib = _find_galera_lib(instance_path)

    galera_config = {
        "default_storage_engine": "InnoDB",
        "innodb_autoinc_lock_mode": "2",
        "log_slave_updates": True,
        # Galera settings
        "wsrep_on": "ON",
        "wsrep_provider": str(galera_lib),
        "wsrep_cluster_name": "myharem_cluster",
        "wsrep_cluster_address": final_cluster_address,
        "wsrep_node_name": f"NODE_{instance_id}",
        "wsrep_node_address": f"127.0.0.1:{wsrep_port}",
        "wsrep_provider_options":
            f"gcs.fc_limit=16;gmcast.listen_addr=tcp://127.0.0.1:{wsrep_port}",
        "wsrep_sst_receive_address": f"127.0.0.1:{sst_port}",
        "wsrep_sst_method": "rsync",
    }

    deployment._generate_my_cnf(instance_id, instance_path,
                                extra_config=galera_config)
    click.echo(f"Generated Galera my.cnf for node {instance_id}")
