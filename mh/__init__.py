"""MyHarem — deploy and manage MariaDB instances from tarballs."""

# The single source of truth for the version: `setup.py` reads it from here
# rather than carrying its own copy, so the two cannot drift (the same reason
# cli.py imports the port-step constants instead of repeating them).
__version__ = '0.5.0'
