"""Durable at-most-once menu-answer receipts, without storing screen or input text.

The caller resolves identities, checks the current menu and holds its conversation
lock. It commits reserve() before send_keys(), then finishes outside any database
transaction. Only an explicit InputNotSent may justify cancel_not_sent(). An
unconfirmed reservation is never expired or automatically retried.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import shutil
import sqlite3
import stat
import uuid

import settings
from session_recovery import same_claude_conversation

MAX_RECORDS = 10000
MAX_DATABASE_BYTES = 16 * 1024 * 1024
DISK_LOW_BYTES = 64 * 1024 * 1024
REQUEST = re.compile(r'[A-Za-z0-9_-]{8,100}\Z')
NAME = re.compile(r'[A-Za-z0-9_.-]{1,40}\Z')
CLAUDE_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{3,127}\Z')
SID = re.compile(r'[0-9A-Za-z-]{8,64}\Z')
SHA = re.compile(r'[a-f0-9]{64}\Z')
TERMINAL_ID = re.compile(r'[a-f0-9]{32}\Z')
REASON = re.compile(r'[a-z][a-z0-9_]{0,63}\Z')
UNAVAILABLE = '질문 응답 기록을 안전하게 확인하지 못했습니다. 같은 응답을 다시 보내지 말고 터미널을 확인해 주세요.'


class PromptReceiptError(RuntimeError):
    def __init__(self, message=UNAVAILABLE, *, status=503, code='prompt_receipts_unavailable'):
        super().__init__(message)
        self.status, self.code = status, code


def _invalid():
    raise PromptReceiptError('질문 응답의 세션과 식별 정보를 확인해 주세요.',
                             status=400, code='invalid_prompt_receipt')


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    ensure_ascii=False).encode()).hexdigest()


def _identity(source):
    if not isinstance(source, dict) or source.get('agent') not in ('codex', 'claude'):
        _invalid()
    host, agent, sid, home = source.get('host'), source['agent'], source.get('id'), source.get('home')
    if not isinstance(host, str) or not NAME.fullmatch(host):
        _invalid()
    if agent == 'codex':
        try:
            valid = isinstance(sid, str) and str(uuid.UUID(sid)) == sid
        except (ValueError, AttributeError):
            valid = False
        if not valid or not isinstance(home, str) or not settings.HOME_KEY.fullmatch(home):
            _invalid()
    else:
        if not isinstance(sid, str) or not CLAUDE_ID.fullmatch(sid) or home not in (None, '', '.claude'):
            _invalid()
        home = '.claude'
    identity = {'host': host, 'agent': agent, 'home': home, 'id': sid}
    if agent == 'claude':
        session_id = source.get('sessionId') or sid
        if not isinstance(session_id, str) or not SID.fullmatch(session_id):
            _invalid()
        cwd = source.get('cwd')
        if (not isinstance(cwd, str) or not cwd or len(cwd.encode()) > 4096
                or any(ord(char) < 32 for char in cwd)
                or not (Path(cwd).is_absolute() or PureWindowsPath(cwd).is_absolute())):
            _invalid()
        identity.update(sessionId=session_id, cwd=cwd)
    return identity


def _request(source, request_id):
    if not isinstance(request_id, str) or not REQUEST.fullmatch(request_id):
        _invalid()
    return _digest([_identity(source), request_id])


def _payload(fingerprint, number):
    if (not isinstance(fingerprint, str) or not SHA.fullmatch(fingerprint)
            or type(number) is not int or not 1 <= number <= 2 ** 31 - 1):
        _invalid()
    return _digest([fingerprint, number])


def _signature(source, fingerprint, number, terminal):
    payload = _payload(fingerprint, number)
    if (not isinstance(terminal, dict) or not isinstance(terminal.get('id'), str)
            or not TERMINAL_ID.fullmatch(terminal['id']) or terminal.get('linkConfidence') == 'estimated'):
        _invalid()
    native = terminal.get('nativeSource') or (terminal.get('metadata') or {}).get('nativeSource')
    actual, expected = _identity(native), _identity(source)
    if any(actual.get(key) != expected.get(key) for key in ('host', 'agent', 'home')):
        _invalid()
    if expected['agent'] == 'claude':
        if not same_claude_conversation(actual, expected):
            _invalid()
    elif actual['id'] != expected['id']:
        _invalid()
    # Even for Codex, a terminal's changed work folder must not reuse a request signature.
    cwd = native.get('cwd')
    if not isinstance(cwd, str) or not cwd or len(cwd.encode()) > 4096 or any(ord(c) < 32 for c in cwd):
        _invalid()
    actual['cwd'] = cwd
    if expected['agent'] != 'claude' and source.get('cwd') is not None and source['cwd'] != cwd:
        _invalid()
    return _digest([payload, terminal['id'], actual])


class PromptReceipts:
    def __init__(self, state_dir, max_records=MAX_RECORDS):
        if type(max_records) is not int or not 1 <= max_records <= MAX_RECORDS:
            _invalid()
        self.max_records = max_records
        self.directory = Path(state_dir) / 'prompt-receipts'
        self.path = self.directory / 'receipts.sqlite3'

    @staticmethod
    def _private_file(path):
        settings.safe_path(path, private=True)
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise PromptReceiptError()
        return info

    @contextmanager
    def _connection(self, *, create=False):
        connection = None
        try:
            state = settings.safe_path(self.directory.parent, directory=True, private=True, allow_missing=True)
            settings.safe_path(self.directory, directory=True, private=True, allow_missing=True)
            settings.safe_path(self.path, private=True, allow_missing=True)
            if not create and not self.path.exists():
                yield None
                return
            if create:
                state.mkdir(parents=True, exist_ok=True, mode=0o700)
                self.directory.mkdir(mode=0o700, exist_ok=True)
            settings.safe_path(self.directory, directory=True, private=True)
            created = False
            if create:
                try:
                    fd = os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                except FileExistsError:
                    pass
                else:
                    os.close(fd)
                    created = True
            info = self._private_file(self.path)
            if info.st_size > MAX_DATABASE_BYTES or (not created and info.st_size < 100):
                raise PromptReceiptError()
            for suffix in ('-journal', '-wal', '-shm'):
                auxiliary = self.path.with_name(self.path.name + suffix)
                settings.safe_path(auxiliary, private=True, allow_missing=True)
                if auxiliary.exists():
                    self._private_file(auxiliary)
            connection = sqlite3.connect(str(self.path), timeout=2, isolation_level=None)
            connection.execute('PRAGMA journal_mode=DELETE')
            connection.execute('PRAGMA synchronous=FULL')
            page_size = connection.execute('PRAGMA page_size').fetchone()[0]
            connection.execute('PRAGMA max_page_count=' + str(MAX_DATABASE_BYTES // page_size))
            connection.execute('BEGIN IMMEDIATE')
            version = connection.execute('PRAGMA user_version').fetchone()[0]
            if version == 0 and created:
                if connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone():
                    raise PromptReceiptError()
                connection.execute('CREATE TABLE receipts ('
                    'request_hash TEXT PRIMARY KEY, signature TEXT NOT NULL, number INTEGER NOT NULL, '
                    "status TEXT NOT NULL CHECK(status IN ('unconfirmed','answered')), reason_code TEXT NOT NULL, "
                    'payload_hash TEXT NOT NULL) WITHOUT ROWID')
                connection.execute('PRAGMA user_version=1')
            elif version != 1:
                raise PromptReceiptError()
            columns = [row[1] for row in connection.execute('PRAGMA table_info(receipts)')]
            if columns != ['request_hash', 'signature', 'number', 'status', 'reason_code', 'payload_hash']:
                raise PromptReceiptError()
            connection.execute('COMMIT')
            yield connection
        except PromptReceiptError:
            raise
        except (sqlite3.Error, OSError, ValueError, UnicodeError, OverflowError):
            try:
                low = shutil.disk_usage(str(self.directory.parent)).free < DISK_LOW_BYTES
            except OSError:
                low = False
            if low:
                raise PromptReceiptError('디스크 공간이 부족해 질문 응답을 기록하지 못했습니다. 터미널을 확인해 주세요.') from None
            raise PromptReceiptError() from None
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def _read(connection, request_hash):
        row = connection.execute('SELECT signature,number,status,reason_code,payload_hash FROM receipts WHERE request_hash=?',
                                 (request_hash,)).fetchone()
        if row is not None and (not isinstance(row[0], str) or not SHA.fullmatch(row[0])
                                or type(row[1]) is not int or not 1 <= row[1] <= 2 ** 31 - 1
                                or row[2] not in ('answered', 'unconfirmed')
                                or not isinstance(row[3], str) or not REASON.fullmatch(row[3])
                                or not isinstance(row[4], str) or not SHA.fullmatch(row[4])):
            raise PromptReceiptError()
        return row

    @staticmethod
    def _receipt(request_id, row):
        return {'requestId': request_id, 'status': row[2] if row else 'not_found',
                'number': row[1] if row else None, 'reasonCode': row[3] if row else 'not_found'}

    @staticmethod
    def _match(row, signature):
        if row[0] != signature:
            raise PromptReceiptError('같은 질문 응답 요청에 다른 선택이나 터미널을 사용할 수 없습니다.',
                                     status=409, code='prompt_request_conflict')

    def lookup(self, source, request_id, fingerprint=None, number=None):
        key = _request(source, request_id)
        payload = None
        if fingerprint is not None or number is not None:
            payload = _payload(fingerprint, number)
        with self._connection() as connection:
            row = self._read(connection, key) if connection is not None else None
            if row is not None and payload is not None and row[4] != payload:
                raise PromptReceiptError('같은 질문 응답 요청에 다른 선택을 사용할 수 없습니다.',
                                         status=409, code='prompt_request_conflict')
        return self._receipt(request_id, row)

    def reserve(self, source, request_id, fingerprint, number, terminal):
        key, signature = _request(source, request_id), _signature(source, fingerprint, number, terminal)
        with self._connection(create=True) as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = self._read(connection, key)
            if row is not None:
                self._match(row, signature)
                connection.execute('COMMIT')
                return {**self._receipt(request_id, row), 'reserved': False}
            if connection.execute('SELECT count(*) FROM receipts').fetchone()[0] >= self.max_records:
                raise PromptReceiptError('질문 응답 기록 한도에 도달했습니다. 터미널에서 확인해 주세요.',
                                         status=409, code='prompt_receipts_full')
            connection.execute('INSERT INTO receipts VALUES (?,?,?,?,?,?)',
                               (key, signature, number, 'unconfirmed', 'reserved', _payload(fingerprint, number)))
            connection.execute('COMMIT')
        return {'requestId': request_id, 'status': 'unconfirmed', 'number': number,
                'reasonCode': 'reserved', 'reserved': True}

    def finish(self, source, request_id, fingerprint, number, terminal, *, status, reason_code=None):
        key, signature = _request(source, request_id), _signature(source, fingerprint, number, terminal)
        reason_code = reason_code if reason_code is not None else ('answered' if status == 'answered' else 'delivery_unconfirmed')
        if status not in ('answered', 'unconfirmed') or not isinstance(reason_code, str) or not REASON.fullmatch(reason_code):
            _invalid()
        with self._connection() as connection:
            row = self._read(connection, key) if connection is not None else None
            if row is None:
                raise PromptReceiptError()
            connection.execute('BEGIN IMMEDIATE')
            row = self._read(connection, key)
            if row is None:
                raise PromptReceiptError()
            self._match(row, signature)
            if row[2] != 'answered':
                connection.execute('UPDATE receipts SET status=?,reason_code=? WHERE request_hash=?',
                                   (status, reason_code, key))
                row = (row[0], row[1], status, reason_code, row[4])
            connection.execute('COMMIT')
        return self._receipt(request_id, row)

    def cancel_not_sent(self, source, request_id, fingerprint, number, terminal):
        """Only call after InputNotSent proves that this reservation sent no keys."""
        key, signature = _request(source, request_id), _signature(source, fingerprint, number, terminal)
        with self._connection() as connection:
            if connection is None:
                return False
            connection.execute('BEGIN IMMEDIATE')
            row = self._read(connection, key)
            if row is not None:
                self._match(row, signature)
            removed = row is not None and row[2:4] == ('unconfirmed', 'reserved')
            if removed:
                connection.execute('DELETE FROM receipts WHERE request_hash=?', (key,))
            connection.execute('COMMIT')
            return removed
