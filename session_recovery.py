"""Small, private locators for exact sessions previously named by this board.

Callers must supply server-owned nativeSource values, never browser-provided sources.
These records locate conversations; they do not authorize a resume or prove a process
is still running. Native ownership and permissions must be checked before execution.
"""
import copy
from contextlib import contextmanager
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import secrets
import sqlite3
import stat
import threading
import time
import unicodedata
import uuid

import settings

MAX_REFS = 500
MAX_BYTES = 1024 * 1024
NAME = re.compile(r"[A-Za-z0-9_.-]{1,40}\Z")
CLAUDE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
SESSION_ID = re.compile(r"[0-9A-Za-z-]{8,64}\Z")
TERMINAL_ID = re.compile(r"[0-9a-f]{32}\Z")
ORIGIN_FIELDS = {"source": 40, "initiator": 16, "initiatorEvidence": 40, "originator": 80, "entrypoint": 40,
                 "executionMode": 40, "parentSessionId": 128, "agentRole": 80}
DISPLAY_FIELDS = {"title": 240, "account": 160, "hostLabel": 160}
CLOSURES = ("none", "connection", "legacy_user_close", "explicit_termination")
REASONS = (None, "user", "released", "connection_ended", "view_closed")


def same_claude_conversation(left, right):
    """Match trusted session references, allowing a proven transcript to change cwd.

    Worker names alone can be reused. Only a shared conversation UUID proves that
    a different folder still belongs to the same conversation.
    """
    if (not isinstance(left, dict) or not isinstance(right, dict)
            or left.get('agent') != 'claude' or right.get('agent') != 'claude'
            or not left.get('host') or left['host'] != right.get('host')
            or (left.get('home') or '.claude') != (right.get('home') or '.claude')):
        return False
    identifier = left.get('sessionId') or left.get('id')
    if not identifier or identifier != (right.get('sessionId') or right.get('id')):
        return False
    if left.get('cwd') == right.get('cwd'):
        return True
    try:
        return str(uuid.UUID(identifier)) == identifier
    except (ValueError, AttributeError, TypeError):
        return False


class RecoveryError(ValueError):
    """A locator could not safely be read or saved; an existing run is unaffected."""
    def __init__(self, message="원래 작업의 복귀 정보를 안전하게 읽거나 저장하지 못했습니다.",
                 code="recovery_sessions_unavailable", status=503):
        super().__init__(message)
        self.status = status
        self.payload = {"error": message, "code": code}


def _text(value, limit):
    if (not isinstance(value, str) or not value or len(value) > limit
            or any(unicodedata.category(char) in ("Cc", "Cs")
                   or "\u202a" <= char <= "\u202e" or "\u2066" <= char <= "\u2069" for char in value)):
        raise RecoveryError("복귀할 작업의 식별 정보를 확인해 주세요.", "invalid_recovery_session", 400)
    return value


def _stamp(value):
    if type(value) is not int or not 0 <= value < 2 ** 53:
        raise RecoveryError("복귀 정보의 확인 시각을 확인해 주세요.", "invalid_recovery_session", 400)
    return value


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RecoveryError()
        result[key] = value
    return result


def _identity(source, *, locator=False):
    if not isinstance(source, dict) or source.get("agent") not in ("codex", "claude"):
        raise RecoveryError("복귀할 작업을 다시 선택해 주세요.", "invalid_recovery_session", 400)
    host, agent, identifier, home = source.get("host"), source["agent"], source.get("id"), source.get("home")
    if not isinstance(host, str) or not NAME.fullmatch(host):
        raise RecoveryError("복귀할 기기를 확인해 주세요.", "invalid_recovery_session", 400)
    if agent == "codex":
        try:
            valid = isinstance(identifier, str) and str(uuid.UUID(identifier)) == identifier
        except (ValueError, AttributeError):
            valid = False
        if not valid or not isinstance(home, str) or not settings.HOME_KEY.fullmatch(home):
            raise RecoveryError("복귀할 Codex 세션과 계정을 확인해 주세요.", "invalid_recovery_session", 400)
    elif (not isinstance(identifier, str) or not CLAUDE_ID.fullmatch(identifier)
          or home not in (None, "", ".claude")):
        raise RecoveryError("복귀할 Claude 세션과 계정을 확인해 주세요.", "invalid_recovery_session", 400)
    if agent == "claude":
        home = ".claude"
    result = {"host": host, "agent": agent, "home": home, "id": identifier}
    if agent == "claude" and source.get("sessionId") is not None:
        sid = source["sessionId"]
        if not isinstance(sid, str) or not SESSION_ID.fullmatch(sid):
            raise RecoveryError("Claude 대화 식별자를 확인해 주세요.", "invalid_recovery_session", 400)
        result["sessionId"] = sid
    if locator or "cwd" in source:
        cwd = _text(source.get("cwd"), 4096)
        if not (cwd.startswith("/") or PureWindowsPath(cwd).is_absolute()):
            raise RecoveryError("복귀할 작업 폴더를 확인해 주세요.", "invalid_recovery_session", 400)
        result["cwd"] = cwd
    if "kind" in source and source["kind"] is not None:
        if agent != "claude" or source["kind"] not in ("interactive", "background", "bg"):
            raise RecoveryError("복귀할 실행 방식을 확인해 주세요.", "invalid_recovery_session", 400)
        result["kind"] = source["kind"]
    return result


def _key(source):
    identity = {key: value for key, value in source.items() if key not in ("cwd", "kind")}
    return hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _record(value):
    if (not isinstance(value, dict) or set(value) - {"source", "openedAt", "updatedAt", "provenance", "presentation", *DISPLAY_FIELDS}
            or not {"source", "openedAt", "provenance"} <= set(value)):
        raise RecoveryError()
    if _identity(value["source"], locator=True) != value["source"]:
        raise RecoveryError()
    _stamp(value["openedAt"])
    if "updatedAt" in value:
        _stamp(value["updatedAt"])
    for key, limit in DISPLAY_FIELDS.items():
        if key in value:
            _text(value[key], limit)
    presentation = value.get("presentation", {})
    if not isinstance(presentation, dict) or set(presentation) - ORIGIN_FIELDS.keys():
        raise RecoveryError()
    for key, text in presentation.items():
        _text(text, ORIGIN_FIELDS[key])
    origin = value["provenance"]
    if (not isinstance(origin, dict) or set(origin) - {"kind", "terminalId", "closedReason", "closure"}
            or origin.get("kind") not in ("terminal-native", "listed-native") or origin.get("closure") not in CLOSURES
            or origin.get("closedReason") not in REASONS
            or ("terminalId" in origin and (not isinstance(origin["terminalId"], str)
                                           or not TERMINAL_ID.fullmatch(origin["terminalId"])))):
        raise RecoveryError()
    expected = ("legacy_user_close" if origin.get("closedReason") == "user"
                else "connection" if origin.get("closedReason") else "none")
    if origin["closure"] not in (expected, "explicit_termination"):
        raise RecoveryError()
    return value


def _state_key(directory, *paths):
    """A body-cache key only; never use a stat result as an execution proof."""
    try:
        settings.safe_path(directory, directory=True, private=True, allow_missing=True)
        result = []
        for path in paths:
            settings.safe_path(path, private=True, allow_missing=True)
            try:
                info = path.lstat()
            except FileNotFoundError:
                result.append(None)
                continue
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
                return None
            result.append((info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink,
                           info.st_size, info.st_mtime_ns, info.st_ctime_ns))
        return tuple(result)
    except (OSError, ValueError):
        return None


class RecoverySessions:
    def __init__(self, state_dir):
        # Lazy I/O: an unavailable locator must not prevent server startup or a launch.
        self.directory = Path(state_dir)
        self.path = self.directory / "recovery-sessions.json"
        self.lock_path = self.directory / "recovery-sessions.lock"
        self.lock = threading.Lock()

    @staticmethod
    def _private_file(fd):
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise RecoveryError()
        return info

    def state_key(self):
        return _state_key(self.directory, self.path, self.lock_path)

    @contextmanager
    def _storage(self, *, write=False):
        with self.lock:
            fd = None
            try:
                directory = settings.safe_path(self.directory, directory=True, private=True, allow_missing=True)
                directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                settings.safe_path(self.lock_path, private=True, allow_missing=True)
                fd = os.open(str(self.lock_path), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
                self._private_file(fd)
                deadline = time.monotonic() + 2
                while True:
                    try:
                        fcntl.flock(fd, (fcntl.LOCK_EX if write else fcntl.LOCK_SH) | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise RecoveryError()
                        time.sleep(0.01)
                yield
            except RecoveryError:
                raise
            except (OSError, ValueError, UnicodeError):
                raise RecoveryError() from None
            finally:
                if fd is not None:
                    os.close(fd)

    def _read(self):
        settings.safe_path(self.path, private=True, allow_missing=True)
        try:
            fd = os.open(str(self.path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return {"version": 1, "revision": 0, "sessions": {}}
        try:
            with os.fdopen(fd, "rb") as stream:
                if self._private_file(stream.fileno()).st_size > MAX_BYTES:
                    raise RecoveryError()
                raw = stream.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise RecoveryError()
            data = json.loads(raw, object_pairs_hook=_unique_object)
            if (not isinstance(data, dict) or set(data) != {"version", "revision", "sessions"}
                    or type(data["version"]) is not int or data["version"] != 1
                    or type(data["revision"]) is not int or data["revision"] < 0
                    or not isinstance(data["sessions"], dict) or len(data["sessions"]) > MAX_REFS):
                raise RecoveryError()
            for key, value in data["sessions"].items():
                if _key(_record(value)["source"]) != key:
                    raise RecoveryError()
            return data
        except (ValueError, TypeError, KeyError, UnicodeError, OverflowError, RecursionError):
            raise RecoveryError() from None

    @staticmethod
    def _encode(data):
        return (json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")

    def _write(self, data):
        raw = self._encode(data)
        if len(raw) > MAX_BYTES:
            raise RecoveryError("복귀 정보의 저장 한도에 도달했습니다.", "recovery_sessions_full", 409)
        settings.safe_path(self.path, private=True, allow_missing=True)
        temporary = self.directory / (".recovery-" + secrets.token_hex(12) + ".tmp")
        fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            folder = os.open(str(self.directory), os.O_RDONLY | os.O_NOFOLLOW)
            try:
                try:
                    os.fsync(folder)
                except OSError as exc:
                    if exc.errno != errno.EINVAL:
                        raise
            finally:
                os.close(folder)
        finally:
            temporary.unlink(missing_ok=True)

    def snapshot(self):
        with self._storage():
            data = self._read()
            rows = sorted(data["sessions"].values(), key=lambda row: (-row["openedAt"], _key(row["source"])))
            return {"ok": True, "revision": data["revision"], "sessions": copy.deepcopy(rows) if rows else []}

    @staticmethod
    def _matches(record, ref):
        source = record["source"]
        return all(source.get(key) == value for key, value in ref.items())

    def get(self, ref):
        identity = _identity(ref)
        with self._storage():
            matches = [row for row in self._read()["sessions"].values() if self._matches(row, identity)]
            return copy.deepcopy(matches[0]) if len(matches) == 1 else None

    @staticmethod
    def _entry(source, terminal_id, closed_reason):
        native = _identity(source, locator=True)
        if source.get("linkConfidence") == "estimated":
            raise RecoveryError("추정한 세션 연결은 복귀 대상으로 저장하지 않습니다.", "invalid_recovery_session", 400)
        if (terminal_id is not None and (not isinstance(terminal_id, str) or not TERMINAL_ID.fullmatch(terminal_id))
                or closed_reason not in REASONS or type(source.get("explicitTermination", False)) is not bool):
            raise RecoveryError("복귀 연결의 근거를 확인해 주세요.", "invalid_recovery_session", 400)
        display = {}
        for key, limit in DISPLAY_FIELDS.items():
            value = source.get(key)
            if isinstance(value, str) and value:
                try:
                    display[key] = _text(value[:limit], limit)
                except RecoveryError:
                    pass  # Display metadata must not discard an exact native locator.
        stamps = {key: _stamp(source[key]) for key in ("openedAt", "updatedAt") if source.get(key) is not None}
        presentation = {}
        for key, limit in ORIGIN_FIELDS.items():
            value = source.get(key)
            if isinstance(value, str) and value:
                try:
                    presentation[key] = _text(value, limit)
                except RecoveryError:
                    pass  # An optional label cannot invalidate an exact locator.
        if presentation:
            display["presentation"] = presentation
        return native, display, stamps, terminal_id, closed_reason, source.get("explicitTermination", False)

    @staticmethod
    def _merged_row(prior, entry, listed=False):
        native, display, stamps, terminal_id, closed_reason, explicit = entry
        if prior and prior["source"]["cwd"] != native["cwd"] and native["agent"] == "claude":
            # A collector may see the shell's worktree first. An exact terminal
            # supplies the transcript folder, which subsequent shell moves cannot replace.
            if not (terminal_id and prior["provenance"]["kind"] == "listed-native" and
                    not prior["provenance"].get("terminalId")):
                native["cwd"] = prior["source"]["cwd"]
        # Only a Claude locator has a kind, and it changes as the board attaches to or resumes the
        # same session (a background agent resumed in a terminal runs interactive): the latest is
        # kept, and a recovery rechecks it on the host before it runs.
        if prior and prior["source"]["cwd"] != native["cwd"] and native["agent"] != "claude":
            raise RecoveryError("기존 복귀 대상의 작업 폴더가 바뀌었습니다.", "recovery_session_conflict", 409)
        origin = copy.deepcopy(prior["provenance"]) if prior else {"kind": "terminal-native", "closure": "none"}
        if terminal_id is not None:
            origin["terminalId"] = terminal_id
        if closed_reason is not None:
            origin.update(closedReason=closed_reason, closure="legacy_user_close" if closed_reason == "user" else "connection")
        view_proven = closed_reason == "view_closed" and terminal_id is not None and prior and (
            prior["provenance"].get("terminalId") == terminal_id)
        if prior and prior["provenance"]["closure"] == "legacy_user_close" and not view_proven:
            origin = copy.deepcopy(prior["provenance"])
        if explicit or (prior and prior["provenance"]["closure"] == "explicit_termination"):
            origin["closure"] = "explicit_termination"
        if listed:
            origin["kind"] = "listed-native"
            if prior:
                stamps.pop("openedAt", None)
            elif "openedAt" not in stamps and "updatedAt" in stamps:
                stamps["openedAt"] = stamps["updatedAt"]
        row = {**(prior or {}), "source": native, **display, **stamps, "provenance": origin}
        if "openedAt" not in row:
            row["openedAt"] = int(time.time())
        return _record(row)

    def _merge(self, data, entry):
        key = _key(entry[0])
        prior = data["sessions"].get(key)
        row = self._merged_row(prior, entry)
        if row == prior:
            return data, row

        data = {**data, "sessions": {**data["sessions"], key: row}, "revision": data["revision"] + 1}
        while len(data["sessions"]) > MAX_REFS or len(self._encode(data)) > MAX_BYTES:
            candidates = [candidate for candidate in data["sessions"] if candidate != key]
            if not candidates:
                raise RecoveryError("복귀 정보의 저장 한도에 도달했습니다.", "recovery_sessions_full", 409)
            oldest = min(candidates, key=lambda candidate: (data["sessions"][candidate]["openedAt"], candidate))
            del data["sessions"][oldest]
        return data, row

    def remember(self, source, terminal_id=None, closed_reason=None):
        """Save only a trusted native locator; callers catch failures after an existing launch.

        Repeated observations do not invent a new openedAt or rewrite the store. A real
        user opening may pass source.openedAt. A legacy `user` close is not proof that
        the CLI ended; only server-confirmed explicitTermination marks that fact.
        """
        entry = self._entry(source, terminal_id, closed_reason)
        with self._storage(write=True):
            data = self._read()
            updated, row = self._merge(data, entry)
            if updated is not data:
                self._write(updated)
            return copy.deepcopy(row)

    def remember_many(self, observations):
        """Apply a terminal listing with one validated read and at most one file write."""
        if not observations:
            return
        with self._storage(write=True):
            data = updated = self._read()
            for source, terminal_id, closed_reason in observations:
                try:
                    updated, _ = self._merge(updated, self._entry(source, terminal_id, closed_reason))
                except RecoveryError:
                    continue  # One bad observation does not discard the other records.
            if updated is not data:
                self._write(updated)

    def forget(self, ref, *, expected=None):
        identity = _identity(ref)
        with self._storage(write=True):
            data = self._read()
            matches = [key for key, row in data["sessions"].items() if self._matches(row, identity)]
            if len(matches) != 1:
                return False
            key = matches[0]
            if expected is not None and data["sessions"][key] != expected:
                return False
            del data["sessions"][key]
            data["revision"] += 1
            self._write(data)
            return True


@contextmanager
def private_sqlite(directory, path, initialize, *, write=False):
    directory = settings.safe_path(directory, directory=True, private=True, allow_missing=True)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    for file in (path, path.with_name(path.name + "-journal")):
        settings.safe_path(file, private=True, allow_missing=True)
        try:
            fd = os.open(file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK |
                         (os.O_CREAT if file == path else 0), 0o600)
        except FileNotFoundError:
            if file == path:
                raise
            continue
        try:
            RecoverySessions._private_file(fd)
        finally:
            os.close(fd)
    connection = sqlite3.connect(path, timeout=0.25)
    try:
        with connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if write or version == 0:
                connection.execute("BEGIN IMMEDIATE")
                version = connection.execute("PRAGMA user_version").fetchone()[0]
            else:
                connection.execute("BEGIN")
            if version == 0:
                initialize(connection)
                connection.execute("PRAGMA user_version = 1")
            elif version != 1:
                raise ValueError("unsupported ledger schema")
            yield connection
    finally:
        connection.close()


class RetainedSessions:
    """Indexed private membership, migrated once from the last bounded JSON ledger."""
    PAGE_SIZE = 50

    def __init__(self, state_dir):
        self.legacy = RecoverySessions(state_dir)
        self.directory = Path(state_dir)
        self.path = self.directory / "retained-sessions.sqlite"

    def state_key(self):
        return _state_key(self.directory, self.path, self.path.with_name(self.path.name + "-journal"))

    def _initialize(self, connection):
        connection.execute("""CREATE TABLE sessions (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, locator TEXT NOT NULL UNIQUE,
            selector TEXT NOT NULL, visible INTEGER NOT NULL, prunable INTEGER NOT NULL,
            value TEXT NOT NULL)""")
        connection.execute("CREATE INDEX session_selector ON sessions(selector)")
        connection.execute("CREATE INDEX session_page ON sessions(visible, seq)")
        connection.execute("CREATE INDEX session_sweep ON sessions(prunable, seq)")
        with self.legacy._storage():
            records = self.legacy._read()["sessions"]
            for row in sorted(records.values(), key=lambda row: row["openedAt"]):
                self._put(connection, row)

    @contextmanager
    def _database(self, *, write=False):
        try:
            with private_sqlite(self.directory, self.path, self._initialize, write=write) as db:
                yield db
        except RecoveryError:
            raise
        except (OSError, ValueError, sqlite3.Error, TypeError, KeyError, RecursionError):
            raise RecoveryError() from None

    @staticmethod
    def _selector(source):
        return _key({key: source[key] for key in ("host", "agent", "home", "id")})

    @classmethod
    def _put(cls, db, row):
        source, origin = row["source"], row["provenance"]
        visible = origin["closure"] not in ("explicit_termination", "legacy_user_close")
        prunable = visible and origin["kind"] == "terminal-native" and origin["closure"] == "none"
        db.execute("""INSERT INTO sessions(locator, selector, visible, prunable, value) VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(locator) DO UPDATE SET visible=excluded.visible, prunable=excluded.prunable,
                   value=excluded.value""",
                   (_key(source), cls._selector(source), visible, prunable,
                    json.dumps(row, ensure_ascii=False, separators=(",", ":"))))

    @staticmethod
    def _decode(stored):
        return _record(json.loads(stored))

    def _find(self, db, identity):
        if "sessionId" in identity or identity["agent"] == "codex":
            rows = db.execute("SELECT locator, value FROM sessions WHERE locator=?", (_key(identity),))
        else:
            rows = db.execute("SELECT locator, value FROM sessions WHERE selector=? LIMIT 2",
                              (self._selector(identity),))
        matches = [(key, self._decode(value)) for key, value in rows]
        if len(matches) == 1 and RecoverySessions._matches(matches[0][1], identity):
            return matches[0]
        return None, None

    def get(self, ref):
        identity = _identity(ref)
        with self._database() as db:
            return self._find(db, identity)[1]

    def _remember(self, db, entry, listed=False):
        stored = db.execute("SELECT value FROM sessions WHERE locator=?", (_key(entry[0]),)).fetchone()
        prior = self._decode(stored[0]) if stored else None
        row = RecoverySessions._merged_row(prior, entry, listed=listed)
        if row != prior:
            self._put(db, row)
        return row, row != prior

    def remember(self, source, terminal_id=None, closed_reason=None):
        entry = RecoverySessions._entry(source, terminal_id, closed_reason)
        with self._database(write=True) as db:
            return self._remember(db, entry)[0]

    def remember_many(self, observations, *, listed=False):
        if not observations:
            return False
        with self._database(write=True) as db:
            changed = False
            for source, terminal_id, closed_reason in observations:
                try:
                    entry = RecoverySessions._entry(source, terminal_id, closed_reason)
                    changed |= self._remember(db, entry, listed=listed)[1]
                except RecoveryError:
                    continue
            return changed

    def forget(self, ref, *, expected=None):
        identity = _identity(ref)
        with self._database(write=True) as db:
            key, row = self._find(db, identity)
            if row is None or expected is not None and row != expected:
                return False
            db.execute("DELETE FROM sessions WHERE locator=?", (key,))
            return True

    def page(self, after=None, *, sweep=False):
        if after is not None and (not isinstance(after, str) or not re.fullmatch(r"[1-9][0-9]{0,18}", after)
                                  or int(after) >= 2 ** 63):
            raise RecoveryError("이전 목록의 위치를 확인해 주세요.", "invalid_retained_cursor", 400)
        with self._database() as db:
            column = "prunable" if sweep else "visible"
            limit = self.PAGE_SIZE
            if after is None:
                rows = db.execute(f"SELECT seq, value FROM sessions WHERE {column}=1 ORDER BY seq DESC LIMIT ?",
                                  (limit + 1,)).fetchall()
            else:
                rows = db.execute(f"SELECT seq, value FROM sessions WHERE {column}=1 AND seq<? ORDER BY seq DESC LIMIT ?",
                                  (int(after), limit + 1)).fetchall()
            return [self._decode(value) for _, value in rows[:limit]], str(rows[limit - 1][0]) if len(rows) > limit else None
