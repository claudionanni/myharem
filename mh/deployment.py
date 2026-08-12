import shutil
import subprocess
import time
import urllib.request
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


def _download(url, dest, timeout=300):
    """Streams `url` to `dest`. Separated from fetch_tarball so tests can
    monkeypatch the network call without touching filesystem/idempotency logic."""
    with urllib.request.urlopen(url, timeout=timeout) as response, \
            open(dest, 'wb') as out:
        shutil.copyfileobj(response, out)


def fetch_tarball(url, filename=None):
    """Downloads a tarball into <basedir>/local/ if not already staged there.

    Idempotent: a pre-existing file with the same name is left untouched and
    the download is skipped entirely (no re-fetch, no partial-overwrite risk).
    `filename` overrides the name to save as — needed when `url` doesn't end
    in the real filename (e.g. a presigned URL with a query string).

    Returns the local Path.
    """
    name = filename or Path(url.split('?', 1)[0]).name
    if not name:
        raise click.ClickException(f"Could not determine a filename from URL: {url}")

    local_dir = config.get_basedir() / 'local'
    local_dir.mkdir(parents=True, exist_ok=True)
    dest = local_dir / name

    if dest.exists():
        report.log(f"Tarball already staged: {dest}")
        return dest

    report.log(f"Fetching tarball from {url} ...")
    tmp_dest = dest.with_suffix(dest.suffix + '.part')
    try:
        _download(url, tmp_dest)
        tmp_dest.rename(dest)
    except Exception as exc:
        tmp_dest.unlink(missing_ok=True)
        raise click.ClickException(f"Failed to fetch tarball from {url}: {exc}")

    report.success(f"Tarball staged at {dest}")
    return dest


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

    # Ids map 1:1 to ports/sockets, so refuse to create a second instance with
    # an id already claimed by another directory (e.g. a different version) —
    # that would make id resolution ambiguous and start the wrong instance.
    if instances_dir.exists():
        clashing = sorted(
            p.name for p in instances_dir.iterdir()
            if p.name.endswith(f".{instance_id}") and p.name != instance_name
        )
        if clashing:
            raise click.ClickException(
                f"Instance id {instance_id} is already in use by: "
                f"{', '.join(clashing)}.\n"
                f"Erase it first (mh erase {instance_id} --purge) or choose "
                f"another id — ids must be unique."
            )

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
    # is_running()/stop() authenticate as the service admin user, which a
    # partially-failed deploy may never have created — in that case
    # is_running() falsely reports "not running" even though mariadbd is
    # very much alive, stop() above never fires, and deleting the directory
    # next would orphan a live process holding deleted files open (hit in
    # production). find_pid()/terminate() are PID-based, not DB-auth-based,
    # so they catch exactly that case as a safety net regardless of whether
    # the graceful stop above actually worked.
    inst.terminate()

    if purge:
        try:
            shutil.rmtree(inst.path)
        except OSError as exc:
            raise click.ClickException(
                f"Could not remove {inst.path}: {exc}\n"
                f"(Instance files are owned by the db user — run with sudo?)"
            )
        if inst.path.exists():
            raise click.ClickException(
                f"Purge removed nothing — {inst.path} still exists "
                f"(check permissions; run with sudo?)."
            )
    else:
        erased = config.get_basedir() / 'erased' / inst.path.name
        erased.parent.mkdir(parents=True, exist_ok=True)
        if erased.exists():
            shutil.rmtree(erased, ignore_errors=True)
        try:
            shutil.move(str(inst.path), str(erased))
        except OSError as exc:
            raise click.ClickException(
                f"Could not move {inst.path} to {erased}: {exc}\n"
                f"(Run with sudo?)"
            )


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
# Credentials: overridable via config/env; defaults kept for backward compat.
SST_PASSWORD = config.get_sst_password()
ADMIN_PASSWORD = config.get_admin_password()


def _create_users_sql(repl_host='localhost'):
    """Service-user SQL. `repl_host` is the host the async-replication user is
    granted for — 'localhost' single-host, '%' (or a peer CIDR) for a slave that
    connects to the master over the network. Admin and SST stay local (SST via
    mariabackup authenticates on the donor locally)."""
    return (
        f"CREATE USER IF NOT EXISTS '{ADMIN_USER}'@'localhost';\n"
        f"ALTER USER '{ADMIN_USER}'@'localhost' IDENTIFIED BY '{ADMIN_PASSWORD}';\n"
        f"GRANT ALL PRIVILEGES ON *.* TO '{ADMIN_USER}'@'localhost' "
        f"WITH GRANT OPTION;\n"
        f"CREATE USER IF NOT EXISTS '{REPL_USER}'@'{repl_host}';\n"
        f"ALTER USER '{REPL_USER}'@'{repl_host}' IDENTIFIED BY '';\n"
        f"GRANT REPLICATION SLAVE ON *.* TO '{REPL_USER}'@'{repl_host}';\n"
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


def create_service_users(instance, retries=5, repl_host='localhost'):
    """Creates service users on a running instance by connecting as root.

    Idempotent — safe to run repeatedly; always re-applies grants. `repl_host`
    widens the async-replication user's grant host for cross-host slaves.

    Returns True on success, False if every attempt failed (callers that can
    tolerate proceeding without service users — e.g. a defensive re-assert
    before stop — may treat False as a warning; callers whose correctness
    depends on these users existing, like the initial deploy path, must
    raise instead of letting the deploy limp forward into a guaranteed
    failure further down the line).
    """
    mariadb = instance.path / 'bin' / 'mariadb'
    if not mariadb.exists():
        mariadb = instance.path / 'bin' / 'mysql'

    report.log("Ensuring service users/grants (myharem, mh_repl, mh_sst)...")

    cmd = [
        str(mariadb), '-uroot',
        f'--socket={instance.socket_path}',
        '-B', '-e', _create_users_sql(repl_host),
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
        return False

    cmd_extra = [
        str(mariadb), '-uroot',
        f'--socket={instance.socket_path}',
        '-B', '-e', ADMIN_EXTRA_GRANTS,
    ]
    subprocess.run(cmd_extra, capture_output=True, text=True)
    # Ignore errors — older versions don't have these privilege names.

    report.success("Service users/grants ensured.")
    return True
