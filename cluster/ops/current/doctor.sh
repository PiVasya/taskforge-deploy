#!/usr/bin/env bash
set -Eeuo pipefail
[ -s /opt/taskforge/control/REVISION ] && [ "$(tr -d '\r\n' < /opt/taskforge/control/REVISION)" = 410509 ] || { echo 'error: v41.5.9 stable control is not installed' >&2; exit 2; }
python3 /opt/taskforge/control/cluster/ops/current/status.py
TASKFORGE_CLUSTER_FORCE=1 bash /opt/taskforge/control/compose.sh config -q
echo 'compose_config=OK'
if [ "$(id -u)" -eq 0 ]; then
  if bash /opt/taskforge/control/cluster/ops/current/local-cert.sh status >/dev/null 2>&1; then echo 'local_certbot_certificate=READY source=local-volume'; else echo 'local_certbot_certificate=NOT_PRESENT_OR_INVALID source=local-volume'; fi
else
  echo 'local_certbot_certificate=ROOT_CHECK_REQUIRED (see local_cert_ready in Agent status above)'
fi
bash /opt/taskforge/control/cluster/ops/current/mount-audit.sh
node="$(tr -d '\r\n' < /var/lib/taskforge/cluster/node-id 2>/dev/null || true)"
read -r profile eligible <<<"$(python3 -S - /etc/taskforge/cluster.json "$node" <<'PY'
import json,sys
try:
    c=json.load(open(sys.argv[1])); n=next(x for x in c['nodes'] if str(x['id'])==sys.argv[2]); a=n.get('app') or {}
    print(str(a.get('profile','full')), 'yes' if a.get('can_be_primary',True) and str(n.get('public_host','')).strip() else 'no')
except Exception: print('unknown no')
PY
)"
if [ "$eligible" = yes ]; then
  if [ -s /etc/taskforge/cloudflare-api-token ] && [ -s /etc/taskforge/cloudflare-failover.json ]; then
    if [ "$(id -u)" -eq 0 ]; then
      python3 /opt/taskforge/control/cluster/cloudflare_dns.py preflight >/dev/null
      echo "cloudflare_dns_failover=READY profile=$profile"
    else
      echo "cloudflare_dns_failover=CONFIGURED_ROOT_CHECK_REQUIRED profile=$profile"
    fi
  else
    echo "cloudflare_dns_failover=NOT_CONFIGURED profile=$profile"
  fi
else
  echo "cloudflare_dns_failover=NOT_REQUIRED profile=$profile"
fi
systemctl --no-pager --full status taskforge-cluster.service 2>/dev/null | sed -n '1,18p' || true
echo 'doctor=PASS'
