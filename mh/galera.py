import os
from . import deployment

def deploy_cluster(tarball_path, first_instance_id):
    """
    Deploys a 3-node Galera cluster.

    Args:
        tarball_path (str): The path to the MariaDB tarball.
        first_instance_id (str): The ID for the first node in the cluster.
    """
    first_instance_id = int(first_instance_id)
    node_ids = [
        first_instance_id,
        first_instance_id + 10000,
        first_instance_id + 20000
    ]

    cluster_address = f"gcomm://127.0.0.1:{first_instance_id}"

    for i, node_id in enumerate(node_ids):
        print(f"Deploying node {i+1} with id {node_id}...")
        # We need to deploy without initializing the database
        # because we need to generate a custom my.cnf first.
        # I will modify the deploy_instance function to allow this.
        instance_path = deployment.deploy_instance(tarball_path, str(node_id), init_db=False)

        if not instance_path:
            print(f"Failed to deploy node {i+1}")
            return

        is_bootstrap_node = (i == 0)
        _generate_galera_my_cnf(instance_path, str(node_id), cluster_address, is_bootstrap_node)

        # Now we can initialize the database
        deployment.initialize_database(instance_path)

def _generate_galera_my_cnf(instance_path, instance_id, cluster_address, is_bootstrap_node):
    from . import config
    dbuser = config.get_dbuser()
    my_cnf_path = os.path.join(instance_path, 'my.cnf')

    wsrep_port = str(int(instance_id) + 1000)
    sst_port = str(int(instance_id) + 2000)

    final_cluster_address = "gcomm://" if is_bootstrap_node else cluster_address

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
        f.write("default_storage_engine=InnoDB\n")
        f.write("innodb_autoinc_lock_mode=2\n")

        f.write("\n# Galera Configuration\n")
        f.write(f"wsrep_provider={os.path.join(instance_path, 'lib', 'galera', 'libgalera_smm.so')}\n")
        f.write("wsrep_cluster_name=myharem_cluster\n")
        f.write(f"wsrep_cluster_address={final_cluster_address}\n")
        f.write(f"wsrep_node_address=127.0.0.1:{wsrep_port}\n")
        f.write("wsrep_sst_method=rsync\n")

    print(f"Generated Galera my.cnf at {my_cnf_path}")
