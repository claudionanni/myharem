import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import click

from . import config
from . import manifest
from . import model
from . import report


def _now():
    return datetime.now(timezone.utc).isoformat()


def resolve_tarball(tarball_path):
    """Resolves a tarball path, searching the local directory as fallback."""
    path = Path(tarball_path)
    if path.exists():
        return path

    local_path = config.get_basedir() / 'local' / path.name
    if local_path.exists():
        report.log(f"Found tarball in local: {local_path}")
        return local_path

    raise click.ClickException(
        f"Tarball not found: {tarball_path}\n"
        f"Also checked: {local_path}"
    )


def deploy_instance(tarball_path, instance_id, init_db=True):
    """Deploys a new MariaDB instance (low-level primitive).

    Returns the path to the new instance directory (Path object).
    """
    t0 = time.time()
    tarball_path = resolve_tarball(tarball_path)

    basedir = config.get_basedir()
    instances_dir = basedir / 'instances'

    dirname = tarball_path.name.replace('.tar.gz', '')
    instance_name = f"{dirname}.{instance_id}"
    instance_path = instances_dir / instance_name

    report.log(f"[1/4] Creating instance directory: {instance_name}")
    instance_path.mkdir(parents=True, exist_ok=True)

    report.log(f"[2/4] Extracting {tarball_path.name}...")
    t1 = time.time()
    process = subprocess.run(
        ['tar', '-zxf', str(tarball_path), '--strip-components=1',
         '-C', str(instance_path)],
        capture_output=True, text=True,
    )
    if process.returncode != 0:
        raise click.ClickException(
            f"Failed to extract tarball:\n{process.stderr}"
        )
    report.log(f"       Extracted in {time.time() - t1:.1f}s")

    if init_db:
        report.log("[3/4] Generating my.cnf...")
        _generate_my_cnf(instance_id, instance_path)
        report.log("[4/4] Initializing database...")
        initialize_database(instance_path)
    else:
        report.log("[3/4] Skipping my.cnf (custom config will be applied)")
        report.log("[4/4] Skipping database init (done after config)")

    config.chown_instance(instance_path)
    report.log(f"  Instance {instance_id} deployed in {time.time() - t0:.1f}s")
    return instance_path


def deploy_single(tarball_path, instance_id):
    """Deploys one standalone instance and records it in the manifest.

    Returns a DeploymentResult.
    """
    instance_path = deploy_instance(tarball_path, str(instance_id), init_db=True)
    result = model.DeploymentResult(
        topology='single',
        cluster_id=str(instance_id),
        tarball=resolve_tarball(tarball_path).name,
        nodes=[model.NodeInfo(
            id=str(instance_id),
            role='single',
            port=int(instance_id),
            socket=f"/tmp/mh-{instance_id}.sock",
            datadir=str(Path(instance_path) / 'data'),
            path=str(instance_path),
        )],
        created_at=_now(),
    )
    manifest.record(result)
    report.success(f"Instance {instance_id} deployed.")
    return result


def rollback_instances(instance_ids):
    """Best-effort teardown of partially-deployed instances (on deploy failure)."""
    for instance_id in instance_ids:
        try:
            teardown_instance(instance_id, purge=True)
        except Exception:
            pass


def teardown_instance(instance_id, purge=False):
    """Stops an instance and removes it.

    purge=True deletes the directory; otherwise it is moved to `erased/`.
    """
    from .instance import Instance

    inst = Instance(str(instance_id))
    if not inst.exists() or not inst.path:
        return
    try:
        if inst.is_running():
            inst.stop()
    except Exception:
        pass

    if purge:
        shutil.rmtree(inst.path, ignore_errors=True)
    else:
        erased = config.get_basedir() / 'erased' / inst.path.name
        erased.parent.mkdir(parents=True, exist_ok=True)
        if erased.exists():
            shutil.rmtree(erased, ignore_errors=True)
        shutil.move(str(inst.path), str(erased))


def _generate_my_cnf(instance_id, instance_path, extra_config=None):
    """Generates a my.cnf for a standalone instance."""
    dbuser = config.get_dbuser()
    instance_path = Path(instance_path)
    my_cnf_path = instance_path / 'my.cnf'

    # Keep socket path short to avoid unix_socket path truncation limits.
    socket_path = Path("/tmp") / f"mh-{instance_id}.sock"

    lines = [
        "[mariadbd]",
        f"port={instance_id}",
        f"socket={socket_path}",
        f"pid-file={instance_path / f'{instance_id}.pid'}",
        f"basedir={instance_path}",
        f"datadir={instance_path / 'data'}",
        f"server_id={instance_id}",
        f"user={dbuser}",
        "innodb_file_per_table",
        "log_bin",
        f"log_error={instance_path / f'error.{instance_id}.log'}",
        "binlog_format=ROW",
    ]

    if extra_config:
        lines.append("")
        for key, value in extra_config.items():
            if value is True:
                lines.append(key)
            else:
                lines.append(f"{key}={value}")

    my_cnf_path.write_text('\n'.join(lines) + '\n')
    report.log(f"Generated my.cnf at {my_cnf_path}")


def initialize_database(instance_path):
    """Initializes the MariaDB data directory using mariadb-install-db."""
    instance_path = Path(instance_path)
    install_db_script = instance_path / 'scripts' / 'mariadb-install-db'
    my_cnf_path = instance_path / 'my.cnf'
    datadir = instance_path / 'data'

    if not install_db_script.exists():
        raise click.ClickException(
            f"mariadb-install-db not found at {install_db_script}"
        )

    report.log("Initializing the database...")
    cmd = [
        str(install_db_script),
        f"--defaults-file={my_cnf_path}",
        f"--basedir={instance_path}",
        f"--datadir={datadir}",
    ]

    process = subprocess.run(cmd, capture_output=True, text=True)

    if process.returncode != 0:
        raise click.ClickException(
            f"Error initializing the database:\n{process.stderr}"
        )

    report.success("Database initialized successfully.")
    if process.stdout.strip():
        report.log(process.stdout)


ADMIN_USER = 'myharem'
REPL_USER = 'mh_repl'
SST_USER = 'mh_sst'
# SST password: overridable via config/env; default kept for backward compat.
SST_PASSWORD = config.get_sst_password()


def _create_users_sql():
    return (
        f"CREATE USER IF NOT EXISTS '{ADMIN_USER}'@'localhost';\n"
        f"ALTER USER '{ADMIN_USER}'@'localhost' IDENTIFIED BY '';\n"
        f"GRANT ALL PRIVILEGES ON *.* TO '{ADMIN_USER}'@'localhost' "
        f"WITH GRANT OPTION;\n"
        f"CREATE USER IF NOT EXISTS '{REPL_USER}'@'localhost';\n"
        f"ALTER USER '{REPL_USER}'@'localhost' IDENTIFIED BY '';\n"
        f"GRANT REPLICATION SLAVE ON *.* TO '{REPL_USER}'@'localhost';\n"
        f"CREATE USER IF NOT EXISTS '{SST_USER}'@'localhost' "
        f"IDENTIFIED BY '{SST_PASSWORD}';\n"
        f"ALTER USER '{SST_USER}'@'localhost' IDENTIFIED BY '{SST_PASSWORD}';\n"
        f"GRANT RELOAD, PROCESS, LOCK TABLES, REPLICATION CLIENT "
        f"ON *.* TO '{SST_USER}'@'localhost';\n"
        f"FLUSH PRIVILEGES;\n"
    )


# Extra grants for MariaDB 10.11+ (separated from ALL PRIVILEGES).
ADMIN_EXTRA_GRANTS = (
    f"GRANT SUPER, SHUTDOWN, REPLICATION SLAVE ADMIN "
    f"ON *.* TO '{ADMIN_USER}'@'localhost';"
)


def create_service_users(instance, retries=5):
    """Creates service users on a running instance by connecting as root.

    Idempotent — safe to run repeatedly; always re-applies grants.
    """
    mariadb = instance.path / 'bin' / 'mariadb'
    if not mariadb.exists():
        mariadb = instance.path / 'bin' / 'mysql'

    report.log("Ensuring service users/grants (myharem, mh_repl, mh_sst)...")

    cmd = [
        str(mariadb), '-uroot',
        f'--socket={instance.socket_path}',
        '-B', '-e', _create_users_sql(),
    ]
    for attempt in range(retries):
        process = subprocess.run(cmd, capture_output=True, text=True)
        if process.returncode == 0:
            break
        if attempt < retries - 1:
            time.sleep(2)
    else:
        output = (process.stderr or process.stdout or '(no output)').strip()
        report.warn(f"Warning: user creation failed:\n{output}")
        return

    cmd_extra = [
        str(mariadb), '-uroot',
        f'--socket={instance.socket_path}',
        '-B', '-e', ADMIN_EXTRA_GRANTS,
    ]
    subprocess.run(cmd_extra, capture_output=True, text=True)
    # Ignore errors — older versions don't have these privilege names.

    report.success("Service users/grants ensured.")
