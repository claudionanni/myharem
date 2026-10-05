import configparser
import os
import shutil
import subprocess
from pathlib import Path

import click

from . import report


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


def get_es_token():
    """Customer token for MariaDB Enterprise downloads (dlm.mariadb.com).

    Resolution: MYHAREM_ES_TOKEN env, then `es_token` in the config file, then
    None. The token is a PATH SEGMENT of the DLM URL, which makes the resulting
    download URL a secret in its own right — hence no --token flag anywhere
    (it would land in shell history and in `ps`).

    Prefer the environment variable: install.sh installs /etc/myharem.conf
    world-readable, so a token kept there is readable by every user on the host.
    warn_if_config_is_world_readable() says so when it matters.
    """
    env_value = os.environ.get('MYHAREM_ES_TOKEN')
    if env_value:
        return env_value
    config = get_config()
    return config.get('DEFAULT', 'es_token', fallback=None) or None


def es_token_came_from_config():
    """True when the token is being read from the config file, not the env.

    Only then is the file's mode worth complaining about.
    """
    if os.environ.get('MYHAREM_ES_TOKEN'):
        return False
    config = get_config()
    return bool(config.get('DEFAULT', 'es_token', fallback=None))


def warn_if_config_is_world_readable():
    """Warns when the config file holding the ES token is readable by others.

    install.sh installs /etc/myharem.conf mode 644, so the default placement of
    a customer token is a world-readable file. Warn rather than refuse: the
    token is the user's to place where they like, and refusing would break a
    working setup.
    """
    if not es_token_came_from_config():
        return
    config_path = os.environ.get('MYHAREM_CONF', '/etc/myharem.conf')
    try:
        mode = os.stat(config_path).st_mode
    except OSError:
        return
    if mode & 0o077:
        report.warn(
            f"{config_path} holds es_token and is group/world-readable "
            f"({oct(mode & 0o777)}). Run 'chmod 600 {config_path}', or set "
            f"MYHAREM_ES_TOKEN in the environment instead."
        )


def get_es_bintar_target():
    """Override for the Enterprise bintar target, e.g. 'rhel-9-x86_64'.

    Resolution: MYHAREM_ES_BINTAR_TARGET env, then `es_bintar_target` in the
    config file, then None — in which case it is detected from /etc/os-release.
    Enterprise publishes one bintar per distro and it must match this host.
    """
    env_value = os.environ.get('MYHAREM_ES_BINTAR_TARGET')
    if env_value:
        return env_value
    config = get_config()
    return config.get('DEFAULT', 'es_bintar_target', fallback=None) or None


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

    try:
        for d in dirs_to_create:
            d.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        # Runs from the group callback, i.e. before every command including
        # --help. Commands that manage instances need root anyway and will fail
        # with their own clear error; `mh repo`, `mh token` and `mh download
        # --list` need no directories at all, and a traceback here used to deny
        # them to any non-root user.
        report.warn(f"Could not prepare {basedir}: {exc}")
        return

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
