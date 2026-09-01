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
image=${ANM_IMAGE_NAME:-ghcr.io/kyupi-git/animemachine}
dist="$root/dist"
mkdir -p "$dist"
docker build --build-arg "ANM_VERSION=$version" -t "$image:$version" -t "$image:latest" -f "$root/packaging/docker/Dockerfile" "$root"
if [ "${ANM_NO_EXPORT:-false}" = true ]; then
  printf 'Image: %s:%s\n' "$image" "$version"
  exit 0
fi
archive="$dist/AnimeMachine-$version-image.tar"
docker save --output "$archive" "$image:$version"
if command -v sha256sum >/dev/null 2>&1; then
  sha256sum "$archive" > "$archive.sha256"
else
  shasum -a 256 "$archive" > "$archive.sha256"
fi
bundle="$dist/AnimeMachine-$version-compose"
rm -rf "$bundle"
mkdir -p "$bundle/docs"
cp "$root/deploy/compose/04-full-stack/compose.yaml" "$root/deploy/compose/04-full-stack/.env.example" "$bundle/"
cp "$root/deploy/compose/torrent-collector.yaml" "$bundle/"
cp "$script_dir/initialize-animemachine.sh" "$bundle/"
cp "$root/scripts/windows/Initialize-AnimeMachine.ps1" "$bundle/"
cp "$root/README.md" "$root/LICENSE" "$root/THIRD-PARTY.md" "$bundle/"
cp "$root/docs/guide.md" "$bundle/docs/"
chmod +x "$bundle/initialize-animemachine.sh"
for file in "$bundle/compose.yaml" "$bundle/.env.example"; do
  sed -E -e "s|ghcr.io/kyupi-git/animemachine:[0-9]+\.[0-9]+\.[0-9]+|$image:$version|g" -e 's|../torrent-collector.yaml|./torrent-collector.yaml|g' "$file" > "$file.tmp"
  mv "$file.tmp" "$file"
done
printf 'Image archive: %s\nCompose bundle: %s\n' "$archive" "$bundle"
