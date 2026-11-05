#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
DEST=/opt/taskforge/control
[ "$(id -u)" -eq 0 ] || { echo 'error: install-control requires root' >&2; exit 2; }
mkdir -p /opt/taskforge
tmp="/opt/taskforge/.control-v41.5.9.$$"
old="/opt/taskforge/.control-v41.5.9-old.$$"
rm -rf "$tmp" "$old"
mkdir -p "$tmp"
cp -a "$ROOT/." "$tmp/"
rm -rf "$tmp/.runtime" "$tmp/tests" 2>/dev/null || true
[ "$(tr -d '\r\n' < "$tmp/REVISION")" = 410509 ] || { rm -rf "$tmp"; echo 'error: staged control revision mismatch' >&2; exit 2; }

# v41.5.9 is state-based and may be re-run from a refreshed v41.5.9 bundle.  Do not
# silently keep an older /opt/taskforge/control merely because REVISION is the
# same: topology-only releases (such as unified runners) must refresh Compose
# and Agent code as well.
if [ -e "$DEST" ]; then
  mv "$DEST" "$old"
fi
if ! mv "$tmp" "$DEST"; then
  rm -rf "$tmp"
  if [ -e "$old" ]; then mv "$old" "$DEST"; fi
  echo 'error: failed to install refreshed v41.5.9 control plane' >&2
  exit 2
fi
chmod -R a+rX "$DEST"
rm -rf "$old"
echo 'PASS: v41.5.9 stable control plane refreshed at /opt/taskforge/control'
