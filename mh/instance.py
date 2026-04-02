import os
import subprocess
from pathlib import Path

import click

from . import config
from .deployment import ADMIN_USER


class Instance:
    """Represents a MariaDB instance."""

    def __init__(self, instance_id):
        self.id = str(instance_id)
        self.path = self._find_path()

    def _find_path(self):
        """Finds the path to the instance directory."""
        instances_dir = config.get_basedir() / 'instances'
        if not instances_dir.exists():
            return None
        for name in os.listdir(instances_dir):
            if name.endswith(f".{self.id}"):
                return instances_dir / name
        return None

    def exists(self):
        """Checks if the instance exists."""
        return self.path is not None

    def _require_exists(self):
        """Raises ClickException if instance doesn't exist."""
        if not self.exists():
            raise click.ClickException(f"Instance {self.id} not found.")

    @property
    def my_cnf_path(self):
        return self.path / 'my.cnf'

    @property
    def socket_path(self):
        return self.path / f"{self.id}.sock"

    @property
    def log_path(self):
        return self.path / f"error.{self.id}.log"

    @property
    def mariadb_bin(self):
        return self.path / 'bin' / 'mariadb'

    @property
    def mariadb_admin_bin(self):
        return self.path / 'bin' / 'mariadb-admin'

    @property
    def mysqld_safe_bin(self):
        return self.path / 'bin' / 'mysqld_safe'

    def start(self):
        """Starts the MariaDB instance."""
        self._require_exists()

        if not self.mysqld_safe_bin.exists():
            raise click.ClickException(
                f"mysqld_safe not found at {self.mysqld_safe_bin}"
            )

        cmd = [str(self.mysqld_safe_bin), f"--defaults-file={self.my_cnf_path}"]
        subprocess.Popen(cmd, cwd=str(self.path),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        click.echo(f"Starting instance {self.id}...")

    def stop(self):
        """Stops the MariaDB instance."""
        self._require_exists()

        if not self.mariadb_admin_bin.exists():
            raise click.ClickException(
                f"mariadb-admin not found at {self.mariadb_admin_bin}"
            )

        cmd = [
            str(self.mariadb_admin_bin),
            f"-u{ADMIN_USER}",
            f"--socket={self.socket_path}",
            "shutdown",
        ]

        process = subprocess.run(cmd, capture_output=True, text=True)

        if process.returncode != 0:
            if "Can't connect" in process.stderr:
                click.secho(f"Instance {self.id} is not running.", fg='yellow')
            else:
                raise click.ClickException(
                    f"Error stopping instance {self.id}:\n{process.stderr}"
                )
        else:
            click.secho(f"Instance {self.id} stopped successfully.", fg='green')

    def scli(self):
        """Connects to the instance via socket using the mariadb client."""
        self._require_exists()

        if not self.mariadb_bin.exists():
            raise click.ClickException(
                f"mariadb client not found at {self.mariadb_bin}"
            )

        os.execv(
            str(self.mariadb_bin),
            [str(self.mariadb_bin), f"-u{ADMIN_USER}",
             f"--socket={self.socket_path}"],
        )

    def cli(self):
        """Connects to the instance via TCP using the mariadb client."""
        self._require_exists()

        if not self.mariadb_bin.exists():
            raise click.ClickException(
                f"mariadb client not found at {self.mariadb_bin}"
            )

        os.execv(
            str(self.mariadb_bin),
            [str(self.mariadb_bin), f"-u{ADMIN_USER}", "--host=127.0.0.1",
             f"--port={self.id}"],
        )

    def get_status(self):
        """Gets the status of the instance (Running or Stopped)."""
        if not self.mariadb_admin_bin.exists():
            return "Unknown (mariadb-admin not found)"

        cmd = [
            str(self.mariadb_admin_bin),
            f"-u{ADMIN_USER}",
            f"--socket={self.socket_path}",
            "ping",
        ]

        process = subprocess.run(
            cmd, capture_output=True, text=True, timeout=5
        )
        return "Running" if process.returncode == 0 else "Stopped"

    def is_running(self):
        return self.get_status() == "Running"

    def run_sql(self, sql, timeout=10):
        """Executes a SQL statement and returns the output.

        Args:
            sql: SQL statement to execute.
            timeout: Command timeout in seconds.

        Returns:
            The stdout output of the command.
        """
        self._require_exists()

        if not self.mariadb_bin.exists():
            raise click.ClickException(
                f"mariadb client not found at {self.mariadb_bin}"
            )

        cmd = [
            str(self.mariadb_bin),
            f"-u{ADMIN_USER}",
            f"--socket={self.socket_path}",
            "-B",
            "-e", sql,
        ]

        process = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )

        if process.returncode != 0:
            raise click.ClickException(
                f"SQL error on instance {self.id}:\n{process.stderr}"
            )

        return process.stdout

    def get_log_entries(self, num_lines=20, level=None):
        """Gets the latest log entries for the instance.

        Args:
            num_lines: Number of lines to retrieve.
            level: Log level to filter by (e.g., 'ERROR').

        Returns:
            A list of log entry strings.
        """
        if not self.log_path.exists():
            return ["Log file not found."]

        entries = []
        with open(self.log_path, 'r') as f:
            lines = f.readlines()

        for line in reversed(lines):
            if len(entries) >= num_lines:
                break
            if level:
                if f"[{level}]" in line:
                    entries.append(line.strip())
            else:
                entries.append(line.strip())

        return list(reversed(entries))

    def get_variable(self, variable_name):
        """Gets the value of a server variable from the instance."""
        if not self.is_running():
            return "Not running"

        try:
            output = self.run_sql(f"SHOW VARIABLES LIKE '{variable_name}'")
        except click.ClickException:
            return "N/A"

        lines = output.strip().split('\n')
        if len(lines) > 1:
            parts = lines[1].split('\t')
            if len(parts) > 1:
                return parts[1]

        return "N/A"

    @staticmethod
    def get_all_instances():
        """Gets a list of all deployed instances."""
        instances_dir = config.get_basedir() / 'instances'
        if not instances_dir.exists():
            return []

        instances = []
        for name in os.listdir(instances_dir):
            full_path = instances_dir / name
            if full_path.is_dir():
                parts = name.split('.')
                if len(parts) > 1:
                    instance_id = parts[-1]
                    instances.append(Instance(instance_id))
        return instances
