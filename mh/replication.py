import time
from datetime import datetime, timezone
from pathlib import Path

import click

from . import deployment
from . import galera
from . import manifest
from . import model
from . import report
from .instance import Instance


# Kept numerically identical to galera.INST_STEP for a single mental model
# across topologies -- a replication slave only needs 1 port today, but the
# same per-node budget leaves room for a future per-slave port without
# another migration.
REPL_STEP = galera.INST_STEP
WAIT_TIMEOUT = 30
WAIT_INTERVAL = 1


def _now():
    return datetime.now(timezone.utc).isoformat()


def _ensure_service_users(instance, repl_host='localhost'):
    """create_service_users(), raising clearly on failure instead of letting
    a replication wire-up proceed with a REPL_USER that doesn't exist — that
    would otherwise surface much later as a confusing CHANGE MASTER/START
    SLAVE authentication failure instead of a clear one right here."""
    if not deployment.create_service_users(instance, repl_host=repl_host):
        raise click.ClickException(
            f"Instance {instance.id} started, but its service users/grants "
            f"could not be created. Check log: mh log {instance.id}"
        )


def _relay_log_config(instance_path, slave_id):
    """Relay-log settings anchored to absolute paths in the instance's own
    datadir. A bare relative basename (e.g. `relay-bin.20000`) leaves
    `relay_log_index`'s location to server defaults, which don't reliably
    resolve relative to the instance's datadir across builds/versions —
    observed failing with `ERROR 29 ... File './relay-bin.index' not found`
    at START SLAVE time (the relay log files are only created lazily then,
    not at server startup, so the bad path goes unnoticed until that point)."""
    relay_base = Path(instance_path) / "data" / f"relay-bin.{slave_id}"
    return {
        "relay_log": str(relay_base),
        "relay_log_index": f"{relay_base}.index",
    }


def compute_slave_ids(master_instance_id, slaves):
    """Returns the list of slave ids for a master + N slaves (pure).

    Raises if the highest derived id would exceed the valid TCP port range.
    """
    master = int(master_instance_id)
    ids = [master + i * REPL_STEP for i in range(1, int(slaves) + 1)]
    if ids and ids[-1] > galera.MAX_PORT:
        raise click.ClickException(
            f"Topology too large: highest derived port {ids[-1]} exceeds "
            f"{galera.MAX_PORT}. Use a lower master instance id or fewer slaves."
        )
    return ids


def deploy_replication(tarball_path, master_instance_id, slaves=1):
    """Deploys a master + N async (GTID) slaves on a single host.

    Slaves are placed at master_id + i*10 (i = 1..N). Deploys and starts all
    instances, wires GTID replication on each slave, records a DeploymentResult
    in the manifest, and rolls back on failure.
    """
    master_id = int(master_instance_id)
    slaves = int(slaves)
    if slaves < 1:
        raise click.ClickException("Replication needs at least one slave.")

    slave_ids = compute_slave_ids(master_id, slaves)
    report.log(
        f"Deploying replication: master={master_id}, slaves={slave_ids}"
    )

    tarball_name = deployment.resolve_tarball(tarball_path).name
    deployed = []
    node_infos = []
    try:
        # --- Master ---
        report.log("=== Deploying Master ===")
        master_path = deployment.deploy_instance(
            tarball_path, str(master_id), init_db=False
        )
        deployed.append(str(master_id))
        deployment._generate_my_cnf(
            str(master_id), master_path, extra_config={"log_slave_updates": True}
        )
        deployment.initialize_database(master_path)
        node_infos.append(_node_info(master_id, "master", master_path))

        # --- Slaves ---
        slave_paths = {}
        for slave_id in slave_ids:
            report.log(f"=== Deploying Slave {slave_id} ===")
            slave_path = deployment.deploy_instance(
                tarball_path, str(slave_id), init_db=False
            )
            deployed.append(str(slave_id))
            deployment._generate_my_cnf(str(slave_id), slave_path, extra_config={
                "log_slave_updates": True,
                "read_only": "ON",
                **_relay_log_config(slave_path, slave_id),
            })
            deployment.initialize_database(slave_path)
            slave_paths[slave_id] = slave_path
            node_infos.append(_node_info(slave_id, "slave", slave_path))

        # --- Start + wire ---
        report.log("=== Starting Master ===")
        master = Instance(str(master_id))
        master.start()
        _wait_for_instance(master)
        _ensure_service_users(master)

        change_master_sql = (
            "CHANGE MASTER TO "
            "MASTER_HOST='127.0.0.1', "
            f"MASTER_PORT={master_id}, "
            f"MASTER_USER='{deployment.REPL_USER}', "
            "MASTER_USE_GTID=slave_pos"
        )
        for slave_id in slave_ids:
            report.log(f"=== Starting + wiring Slave {slave_id} ===")
            slave = Instance(str(slave_id))
            slave.start()
            _wait_for_instance(slave)
            _ensure_service_users(slave)
            slave.run_sql(change_master_sql)
            slave.run_sql("START SLAVE")
    except Exception:
        report.error("Replication deploy failed — rolling back.")
        deployment.rollback_instances(deployed)
        raise

    result = model.DeploymentResult(
        topology="replication",
        cluster_id=str(master_id),
        tarball=tarball_name,
        nodes=node_infos,
        created_at=_now(),
    )
    manifest.record(result)

    report.success(
        f"Replication deployed: master {master_id} + {slaves} slave(s)."
    )
    report.log(f"Start/stop the whole set with:  sudo mh cluster start {master_id}")
    return result


def deploy_master(tarball_path, master_instance_id, advertise="127.0.0.1"):
    """Deploys + starts a replication master on THIS host (distributed).

    Grants the replication user for remote slaves (`@'%'`) when advertising a
    real IP. Records a single-node manifest entry.
    """
    master_id = int(master_instance_id)
    tarball_name = deployment.resolve_tarball(tarball_path).name
    deployed = []
    try:
        master_path = deployment.deploy_instance(
            tarball_path, str(master_id), init_db=False
        )
        deployed.append(str(master_id))
        deployment._generate_my_cnf(
            str(master_id), master_path, extra_config={"log_slave_updates": True}
        )
        deployment.initialize_database(master_path)
        master = Instance(str(master_id))
        master.start()
        _wait_for_instance(master)
        repl_host = "localhost" if advertise == "127.0.0.1" else "%"
        _ensure_service_users(master, repl_host=repl_host)
    except Exception:
        report.error("Master deploy failed — rolling back.")
        deployment.rollback_instances(deployed)
        raise

    result = model.DeploymentResult(
        topology="replication", cluster_id=str(master_id), tarball=tarball_name,
        nodes=[_node_info(master_id, "master", master_path)], created_at=_now(),
    )
    manifest.record(result)
    report.success(f"Replication master {master_id} deployed (advertise {advertise}).")
    return result


def deploy_slave(tarball_path, slave_instance_id, master_host, master_port,
                 advertise="127.0.0.1"):
    """Deploys + starts a replication slave on THIS host and wires GTID
    replication to a (possibly remote) master."""
    slave_id = int(slave_instance_id)
    master_port = int(master_port)
    tarball_name = deployment.resolve_tarball(tarball_path).name
    deployed = []
    try:
        slave_path = deployment.deploy_instance(
            tarball_path, str(slave_id), init_db=False
        )
        deployed.append(str(slave_id))
        deployment._generate_my_cnf(str(slave_id), slave_path, extra_config={
            "log_slave_updates": True,
            "read_only": "ON",
            **_relay_log_config(slave_path, slave_id),
        })
        deployment.initialize_database(slave_path)
        slave = Instance(str(slave_id))
        slave.start()
        _wait_for_instance(slave)
        repl_host = "localhost" if advertise == "127.0.0.1" else "%"
        _ensure_service_users(slave, repl_host=repl_host)
        slave.run_sql(
            "CHANGE MASTER TO "
            f"MASTER_HOST='{master_host}', "
            f"MASTER_PORT={master_port}, "
            f"MASTER_USER='{deployment.REPL_USER}', "
            "MASTER_USE_GTID=slave_pos"
        )
        slave.run_sql("START SLAVE")
    except Exception:
        report.error("Slave deploy failed — rolling back.")
        deployment.rollback_instances(deployed)
        raise

    result = model.DeploymentResult(
        topology="replication", cluster_id=str(slave_id), tarball=tarball_name,
        nodes=[_node_info(slave_id, "slave", slave_path)], created_at=_now(),
    )
    manifest.record(result)
    report.success(
        f"Replication slave {slave_id} wired to {master_host}:{master_port}."
    )
    return result


def _node_info(instance_id, role, path):
    return model.NodeInfo(
        id=str(instance_id),
        role=role,
        port=int(instance_id),
        socket=f"/tmp/mh-{instance_id}.sock",
        datadir=str(Path(path) / "data"),
        path=str(path),
    )


def _wait_for_instance(instance, timeout=WAIT_TIMEOUT):
    """Waits for an instance to actually accept connections; raises on timeout.

    is_socket_ready() only proves the socket file exists, which can be true
    well before mariadbd is actually ready to answer a query (observed in
    production) — is_accepting_connections() closes that race by really
    connecting instead of trusting a proxy signal.
    """
    report.log(f"Waiting for instance {instance.id} to be ready...", nl=False)
    elapsed = 0
    while elapsed < timeout:
        if instance.is_accepting_connections():
            report.log(" OK", fg="green")
            return
        report.log(".", nl=False)
        time.sleep(WAIT_INTERVAL)
        elapsed += WAIT_INTERVAL

    report.log("")
    raise click.ClickException(
        f"Instance {instance.id} did not start within {timeout}s. "
        f"Check log: mh log {instance.id}"
    )
