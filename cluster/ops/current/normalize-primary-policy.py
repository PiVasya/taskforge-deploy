#!/usr/bin/env python3
from __future__ import annotations

import argparse
import grp
import json
import os
import pwd
import stat
import tempfile
from pathlib import Path


def read_required_name(path: Path, label: str) -> str:
    if not path.is_file():
        raise SystemExit(f'error: {label} file missing: {path}')
    value = path.read_text(encoding='utf-8').strip()
    if not value:
        raise SystemExit(f'error: {label} file is empty: {path}')
    return value


def resolve_owner(user_file: Path, group_file: Path) -> tuple[int, int, str, str]:
    user = read_required_name(user_file, 'operator-user')
    group = read_required_name(group_file, 'operator-group')
    try:
        uid = pwd.getpwnam(user).pw_uid
    except KeyError:
        raise SystemExit(f'error: operator user does not exist: {user}')
    try:
        gid = grp.getgrnam(group).gr_gid
    except KeyError:
        raise SystemExit(f'error: operator group does not exist: {group}')
    return uid, gid, user, group


def atomic_write(path: Path, text: str, mode: int, uid: int, gid: int) -> None:
    fd, tmp_raw = tempfile.mkstemp(prefix='.' + path.name + '.r55.', dir=str(path.parent), text=True)
    tmp = Path(tmp_raw)
    try:
        os.fchmod(fd, mode)
        os.fchown(fd, uid, gid)
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        dir_fd = os.open(path.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def update(path: Path, cluster: bool, uid: int, gid: int, owner_label: str) -> None:
    if not path.is_file():
        return
    st = path.stat()
    mode = stat.S_IMODE(st.st_mode)
    raw = path.read_text(encoding='utf-8')
    doc = json.loads(raw)
    changed = False

    if cluster:
        old = doc.pop('maximum_lag_on_failback_bytes', None)
        if 'maximum_lag_on_switchover_bytes' not in doc:
            doc['maximum_lag_on_switchover_bytes'] = int(old if old is not None else 16777216)
            changed = True
        elif old is not None:
            changed = True
        for key in ('preferred_primary', 'automatic_failback', 'failback_delay_seconds', 'failback_stable_seconds'):
            if key in doc:
                doc.pop(key)
                changed = True
        expected_profiles = {'A': 'full', 'B': 'full', 'C': 'lite'}
        for node in doc.get('nodes', []):
            node_id = str(node.get('id', ''))
            app = node.setdefault('app', {})
            expected = expected_profiles.get(node_id)
            if expected and app.get('profile') != expected:
                app['profile'] = expected
                changed = True
            if node_id in expected_profiles and app.get('can_be_primary') is not True:
                app['can_be_primary'] = True
                changed = True
    elif 'preferred_primary' in doc:
        doc.pop('preferred_primary')
        changed = True

    if changed:
        backup = path.with_name(path.name + '.pre-r55')
        if not backup.exists():
            atomic_write(backup, raw, mode, uid, gid)
        else:
            os.chown(backup, uid, gid)
            os.chmod(backup, mode)
        rendered = json.dumps(doc, ensure_ascii=False, indent=2) + '\n'
        atomic_write(path, rendered, mode, uid, gid)
        print(f'policy_normalized={path} backup={backup}')
    else:
        # Ownership is part of the live-state contract even when JSON content is unchanged.
        os.chown(path, uid, gid)
        os.chmod(path, mode)
        print(f'policy_normalized={path} unchanged')

    final = path.stat()
    if final.st_uid != uid or final.st_gid != gid:
        raise SystemExit(f'error: failed to restore operator ownership on {path}: expected {owner_label}')
    if stat.S_IMODE(final.st_mode) != mode:
        raise SystemExit(f'error: mode changed unexpectedly on {path}')
    print(f'operator_owner={path} owner={owner_label} mode={mode:04o}')


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--cluster', default='/etc/taskforge/cluster.json')
    ap.add_argument('--inventory', default='/etc/taskforge/inventory.json')
    ap.add_argument('--operator-user-file', default='/etc/taskforge/operator-user')
    ap.add_argument('--operator-group-file', default='/etc/taskforge/operator-group')
    args = ap.parse_args()

    uid, gid, user, group = resolve_owner(Path(args.operator_user_file), Path(args.operator_group_file))
    label = f'{user}:{group}'
    update(Path(args.cluster), True, uid, gid, label)
    update(Path(args.inventory), False, uid, gid, label)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
