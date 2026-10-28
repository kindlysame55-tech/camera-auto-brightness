#!/bin/sh
# Use the project environment without modifying shell profiles or system Python.
set -eu
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_dir"
if [ ! -x .venv/bin/python ]; then
    printf '%s\n' '请先创建环境 / Create the environment first:' 'python3 -m venv .venv' '.venv/bin/python -m pip install -e .'
    exit 1
fi
exec .venv/bin/python -m camab tray "$@"
