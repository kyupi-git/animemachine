#!/usr/bin/env sh
set -eu
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
root=$(CDPATH= cd -- "$script_dir/../.." && pwd)
if [ ! -f "$root/VERSION" ]; then printf '%s\n' 'Application directory not found.' >&2; exit 1; fi
python_cmd=${PYTHON:-python3}
if [ -x "$root/.venv/bin/python" ]; then python_cmd="$root/.venv/bin/python"; fi
cleaner="$script_dir/clean_runtime.py"
if [ ! -f "$cleaner" ]; then cleaner="$root/scripts/clean_runtime.py"; fi
exec "$python_cmd" "$cleaner" --root "$root" "$@"
