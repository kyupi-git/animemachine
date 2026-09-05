#!/usr/bin/env sh
set -eu
root=${1:-.}
root=$(CDPATH= cd -- "$root" && pwd)
[ -f "$root/.env.example" ] || {
  printf '.env.example was not found in %s\n' "$root" >&2
  exit 1
}
managed_qbt=0
managed_ani=0
if [ -f "$root/compose.yaml" ]; then
  grep -Eq '^[[:space:]]+qbt-bootstrap:' "$root/compose.yaml" && managed_qbt=1 || true
  grep -Eq '^[[:space:]]+ani-rss-bootstrap:' "$root/compose.yaml" && managed_ani=1 || true
  if [ "$managed_qbt" -eq 1 ] || [ "$managed_ani" -eq 1 ]; then
    command -v docker >/dev/null 2>&1 || { printf '%s\n' 'Docker Compose 2.20.3 or newer is required.' >&2; exit 1; }
    compose_version=$(docker compose version --short 2>/dev/null | sed 's/^v//' | sed 's/[^0-9.].*$//')
    awk -v v="$compose_version" 'BEGIN { split(v,a,"."); ok=(a[1]>2 || (a[1]==2 && (a[2]>20 || (a[2]==20 && a[3]>=3)))); exit ok?0:1 }' || {
      printf 'Docker Compose 2.20.3 or newer is required (found %s).\n' "${compose_version:-unknown}" >&2
      exit 1
    }
  fi
fi
mkdir -p "$root/config/qbittorrent" "$root/config/ani-rss" "$root/config/incomplete" \
  "$root/data" "$root/imports" "$root/torrents" "$root/library" \
  "$root/external/read-only" "$root/external/ani-rss"
[ -f "$root/.env" ] || cp "$root/.env.example" "$root/.env"

random_key() {
  prefix=$1
  if command -v openssl >/dev/null 2>&1; then
    value=$(openssl rand -hex 24)
  else
    value=$(od -An -N24 -tx1 /dev/urandom | tr -d ' \n')
  fi
  printf '%s%s' "$prefix" "$value"
}
qbt_key() {
  if command -v openssl >/dev/null 2>&1; then
    value=$(openssl rand -base64 21 | tr '+/' 'AB' | tr -d '=\n')
  else
    value=$(od -An -N21 -tx1 /dev/urandom | tr -d ' \n' | cut -c1-28)
  fi
  printf 'qbt_%s' "$value"
}
set_key_if_empty() {
  name=$1
  value=$2
  current=$(sed -n "s/^$name=//p" "$root/.env" | head -n 1)
  [ -n "$current" ] && return 0
  awk -v key="$name" -v replacement="$name=$value" '
    BEGIN { found=0 }
    index($0, key "=")==1 { print replacement; found=1; next }
    { print }
    END { if (!found) print replacement }
  ' "$root/.env" > "$root/.env.tmp"
  mv "$root/.env.tmp" "$root/.env"
}
value() { sed -n "s/^$1=//p" "$root/.env" | head -n 1; }
had_qbt_web=$(value ANM_QBT_WEB_PASSWORD)
had_ani_key=$(value ANM_ANI_RSS_API_KEY)
had_admin=$(value ANM_ADMIN_PASSWORD)
if [ "$managed_qbt" -eq 1 ]; then
  set_key_if_empty ANM_QBT_API_KEY "$(qbt_key)"
  set_key_if_empty ANM_QBT_WEB_PASSWORD "$(random_key qbtweb_)"
fi
if [ "$managed_ani" -eq 1 ]; then
  set_key_if_empty ANM_ANI_RSS_API_KEY "$(random_key ani_)"
fi
set_key_if_empty ANM_ADMIN_PASSWORD "$(random_key anm_)"
chmod 600 "$root/.env"
show_qbt=0; show_ani=0
[ "$managed_qbt" -eq 1 ] && [ -z "$had_qbt_web" ] && show_qbt=1
[ "$managed_ani" -eq 1 ] && [ -z "$had_ani_key" ] && show_ani=1
if [ -z "$had_admin" ] || [ "$show_qbt" -eq 1 ] || [ "$show_ani" -eq 1 ]; then
  printf '\n%s\n' 'AnimeMachine initial access'
  if [ -z "$had_admin" ]; then
    web_port=$(value ANM_WEB_PORT); [ -n "$web_port" ] || web_port=8787
    printf '  AnimeMachine  url=http://localhost:%s  user=%s  password=%s\n' "$web_port" "$(value ANM_ADMIN_USERNAME)" "$(value ANM_ADMIN_PASSWORD)"
  fi
  [ "$show_qbt" -eq 0 ] || printf '  qBittorrent  user=%s  password=%s\n' "$(value ANM_QBT_WEB_USERNAME)" "$(value ANM_QBT_WEB_PASSWORD)"
  [ "$show_ani" -eq 0 ] || printf '  Ani-RSS      API key=%s\n' "$(value ANM_ANI_RSS_API_KEY)"
  printf 'Private values are stored in %s\n' "$root/.env"
else
  printf 'Existing credentials preserved in %s\n' "$root/.env"
fi
