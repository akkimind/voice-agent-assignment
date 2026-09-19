#!/usr/bin/env bash
# One-command setup: a virtual environment, the Python packages, then the
# interactive setup that asks for every key and writes .env.
#
#   ./install.sh            # everything
#   ./install.sh --keys     # only the questions (packages already installed)
set -euo pipefail
cd "$(dirname "$0")"

if [[ "${1:-}" != "--keys" ]]; then
    PYTHON="${PYTHON:-python3}"
    if ! "$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
        echo "Python 3.11 or newer is needed (found: $("$PYTHON" --version 2>&1)). Set PYTHON=/path/to/python3.12 and retry." >&2
        exit 1
    fi
    if [[ ! -d .venv ]]; then
        echo "Creating the virtual environment in .venv"
        "$PYTHON" -m venv .venv
    fi
    echo "Installing Python packages from requirements.txt"
    ./.venv/bin/pip install --quiet --upgrade pip
    ./.venv/bin/pip install --quiet -r requirements.txt
fi

exec ./.venv/bin/python install.py
