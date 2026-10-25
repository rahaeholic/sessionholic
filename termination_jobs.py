"""Durable termination receipts. Recovery never resends a native mutation."""
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time

import settings


def source_key(source):
    return json.dumps([source.get(k) for k in ('host', 'agent', 'home', 'id', 'sessionId', 'cwd')], separators=(',', ':'))


def terminal_stamp(info):
    # Stable connection identity; liveness and output timestamps can change.
    native = info.get('nativeSource') or {}
    identity = {k: native.get(k) for k in ('host', 'agent', 'id', 'home', 'cwd', 'kind', 'sessionId')}
    stable = {k: info.get(k) for k in ('id', 'host', 'createdAt', 'mode', 'holdName')}
    return hashlib.sha256(json.dumps({**stable, 'nativeSource': identity}, sort_keys=True).encode()).hexdigest()


class Jobs:
    def __init__(self, folder, runner):
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = settings.safe_path(folder / 'termination-jobs.sqlite', private=True, allow_missing=True)
        if not self.path.exists():
            self.path.touch(mode=0o600)
        self.lock, self.stop = threading.Lock(), threading.Event()
        self.source_locks = {}
        self.runner, self.thread = runner, None
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, source TEXT NOT NULL, active INTEGER NOT NULL, due REAL NOT NULL, value TEXT NOT NULL)')
            db.execute('CREATE UNIQUE INDEX IF NOT EXISTS one_active_source ON jobs(source) WHERE active=1')

    @contextmanager
    def db(self):
        db = sqlite3.connect(str(self.path), timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, request_id):
        with self.db() as db:
            row = db.execute('SELECT value FROM jobs WHERE id=?', (request_id,)).fetchone()
        return json.loads(row[0]) if row else None

    @contextmanager
    def source_lock(self, source):
        key = source_key(source)
        with self.lock:
            # Keep the selected job until concurrent waiters have consumed its
            # result, even when it completed just before they acquired the lock.
            entry = self.source_locks.setdefault(key, [threading.RLock(), 0, None])
            entry[1] += 1
        try:
            with entry[0]:
                yield entry
        finally:
            with self.lock:
                entry[1] -= 1
                if not entry[1]:
                    del self.source_locks[key]

    def pending(self, source):
        with self.db() as db:
            row = db.execute('SELECT value FROM jobs WHERE source=? AND active=1', (source_key(source),)).fetchone()
        return json.loads(row[0]) if row else None

    def claim(self, source, request_id, terminals):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT source,value FROM jobs WHERE id=?', (request_id,)).fetchone()
            if row:
                if row[0] != source_key(source):
                    raise ValueError('같은 종료 요청 ID를 다른 세션에 사용할 수 없습니다.')
                return json.loads(row[1]), False
            row = db.execute('SELECT value FROM jobs WHERE source=? AND active=1', (source_key(source),)).fetchone()
            if row:
                return json.loads(row[0]), False
            if db.execute('SELECT count(*) FROM jobs').fetchone()[0] >= 10000:
                raise ValueError('종료 요청 보관 한도에 도달했습니다. 실행은 변경하지 않았습니다.')
            job = {'requestId': request_id, 'source': source, 'terminals': terminals, 'attempts': 0,
                   'createdAt': time.time(), 'updatedAt': time.time(), 'state': 'pending',
                   'result': {'confirmed': False, 'status': 'unconfirmed', 'applied': None,
                              'reason': '종료 요청을 확인 중입니다. 기기 연결이 복구되면 자동으로 다시 확인합니다.'}}
            db.execute('INSERT INTO jobs VALUES (?,?,?,?,?)',
                       (request_id, source_key(source), 1, time.time() + 30, json.dumps(job)))
            return job, True

    @staticmethod
    def view(job):
        return {**job['result'], 'requestId': job['requestId'], 'job': {
            k: job[k] for k in ('requestId', 'state', 'attempts', 'updatedAt')}}

    def run(self, job, initial=None):
        # Serialize initial dispatch with readback. A second process can only
        # reconcile the durable claim, and native tombstones fence late dispatch.
        with self.source_lock(job['source']) as entry:
            job = self.get(job['requestId'])
            if job['state'] != 'pending':
                return {**self.view(job), 'historical': True}
            entry[2] = job['requestId']
            try:
                result = self.runner(job, initial)
                if not isinstance(result, dict) or type(result.get('confirmed')) is not bool:
                    raise ValueError('invalid native receipt')
                result = {k: v for k, v in result.items() if k not in ('preview', 'targetProof')}
            except (OSError, ValueError, RuntimeError, TimeoutError):
                result = {**job['result'], 'reconcileCode': 'readback_unavailable',
                          'reason': '기기의 종료 결과를 확인 중입니다. 연결이 복구되면 자동으로 다시 확인합니다.'}
            state = 'pending'
            if result.get('dispatchState') == 'not_started' or result.get('applied') is False:
                state = 'not_started'
            elif result.get('reconcileCode') in ('superseded', 'execution_changed') or result.get('cleanup', {}).get('superseded'):
                state = 'superseded'
            elif result.get('cleanup', {}).get('complete'):
                state = 'complete'
            job.update(result=result, state=state, attempts=job['attempts'] + 1, updatedAt=time.time())
            delay = (2, 5, 15, 30, 60, 300)[min(job['attempts'] - 1, 5)]
            with self.db() as db:
                db.execute('UPDATE jobs SET active=?,due=?,value=? WHERE id=?',
                           (int(state == 'pending'), time.time() + delay, json.dumps(job), job['requestId']))
            return self.view(job)

    def submit(self, source, request_id, terminals, initial=None):
        with self.source_lock(source) as entry:
            previous = self.get(request_id)
            if previous is not None and source_key(previous['source']) != source_key(source):
                raise ValueError('같은 종료 요청 ID를 다른 세션에 사용할 수 없습니다.')
            job = previous or self.pending(source) or (self.get(entry[2]) if entry[2] else None)
            if job is None:
                job, created = self.claim(source, request_id, terminals)
            else:
                created = False
            return self.run(job, initial if created else None)

    def tick(self):
        with self.db() as db:
            rows = db.execute('SELECT id FROM jobs WHERE active=1 AND due<=? ORDER BY due LIMIT 2', (time.time(),)).fetchall()
        if rows and not self.stop.is_set():
            # Keep the existing two-job budget, without one host's network wait
            # delaying the other. Initial dispatch uses the same source locks.
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix='termination-readback') as pool:
                futures = [pool.submit(self.run, self.get(row[0])) for row in rows if not self.stop.is_set()]
                for future in futures:
                    future.result()

    def start(self):
        def work():
            while not self.stop.wait(2):
                try:
                    self.tick()
                except (OSError, ValueError, RuntimeError, sqlite3.Error):
                    # Keep receipts on disk; a failed store never dispatches again.
                    self.stop.wait(30)
        self.thread = threading.Thread(target=work, name='termination-readback', daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=1)
