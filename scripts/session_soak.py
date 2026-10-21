"""Read-only, bounded connection observation. Never sends input or resumes a CLI.

Run on the board host with its tailnet HTTPS URL. Output is private and contains
only hashed identities, connection metadata and counts, never transcripts or tokens.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import time
import urllib.error
import urllib.parse
import urllib.request


def identity(row):
    source = row.get('nativeSource')
    if not isinstance(source, dict):
        source = row.get('source') if isinstance(row.get('source'), dict) else row
    if not isinstance(source, dict):
        return None
    parts = [source.get(k) for k in ('host', 'agent', 'home', 'id', 'cwd', 'sessionId')]
    if not parts[0] or not parts[1] or not parts[3]:
        return None
    if parts[1] == 'claude':
        parts[2] = parts[2] or '.claude'
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()


def sample(snapshot, terminals, host):
    matches = [r for r in snapshot.get('hosts', []) if r.get('name') == host]
    if len(matches) != 1:
        raise ValueError('Registered host missing or duplicated')
    machine = matches[0]
    rows = [{**r, 'host': host, 'agent': agent} for agent in ('codex', 'claude')
            for r in (machine.get('data') or {}).get(agent, [])]
    records = [r for r in terminals.get('terminals', []) if r.get('host') == host]
    return {'at': time.time(), 'bootId': snapshot.get('bootId'), 'online': machine.get('ok') is True,
            'refreshing': machine.get('refreshing') is True,
            'collectedAt': machine.get('fetchedAt'),
            'listed': sorted(set(filter(None, map(identity, rows)))),
            'recoverable': sorted(set(filter(None, (identity(r) for r in snapshot.get('recoverySessions', []) if r.get('host') == host)))),
            'terminals': [{'key': hashlib.sha256(str(r.get('id')).encode()).hexdigest(),
                           'source': identity(r), 'alive': r.get('alive') is True,
                           'closedReason': r.get('closedReason') if r.get('closedReason') in
                           ('released', 'user', 'connection_ended') else None}
                          for r in records]}


def compare(previous, current):
    before = {r['key']: r for r in (previous or {}).get('terminals', [])}
    events = []
    for row in current['terminals']:
        old = before.get(row['key'])
        if old and old['alive'] and not row['alive']:
            events.append({'event': 'connection_ended', 'key': row['key'], 'source': row['source'],
                           'locatable': row['source'] in current['listed'] + current['recoverable'],
                           'nativeProcessSurvival': 'not_verified'})
    # A bounded list losing a row is never evidence that the CLI ended.
    for source in set((previous or {}).get('listed', [])) - set(current['listed']):
        events.append({'event': 'outside_current_list', 'source': source,
                       'recoverable': source in current['recoverable'], 'nativeProcessSurvival': 'not_verified'})
    return events


def private_output(path):
    path = Path(path).absolute()
    for parent in [path.parent, *path.parent.parents]:
        if parent.is_symlink():
            raise ValueError('Symlink output path refused')
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = path.parent.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError('Output directory must be private')
    return os.fdopen(os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600), 'w')


def observe(base, host, output, duration=86400, interval=300, clock=time.monotonic, sleep=time.sleep):
    url = urllib.parse.urlsplit(base)
    if url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment or url.path not in ('', '/'):
        raise ValueError('An HTTPS board origin without credentials is required')
    if not 0 <= duration <= 86400 or not 60 <= interval <= 3600:
        raise ValueError('Observation must be bounded to 24 hours, interval 60–3600 seconds')
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    opener = urllib.request.build_opener(NoRedirect())
    def get(path):
        with opener.open(base.rstrip('/') + path, timeout=20) as response:
            raw = response.read(8 * 1024 * 1024 + 1)
            if len(raw) > 8 * 1024 * 1024:
                raise ValueError('Oversized response')
            return json.loads(raw)
    with private_output(output) as stream:
        started, previous, count = clock(), None, 0
        while True:
            try:
                snapshot = get('/api/snapshot?refresh=1&host=' + urllib.parse.quote(host, safe=''))
                deadline = min(started + duration, clock() + 50)
                while any(r.get('name') == host and r.get('refreshing') for r in snapshot.get('hosts', [])) and clock() < deadline:
                    sleep(min(5, deadline - clock()))
                    snapshot = get('/api/snapshot')
                current = sample(snapshot, get('/api/terminals'), host)
                record = {'sample': current, 'changes': compare(previous, current)}
                previous = current
            except (OSError, ValueError, TypeError, KeyError, urllib.error.URLError):
                record = {'at': time.time(), 'event': 'read_unavailable'}
            count += 1
            stream.write(json.dumps(record, separators=(',', ':')) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
            elapsed = clock() - started
            if elapsed >= duration:
                stream.write(json.dumps({'event': 'observation_finished', 'elapsedSeconds': elapsed,
                                         'samples': count, 'nativeProcessSurvival': 'not_verified'}) + '\n')
                return
            sleep(min(interval, duration - elapsed))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True)
    parser.add_argument('--host', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--hours', type=float, default=24)
    parser.add_argument('--interval', type=int, default=300)
    args = parser.parse_args()
    observe(args.url, args.host, args.output, args.hours * 3600, args.interval)
