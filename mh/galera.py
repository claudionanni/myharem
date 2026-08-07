from datetime import datetime, timezone
from pathlib import Path

import click

from . import deployment
from . import manifest
from . import model
from . import report


INST_STEP = 10000
WSREP_STEP = 1000
SST_STEP = 2000


def _now():
    return datetime.now(timezone.utc).isoformat()


def compute_node_ids(first_instance_id, nodes):
    """Returns the list of node ids for an N-node cluster (pure)."""
    first = int(first_instance_id)
    return [first + i * INST_STEP for i in range(int(nodes))]


def deploy_cluster(tarball_path, first_instance_id, nodes=3, wsrep_provider=None):
    """Deploys an N-node Galera cluster (default 3) on a single host.

    Nodes are placed at first_id, first_id+10000, first_id+20000, ... and share
    one gcomm:// address listing every node's wsrep port. Returns a
    DeploymentResult and records it in the manifest. Rolls back partial nodes on
    failure.

    `wsrep_provider` overrides the Galera provider library path for tarballs
    that don't bundle it (e.g. generic 'linux-x86_64' builds).
    """
    first = int(first_instance_id)
    nodes = int(nodes)
    if nodes < 1:
        raise click.ClickException("Galera cluster needs at least 1 node.")

    # Validate an explicit override up front, before extracting any node.
    if wsrep_provider and not Path(wsrep_provider).expanduser().exists():
        raise click.ClickException(
            f"--wsrep-provider path does not exist: {wsrep_provider}"
        )

    node_ids = compute_node_ids(first, nodes)
    wsrep_ports = [nid + WSREP_STEP for nid in node_ids]
    cluster_address = "gcomm://" + ",".join(
        f"127.0.0.1:{p}" for p in wsrep_ports
    )
    # Unique per-cluster name so multiple clusters can coexist on one host.
    cluster_name = f"mh_cluster_{first}"

    tarball_name = deployment.resolve_tarball(tarball_path).name
    deployed = []
    node_infos = []
    try:
        for i, node_id in enumerate(node_ids):
            report.log(f"Deploying Galera node {i + 1}/{nodes} (id {node_id})...")
            instance_path = deployment.deploy_instance(
                tarball_path, str(node_id), init_db=False
            )
            deployed.append(str(node_id))
            _generate_galera_my_cnf(
                instance_path, str(node_id), cluster_address, cluster_name,
                wsrep_provider=wsrep_provider,
            )
            deployment.initialize_database(instance_path)
            node_infos.append(model.NodeInfo(
                id=str(node_id),
                role="galera",
                port=node_id,
                socket=f"/tmp/mh-{node_id}.sock",
                datadir=str(Path(instance_path) / "data"),
                path=str(instance_path),
                wsrep_port=node_id + WSREP_STEP,
                sst_port=node_id + SST_STEP,
            ))
    except Exception:
        report.error("Galera deploy failed — rolling back partial nodes.")
        deployment.rollback_instances(deployed)
        raise

    result = model.DeploymentResult(
        topology="galera",
        cluster_id=str(first),
        tarball=tarball_name,
        nodes=node_infos,
        created_at=_now(),
    )
    manifest.record(result)

    report.success(f"Galera cluster '{cluster_name}' deployed ({nodes} nodes).")
    report.log(f"Node IDs: {', '.join(str(n) for n in node_ids)}")
    report.log(f"Start the whole cluster with:  sudo mh cluster start {first}")
    return result


# System-installed Galera providers, used only to *hint* in the error message
# (never adopted automatically — the provider version must match the server).
_SYSTEM_GALERA_HINTS = [
    '/usr/lib64/galera-4/libgalera_smm.so',
    '/usr/lib64/galera/libgalera_smm.so',
    '/usr/lib/galera-4/libgalera_smm.so',
    '/usr/lib/galera/libgalera_smm.so',
    '/usr/lib64/galera-enterprise-4/libgalera_enterprise_smm.so',
]


def _find_galera_lib(instance_path, override=None):
    """Resolves the Galera provider library path for an instance.

    With `override`, that path must exist (CLI/config/env escape hatch for
    tarballs that don't bundle Galera). Otherwise the instance's own lib/ is
    searched (CS and ES layouts). Raises ClickException with an actionable
    message if nothing is found — rather than writing a dead path that only
    fails cryptically when the server starts.
    """
    instance_path = Path(instance_path)

    if override:
        override_path = Path(override).expanduser()
        if not override_path.exists():
            raise click.ClickException(
                f"--wsrep-provider path does not exist: {override_path}"
            )
        return override_path

    candidates = [
        instance_path / 'lib' / 'galera' / 'libgalera_smm.so',
        instance_path / 'lib' / 'galera' / 'libgalera_enterprise_smm.so',
        instance_path / 'lib' / 'libgalera_smm.so',
        instance_path / 'lib' / 'libgalera_enterprise_smm.so',
    ]
    for path in candidates:
        if path.exists():
            return path

    found_system = [p for p in _SYSTEM_GALERA_HINTS if Path(p).exists()]
    hint = ""
    if found_system:
        hint = ("\nA system Galera provider was detected (its version must match "
                f"the server):\n  --wsrep-provider {found_system[0]}")
    raise click.ClickException(
        "No Galera provider (libgalera_smm.so) found in this tarball — searched "
        "lib/galera/ and lib/ for CS and ES names.\n"
        "This MariaDB build does not bundle Galera: generic 'linux-x86_64' "
        "tarballs omit it, while 'linux-systemd-x86_64' and Enterprise 'rhel-*' "
        "tarballs include it.\n"
        "Supply one with:  --wsrep-provider /path/to/libgalera_smm.so\n"
        "(or set 'wsrep_provider' in myharem.conf / MYHAREM_WSREP_PROVIDER)."
        + hint
    )


def _generate_galera_my_cnf(instance_path, instance_id, cluster_address,
                            cluster_name="myharem_cluster", wsrep_provider=None):
    """Generates a Galera-specific my.cnf for a cluster node."""
    instance_path = Path(instance_path)
    wsrep_port = int(instance_id) + WSREP_STEP
    sst_port = int(instance_id) + SST_STEP

    galera_lib = _find_galera_lib(instance_path, override=wsrep_provider)

    galera_config = {
        "default_storage_engine": "InnoDB",
        "innodb_autoinc_lock_mode": "2",
        "log_slave_updates": True,
        "wsrep_on": "ON",
        "wsrep_provider": str(galera_lib),
        "wsrep_cluster_name": cluster_name,
        "wsrep_cluster_address": cluster_address,
        "wsrep_node_name": f"NODE_{instance_id}",
        "wsrep_node_address": f"127.0.0.1:{wsrep_port}",
        "wsrep_provider_options":
            f"gcs.fc_limit=16;gmcast.listen_addr=tcp://127.0.0.1:{wsrep_port}",
        "wsrep_sst_receive_address": f"127.0.0.1:{sst_port}",
        "wsrep_sst_method": "mariabackup",
        "wsrep_sst_auth": f"{deployment.SST_USER}:{deployment.SST_PASSWORD}",
    }

    deployment._generate_my_cnf(instance_id, instance_path,
                                extra_config=galera_config)
    report.log(f"Generated Galera my.cnf for node {instance_id}")
