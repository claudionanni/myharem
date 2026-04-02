import time

import click

from . import config
from . import deployment
from .instance import Instance


REPL_STEP = 10000
WAIT_TIMEOUT = 30
WAIT_INTERVAL = 1


def deploy_replication(tarball_path, master_instance_id):
    """Deploys a master/slave async replication pair using GTID.

    Deploys two instances, starts them, and configures GTID-based
    replication automatically.

    Args:
        tarball_path: Path to the MariaDB tarball.
        master_instance_id: The ID for the master instance.
            The slave will be master_id + 10000.
    """
    master_id = int(master_instance_id)
    slave_id = master_id + REPL_STEP

    click.echo(f"Deploying replication: master={master_id}, slave={slave_id}")

    # --- Deploy master ---
    click.secho("\n=== Deploying Master ===", fg='cyan', bold=True)
    master_path = deployment.deploy_instance(
        tarball_path, str(master_id), init_db=False
    )
    if not master_path:
        raise click.ClickException("Failed to deploy master instance")

    master_extra = {
        "log_slave_updates": True,
    }
    deployment._generate_my_cnf(str(master_id), master_path,
                                extra_config=master_extra)
    deployment.initialize_database(master_path)

    # --- Deploy slave ---
    click.secho("\n=== Deploying Slave ===", fg='cyan', bold=True)
    slave_path = deployment.deploy_instance(
        tarball_path, str(slave_id), init_db=False
    )
    if not slave_path:
        raise click.ClickException("Failed to deploy slave instance")

    slave_extra = {
        "log_slave_updates": True,
        "read_only": "ON",
        "relay_log": f"relay-bin.{slave_id}",
    }
    deployment._generate_my_cnf(str(slave_id), slave_path,
                                extra_config=slave_extra)
    deployment.initialize_database(slave_path)

    # --- Start both instances ---
    click.secho("\n=== Starting Instances ===", fg='cyan', bold=True)

    master = Instance(str(master_id))
    slave = Instance(str(slave_id))

    master.start()
    _wait_for_instance(master)
    deployment.create_service_users(master)

    slave.start()
    _wait_for_instance(slave)
    deployment.create_service_users(slave)

    # --- Configure replication ---
    click.secho("\n=== Configuring GTID Replication ===", fg='cyan', bold=True)

    change_master_sql = (
        "CHANGE MASTER TO "
        "MASTER_HOST='127.0.0.1', "
        f"MASTER_PORT={master_id}, "
        f"MASTER_USER='{deployment.REPL_USER}', "
        "MASTER_USE_GTID=slave_pos"
    )

    click.echo(f"Running on slave: {change_master_sql}")
    slave.run_sql(change_master_sql)

    click.echo("Starting slave...")
    slave.run_sql("START SLAVE")

    # --- Verify replication ---
    click.secho("\n=== Verifying Replication ===", fg='cyan', bold=True)
    time.sleep(2)

    output = slave.run_sql("SHOW SLAVE STATUS\\G")
    _print_replication_status(output)

    click.secho("\nReplication pair deployed successfully!", fg='green',
                bold=True)
    click.echo(f"  Master: {master_id}")
    click.echo(f"  Slave:  {slave_id}")
    click.echo(f"\nConnect to master: mh scli {master_id}")
    click.echo(f"Connect to slave:  mh scli {slave_id}")


def _wait_for_instance(instance, timeout=WAIT_TIMEOUT):
    """Waits for an instance to become ready.

    Args:
        instance: Instance object to wait for.
        timeout: Maximum seconds to wait.
    """
    click.echo(f"Waiting for instance {instance.id} to be ready...", nl=False)
    elapsed = 0
    while elapsed < timeout:
        if instance.is_running():
            click.secho(" OK", fg='green')
            return
        click.echo(".", nl=False)
        time.sleep(WAIT_INTERVAL)
        elapsed += WAIT_INTERVAL

    click.echo()
    raise click.ClickException(
        f"Instance {instance.id} did not start within {timeout}s. "
        f"Check log: mh log {instance.id}"
    )


def _print_replication_status(show_slave_output):
    """Parses and prints key fields from SHOW SLAVE STATUS output."""
    fields_of_interest = [
        'Slave_IO_Running',
        'Slave_SQL_Running',
        'Master_Host',
        'Master_Port',
        'Using_Gtid',
        'Gtid_IO_Pos',
        'Last_Error',
        'Seconds_Behind_Master',
    ]

    for line in show_slave_output.splitlines():
        line = line.strip()
        for field in fields_of_interest:
            if line.startswith(f"{field}:"):
                value = line.split(':', 1)[1].strip()
                if field == 'Slave_IO_Running' and value == 'Yes':
                    click.secho(f"  {field}: {value}", fg='green')
                elif field == 'Slave_SQL_Running' and value == 'Yes':
                    click.secho(f"  {field}: {value}", fg='green')
                elif field == 'Last_Error' and value:
                    click.secho(f"  {field}: {value}", fg='red')
                else:
                    click.echo(f"  {field}: {value}")
                break
