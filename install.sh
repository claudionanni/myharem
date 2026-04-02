#!/bin/bash
set -e

echo "==============================="
echo " MyHarem Installer"
echo "==============================="
echo

# Must run as root for system-level setup
if [ "$(id -u)" -ne 0 ]; then
    echo "Error: This script must be run as root (or with sudo)."
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG_SRC="$SCRIPT_DIR/etc/myharem.conf"
CONFIG_DEST="/etc/myharem.conf"

# --- Read config values ---
if [ -f "$CONFIG_SRC" ]; then
    BASEDIR=$(grep -E "^basedir=" "$CONFIG_SRC" | cut -d= -f2)
    DBUSER=$(grep -E "^dbuser=" "$CONFIG_SRC" | cut -d= -f2)
fi
BASEDIR="${BASEDIR:-/var/opt/myharem}"
DBUSER="${DBUSER:-mysql}"

echo "Configuration:"
echo "  basedir = $BASEDIR"
echo "  dbuser  = $DBUSER"
echo

# --- Ensure dbuser exists ---
if ! id "$DBUSER" &>/dev/null; then
    echo "Creating system user '$DBUSER'..."
    useradd --system --no-create-home --shell /usr/sbin/nologin "$DBUSER"
fi

# --- Install config file ---
if [ -f "$CONFIG_DEST" ]; then
    echo "Config file $CONFIG_DEST already exists, keeping it."
else
    echo "Installing config to $CONFIG_DEST..."
    cp "$CONFIG_SRC" "$CONFIG_DEST"
    chmod 644 "$CONFIG_DEST"
fi

# --- Create directory structure ---
echo "Creating directory structure under $BASEDIR..."
for dir in "$BASEDIR" "$BASEDIR/instances" "$BASEDIR/local" "$BASEDIR/remote" "$BASEDIR/erased" "$BASEDIR/logs"; do
    mkdir -p "$dir"
done
chown -R "$DBUSER:$DBUSER" "$BASEDIR"

# --- Install Python package ---
echo "Installing MyHarem Python package..."
pip install "$SCRIPT_DIR" --quiet

echo
echo "==============================="
echo " Installation complete!"
echo "==============================="
echo
echo "Usage: mh --help"
echo
