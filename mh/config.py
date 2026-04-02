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
