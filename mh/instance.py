import os
from . import config

class Instance:
    """Represents a MariaDB instance."""

    def __init__(self, instance_id):
        """
        Initializes an Instance object.

        Args:
            instance_id (str): The ID of the instance.
        """
        self.id = instance_id
        self.path = self._find_path()

    def _find_path(self):
        """Finds the path to the instance directory."""
        basedir = config.get_basedir()
        instances_dir = os.path.join(basedir, 'instances')
        if not os.path.exists(instances_dir):
            return None
        for name in os.listdir(instances_dir):
            if name.endswith(f".{self.id}"):
                return os.path.join(instances_dir, name)
        return None

    def exists(self):
        """Checks if the instance exists."""
        return self.path is not None

    def start(self):
        """Starts the MariaDB instance."""
        import subprocess
        mysqld_safe_path = os.path.join(self.path, 'bin', 'mysqld_safe')
        my_cnf_path = os.path.join(self.path, 'my.cnf')

        if not os.path.exists(mysqld_safe_path):
            print(f"mysqld_safe not found at {mysqld_safe_path}")
            return

        cmd = [
            mysqld_safe_path,
            f"--defaults-file={my_cnf_path}"
        ]

        subprocess.Popen(cmd, cwd=self.path)
        print(f"Starting instance {self.id}...")

    def stop(self):
        """Stops the MariaDB instance."""
        import subprocess
        mariadb_admin_path = os.path.join(self.path, 'bin', 'mariadb-admin')

        if not os.path.exists(mariadb_admin_path):
            print(f"mariadb-admin not found at {mariadb_admin_path}")
            return

        cmd = [
            mariadb_admin_path,
            "-uroot",
            f"--port={self.id}",
            "shutdown"
        ]

        process = subprocess.run(cmd, capture_output=True, text=True)

        if process.returncode != 0:
            if "Can't connect to server on" in process.stderr:
                 print(f"Instance {self.id} is not running.")
            else:
                print(f"Error stopping instance {self.id}:")
                print(process.stderr)
        else:
            print(f"Instance {self.id} stopped successfully.")

    def scli(self):
        """Connects to the instance using the mariadb client and a socket."""
        mariadb_path = os.path.join(self.path, 'bin', 'mariadb')
        socket_path = os.path.join(self.path, f"{self.id}.sock")

        if not os.path.exists(mariadb_path):
            print(f"mariadb client not found at {mariadb_path}")
            return

        os.execv(mariadb_path, [mariadb_path, "-uroot", f"--socket={socket_path}"])

    def get_status(self):
        """
        Gets the status of the instance (Running or Stopped).

        Returns:
            str: The status of the instance.
        """
        import subprocess
        mariadb_admin_path = os.path.join(self.path, 'bin', 'mariadb-admin')

        if not os.path.exists(mariadb_admin_path):
            return "Unknown (mariadb-admin not found)"

        cmd = [
            mariadb_admin_path,
            "-uroot",
            f"--port={self.id}",
            "ping"
        ]

        process = subprocess.run(cmd, capture_output=True, text=True)

        if process.returncode == 0:
            return "Running"
        else:
            return "Stopped"

    def get_log_entries(self, num_lines=20, level=None):
        """
        Gets the latest log entries for the instance.

        Args:
            num_lines (int, optional): The number of lines to retrieve. Defaults to 20.
            level (str, optional): The log level to filter by (e.g., 'ERROR'). Defaults to None.

        Returns:
            list: A list of log entry strings.
        """
        log_file_path = os.path.join(self.path, f"error.{self.id}.log")
        if not os.path.exists(log_file_path):
            return ["Log file not found."]

        entries = []
        with open(log_file_path, 'r') as f:
            lines = f.readlines()

        # This is not very efficient for large log files, but it's simple.
        for line in reversed(lines):
            if len(entries) >= num_lines:
                break
            if level:
                # MariaDB log lines start with date and time, e.g.,
                # 2025-08-12 11:44:14 [ERROR] Aborting
                if f"[{level}]" in line:
                    entries.append(line.strip())
            else:
                entries.append(line.strip())

        return reversed(entries)

    def get_variable(self, variable_name):
        """
        Gets the value of a server variable from the instance.

        Args:
            variable_name (str): The name of the variable to retrieve.

        Returns:
            str: The value of the variable, or 'Not running' or 'N/A'.
        """
        import subprocess

        if self.get_status() != "Running":
            return "Not running"

        mariadb_path = os.path.join(self.path, 'bin', 'mariadb')

        if not os.path.exists(mariadb_path):
            return "Unknown (mariadb client not found)"

        cmd = [
            mariadb_path,
            "-uroot",
            f"--port={self.id}",
            "-B", # Batch mode, to get tab-separated output
            "-e",
            f"SHOW VARIABLES LIKE '{variable_name}'"
        ]

        process = subprocess.run(cmd, capture_output=True, text=True)

        if process.returncode == 0:
            lines = process.stdout.strip().split('\n')
            if len(lines) > 1:
                parts = lines[1].split('\t')
                if len(parts) > 1:
                    return parts[1]

        return "N/A"

    @staticmethod
    def get_all_instances():
        """
        Gets a list of all deployed instances.

        Returns:
            list: A list of Instance objects.
        """
        basedir = config.get_basedir()
        instances_dir = os.path.join(basedir, 'instances')
        if not os.path.exists(instances_dir):
            return []

        instances = []
        for name in os.listdir(instances_dir):
            # This is not perfect, as it assumes the id is after the last dot.
            # It will fail if the dirname contains dots.
            # The original script had this issue too.
            # For now, I'll stick with this logic.
            parts = name.split('.')
            if len(parts) > 1:
                instance_id = parts[-1]
                instances.append(Instance(instance_id))
        return instances
