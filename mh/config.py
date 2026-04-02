import configparser
import os
import shutil
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

    _chown_tree(basedir)


def _chown_tree(path):
    """Changes ownership of the directory tree to the configured dbuser."""
    dbuser = get_dbuser()
    try:
        shutil.chown(str(path), user=dbuser, group=dbuser)
        for root, dirs, files in os.walk(path):
            for d in dirs:
                shutil.chown(os.path.join(root, d), user=dbuser, group=dbuser)
            for f in files:
                shutil.chown(os.path.join(root, f), user=dbuser, group=dbuser)
    except (PermissionError, LookupError):
        # Non-root users or unknown user — skip silently
        pass
