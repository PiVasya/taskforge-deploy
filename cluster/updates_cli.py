#!/usr/bin/env python3
"""Read local updater state or request an immediate registry check (no role change)."""
import json
import os
import sys
from pathlib import Path

runtime = Path(os.environ.get('TASKFORGE_CLUSTER_RUNTIME', '/var/lib/taskforge/cluster'))
action = sys.argv[1] if len(sys.argv) > 1 else 'status'
if action == 'check':
    if os.geteuid() != 0:
        raise SystemExit('Run with sudo: cluster.sh updates check')
    runtime.mkdir(parents=True, exist_ok=True)
    with (runtime / 'updates-check-request').open('w') as handle:
        os.chmod(handle.name, 0o600)
        handle.write('check\n')
    print('Registry check requested. The existing Node Agent processes it automatically.')
elif action == 'status':
    path = runtime / 'application-updates.json'
    if not path.exists():
        raise SystemExit('No updater state yet. Check taskforge-cluster.service.')
    data = json.loads(path.read_text())
    for service, row in sorted(data.get('services', {}).items()):
        print(f"{service}: state={row.get('status','unknown')} current={row.get('current_image','-')} target={row.get('target_image','-')} error={row.get('error') or row.get('pull_error') or '-'} prepare_reason={row.get('last_prepare_reason','-')} attempts={row.get('attempts',0)}")
else:
    raise SystemExit('Usage: cluster.sh updates status|check')
