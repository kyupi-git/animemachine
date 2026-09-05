#!/usr/bin/env sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ -d "$script_dir/../../src/animemachine" ]; then
  root=$(CDPATH= cd -- "$script_dir/../.." && pwd)
  release=0
elif [ -d "$script_dir/packages" ] || [ -d "$script_dir/app/animemachine" ]; then
  root=$script_dir
  release=1
else
  printf '%s\n' 'AnimeMachine source or Release files were not found.' >&2
  exit 1
fi

export ANM_INSTALL_ROOT="$root"
if [ "$release" -eq 1 ]; then export ANM_INSTALL_MODE=portable; else export ANM_INSTALL_MODE=source; fi

if [ "$release" -eq 1 ]; then
  env_example="$root/.env.local.example"
  env_file="$root/.env.local"
  state_default="$root/data/state"
else
  env_example="$root/deploy/local/.env.local.example"
  env_file="$root/.local/.env.local"
  state_default="$root/.local/state"
fi
mkdir -p "$(dirname "$env_file")" "$state_default" "$root/imports"
[ -f "$env_file" ] || cp "$env_example" "$env_file"

while IFS= read -r line || [ -n "$line" ]; do
  case "$line" in ''|\#*) continue;; *=*) export "$line";; esac
done < "$env_file"
export ANM_ENV_FILE="$env_file"

chmod 600 "$env_file" 2>/dev/null || printf '%s\n' "Warning: unable to restrict permissions on $env_file" >&2

python_cmd=${PYTHON:-python3}
command -v "$python_cmd" >/dev/null 2>&1 || {
  printf '%s\n' 'Python 3.11 or newer was not found.' >&2
  exit 1
}
venv="$root/.local/venv"
if [ "$release" -eq 1 ]; then
  runtime_platform=$(uname -s | tr '[:upper:]' '[:lower:]')
  runtime_arch=$(uname -m)
  venv="$root/.runtime/$runtime_platform-$runtime_arch"
fi
if [ ! -x "$venv/bin/python" ]; then
  "$python_cmd" -m venv "$venv"
fi
python="$venv/bin/python"
if [ "$release" -eq 1 ] && [ -d "$root/packages" ]; then
  if ! "$python" -c 'import animemachine,httpx,PIL,certifi,truststore' >/dev/null 2>&1; then
    app_wheel=$(find "$root/packages" -maxdepth 1 -type f -name 'animemachine-*.whl' | sort | tail -n 1)
    [ -n "$app_wheel" ] || { printf '%s\n' 'The AnimeMachine wheel is missing from this Release.' >&2; exit 1; }
    "$python" -m pip install --disable-pip-version-check --quiet --no-index --find-links "$root/packages" "$app_wheel" ||
      "$python" -m pip install --disable-pip-version-check --quiet --find-links "$root/packages" "$app_wheel"
  fi
else
  "$python" -c 'import animemachine,httpx,PIL,certifi,truststore' >/dev/null 2>&1 ||
    "$python" -m pip install --disable-pip-version-check --quiet --editable "$root"
fi

export ANM_STATE_DIR=${ANM_STATE_DIR:-$state_default}
export ANM_CONFIG_PATH=${ANM_CONFIG_PATH:-$root/config.json}
export ANM_CATALOG_DB=${ANM_CATALOG_DB:-$ANM_STATE_DIR/catalog/anime-catalog.sqlite3}
export ANM_RUNTIME_CATALOG_DB=${ANM_RUNTIME_CATALOG_DB:-$ANM_STATE_DIR/catalog/runtime.sqlite3}
export ANM_ARCHIVE_DIR=${ANM_ARCHIVE_DIR:-$ANM_STATE_DIR/metadata/archive}
export ANM_METADATA_CACHE_DIR=${ANM_METADATA_CACHE_DIR:-$ANM_STATE_DIR/metadata/cache}
export ANM_IMPORTS_DIR=${ANM_IMPORTS_DIR:-$root/imports}
export ANM_BIND_ADDRESS=${ANM_BIND_ADDRESS:-0.0.0.0}
export ANM_WEB_PORT=${ANM_WEB_PORT:-8787}
export ANM_SUBMISSION_ENABLED=${ANM_SUBMISSION_ENABLED:-true}
export PYTHONDONTWRITEBYTECODE=1
if [ ! -f "$ANM_CONFIG_PATH" ]; then
  mkdir -p "$(dirname "$ANM_CONFIG_PATH")"
  cp "$root/config/config.example.json" "$ANM_CONFIG_PATH"
fi

cd "$root"
exec "$python" -m animemachine run --host "$ANM_BIND_ADDRESS" --port "$ANM_WEB_PORT"
