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
DISPLAY_FIELDS = {"title": 240, "account": 160, "hostLabel": 160}
CLOSURES = ("none", "connection", "legacy_user_close", "explicit_termination")
REASONS = (None, "user", "released", "connection_ended", "view_closed")


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
    if (not isinstance(value, dict) or set(value) - {"source", "openedAt", "updatedAt", "provenance", *DISPLAY_FIELDS}
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
    origin = value["provenance"]
    if (not isinstance(origin, dict) or set(origin) - {"kind", "terminalId", "closedReason", "closure"}
            or origin.get("kind") != "terminal-native" or origin.get("closure") not in CLOSURES
            or origin.get("closedReason") not in REASONS
            or ("terminalId" in origin and (not isinstance(origin["terminalId"], str)
                                           or not TERMINAL_ID.fullmatch(origin["terminalId"])))):
        raise RecoveryError()
    expected = ("legacy_user_close" if origin.get("closedReason") == "user"
                else "connection" if origin.get("closedReason") else "none")
    if origin["closure"] not in (expected, "explicit_termination"):
        raise RecoveryError()
    return value


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
        """A body-cache key only; never use a stat result as an execution proof."""
        try:
            settings.safe_path(self.directory, directory=True, private=True, allow_missing=True)
            result = []
            for path in (self.path, self.lock_path):
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

    def remember(self, source, terminal_id=None, closed_reason=None):
        """Save only a trusted native locator; callers catch failures after an existing launch.

        Repeated observations do not invent a new openedAt or rewrite the store. A real
        user opening may pass source.openedAt. A legacy `user` close is not proof that
        the CLI ended; only server-confirmed explicitTermination marks that fact.
        """
        native = _identity(source, locator=True)
        if source.get("linkConfidence") == "estimated":
            raise RecoveryError("추정한 세션 연결은 복귀 대상으로 저장하지 않습니다.", "invalid_recovery_session", 400)
        if (terminal_id is not None and (not isinstance(terminal_id, str) or not TERMINAL_ID.fullmatch(terminal_id))
                or closed_reason not in REASONS or type(source.get("explicitTermination", False)) is not bool):
            raise RecoveryError("복귀 연결의 근거를 확인해 주세요.", "invalid_recovery_session", 400)
        display = {key: _text(source[key], limit) for key, limit in DISPLAY_FIELDS.items() if source.get(key)}
        stamps = {key: _stamp(source[key]) for key in ("openedAt", "updatedAt") if source.get(key) is not None}
        with self._storage(write=True):
            data = self._read()
            key = _key(native)
            prior = data["sessions"].get(key)
            if prior and (prior["source"]["cwd"] != native["cwd"]
                          or prior["source"].get("kind") != native.get("kind")):
                raise RecoveryError("기존 복귀 대상의 작업 폴더나 실행 방식이 바뀌었습니다.", "recovery_session_conflict", 409)
            origin = copy.deepcopy(prior["provenance"]) if prior else {"kind": "terminal-native", "closure": "none"}
            if terminal_id is not None:
                origin["terminalId"] = terminal_id
            if closed_reason is not None:
                origin.update(closedReason=closed_reason, closure="legacy_user_close" if closed_reason == "user" else "connection")
            if prior and prior['provenance']['closure'] == 'legacy_user_close':
                origin.update(closedReason='user', closure='legacy_user_close')
            if source.get("explicitTermination") or (prior and prior["provenance"]["closure"] == "explicit_termination"):
                origin["closure"] = "explicit_termination"
            row = {**(prior or {}), "source": native, **display, **stamps, "provenance": origin}
            if "openedAt" not in row:
                row["openedAt"] = int(time.time())
            _record(row)
            if row == prior:
                return copy.deepcopy(row)
            data["sessions"][key] = row
            data["revision"] += 1
            while len(data["sessions"]) > MAX_REFS or len(self._encode(data)) > MAX_BYTES:
                candidates = [candidate for candidate in data["sessions"] if candidate != key]
                if not candidates:
                    raise RecoveryError("복귀 정보의 저장 한도에 도달했습니다.", "recovery_sessions_full", 409)
                oldest = min(candidates, key=lambda candidate: (data["sessions"][candidate]["openedAt"], candidate))
                del data["sessions"][oldest]
            self._write(data)
            return copy.deepcopy(row)

    def forget(self, ref):
        identity = _identity(ref)
        with self._storage(write=True):
            data = self._read()
            matches = [key for key, row in data["sessions"].items() if self._matches(row, identity)]
            if len(matches) != 1:
                return False
            del data["sessions"][matches[0]]
            data["revision"] += 1
            self._write(data)
            return True
