import os
import tarfile
from . import config

def deploy_instance(tarball_path, instance_id, init_db=True):
    """
    Deploys a new MariaDB instance.

    Args:
        tarball_path (str): The path to the MariaDB tarball.
        instance_id (str): The ID for the new instance.
        init_db (bool, optional): Whether to initialize the database. Defaults to True.

    Returns:
        str: The path to the new instance directory.
    """
    basedir = config.get_basedir()
    instances_dir = os.path.join(basedir, 'instances')

    # Get the directory name from the tarball
    dirname = os.path.basename(tarball_path).replace('.tar.gz', '')
    instance_name = f"{dirname}.{instance_id}"
    instance_path = os.path.join(instances_dir, instance_name)

    print(f"Creating instance directory: {instance_path}")
    os.makedirs(instance_path, exist_ok=True)

    print(f"Extracting {tarball_path} to {instance_path}")
    with tarfile.open(tarball_path, 'r:gz') as tar:
        # Extract to a temporary directory first to get the root directory name
        # and then move the contents to the final destination
        # This is because tarballs can have different root directory names
        # and we want to have a clean instance directory

        # This is a bit of a hack to strip the top-level directory from the tarball
        # It's equivalent to tar's --strip-components=1
        members = tar.getmembers()
        root_dir = members[0].name.split('/')[0]
        for member in members:
            if member.path.startswith(root_dir + '/'):
                member.path = member.path[len(root_dir) + 1:]
                if member.path:
                    tar.extract(member, path=instance_path)

    if init_db:
        _generate_my_cnf(instance_id, instance_path)
        initialize_database(instance_path)

    return instance_path

def _generate_my_cnf(instance_id, instance_path):
    dbuser = config.get_dbuser()
    my_cnf_path = os.path.join(instance_path, 'my.cnf')

    with open(my_cnf_path, 'w') as f:
        f.write("[mysqld]\n")
        f.write(f"port={instance_id}\n")
        f.write(f"socket={instance_id}.sock\n")
        f.write(f"basedir={instance_path}\n")
        f.write(f"datadir={os.path.join(instance_path, 'data')}\n")
        f.write(f"server_id={instance_id}\n")
        f.write(f"user={dbuser}\n")
        f.write("innodb_file_per_table\n")
        f.write("log_bin\n")
        f.write(f"log_error=error.{instance_id}.log\n")
        f.write("binlog_format=ROW\n")

    print(f"Generated my.cnf at {my_cnf_path}")

def initialize_database(instance_path):
    install_db_script = os.path.join(instance_path, 'scripts', 'mariadb-install-db')
    my_cnf_path = os.path.join(instance_path, 'my.cnf')

    print("Initializing the database...")
    # The command should be run from the instance_path
    # so that the paths in my.cnf are resolved correctly
    # However, mariadb-install-db uses the basedir from my.cnf
    # so it should be fine to run it from anywhere as long as
    # the defaults-file is specified.
    # For safety, let's also specify the basedir and datadir
    datadir = os.path.join(instance_path, 'data')
    cmd = [
        install_db_script,
        f"--defaults-file={my_cnf_path}",
        f"--basedir={instance_path}",
        f"--datadir={datadir}"
    ]

    # We need to capture the output to see if it was successful
    # and to show it to the user.
    import subprocess
    process = subprocess.run(cmd, capture_output=True, text=True)

    if process.returncode != 0:
        print("Error initializing the database:")
        print(process.stderr)
    else:
        print("Database initialized successfully.")
        print(process.stdout)
