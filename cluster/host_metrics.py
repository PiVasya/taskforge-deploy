"""Bounded host measurements; never synthesize missing samples as zero load."""
from __future__ import annotations
import json
import math
import os
import platform
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path


def stamp(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec='seconds')


def cpu_ticks(text):
    row = next((line.split()[1:] for line in text.splitlines() if line.startswith('cpu ')), [])
    if len(row) < 4:
        return None
    # guest/guest_nice are already included in user/nice.
    numbers = [int(value) for value in row[:8]]
    return numbers + [0] * (8 - len(numbers))


def cpu_delta(previous, current):
    if previous is None or current is None:
        return None, None, None
    changes = [b - a for a, b in zip(previous, current)]
    if any(value < 0 for value in changes) or sum(changes) <= 0:
        return None, None, None
    total = sum(changes)
    busy = sum(changes[index] for index in (0, 1, 2, 5, 6))
    return tuple(round(100 * value / total, 2) for value in (busy, changes[4], changes[7]))


def memory_values(text):
    values = {}
    for line in text.splitlines():
        parts = line.replace(':', '').split()
        if len(parts) >= 2:
            try:
                values[parts[0]] = int(parts[1]) * 1024
            except ValueError:
                pass
    total, available = values.get('MemTotal'), values.get('MemAvailable')
    used = max(0, total - min(total, available)) if total and available is not None else None
    memory = {'total': total, 'available': available, 'used': used,
              'percent': round(100 * used / total, 2) if used is not None else None}
    st, sf = values.get('SwapTotal'), values.get('SwapFree')
    swap = {'total': st, 'used': max(0, st - sf) if st is not None and sf is not None else None}
    return memory, swap


class HostMetrics:
    def __init__(self, runtime, stop, proc=Path('/proc'), sys=Path('/sys'), interval=5, limit=720):
        self.runtime = Path(runtime)
        self.proc, self.sys, self.stop = proc, sys, stop
        self.interval = interval
        self.points = deque(maxlen=limit)
        self.lock = threading.Lock()
        self.previous = None
        self.latest = {}
        self.persist_at = 0.0
        self.boot = self._read(self.proc / 'sys/kernel/random/boot_id')
        try:
            saved = json.loads((self.runtime / 'host-metrics.json').read_text())
            now = time.time()
            if saved.get('boot_id') == self.boot:
                for point in saved.get('history', [])[-limit:]:
                    if isinstance(point, dict) and now - 3600 <= point.get('epoch', 0) <= now:
                        self.points.append(point)
        except (OSError, ValueError, TypeError):
            pass

    @staticmethod
    def _read(path):
        try:
            return path.read_text().strip()
        except OSError:
            return ''

    def temperatures(self):
        readings, fans = [], []
        # Select CPU package/core sensors, not GPU, disk, ambient or batteries.
        for hw in sorted((self.sys / 'class/hwmon').glob('hwmon*')):
            name = self._read(hw / 'name')
            for entry in sorted(hw.glob('temp*_input')):
                label = self._read(entry.with_name(entry.name.replace('_input', '_label')))
                is_cpu = name in {'coretemp', 'k10temp', 'zenpower', 'cpu_thermal'} or label.lower() in {'cpu', 'processor', 'tctl', 'tdie'}
                if not is_cpu:
                    continue
                try:
                    value = float(self._read(entry)) / 1000
                    if not math.isfinite(value) or not 0 < value < 130:
                        continue
                    readings.append({'sensor': name + ':' + (label or entry.name), 'c': round(value, 1)})
                except ValueError:
                    pass
            for entry in sorted(hw.glob('fan*_input')):
                try:
                    value = int(self._read(entry))
                    if 0 <= value <= 50000:
                        fans.append({'sensor': name + ':' + entry.name, 'rpm': value})
                except ValueError:
                    pass
        if not readings:
            for zone in sorted((self.sys / 'class/thermal').glob('thermal_zone*')):
                name = self._read(zone / 'type')
                if not any(token in name.lower() for token in ('x86_pkg', 'cpu', 'soc_thermal')):
                    continue
                try:
                    value = float(self._read(zone / 'temp')) / 1000
                    if 0 < value < 130:
                        readings.append({'sensor': name, 'c': round(value, 1)})
                except ValueError:
                    pass
        return readings[:32], fans[:16]

    def sample(self, epoch=None):
        epoch = time.time() if epoch is None else epoch
        try:
            current = cpu_ticks(self._read(self.proc / 'stat'))
        except ValueError:
            current = None
        cpu, wait, steal = cpu_delta(self.previous, current)
        self.previous = current
        mem, swap = memory_values(self._read(self.proc / 'meminfo'))
        temperatures, fans = self.temperatures()
        temp = max((item['c'] for item in temperatures), default=None)
        try:
            load = [float(x) for x in self._read(self.proc / 'loadavg').split()[:3]]
            uptime = int(float(self._read(self.proc / 'uptime').split()[0]))
        except (ValueError, IndexError):
            load, uptime = [], None
        point = {'at': stamp(epoch), 'epoch': epoch, 'cpu_percent': cpu, 'iowait_percent': wait,
                 'steal_percent': steal, 'memory_percent': mem['percent'], 'memory_used_bytes': mem['used'],
                 'memory_total_bytes': mem['total'], 'temperature_c': temp,
                 'fan_rpm': max((f['rpm'] for f in fans), default=None)}
        with self.lock:
            self.points.append(point)
            self.latest = {'status': 'ok' if current and mem['total'] else 'partial', 'sampled_at': point['at'],
                           'hostname': platform.node(), 'kernel': platform.release(), 'cpu_count': os.cpu_count(),
                           'cpu_percent': cpu, 'iowait_percent': wait, 'steal_percent': steal, 'memory': mem,
                           'swap': swap, 'load': load, 'uptime_seconds': uptime, 'temperature_c': temp,
                           'temperature_sensors': temperatures, 'fans': fans, 'sample_interval_seconds': self.interval}
        if epoch - self.persist_at >= 60:
            self.persist_at = epoch
            self.persist()
        return point

    def snapshot(self):
        with self.lock:
            return {**self.latest, 'history': list(self.points)}

    def persist(self):
        try:
            self.runtime.mkdir(parents=True, exist_ok=True)
            target = self.runtime / 'host-metrics.json'
            tmp = target.with_suffix('.tmp')
            with self.lock:
                value = {'boot_id': self.boot, 'history': list(self.points)}
            tmp.write_text(json.dumps(value, separators=(',', ':'), allow_nan=False))
            tmp.chmod(0o600)
            os.replace(tmp, target)
        except OSError:
            pass

    def loop(self):
        while not self.stop.is_set():
            try:
                self.sample()
            except Exception as error:
                print('[TFMETRICS] sample failed: ' + type(error).__name__, flush=True)
                self.previous = None
            self.stop.wait(self.interval)
        self.persist()
