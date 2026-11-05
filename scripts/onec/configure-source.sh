#!/usr/bin/env bash
set -Eeuo pipefail

ENV_FILE="${TASKFORGE_ENV_FILE:-/etc/taskforge/taskforge.env}"
CONFIG_ROOT="${TASKFORGE_CONFIG_ROOT:-/etc/taskforge}"
INSTALL_DIR="$CONFIG_ROOT/onec"
DEFAULT_INSTALL_FILE="$INSTALL_DIR/installer.run"

usage(){
  cat <<'TXT'
Usage:
  sudo bash ./cluster.sh onec seed --file /path/setup-full.run --version X.Y.Z.BUILD
  sudo bash ./cluster.sh onec seed --url URL --version X.Y.Z.BUILD --sha256 HEX
  sudo bash ./cluster.sh onec configure --file /path/setup-full.run --version X.Y.Z.BUILD
  sudo bash ./cluster.sh onec configure --url URL --version X.Y.Z.BUILD --sha256 HEX
  sudo bash ./cluster.sh onec disable
  sudo bash ./cluster.sh onec status

Cluster seed (recommended):
  Run once on the current Patroni Primary. The verified installer is split into
  chunks in that node's MinIO and becomes the cluster-wide desired 1C runtime.
  Every other node then adopts it automatically during migrate and stores its
  own verified MinIO copy.

Options:
  --file PATH       Seed from an already downloaded official Linux .run installer.
                    SHA-256 is calculated automatically.
  --url URL         Download once on Primary from the official/external URL. Requires --sha256.
  --version VALUE   Exact 1C platform version associated with the installer.
  --sha256 HEX      Expected installer SHA-256 (required for URL source).
  --components CSV  Installer components. Default: client_full,client_thin_fib,ru
  --curl-config PATH Extra curl config for authenticated official/private URL seeding.
TXT
}

die(){ echo "error: $*" >&2; exit 2; }
need_root(){ [ "$(id -u)" -eq 0 ] || die 'run with sudo'; }

read_env_value(){
  local key="$1"
  [ -s "$ENV_FILE" ] || return 0
  python3 - "$ENV_FILE" "$key" <<'PY'
import sys
path,key=sys.argv[1:]
value=''
for raw in open(path, encoding='utf-8'):
    line=raw.strip()
    if not line or line.startswith('#') or '=' not in line:
        continue
    k,v=line.split('=',1)
    if k.strip()==key:
        value=v.strip()
print(value)
PY
}

update_env(){
  [ -s "$ENV_FILE" ] || die "environment missing: $ENV_FILE"
  python3 - "$ENV_FILE" "$@" <<'PY'
from pathlib import Path
import os,sys,tempfile
path=Path(sys.argv[1])
updates={}
for item in sys.argv[2:]:
    k,v=item.split('=',1)
    updates[k]=v
lines=path.read_text(encoding='utf-8').splitlines()
out=[]
seen=set()
for line in lines:
    stripped=line.strip()
    if stripped and not stripped.startswith('#') and '=' in line:
        key=line.split('=',1)[0].strip()
        if key in updates:
            out.append(f"{key}={updates[key]}")
            seen.add(key)
            continue
    out.append(line)
for key,value in updates.items():
    if key not in seen:
        out.append(f"{key}={value}")
text='\n'.join(out)+'\n'
fd,tmp=tempfile.mkstemp(prefix=path.name+'.', dir=str(path.parent))
try:
    os.write(fd, text.encode())
finally:
    os.close(fd)
os.chmod(tmp, 0o600)
os.replace(tmp, path)
PY
}

show_status(){
  local enabled version source sha file marker
  enabled="$(read_env_value ONEC_ENABLED)"; enabled="${enabled:-false}"
  version="$(read_env_value ONEC_PLATFORM_VERSION)"
  source="$(read_env_value ONEC_INSTALL_SOURCE)"; source="${source:-auto}"
  sha="$(read_env_value ONEC_INSTALL_SHA256)"
  file="$(read_env_value ONEC_INSTALL_FILE)"; file="${file:-$DEFAULT_INSTALL_FILE}"
  printf 'onec_enabled=%s\n' "$enabled"
  printf 'platform_version=%s\n' "${version:--}"
  printf 'install_source=%s\n' "$source"
  if [ -n "$sha" ]; then printf 'installer_sha256=%s...%s\n' "${sha:0:12}" "${sha: -8}"; else printf 'installer_sha256=-\n'; fi
  if [ "$source" = file ] || { [ "$source" = auto ] && [ -e "$file" ]; }; then
    if [ -s "$file" ]; then printf 'installer_file=%s present=yes\n' "$file"; else printf 'installer_file=%s present=no\n' "$file"; fi
  fi
  marker=""
  if command -v docker >/dev/null 2>&1; then
    local project volume
    project="$(read_env_value COMPOSE_PROJECT_NAME)"; project="${project:-taskforge-prod}"
    volume="${project}_onec-platform"
    if docker volume inspect "$volume" >/dev/null 2>&1; then
      mountpoint="$(docker volume inspect -f '{{.Mountpoint}}' "$volume" 2>/dev/null || true)"
      if [ -n "$mountpoint" ] && [ -r "$mountpoint/.taskforge-platform" ]; then
        marker="$(cat "$mountpoint/.taskforge-platform" 2>/dev/null || true)"
      fi
    fi
  fi
  if [ -n "$marker" ]; then
    printf 'installed_platform=yes\n%s\n' "$marker"
  else
    printf 'installed_platform=not-detected\n'
  fi
  dist="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/cluster/ops/current/onec-distribution.sh"
  if [ "$(id -u)" -eq 0 ] && [ -x "$dist" ] && [ -s "${TASKFORGE_CONFIG_ROOT:-/etc/taskforge}/cluster.json" ]; then
    "$dist" status 2>/dev/null || true
  fi
}

cmd="${1:-status}"; [ $# -eq 0 ] || shift
case "$cmd" in
  seed)
    need_root
    dist="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/cluster/ops/current/onec-distribution.sh"
    [ -x "$dist" ] || die 'cluster 1C distribution helper is missing'
    exec "$dist" seed "$@"
    ;;
  configure)
    need_root
    mode='' source_value='' version='' sha='' components='client_full,client_thin_fib,ru'
    while [ $# -gt 0 ]; do
      case "$1" in
        --file) [ $# -ge 2 ] || die '--file requires a path'; [ -z "$mode" ] || die 'choose either --file or --url'; mode=file; source_value="$2"; shift 2 ;;
        --url) [ $# -ge 2 ] || die '--url requires a value'; [ -z "$mode" ] || die 'choose either --file or --url'; mode=url; source_value="$2"; shift 2 ;;
        --version) [ $# -ge 2 ] || die '--version requires a value'; version="$2"; shift 2 ;;
        --sha256) [ $# -ge 2 ] || die '--sha256 requires a value'; sha="${2,,}"; shift 2 ;;
        --components) [ $# -ge 2 ] || die '--components requires a value'; components="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option: $1" ;;
      esac
    done
    [ -n "$mode" ] || die 'configure requires --file or --url'
    [ -n "$version" ] || die 'configure requires --version'
    [[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || die '--version must use exact A.B.C.D numeric form'
    mkdir -p "$INSTALL_DIR"; chmod 0700 "$INSTALL_DIR"
    if [ "$mode" = file ]; then
      [ -s "$source_value" ] || die "installer not found: $source_value"
      sha="$(sha256sum "$source_value" | awk '{print tolower($1)}')"
      tmp="$DEFAULT_INSTALL_FILE.tmp.$$"
      cp -f -- "$source_value" "$tmp"
      chmod 0600 "$tmp"
      mv -f "$tmp" "$DEFAULT_INSTALL_FILE"
      update_env \
        ONEC_ENABLED=true \
        "ONEC_PLATFORM_VERSION=$version" \
        ONEC_INSTALL_SOURCE=file \
        ONEC_INSTALL_URL= \
        "ONEC_INSTALL_FILE=$DEFAULT_INSTALL_FILE" \
        "ONEC_INSTALL_SHA256=$sha" \
        "ONEC_INSTALL_COMPONENTS=$components"
      echo "1C source configured: file=$DEFAULT_INSTALL_FILE version=$version sha256=$sha"
    else
      case "$sha" in ''|*[!0-9a-f]*) die '--url requires a hexadecimal --sha256' ;; esac
      [ "${#sha}" -eq 64 ] || die '--sha256 must contain exactly 64 hex characters'
      update_env \
        ONEC_ENABLED=true \
        "ONEC_PLATFORM_VERSION=$version" \
        ONEC_INSTALL_SOURCE=url \
        "ONEC_INSTALL_URL=$source_value" \
        "ONEC_INSTALL_FILE=$DEFAULT_INSTALL_FILE" \
        "ONEC_INSTALL_SHA256=$sha" \
        "ONEC_INSTALL_COMPONENTS=$components"
      echo "1C source configured: url=yes version=$version sha256=$sha"
    fi
    echo 'Run: sudo bash ./cluster.sh migrate'
    ;;
  disable)
    need_root
    update_env ONEC_ENABLED=false
    echo '1C disabled in environment. Persistent platform/runtime volumes are preserved.'
    ;;
  status)
    show_status
    ;;
  -h|--help|help)
    usage
    ;;
  *) die "unknown onec command: $cmd" ;;
esac
