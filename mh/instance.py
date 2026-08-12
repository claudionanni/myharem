import os
import signal
import subprocess
import time
from pathlib import Path

import click

from . import config
from . import report
from .deployment import ADMIN_USER, ADMIN_PASSWORD


class Instance:
    """Represents a MariaDB instance."""

    def __init__(self, instance_id):
        self.id = str(instance_id)
        self.path = self._find_path()

    def _find_path(self):
        """Finds the instance directory by its id suffix.

        Instance ids map 1:1 to ports and sockets, so an id must be unique. If
        more than one directory claims the same id (e.g. the same id deployed
        for two different tarball versions), fail loudly — picking one
        arbitrarily silently operates on the wrong instance.
        """
        instances_dir = config.get_basedir() / 'instances'
        if not instances_dir.exists():
            return None
        matches = sorted(
            name for name in os.listdir(instances_dir)
            if name.endswith(f".{self.id}")
        )
        if len(matches) > 1:
            listing = "\n  ".join(matches)
            raise click.ClickException(
                f"Ambiguous instance id '{self.id}' — {len(matches)} instances "
                f"share it:\n  {listing}\n"
                f"Ids must be unique (they map to ports/sockets). Remove the "
                f"stale directory so the id resolves to exactly one instance."
            )
        if matches:
            return instances_dir / matches[0]
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

    def _section_rank(self, section_name):
        section_order = ['mariadbd', 'mysqld', 'server', 'client-server',
                         'client', '']
        try:
            return section_order.index(section_name)
        except ValueError:
            return len(section_order)

    def _primary_datadir(self):
        """Returns configured datadir path if available."""
        datadir_pairs = sorted(
            self._read_my_cnf_options('datadir'),
            key=lambda pair: self._section_rank(pair[0]),
        )
        if not datadir_pairs:
            return None

        datadir_raw = Path(datadir_pairs[0][1])
        if datadir_raw.is_absolute():
            return datadir_raw
        if self.path:
            return self.path / datadir_raw
        return None

    @property
    def socket_path(self):
        candidates = self._socket_candidates()
        for candidate in candidates:
            if candidate.exists():
                return candidate
        if candidates:
            return candidates[0]
        return Path(f"/tmp/mh-{self.id}.sock")

    def _read_my_cnf_options(self, option_name):
        """Reads option values from my.cnf with section context.

        Returns:
            List of (section, value) tuples in file order.
        """
        if not self.path:
            return []
        if not self.my_cnf_path.exists():
            return []

        pairs = []
        section = ''
        for raw in self.my_cnf_path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith('#') or line.startswith(';'):
                continue
            if line.startswith('[') and line.endswith(']'):
                section = line[1:-1].strip().lower()
                continue
            if '=' not in line:
                continue

            key, value = line.split('=', 1)
            if key.strip().lower() != option_name:
                continue
            cleaned = value.strip().strip('"').strip("'")
            if cleaned:
                pairs.append((section, cleaned))
        return pairs

    def _socket_candidates(self):
        """Returns candidate socket paths for this instance.

        Includes compatibility fallback for legacy long socket paths that
        can be truncated by client/server socket path limits.
        """
        candidates = []

        # Read configured options from my.cnf, prioritizing server sections.
        socket_pairs = self._read_my_cnf_options('socket')
        socket_pairs = sorted(
            socket_pairs, key=lambda pair: self._section_rank(pair[0])
        )
        datadir = self._primary_datadir()

        for _, socket_raw in socket_pairs:
            socket_cfg = Path(socket_raw)
            if socket_cfg.is_absolute():
                candidates.append(socket_cfg)
                socket_cfg_str = str(socket_cfg)
                if len(socket_cfg_str) > 64:
                    candidates.append(Path(socket_cfg_str[:64]))
            else:
                # Relative socket path: resolve against common roots.
                if self.path:
                    candidates.append(self.path / socket_cfg)
                if datadir:
                    candidates.append(datadir / socket_cfg)
                candidates.append(socket_cfg)

        if self.path:
            candidates.append(self.path / f"{self.id}.sock")
            candidates.append(self.path / 'data' / f"{self.id}.sock")
        candidates.append(Path('/tmp') / f"mh-{self.id}.sock")

        deduped = []
        for candidate in candidates:
            if candidate not in deduped:
                deduped.append(candidate)
        return deduped

    def _pid_candidates(self):
        """Returns candidate PID file paths for this instance."""
        candidates = []
        datadir = self._primary_datadir()

        pid_pairs = (
            self._read_my_cnf_options('pid-file')
            + self._read_my_cnf_options('pid_file')
        )
        pid_pairs = sorted(pid_pairs, key=lambda pair: self._section_rank(pair[0]))

        for _, pid_raw in pid_pairs:
            pid_cfg = Path(pid_raw)
            if pid_cfg.is_absolute():
                candidates.append(pid_cfg)
            else:
                if self.path:
                    candidates.append(self.path / pid_cfg)
                if datadir:
                    candidates.append(datadir / pid_cfg)
                candidates.append(pid_cfg)

        if self.path:
            candidates.append(self.path / f"{self.id}.pid")
        if datadir:
            candidates.append(datadir / f"{self.id}.pid")
            for pid_file in datadir.glob('*.pid'):
                candidates.append(pid_file)

        deduped = []
        for candidate in candidates:
            if candidate not in deduped:
                deduped.append(candidate)
        return deduped

    def _ensure_pid_file_config(self):
        """Ensures my.cnf has a dedicated pid-file for this instance."""
        if not self.my_cnf_path.exists():
            return

        pid_pairs = (
            self._read_my_cnf_options('pid-file')
            + self._read_my_cnf_options('pid_file')
        )
        if pid_pairs:
            return

        with open(self.my_cnf_path, 'a') as f:
            f.write(f"\npid-file={self.path / f'{self.id}.pid'}\n")

    @staticmethod
    def _pid_alive(pid):
        # Opportunistically reap if this happens to be our own child (e.g.
        # started earlier in the same long-lived process) — a killed child
        # stays a zombie, and kill(pid, 0) keeps reporting it "alive," until
        # something reaps it. Across separate CLI invocations (the normal
        # case: whatever process started mariadbd has long since exited)
        # this is a harmless no-op — os.waitpid raises ChildProcessError for
        # a PID that isn't our child, which we simply ignore and fall
        # through to the signal-based check.
        try:
            reaped_pid, _status = os.waitpid(pid, os.WNOHANG)
            if reaped_pid == pid:
                return False
        except (ChildProcessError, OSError):
            pass
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError, OSError):
            return False

    @staticmethod
    def _pid_cmdline(pid):
        try:
            data = Path(f"/proc/{pid}/cmdline").read_bytes()
            return data.decode(errors='ignore').replace('\x00', ' ').strip()
        except Exception:
            return ""

    def _cleanup_stale_pid_files(self):
        """Removes stale PID files that block mysqld_safe startup."""
        for pid_file in self._pid_candidates():
            if not pid_file.exists():
                continue
            try:
                raw = pid_file.read_text().strip()
                token = raw.split()[0] if raw else ""
                pid = int(token)
            except Exception:
                continue

            if self._pid_alive(pid):
                cmdline = self._pid_cmdline(pid)
                if (
                    ('mysqld' in cmdline or 'mariadbd' in cmdline)
                    and self.path
                    and str(self.path) in cmdline
                ):
                    # Looks like this instance is already represented by the PID.
                    continue

            try:
                pid_file.unlink()
            except FileNotFoundError:
                pass

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

    def _admin_env(self):
        """Environment for client connections as the admin user.

        Passes the admin password via MYSQL_PWD so it never appears on the
        command line (process list). Empty password → unchanged environment.
        """
        env = os.environ.copy()
        if ADMIN_PASSWORD:
            env['MYSQL_PWD'] = ADMIN_PASSWORD
        return env

    def start(self, wsrep_new_cluster=False):
        """Starts the MariaDB instance.

        Args:
            wsrep_new_cluster: If True, starts with --wsrep-new-cluster
                (Galera bootstrap — first node of a new cluster).
        """
        self._require_exists()

        if not self.mysqld_safe_bin.exists():
            raise click.ClickException(
                f"mysqld_safe not found at {self.mysqld_safe_bin}"
            )

        # Upgrades/manual datadir copies can bring stale PID files.
        self._ensure_pid_file_config()
        self._cleanup_stale_pid_files()

        cmd = [str(self.mysqld_safe_bin), f"--defaults-file={self.my_cnf_path}"]
        if wsrep_new_cluster:
            cmd.append("--wsrep-new-cluster")
        subprocess.Popen(cmd, cwd=str(self.path),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        label = f"Starting instance {self.id}"
        if wsrep_new_cluster:
            label += " (Galera bootstrap)"
        report.log(f"{label}...")

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

        process = subprocess.run(cmd, capture_output=True, text=True,
                                 env=self._admin_env())

        if process.returncode != 0:
            if "Can't connect" in process.stderr:
                report.warn(f"Instance {self.id} is not running.")
            else:
                raise click.ClickException(
                    f"Error stopping instance {self.id}:\n{process.stderr}"
                )
        else:
            report.success(f"Instance {self.id} stopped successfully.")

    def scli(self):
        """Connects to the instance via socket using the mariadb client."""
        self._require_exists()

        if not self.mariadb_bin.exists():
            raise click.ClickException(
                f"mariadb client not found at {self.mariadb_bin}"
            )

        if ADMIN_PASSWORD:
            os.environ['MYSQL_PWD'] = ADMIN_PASSWORD
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

        if ADMIN_PASSWORD:
            os.environ['MYSQL_PWD'] = ADMIN_PASSWORD
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
            cmd, capture_output=True, text=True, timeout=5,
            env=self._admin_env(),
        )
        return "Running" if process.returncode == 0 else "Stopped"

    def is_running(self):
        return self.get_status() == "Running"

    def is_socket_ready(self):
        """Checks if the instance socket FILE exists.

        This only proves mariadbd has bound the socket path, not that it is
        actually accepting/answering connections yet (Galera's wsrep
        initialization can hold that up well after the file appears) —
        use is_accepting_connections() before doing anything that requires a
        working connection. Kept for the narrow case of checking a file's
        mere presence (e.g. before service users exist and no connection
        attempt is appropriate yet).
        """
        return any(candidate.exists() for candidate in self._socket_candidates())

    def is_accepting_connections(self):
        """Checks if the server is actually answering queries yet, by really
        connecting (as root over the unix socket — the one identity
        guaranteed to exist immediately after mariadb-install-db, before any
        service user has been created).

        This is the real readiness signal: is_socket_ready() only proves the
        socket file exists, which can be true well before mariadbd is done
        initializing (especially with Galera/wsrep enabled) — code that acts
        on is_socket_ready() alone can race ahead of a server that isn't
        actually ready to authenticate a connection yet.
        """
        if not self.path:
            return False
        mariadb = self.path / 'bin' / 'mariadb'
        if not mariadb.exists():
            mariadb = self.path / 'bin' / 'mysql'
        if not mariadb.exists():
            return False
        cmd = [
            str(mariadb), '-uroot',
            f'--socket={self.socket_path}',
            '-e', 'SELECT 1',
        ]
        try:
            process = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        except Exception:
            return False
        return process.returncode == 0

    def find_pid(self):
        """Finds this instance's live mariadbd PID from its PID file, or None.

        Unlike is_running() (which authenticates as the service admin user),
        this never touches the database at all — it only reads the PID file
        and confirms the OS process is alive, so it stays reliable even if
        the admin user was never successfully created (e.g. a deploy that
        failed before create_service_users() succeeded).
        """
        for pid_file in self._pid_candidates():
            if not pid_file.exists():
                continue
            try:
                raw = pid_file.read_text().strip()
                pid = int(raw.split()[0] if raw else "")
            except Exception:
                continue
            if self._pid_alive(pid):
                return pid
        return None

    def terminate(self, timeout=15):
        """Forcibly ensures this instance's process is dead, independent of
        any database-level shutdown (mariadb-admin needs the admin user to
        exist, which a partially-failed deploy may never have created).

        Sends SIGTERM, waits up to `timeout` seconds, then SIGKILLs if it's
        still alive. Safe to call when nothing is running (no-op). Deleting
        an instance's files while its process is still alive leaves an
        orphan holding deleted files open — always call this (or otherwise
        confirm find_pid() is None) before removing an instance's directory.
        """
        pid = self.find_pid()
        if pid is None:
            return
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except PermissionError:
            raise click.ClickException(
                f"Cannot stop instance {self.id} (pid {pid}): permission denied. "
                f"Run with sudo?"
            )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self._pid_alive(pid):
                return
            time.sleep(0.5)
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        # Give the kernel a moment to actually reap/release it.
        for _ in range(10):
            if not self._pid_alive(pid):
                return
            time.sleep(0.5)
        raise click.ClickException(
            f"Instance {self.id} (pid {pid}) would not die even after SIGKILL."
        )

    def wsrep_local_state_comment(self):
        """Returns the Galera wsrep_local_state_comment status value (e.g.
        'Synced', 'Joiner', 'Donor/Desynced'), or None if not reachable yet
        or not a Galera instance.

        Connects as root over the unix socket rather than run_sql()'s
        ADMIN_USER — a Galera joiner receives the myharem admin user via SST
        from the donor, which can still be in flight at the moment this is
        checked (right after the socket becomes connectable). Root over the
        socket is the only identity guaranteed to exist immediately after
        mariadb-install-db.
        """
        if not self.path:
            return None
        mariadb = self.path / 'bin' / 'mariadb'
        if not mariadb.exists():
            mariadb = self.path / 'bin' / 'mysql'
        if not mariadb.exists():
            return None

        cmd = [
            str(mariadb), '-uroot',
            f'--socket={self.socket_path}',
            '-B', '-N',
            '-e', "SHOW STATUS LIKE 'wsrep_local_state_comment'",
        ]
        try:
            process = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        except Exception:
            return None
        if process.returncode != 0:
            return None
        parts = process.stdout.strip().split('\t')
        if len(parts) != 2:
            return None
        return parts[1].strip()

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
            cmd, capture_output=True, text=True, timeout=timeout,
            env=self._admin_env(),
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
