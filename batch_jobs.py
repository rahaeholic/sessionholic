"""Durable, explicitly reviewed multi-conversation sends."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid

import settings
from chat import SendRejected

REQUEST = re.compile(r'[A-Za-z0-9_-]{8,100}\Z')


class BatchJobs:
    def __init__(self, folder, chat):
        self.chat = chat
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = settings.safe_path(folder / 'batch-jobs.sqlite', private=True, allow_missing=True)
        if not self.path.exists():
            self.path.touch(mode=0o600)
        lock_path = settings.safe_path(folder / 'batch-jobs.lock', private=True, allow_missing=True)
        self.lock_fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.worker_owner = True
        except BlockingIOError:
            self.worker_owner = False
        self.lock, self.stop, self.wakeup = threading.RLock(), threading.Event(), threading.Event()
        self.thread = None
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS jobs (owner TEXT PRIMARY KEY, id TEXT UNIQUE NOT NULL, value TEXT NOT NULL)')
            # A process may have stopped after a native send but before the receipt was stored.
            for owner, value in db.execute('SELECT owner,value FROM jobs').fetchall() if self.worker_owner else []:
                record = json.loads(value)
                if any(task['status'] == 'sending' for task in record['tasks']):
                    for task in record['tasks']:
                        if task['status'] == 'sending': task['status'] = 'unknown'
                    record['running'] = False
                    self._put(db, owner, record)

    @contextmanager
    def db(self):
        db = sqlite3.connect(str(self.path), timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def _owner(self, ref, sending=False):
        source, _ = self.chat.source(ref, sending=sending)
        identity = self.chat.identity(source)
        if source.get('agent') == 'claude': identity['sessionId'] = source.get('sessionId') or source.get('id')
        return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()

    @staticmethod
    def _get(db, owner):
        row = db.execute('SELECT value FROM jobs WHERE owner=?', (owner,)).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def _put(db, owner, record):
        db.execute('INSERT INTO jobs(owner,id,value) VALUES(?,?,?) ON CONFLICT(owner) DO UPDATE SET id=excluded.id,value=excluded.value',
                   (owner, record['id'], json.dumps(record, ensure_ascii=False)))

    def get(self, ref):
        owner = self._owner(ref)
        with self.lock, self.db() as db:
            return {'record': self._get(db, owner)}

    def start(self, ref, record):
        owner = self._owner(ref, sending=True)
        if not isinstance(record, dict) or not REQUEST.fullmatch(str(record.get('id') or '')):
            raise ValueError('작업 요청 식별자를 확인해 주세요.')
        tasks = record.get('tasks')
        if not isinstance(tasks, list) or not 2 <= len(tasks) <= 8:
            raise ValueError('작업은 2개부터 8개까지 선택해 주세요.')
        validated, seen, requests = [], set(), set()
        for task in tasks:
            if not isinstance(task, dict) or not isinstance(task.get('source'), dict):
                raise ValueError('작업 대상을 확인해 주세요.')
            source, _ = self.chat.source(task['source'])
            self.chat._direct_session(source)
            if source.get('cwd') != task['source'].get('cwd'):
                raise ValueError('대상의 작업 폴더가 바뀌었습니다.')
            if source.get('agent') == 'claude':
                sid = source.get('sessionId') or source.get('id')
                try: uuid.UUID(str(sid))
                except (ValueError, AttributeError): raise ValueError('Claude 대화 식별자를 확인해 주세요.') from None
                if source.get('sessionId') and task['source'].get('sessionId') != source['sessionId']:
                    raise ValueError('Claude 대화 식별자가 바뀌었습니다.')
            identity = json.dumps(self.chat.identity(source), sort_keys=True)
            rid, text = task.get('requestId'), task.get('text')
            if (identity in seen or not isinstance(rid, str) or not REQUEST.fullmatch(rid) or rid in requests
                    or not isinstance(text, str) or not text.strip() or len(text.encode()) > 8000):
                raise ValueError('서로 다른 대화와 작업 내용을 확인해 주세요.')
            seen.add(identity); requests.add(rid)
            validated.append({'identity': task.get('identity', ''), 'source': task['source'],
                              'title': str(task.get('title') or '대화')[:200], 'text': text,
                              'requestId': rid, 'status': 'ready'})
        with self.lock, self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old = self._get(db, owner)
            if old:
                if old['id'] == record['id']:
                    if [(t['source'], t['text'], t['requestId']) for t in old['tasks']] != [(t['source'], t['text'], t['requestId']) for t in validated]:
                        raise ValueError('같은 요청 번호에 다른 작업을 사용할 수 없습니다.')
                    return {'record': old}
                raise ValueError('먼저 기존 실행 기록을 확인하고 지워 주세요.')
            if db.execute('SELECT 1 FROM jobs WHERE id=?', (record['id'],)).fetchone():
                raise ValueError('이미 사용한 작업 요청 번호입니다.')
            if db.execute('SELECT count(*) FROM jobs').fetchone()[0] >= 1000:
                raise ValueError('저장된 실행 기록이 가득 찼습니다. 확인한 기록을 정리해 주세요.')
            value = {'version': 2, 'id': record['id'], 'tasks': validated, 'running': True, 'createdAt': time.time()}
            self._put(db, owner, value)
        self.wakeup.set()
        return {'record': value}

    def control(self, ref, action, request_id=None):
        owner = self._owner(ref)
        with self.lock, self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            job = self._get(db, owner)
            if not job: raise ValueError('저장된 실행 기록이 없습니다.')
            if action == 'stop':
                job['running'] = False
            elif action == 'resume':
                if any(t['status'] in ('sending', 'unknown') for t in job['tasks']):
                    raise ValueError('확인되지 않은 전송을 먼저 확인해 주세요.')
                job['running'] = True
            elif action == 'skip':
                task = next((t for t in job['tasks'] if t['requestId'] == request_id), None)
                if job['running'] or not task or task['status'] != 'unknown':
                    raise ValueError('전송 여부를 확인한 뒤 건너뛰어 주세요.')
                task['status'] = 'skipped'
            elif action == 'clear':
                if job['running'] or any(t['status'] in ('sending', 'unknown') for t in job['tasks']):
                    raise ValueError('실행과 확인되지 않은 전송을 먼저 확인해 주세요.')
                db.execute('DELETE FROM jobs WHERE owner=?', (owner,))
                return {'record': None}
            else:
                raise ValueError('지원하지 않는 실행 제어입니다.')
            self._put(db, owner, job)
        self.wakeup.set()
        return {'record': job}

    def check(self, ref):
        owner = self._owner(ref)
        with self.lock, self.db() as db:
            job = self._get(db, owner)
        if not job: raise ValueError('저장된 실행 기록이 없습니다.')
        for task in job['tasks']:
            if task['status'] not in ('unknown', 'accepted'): continue
            try:
                result = self.chat.state({'source': task['source'], 'requestId': task['requestId']})
                receipt = next((r for r in result.get('receipts', []) if r.get('requestId') == task['requestId']), {})
            except (ValueError, RuntimeError, OSError):
                continue
            status = receipt.get('status')
            mapped = ('answered' if status == 'completed' else 'interrupted' if status in ('interrupted', 'failed')
                      else 'accepted' if status in ('accepted', 'queued') else None)
            if receipt.get('confirmed') is not True or not mapped: continue
            with self.lock, self.db() as db:
                db.execute('BEGIN IMMEDIATE')
                fresh = self._get(db, owner)
                if not fresh or fresh['id'] != job['id']: break
                current = next((t for t in fresh['tasks'] if t['requestId'] == task['requestId']), None)
                if current and current['status'] == task['status']:
                    current['status'] = mapped
                    self._put(db, owner, fresh)
        return self.get(ref)

    def tick(self):
        if not self.worker_owner: return
        with self.lock, self.db() as db:
            jobs = [(owner, json.loads(value)) for owner, value in db.execute('SELECT owner,value FROM jobs').fetchall()]
        for owner, job in jobs:
            if self.stop.is_set(): return
            with self.lock, self.db() as db:
                db.execute('BEGIN IMMEDIATE')
                fresh = self._get(db, owner)
                if not fresh or not fresh['running'] or any(t['status'] in ('sending', 'unknown') for t in fresh['tasks']): continue
                task = next((t for t in fresh['tasks'] if t['status'] == 'ready'), None)
                if not task:
                    fresh['running'] = False; self._put(db, owner, fresh); continue
                board = getattr(self.chat, 'board', None)
                if board is not None and not board.state.get(task['source'].get('host'), {}).get('ok'):
                    continue
                task['status'] = 'sending'
                self._put(db, owner, fresh)
            try:
                result = self.chat.send({'source': task['source'], 'requestId': task['requestId'],
                                         'text': task['text'], 'attachments': [], 'requireIdle': True})
                receipt, status = result.get('receipt') or {}, result.get('status')
                mapped = ('accepted' if status in ('accepted', 'queued', 'completed') and receipt.get('confirmed') is True
                          else 'interrupted' if status == 'failed' and receipt.get('confirmed') is True else 'unknown')
                reason = receipt.get('reason') or ''
            except SendRejected as exc:
                mapped, reason = 'rejected', str(exc)
            except Exception:
                mapped, reason = 'unknown', '전송 결과를 확인하지 못했습니다.'
            with self.lock, self.db() as db:
                db.execute('BEGIN IMMEDIATE')
                fresh = self._get(db, owner)
                if not fresh or fresh['id'] != job['id']: continue
                current = next((t for t in fresh['tasks'] if t['requestId'] == task['requestId']), None)
                if not current or current['status'] != 'sending': continue
                current.update(status=mapped, reason=reason)
                if mapped != 'accepted': fresh['running'] = False
                self._put(db, owner, fresh)

    def start_worker(self):
        if not self.worker_owner: return
        def work():
            while not self.stop.is_set():
                try: self.tick()
                except (OSError, ValueError, sqlite3.Error): pass
                self.wakeup.wait(2); self.wakeup.clear()
        self.thread = threading.Thread(target=work, name='batch-work', daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set(); self.wakeup.set()
        if self.thread: self.thread.join(timeout=1)
        if self.lock_fd is not None:
            os.close(self.lock_fd); self.lock_fd = None
