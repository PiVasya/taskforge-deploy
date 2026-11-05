#!/usr/bin/env bash
set -Eeuo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$(cd "$HERE/../.." && pwd)/lib/common.sh"
cluster_runtime_prepare
out="$TASKFORGE_CLUSTER_RUNTIME/bin/mc"
mkdir -p "$(dirname "$out")"
chmod 700 "$(dirname "$out")" 2>/dev/null || true
cluster_chown_owner "$(dirname "$out")" 2>/dev/null || true
arch="$(uname -m)"; case "$arch" in x86_64|amd64) a=amd64;; aarch64|arm64) a=arm64;; *) echo "error: unsupported arch $arch" >&2; exit 2;; esac
url="https://dl.min.io/client/mc/release/linux-$a/mc"
command -v curl >/dev/null || { echo 'error: curl required to install MinIO mc' >&2; exit 2; }
tmp="$out.tmp.$$"
curl -fL --retry 3 --connect-timeout 10 "$url" -o "$tmp"
chmod 755 "$tmp"
cluster_chown_owner "$tmp" 2>/dev/null || true
mv -f "$tmp" "$out"
"$out" --version
