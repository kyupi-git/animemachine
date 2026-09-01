#!/usr/bin/env sh
set -eu
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ -d "$script_dir/../../src/animemachine" ]; then
  root=$(CDPATH= cd -- "$script_dir/../.." && pwd)
else
  root=$script_dir
fi
find "$root" -type d \( -name __pycache__ -o -name .pytest_cache -o -name .mypy_cache -o -name .ruff_cache -o -name '*.egg-info' \) \
  ! -path "$root/.local/state/*" -prune -exec rm -rf {} +
find "$root" -type f \( -name '*.pyc' -o -name '*.pyo' -o -name '*.tmp' -o -name '*.part' \) \
  ! -path "$root/.local/state/*" -delete
rm -rf "$root/build"
if [ "${1:-}" = "--include-builds" ]; then rm -rf "$root/dist"; fi
printf '%s\n' 'Temporary files were removed. Databases, cover cache, credentials and history were preserved.'
