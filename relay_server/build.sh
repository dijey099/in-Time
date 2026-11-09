#!/bin/sh
# Builds dist/start_relay (Linux). All options are in relay.spec.
# Use a venv that only has requirements.txt installed: PyInstaller bundles whatever it can import.
set -e
cd "$(dirname "$0")"
pyinstaller --clean --noconfirm relay.spec
echo "Copy dist/start_relay next to relay.ini, then run it."
