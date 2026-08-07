import configparser
import os
import shutil
import subprocess
from pathlib import Path

import click


def get_config():
    """Gets the configuration from the myharem.conf file.

    Config path is resolved in order:
    1. MYHAREM_CONF environment variable
    2. /etc/myharem.conf
    """
    config = configparser.ConfigParser()
    config_path = os.environ.get('MYHAREM_CONF', '/etc/myharem.conf')

    if not os.path.exists(config_path):
        return config

    with open(config_path, 'r') as f:
        content = f.read()

    # Support the old bash-style config (no [DEFAULT] section header)
    if not content.strip().startswith('['):
        content = '[DEFAULT]\n' + content

    config.read_string(content)
    return config


def get_basedir():
    """Gets the basedir from the configuration."""
    config = get_config()
    return Path(config.get('DEFAULT', 'basedir', fallback='/var/opt/myharem'))


def get_dbuser():
    """Gets the dbuser from the configuration."""
    config = get_config()
    return config.get('DEFAULT', 'dbuser', fallback='mysql')


def get_sst_password():
    """Gets the Galera SST password.

    Resolution order: MYHAREM_SST_PASSWORD env, `sst_password` in the config
    file, then the legacy default. Kept configurable so deployments can avoid
    the hardcoded credential.
    """
    env_value = os.environ.get('MYHAREM_SST_PASSWORD')
    if env_value:
        return env_value
    config = get_config()
    return config.get('DEFAULT', 'sst_password', fallback='sstpwd')


def get_admin_password():
    """Password for the 'myharem' admin user.

    Resolution: MYHAREM_ADMIN_PASSWORD env, `admin_password` in the config file,
    then empty (passwordless local-socket auth — backward compatible). Set it to
    require a password for the admin user that all `mh` commands connect as.
    """
    env_value = os.environ.get('MYHAREM_ADMIN_PASSWORD')
    if env_value:
        return env_value
    config = get_config()
    return config.get('DEFAULT', 'admin_password', fallback='')


def get_wsrep_provider():
    """Path to the Galera provider library (libgalera_smm.so), if configured.

    Resolution: MYHAREM_WSREP_PROVIDER env, then `wsrep_provider` in the config
    file, then None. Used as an override for MariaDB builds that do not bundle
    the Galera provider (e.g. the generic 'linux-x86_64' tarballs); binary
    'linux-systemd' and Enterprise 'rhel-*' tarballs ship it and need no override.
    """
    env_value = os.environ.get('MYHAREM_WSREP_PROVIDER')
    if env_value:
        return env_value
    config = get_config()
    return config.get('DEFAULT', 'wsrep_provider', fallback=None) or None


def get_advertise_address():
    """The IP address this host advertises to Galera/replication peers.

    Default 127.0.0.1 (single-host / colocated — keeps behaviour unchanged). Set
    to the host's reachable IP for multi-host clusters (one node per VM), so
    peers can reach this node's Galera and replication endpoints. Resolution:
    MYHAREM_ADVERTISE_ADDRESS env, then `advertise_address` in the config file.
    """
    env_value = os.environ.get('MYHAREM_ADVERTISE_ADDRESS')
    if env_value:
        return env_value
    config = get_config()
    return config.get('DEFAULT', 'advertise_address', fallback='127.0.0.1')


def setup_myharem_dirs():
    """Sets up the necessary directories for MyHarem."""
    basedir = get_basedir()

    dirs_to_create = [
        basedir,
        basedir / 'instances',
        basedir / 'local',
        basedir / 'remote',
        basedir / 'erased',
        basedir / 'logs',
    ]

    for d in dirs_to_create:
        d.mkdir(parents=True, exist_ok=True)

    # Only chown the top-level dirs, not the entire tree
    _chown_dirs(dirs_to_create)


def _chown_dirs(paths):
    """Changes ownership of specific directories (non-recursive)."""
    dbuser = get_dbuser()
    try:
        for p in paths:
            shutil.chown(str(p), user=dbuser, group=dbuser)
    except (PermissionError, LookupError):
        pass


def chown_instance(path):
    """Changes ownership of an instance directory tree to dbuser.

    Uses system chown -R for performance on large directory trees.
    """
    dbuser = get_dbuser()
    try:
        subprocess.run(
            ['chown', '-R', f'{dbuser}:{dbuser}', str(path)],
            capture_output=True, timeout=60,
        )
    except (PermissionError, FileNotFoundError, subprocess.TimeoutExpired):
        pass
