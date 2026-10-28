#!/bin/sh
# Double-clickable macOS source launcher (chmod +x scripts/start.command once).
set -eu
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec /bin/sh "$script_dir/start.sh" "$@"
