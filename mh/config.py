import configparser
import os

def get_config():
    """Gets the configuration from the myharem.conf file."""
    config = configparser.ConfigParser()
    config_path = '/etc/myharem.conf'  # TODO: make configurable

    if not os.path.exists(config_path):
        return config  # Return empty config, fallbacks will be used

    with open(config_path, 'r') as f:
        content = f.read()

    # Prepend [DEFAULT] section header if it's missing to support the old format
    if not content.strip().startswith('['):
        content = '[DEFAULT]\n' + content

    config.read_string(content)
    return config

def get_basedir():
    """Gets the basedir from the configuration."""
    config = get_config()
    return config.get('DEFAULT', 'basedir', fallback='/var/opt/myharem')

def get_dbuser():
    """Gets the dbuser from the configuration."""
    config = get_config()
    return config.get('DEFAULT', 'dbuser', fallback='mysql')

def setup_myharem_dirs():
    """Sets up the necessary directories for MyHarem."""
    basedir = get_basedir()

    dirs_to_create = [
        basedir,
        os.path.join(basedir, 'instances'),
        os.path.join(basedir, 'local'),
        os.path.join(basedir, 'remote'),
        os.path.join(basedir, 'erased'),
        os.path.join(basedir, 'logs'),
    ]

    for d in dirs_to_create:
        os.makedirs(d, exist_ok=True)
