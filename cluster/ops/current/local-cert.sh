#!/usr/bin/env bash
set -Eeuo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/lib/common.sh"
cluster_need_env
mode="${1:-status}"
case "$mode" in status|volume-path|webroot-path) ;; *) cluster_die 'usage: local-cert.sh status|volume-path|webroot-path';; esac
project="$(cluster_project)"
domain="$(cluster_read_env DOMAIN)"
ct_domain="$(cluster_read_env CT_DOMAIN)"
[ -n "$domain" ] || cluster_die 'DOMAIN is empty'

find_volume(){
  local suffix="$1" target selected pinned live preferred v meta
  case "$suffix" in letsencrypt) target=/etc/letsencrypt ;; certbot-webroot) target=/var/www/certbot ;; *) return 1;; esac
  # Prefer the established baseline and actual gateway mount, not a newly
  # invented conventional name. Never create a volume during certificate checks.
  pinned="$(python3 -S - "$TASKFORGE_CLUSTER_RUNTIME/storage-baseline-r55.json" "$suffix" <<'PY_VOLUME'
import json,sys
from pathlib import Path
p=Path(sys.argv[1])
if p.is_file():
    try: print((json.loads(p.read_text()).get('logical_volumes') or {}).get(sys.argv[2],''))
    except Exception: raise SystemExit(2)
PY_VOLUME
)" || return 2
  live="$(docker inspect -f '{{range .Mounts}}{{if and (eq .Type "volume") (eq .Destination "'"$target"'")}}{{println .Name}}{{end}}{{end}}' "$project-gateway-1" 2>/dev/null || true)"
  live="$(printf '%s' "$live" | tr -d '\r\n')"
  if [ -n "$pinned" ] && [ -n "$live" ] && [ "$pinned" != "$live" ]; then
    echo "error: $suffix gateway mount disagrees with persistent-volume baseline" >&2; return 2
  fi
  selected="${pinned:-$live}"
  if [ -n "$selected" ]; then
    docker volume inspect "$selected" >/dev/null 2>&1 || return 1
    printf '%s\n' "$selected"; return 0
  fi
  preferred="${project}_${suffix}"
  selected=''
  while IFS= read -r v; do
    [ -n "$v" ] || continue
    meta="$(docker volume inspect -f '{{index .Labels "com.docker.compose.project"}}|{{index .Labels "com.docker.compose.volume"}}' "$v" 2>/dev/null || true)"
    if [ "$meta" = "$project|$suffix" ] || [ "$v" = "$preferred" ]; then
      [ -z "$selected" ] || { echo "error: ambiguous existing $suffix volume" >&2; return 2; }
      selected="$v"
    fi
  done < <(docker volume ls -q 2>/dev/null || true)
  [ -n "$selected" ] || return 1
  printf '%s\n' "$selected"
}
ensure_volume(){
  local v
  v="$(find_volume "$1")" || cluster_die "existing TaskForge Docker volume unavailable/ambiguous: $1"
  printf '%s\n' "$v"
}
mountpoint(){ docker volume inspect -f '{{.Mountpoint}}' "$1" 2>/dev/null; }

if [ "$mode" = volume-path ]; then v="$(ensure_volume letsencrypt)"; mountpoint "$v"; exit 0; fi
if [ "$mode" = webroot-path ]; then v="$(ensure_volume certbot-webroot)"; mountpoint "$v"; exit 0; fi

v="$(find_volume letsencrypt 2>/dev/null || true)"
[ -n "$v" ] || exit 1
mp="$(mountpoint "$v" 2>/dev/null || true)"
[ -d "$mp" ] || exit 1
cert="$mp/live/$domain/fullchain.pem"
key="$mp/live/$domain/privkey.pem"
[ -s "$cert" ] && [ -s "$key" ] || exit 1
openssl x509 -in "$cert" -noout >/dev/null 2>&1 || exit 1
openssl pkey -in "$key" -noout >/dev/null 2>&1 || exit 1
openssl x509 -in "$cert" -noout -checkhost "$domain" >/dev/null 2>&1 || exit 1
if [ -n "$ct_domain" ]; then openssl x509 -in "$cert" -noout -checkhost "$ct_domain" >/dev/null 2>&1 || exit 1; fi
openssl x509 -in "$cert" -noout -checkend 0 >/dev/null 2>&1 || exit 1
cert_pub="$(openssl x509 -in "$cert" -pubkey -noout 2>/dev/null | openssl pkey -pubin -outform DER 2>/dev/null | sha256sum | awk '{print $1}')"
key_pub="$(openssl pkey -in "$key" -pubout -outform DER 2>/dev/null | sha256sum | awk '{print $1}')"
[ -n "$cert_pub" ] && [ "$cert_pub" = "$key_pub" ] || exit 1
fp="$(openssl x509 -in "$cert" -noout -fingerprint -sha256 2>/dev/null | sed 's/^[^=]*=//;s/://g' | tr 'A-F' 'a-f')"
not_after="$(openssl x509 -in "$cert" -noout -enddate 2>/dev/null | sed 's/^notAfter=//')"
not_after_epoch="$(date -u -d "$not_after" +%s 2>/dev/null || printf 0)"
issued_on="$(openssl x509 -in "$cert" -noout -startdate 2>/dev/null | sed 's/^notBefore=//')"
python3 -S - "$v" "$domain" "$ct_domain" "$fp" "$not_after_epoch" "$issued_on" <<'PY'
import json,sys
print(json.dumps({
  'source':'local-certbot-volume',
  'volume':sys.argv[1],
  'domain':sys.argv[2],
  'ct_domain':sys.argv[3],
  'fingerprint':sys.argv[4],
  'not_after_epoch':int(sys.argv[5] or 0),
  'not_before':sys.argv[6],
},separators=(',',':')))
PY
