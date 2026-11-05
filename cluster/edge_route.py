#!/usr/bin/env python3
"""Public HTTP-01 path proof. Uses real DNS, fresh node-local nonces and curl.

This proves the route from this host, not propagation to every Cloudflare POP or
all ACME validation locations. It deliberately does not follow HTTP redirects:
a first-certificate bootstrap must work over public port 80 without origin TLS.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import tempfile
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit


class RouteError(RuntimeError):
    pass


def normalize_hosts(values: list[str]) -> list[str]:
    result: list[str] = []
    for raw in values:
        host = str(raw).strip().rstrip('.').lower()
        if not host:
            continue
        try:
            host = host.encode('idna').decode('ascii')
        except UnicodeError as exc:
            raise RouteError('invalid public hostname') from exc
        if (len(host) > 253 or not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', host)
                or any(not label or len(label) > 63 or label.startswith('-') or label.endswith('-')
                       for label in host.split('.'))):
            raise RouteError(f'invalid public hostname: {host!r}')
        if host not in result:
            result.append(host)
    if not result:
        raise RouteError('no public hostnames to verify')
    return result


def header_map(raw: str) -> dict[str, str]:
    # curl can include an interim response. Only the last response block counts.
    blocks = re.split(r'\r?\n\r?\n', raw.strip())
    response = next((x for x in reversed(blocks) if x.startswith('HTTP/')), '')
    headers: dict[str, str] = {}
    for line in response.splitlines()[1:]:
        if ':' in line:
            key, value = line.split(':', 1)
            headers[key.strip().lower()] = value.strip()
    return headers


class PublicRoute:
    def __init__(self, webroot: Path, hosts: list[str], node_id: str,
                 require_cloudflare: bool, command: Callable):
        self.webroot = webroot
        self.hosts = normalize_hosts(hosts)
        self.node_id = node_id
        self.require_cloudflare = require_cloudflare
        self.command = command

    def probe(self, scheme: str = 'http', timeout: float = 5) -> dict:
        if scheme not in {'http', 'https'}:
            raise RouteError('unsupported public route protocol')
        if not self.webroot.is_dir():
            raise RouteError('existing certbot-webroot volume is unavailable')
        directory = self.webroot / '.well-known' / 'acme-challenge'
        # Refuse symlink traversal rather than writing outside the proven volume.
        for parent in (self.webroot / '.well-known', directory):
            if parent.is_symlink():
                raise RouteError('ACME challenge directory must not be a symlink')
            parent.mkdir(mode=0o755, exist_ok=True)
            parent.chmod(0o755)
        token = 'taskforge-edge-' + secrets.token_hex(24)
        value = f'taskforge-route node={self.node_id} nonce={secrets.token_hex(32)}\n'
        marker = directory / token
        fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                handle.write(value)
            marker.chmod(0o644)
            results = []
            for host in self.hosts:
                with tempfile.TemporaryDirectory(prefix='taskforge-edge-probe-') as raw:
                    temp = Path(raw)
                    body = temp / 'body'
                    headers = temp / 'headers'
                    url = (f'http://{host}/.well-known/acme-challenge/{token}' if scheme == 'http'
                           else f'https://{host}/ha/live?taskforge_probe={token}')
                    # -q ignores curlrc; --noproxy ignores HTTP_PROXY; no --resolve,
                    # no hosts override, no -k, no redirect following, no retries.
                    args = ['curl', '-q', '-4', '-sS', '--noproxy', '*',
                            '--connect-timeout', str(min(3, timeout)),
                            '--max-time', str(timeout), '--retry', '0',
                            '--proto', '=' + scheme, '--max-filesize', '16384',
                            '-H', 'Cache-Control: no-cache, no-store',
                            '-H', 'Pragma: no-cache', '-D', str(headers),
                            '-o', str(body), '-w', '%{http_code}', url]
                    proc = self.command(args, timeout=timeout + 2)
                    status = proc.stdout.strip()[-3:] if proc.stdout else '000'
                    hdr = header_map(headers.read_text(errors='replace')) if headers.is_file() else {}
                    actual = body.read_text(errors='replace') if body.is_file() else ''
                    reason = ''
                    if proc.returncode:
                        reason = f'curl={proc.returncode} http={status}'
                    elif status != '200':
                        reason = f'http={status}'
                        if status.startswith('3'):
                            dest = urlsplit(hdr.get('location', ''))
                            reason += f' redirect={dest.scheme or "relative"}://{dest.netloc or host}; exempt ACME path from redirects'
                    elif not self._body_matches(scheme, actual, value):
                        reason = 'wrong-node-or-wrong-body'
                    elif self.require_cloudflare and not hdr.get('cf-ray'):
                        reason = 'cloudflare-response-not-proven (CF-Ray missing)'
                    elif hdr.get('cf-cache-status', '').upper() in {'HIT', 'STALE', 'UPDATING', 'REVALIDATED'}:
                        reason = 'cached-response-rejected'
                    results.append({'host': host, 'scheme': scheme, 'http': status,
                                    'ok': not reason, 'reason': reason,
                                    'cloudflare': bool(hdr.get('cf-ray'))})
            return {'ok': all(row['ok'] for row in results), 'hosts': results}
        finally:
            marker.unlink(missing_ok=True)

    def _body_matches(self, scheme: str, actual: str, value: str) -> bool:
        if scheme == 'http':
            return actual == value
        # The current gateway exposes /ha/live on HTTPS, but serves ACME files
        # only in its HTTP server block. Do not require a nonexistent TLS ACME
        # location and deadlock a valid certificate deployment.
        try:
            doc = json.loads(actual)
        except (ValueError, TypeError):
            return False
        return isinstance(doc, dict) and doc.get('node') == self.node_id and doc.get('status') == 'ok' and doc.get('service') == 'gateway'
