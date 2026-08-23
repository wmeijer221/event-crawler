#!/bin/bash
set -e

sudo rm -f /etc/apt/sources.list.d/yarn.list

sudo apt-get update

# Packages used by vscode plugins (i.e., dev-dependencies).
sudo apt-get install -y graphviz socat default-jre ripgrep
sudo apt-get install zstd

# The project uses uv for dependency management.
pip install --upgrade pip
pip install uv

# We use venv to deal with weird conflicts.
# we force that to the vscode folder, because that's faster.
VENV_PATH="/home/vscode/.venv"
python3 -m venv "$VENV_PATH"
source "$VENV_PATH/bin/activate"

# Installs project dependencies.
uv sync --dev --python "$VENV_PATH/bin/python"

# .ssh setup.
sudo chown -R vscode:vscode /home/vscode/.ssh
chmod 700 /home/vscode/.ssh
chmod 400 /home/vscode/.ssh/id_ed25519
