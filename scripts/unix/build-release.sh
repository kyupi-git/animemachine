#!/usr/bin/env sh
set -eu
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
root=$(CDPATH= cd -- "$script_dir/../.." && pwd)
python_cmd=${PYTHON:-python3}
version=$($python_cmd "$root/scripts/build_info.py" --print-version)
$python_cmd "$root/scripts/build_info.py" --check-repository
if [ "${1:-}" ] && [ "$1" != "$version" ]; then
  printf 'Requested version %s does not match VERSION %s.\n' "$1" "$version" >&2
  exit 1
fi
dist="$root/dist"
stage=$(mktemp -d "${TMPDIR:-/tmp}/animemachine-release.XXXXXX")
trap 'rm -rf "$stage"' EXIT HUP INT TERM
case "$(uname -s)" in
  Linux) platform=linux ;;
  Darwin) platform=macos ;;
  *) platform=$(uname -s | tr '[:upper:]' '[:lower:]') ;;
esac
arch=$(uname -m)
release="$stage/AnimeMachine-$version"
mkdir -p "$release/packages" "$release/config" "$release/imports" "$release/data" "$release/docs" "$dist"

rm -rf "$root/build" "$root/src/animemachine.egg-info"
"$python_cmd" -m pip wheel --disable-pip-version-check --wheel-dir "$release/packages" "$root"
cp "$root/config/config.example.json" "$root/config/config.schema.json" "$release/config/"
cp "$root/deploy/local/.env.local.example" "$release/.env.local.example"
cp "$script_dir/AnimeMachine.sh" "$release/AnimeMachine.sh"
case "$platform" in
  linux) launcher=AnimeMachine-Linux.sh ;;
  macos) launcher=AnimeMachine-macOS.command ;;
  *) printf 'Unsupported release platform: %s\n' "$platform" >&2; exit 1 ;;
esac
cp "$script_dir/$launcher" "$release/$launcher"
mkdir -p "$release/tools/maintenance"
cp "$script_dir/Clean-AnimeMachine.sh" "$root/scripts/clean_runtime.py" "$release/tools/maintenance/"

cp "$root/README.md" "$root/README.en.md" "$root/README.ja.md" "$root/CHANGELOG.md" "$root/CHANGELOG.en.md" "$root/CHANGELOG.ja.md" "$root/LICENSE" "$root/THIRD-PARTY.md" "$root/SECURITY.md" "$root/CONTRIBUTING.md" "$root/CONTRIBUTING.en.md" "$root/CONTRIBUTING.ja.md" "$release/"
find "$root/docs" -maxdepth 1 -type f -name '*.md' -exec cp {} "$release/docs/" \;
if [ -d "$root/docs/images" ]; then
    "$python_cmd" "$root/scripts/copy_document_images.py" "$root/docs/images" "$release/docs/images"
fi
cp "$root/VERSION" "$root/RELEASE_PYTHON_VERSION" "$release/"
"$python_cmd" "$root/scripts/build_info.py" --output "$release/BUILD-INFO.json" --build-type "$platform-portable" --platform "$platform" --python-version "$("$python_cmd" -c 'import platform; print(platform.python_version())')"
printf '%s\n' 'Place a verified Bangumi Archive dump-*.zip here, then start AnimeMachine.' > "$release/imports/README.txt"
chmod +x "$release/AnimeMachine.sh" "$release/$launcher" "$release/tools/maintenance/Clean-AnimeMachine.sh"
"$python_cmd" "$root/scripts/check_public_tree.py" "$release"
archive="$dist/AnimeMachine-$version-release-$platform-$arch.tar.gz"
tar -C "$stage" -czf "$archive" "AnimeMachine-$version"
"$python_cmd" "$root/scripts/check_public_tree.py" "$archive"
if command -v sha256sum >/dev/null 2>&1; then
  sha256sum "$archive" > "$archive.sha256"
else
  shasum -a 256 "$archive" > "$archive.sha256"
fi
printf 'Release: %s\n' "$archive"
