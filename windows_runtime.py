"""Finite Windows native-session actions, preserving the owning PC's settings.

The board injects this module, windows_collector and windows_attachments over SSH stdin.
It starts no server and accepts no arbitrary shell command, PID, binary or filesystem path.
Besides uploads kept in the session's own work folder (windows_attachments), only metadata
receipts, unresolved resume permissions and a local proof key are persisted; input is not logged.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import hashlib
import hmac
import json
import os
from pathlib import Path
import queue
import re
import secrets
import stat
import time
import uuid

import windows_attachments as wa
import windows_collector as wc

REQUEST = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}\Z')
# A Claude background run's job id: what `claude --bg` prints, `claude agents` lists and
# `claude attach` takes (the session UUID is refused there).
CLAUDE_JOB = re.compile(r'[0-9a-z]{8}\Z')


class NotLoaded(ValueError):
    """No PC server has this stored session open: it may be resumed into its project's server."""
CONTROL_REQUEST = re.compile(r'[A-Za-z0-9_-]{8,100}\Z')
PROOF_TTL = 45
ARCHIVE_EFFECT = '선택한 세션을 종료하고 대화 기록을 보관합니다. 하위 에이전트도 함께 종료·보관을 요청합니다.'
STORE_RELATIVE = '.local/state/sessionholic-windows'
RESUME_PENDING_REASON = 'PC 세션의 재개 권한 확인이 끝나지 않았습니다. 원본 PC 터미널에서 확인한 뒤 해당 세션을 닫거나 프로젝트 서버를 다시 시작해 주세요.'
TERMINAL_SECONDS = 15
RESUME_MIN_SECONDS = 6
# A board terminal's `codex resume --remote` TUI sends no permissions. Resuming a loaded thread
# nobody is subscribed to, the server rebuilds it from its record with the project server's
# default sandbox (only the approval policy is restored). Subscribed while the TUI joins, the
# server keeps the loaded thread and its permissions: settle stays subscribed this long.
SETTLE_HOLD_SECONDS = 45
SETTLE_CONFIRM_SECONDS = 8
# The plan's record of what the terminal must keep is used once, shortly after the plan.
SETTLE_RECORD_SECONDS = 120
SETTLE_FAILED = ('PC 대화의 실행 권한이 터미널 연결 뒤에도 그대로인지 확인하지 못했습니다. '
                 'Codex 화면의 /permissions에서 확인해 주세요.')


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def _identity(source):
    if not isinstance(source, dict) or source.get('agent') not in ('codex', 'claude'):
        raise ValueError('PC 세션 정보가 올바르지 않습니다.')
    agent = source['agent']
    sid = source.get('id') or source.get('sessionId')
    if not isinstance(sid, str) or not wc.SID.fullmatch(sid):
        raise ValueError('PC 세션 ID가 올바르지 않습니다.')
    if agent == 'codex' and (str(uuid.UUID(sid)) != sid or source.get('home') != '.codex'):
        raise ValueError('등록된 PC Codex 계정을 확인해 주세요.')
    host = source.get('host')
    if not isinstance(host, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,40}', host):
        raise ValueError('PC 기기 식별자가 올바르지 않습니다.')
    return {'host': host, 'agent': agent, 'id': sid, 'home': '.codex' if agent == 'codex' else '.claude'}


def _codex_resume_settings(row):
    """Restore the stored policy before a remote TUI attaches to a closed thread.

    Only faithfully representable native policies are accepted. Custom filesystem
    rules must not be weakened or silently replaced by a built-in sandbox.
    """
    message = 'PC 세션의 기존 실행 권한을 그대로 연결할 수 없습니다. 원본 PC 터미널에서 권한 설정을 확인해 주세요.'
    approval = row.get('approval_mode')
    if approval not in ('never', 'on-request'):
        raise ValueError(message)
    try:
        policy = json.loads(row['sandbox_policy'])
    except (KeyError, TypeError, ValueError):
        raise ValueError(message) from None
    if not isinstance(policy, dict):
        raise ValueError(message)
    kind = policy.get('type')
    config = {}
    if policy == {'type': 'disabled'} or policy == {'type': 'danger-full-access'}:
        mode = 'danger-full-access'
    elif kind in ('read-only', 'workspace-write'):
        mode = kind
        fields = {'type', 'network_access'}
        if mode == 'workspace-write':
            fields |= {'writable_roots', 'exclude_slash_tmp', 'exclude_tmpdir_env_var'}
        if set(policy) - fields:
            raise ValueError(message)
        for key in fields - {'type'}:
            value = policy.get(key, [] if key == 'writable_roots' else False)
            if key == 'writable_roots':
                if not isinstance(value, list) or any(not isinstance(p, str) or not wc._windows_path(p).startswith(('/',))
                    and not re.match(r'^[A-Za-z]:/', wc._windows_path(p)) for p in value):
                    raise ValueError(message)
            elif type(value) is not bool:
                raise ValueError(message)
            if mode == 'read-only' and value:
                # The native legacy read-only preset cannot restore enabled networking.
                raise ValueError(message)
            if mode == 'workspace-write':
                config['sandbox_workspace_write.' + key] = value
    elif kind == 'managed' and set(policy) == {'type', 'file_system', 'network'}:
        fs = policy['file_system']
        if (not isinstance(fs, dict) or set(fs) != {'type', 'entries'} or fs['type'] != 'restricted'
                or not isinstance(fs['entries'], list) or policy['network'] not in ('restricted', 'enabled')):
            raise ValueError(message)
        # These are the exact built-in workspace entries emitted by native Codex.
        cwd = wc._norm(row.get('cwd'))
        expected = {
            ('special', 'root', 'read', None),
            ('path', cwd, 'write', None),
            ('special', 'slash_tmp', 'write', None),
            ('special', 'tmpdir', 'write', None),
            *(('path', wc._norm(str(row['cwd']) + '/' + name), 'read', 'skip')
              for name in ('.git', '.agents', '.codex', '.aws')),
        }
        entries = set()
        for entry in fs['entries']:
            if not isinstance(entry, dict) or set(entry) - {'path', 'access', 'missing_path_behavior'}:
                raise ValueError(message)
            path = entry.get('path')
            if not isinstance(path, dict):
                raise ValueError(message)
            if path.get('type') == 'path' and set(path) == {'type', 'path'} and isinstance(path['path'], str):
                target = ('path', wc._norm(path['path']))
            elif (path.get('type') == 'special' and set(path) == {'type', 'value'}
                  and isinstance(path['value'], dict) and set(path['value']) == {'kind'}
                  and isinstance(path['value']['kind'], str)):
                target = ('special', path['value']['kind'])
            else:
                raise ValueError(message)
            access, missing = entry.get('access'), entry.get('missing_path_behavior')
            if not isinstance(access, str) or missing is not None and not isinstance(missing, str):
                raise ValueError(message)
            entries.add((*target, access, missing))
        readonly = {('special', 'root', 'read', None)}
        if entries == readonly and len(fs['entries']) == 1 and policy['network'] == 'restricted':
            return {'sandbox': 'read-only', 'approvalPolicy': approval}
        if entries != expected or len(entries) != len(fs['entries']):
            raise ValueError(message)
        mode = 'workspace-write'
        config['sandbox_workspace_write.network_access'] = policy['network'] == 'enabled'
        config['sandbox_workspace_write.writable_roots'] = []
        config['sandbox_workspace_write.exclude_slash_tmp'] = False
        config['sandbox_workspace_write.exclude_tmpdir_env_var'] = False
    else:
        raise ValueError(message)
    return {'sandbox': mode, 'approvalPolicy': approval, **({'config': config} if config else {})}


def _same_resume_permissions(result, settings):
    if not isinstance(result, dict) or result.get('approvalPolicy') != settings['approvalPolicy']:
        return False
    actual = result.get('sandbox')
    mode = settings['sandbox']
    expected = {'type': {'danger-full-access': 'dangerFullAccess', 'read-only': 'readOnly',
                         'workspace-write': 'workspaceWrite'}[mode]}
    config = settings.get('config', {})
    if mode == 'read-only':
        expected['networkAccess'] = False
    elif mode == 'workspace-write':
        for key, native, default in [('network_access', 'networkAccess', False),
                                     ('writable_roots', 'writableRoots', []),
                                     ('exclude_slash_tmp', 'excludeSlashTmp', False),
                                     ('exclude_tmpdir_env_var', 'excludeTmpdirEnvVar', False)]:
            expected[native] = config.get('sandbox_workspace_write.' + key, default)
    return actual == expected


def _live_permissions(value):
    """The approval policy and sandbox a loaded thread reports: a thread/resume result, or a
    thread/settings/updated notification's threadSettings. None when either is missing."""
    if not isinstance(value, dict):
        return None
    approval = value.get('approvalPolicy')
    sandbox = value['sandbox'] if 'sandbox' in value else value.get('sandboxPolicy')
    if (not isinstance(approval, (str, dict)) or not isinstance(sandbox, dict)
            or not isinstance(sandbox.get('type'), str)):
        return None
    return {'approvalPolicy': approval, 'sandbox': sandbox}


def _restricted(permissions):
    """Never asking for approval inside a sandbox: what needs approval (a connector's
    write, for one) is refused rather than asked."""
    return permissions['approvalPolicy'] == 'never' and permissions['sandbox']['type'] != 'dangerFullAccess'


def _private_acl(path):
    if os.name != 'nt':
        path.chmod(0o700)
        return
    # Owner and SYSTEM only, inherited by receipts created inside this directory.
    advapi = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    descriptor = ctypes.c_void_p()
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    advapi.SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    advapi.SetFileSecurityW.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW('D:P(A;OICI;FA;;;OW)(A;OICI;FA;;;SY)', 1, ctypes.byref(descriptor), None):
        raise OSError('PC 세션 작업 디렉터리 권한을 준비하지 못했습니다.')
    try:
        if not advapi.SetFileSecurityW(str(path), 0x80000004, descriptor):
            raise OSError('PC 세션 작업 디렉터리 권한을 적용하지 못했습니다.')
    finally:
        kernel.LocalFree(descriptor)


class Store:
    def __init__(self, home):
        self.home = Path(home)
        if (not isinstance(STORE_RELATIVE, str) or Path(STORE_RELATIVE).is_absolute()
                or '..' in Path(STORE_RELATIVE).parts or not STORE_RELATIVE):
            raise ValueError('PC 작업 상태 경로가 올바르지 않습니다.')
        self.root = self.home / STORE_RELATIVE

    def prepare(self):
        wc._safe(self.root, self.home)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        wc._safe(self.root, self.home)
        _private_acl(self.root)

    def path(self, name):
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,180}', name):
            raise ValueError('PC 작업 기록 이름이 올바르지 않습니다.')
        return wc._safe(self.root / name, self.root)

    def read(self, name):
        path = self.path(name)
        try:
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 65536:
                raise ValueError('PC 작업 기록이 안전하지 않습니다.')
            return wc._json(path, self.root)
        except FileNotFoundError:
            return None

    def save(self, name, value):
        target = self.path(name)
        temporary = self.path('.' + secrets.token_hex(16) + '.tmp')
        fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as out:
                json.dump(value, out, ensure_ascii=True, separators=(',', ':'))
                out.flush()
                os.fsync(out.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    @contextmanager
    def lock(self):
        self.prepare()
        fd = os.open(str(self.path('.operation.lock')), os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            if os.fstat(fd).st_nlink != 1:
                raise ValueError('PC 작업 잠금 파일이 안전하지 않습니다.')
            try:
                if os.name == 'nt':
                    import msvcrt
                    if os.fstat(fd).st_size == 0:
                        os.write(fd, b'0')
                    os.lseek(fd, 0, 0)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise ValueError('PC에서 다른 작업을 처리하고 있습니다. 잠시 뒤 다시 시도해 주세요.') from None
            yield
        finally:
            os.close(fd)

    def secret(self):
        found = self.read('proof-key.json')
        if found is None:
            found = {'key': secrets.token_hex(32)}
            self.save('proof-key.json', found)
        if not isinstance(found, dict) or not re.fullmatch(r'[a-f0-9]{64}', found.get('key', '')):
            raise ValueError('PC 세션 확인 키를 읽지 못했습니다.')
        return bytes.fromhex(found['key'])


class NativeProxy(wc.Proxy):
    ALLOWED = wc.Proxy.ALLOWED | {'turn/start', 'turn/interrupt', 'thread/archive', 'thread/resume',
                                  'thread/settings/update'}

    def notification(self, until):
        """The next server notification before `until` (monotonic), else None. Waiting stops
        only between messages, so no frame is left half read; a message that has begun
        arriving is read whole within the connection's own deadline."""
        while True:
            if not self.pending:
                try:
                    chunk = self.frames.get(timeout=max(0, until - time.monotonic()))
                except queue.Empty:
                    return None
                if not chunk:
                    raise ValueError('PC 서버 읽기 연결이 끝났습니다.')
                self.pending.extend(chunk)
            message = self._message()
            # A server request (it has an id) is the TUI's to answer; it is left unanswered.
            if isinstance(message.get('method'), str) and 'id' not in message:
                return message


def _loaded(client):
    """Bounded pagination: a truncated list never proves a thread is absent."""
    found, cursor, seen = set(), None, set()
    for unused in range(3):
        result = client.call('thread/loaded/list', {'limit': wc.LIMIT, **({'cursor': cursor} if cursor else {})})
        if not isinstance(result, dict) or not isinstance(result.get('data'), list) or any(not isinstance(sid, str) for sid in result['data']):
            raise ValueError('PC 실행 목록 형식이 올바르지 않습니다.')
        found.update(result['data'])
        cursor = result.get('nextCursor')
        if cursor is None:
            return found, True
        if not isinstance(cursor, str) or not cursor or cursor in seen or len(cursor) > 4096:
            raise ValueError('PC 실행 목록 페이지를 확인하지 못했습니다.')
        seen.add(cursor)
    return found, False


def _state(thread):
    turns = thread.get('turns')
    if not isinstance(turns, list):
        raise ValueError('PC 세션 응답 형식이 올바르지 않습니다.')
    known = all(isinstance(t, dict) and t.get('status') in ('inProgress', 'completed', 'interrupted', 'failed') for t in turns)
    active = [t for t in turns if isinstance(t, dict) and t.get('status') == 'inProgress']
    runtime = wc._codex_runtime(thread.get('status')) if known else wc._unknown_runtime('turn_status_unsupported', 'unsupported')
    phase = runtime['phase']
    if phase == 'idle' and active:
        runtime = wc._unknown_runtime('status_inconsistent', 'unsupported')
    return {**runtime, 'activeTurnId': active[0].get('id') if len(active) == 1 else None,
            'activeFlags': wc._flags(thread.get('status')),
            'turns': [{'id': t.get('id'), 'status': t.get('status')} for t in turns[-80:] if isinstance(t, dict)],
            'updatedAt': wc._epoch(thread.get('updatedAt'))}


def _remote(value):
    if isinstance(value, dict):
        return any(key in ('vendorRemote', 'remote') or _remote(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_remote(item) for item in value)
    return value in ('vendorRemote', 'remote') if isinstance(value, str) else False


def _capabilities(thread):
    state = _state(thread)
    remote = _remote(thread.get('source')) or _remote(thread.get('threadSource'))
    supported = thread.get('canAcceptDirectInput') is True and state['runtimeKnown'] and not remote
    return {'canRead': True, 'canSend': supported, 'canAttachImages': supported,
            'canAnswerApprovals': False, 'canAnswerQuestions': False,
            'reason': None if supported else '이 PC 세션의 직접 입력 지원을 확인하지 못했습니다. 터미널에서 이어가세요.'}


def _receipt(identity, request_id, thread):
    for turn in reversed(thread.get('turns') or []):
        for item in turn.get('items') or []:
            if isinstance(item, dict) and item.get('type') == 'userMessage' and item.get('clientId') == request_id:
                delivery = turn.get('status')
                return {'source': identity, 'requestId': request_id,
                        'delivery': delivery if delivery in ('completed', 'interrupted', 'failed') else 'accepted',
                        'confirmed': True, 'readbackConfirmed': True, 'turnId': turn.get('id'), 'canRetry': False}
    return {'source': identity, 'requestId': request_id, 'delivery': 'unknown', 'confirmed': False,
            'readbackConfirmed': False, 'turnId': None, 'canRetry': False,
            'reason': 'PC 수신 결과를 확인하지 못했습니다. 같은 입력을 자동으로 다시 보내지 않습니다.'}


def _terminate_windows(expected):
    """Pin one process handle, recheck creation time and executable, then terminate."""
    if os.name != 'nt':
        raise ValueError('Windows 원본 프로세스에서만 종료할 수 있습니다.')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    handle = kernel.OpenProcess(0x1000 | 0x100000 | 1, False, expected['pid'])
    if not handle:
        raise OSError('원본 프로세스를 열지 못했습니다.')
    try:
        values = [wintypes.FILETIME() for unused in range(4)]
        length, image = wintypes.DWORD(32768), ctypes.create_unicode_buffer(32768)
        if not kernel.GetProcessTimes(handle, *[ctypes.byref(value) for value in values]) or not kernel.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(length)):
            raise OSError('원본 프로세스 신원을 확인하지 못했습니다.')
        start = str((values[0].dwHighDateTime << 32) | values[0].dwLowDateTime)
        if start != expected['start'] or wc._norm(image.value) != wc._norm(expected['exe']):
            return {'applied': False, 'confirmed': False, 'status': 'stale', 'reason': '원본 프로세스가 변경되어 종료하지 않았습니다.'}
        if not kernel.TerminateProcess(handle, 0):
            raise OSError('원본 프로세스 종료 요청을 확인하지 못했습니다.')
        confirmed = kernel.WaitForSingleObject(handle, 1500) == 0
        return {'applied': True, 'confirmed': confirmed, 'status': 'terminated' if confirmed else 'unconfirmed',
                'reason': None if confirmed else '종료 요청을 보냈지만 원본 종료를 확인하지 못했습니다.'}
    finally:
        kernel.CloseHandle(handle)


class WindowsRuntime:
    def __init__(self, home=None, reader=None, proxy_factory=NativeProxy, terminate_process=_terminate_windows):
        self.reader = reader or wc.WindowsCollector(home)
        self.home, self.proxy_factory = self.reader.home, proxy_factory
        self.store = Store(self.home)
        self.terminate_process = terminate_process

    def recover_session(self, source):
        """Read exact saved metadata only; no endpoint, process launch or native write."""
        identity = _identity(source)
        if identity['agent'] == 'codex':
            rows = self.reader.database_rows(identity['id'])
            cwd = source.get('cwd')
            if (len(rows) != 1 or rows[0].get('id') != identity['id'] or rows[0].get('archived') != 0
                    or not isinstance(cwd, str) or not Path(wc._windows_path(cwd)).is_absolute()
                    or not isinstance(rows[0].get('cwd'), str)
                    or wc._norm(cwd) != wc._norm(rows[0]['cwd'])):
                raise ValueError('PC 원본 세션과 저장된 작업 폴더를 확인하지 못했습니다.')
            row = self.reader._row(rows[0])
            row = {**row, **identity, 'cwd': rows[0]['cwd']}
        else:
            if source.get('home') not in (None, '', '.claude'):
                raise ValueError('원본 PC Claude 계정 경로를 확인해 주세요.')
            sid = source.get('sessionId') or identity['id']
            if not isinstance(sid, str) or not wc.SID.fullmatch(sid):
                raise ValueError('PC Claude 대화 식별자가 올바르지 않습니다.')
            root = self.reader.claude_home / 'projects'
            matches = list(root.glob('*/' + sid + '.jsonl'))
            if len(matches) != 1:
                raise ValueError('정확한 PC Claude 저장 대화를 확인하지 못했습니다.')
            path = wc._safe(matches[0], root)
            if path.stem != sid or not path.is_file():
                raise ValueError('PC Claude 저장 대화의 식별자가 바뀌었습니다.')
            header = wc._head(path)
            ids = {entry['sessionId'] for entry in header if isinstance(entry.get('sessionId'), str) and entry['sessionId']}
            cwds = {entry['cwd'] for entry in header if isinstance(entry.get('cwd'), str) and entry['cwd']}
            cwd = source.get('cwd')
            if (ids != {sid} or not isinstance(cwd, str) or not Path(wc._windows_path(cwd)).is_absolute()
                    or not cwds or any(wc._norm(value) != wc._norm(cwd) for value in cwds)):
                raise ValueError('PC Claude 저장 대화의 식별자 또는 작업 폴더가 바뀌었습니다.')
            row = {**identity, 'home': source.get('home'), 'cwd': cwd}
            if source.get('sessionId'):
                row['sessionId'] = sid
            if source.get('kind') in ('interactive', 'background', 'bg'):
                row['kind'] = source['kind']
        return {'source': {**row, 'phase': 'unknown', 'runtimeKnown': False, 'readOnly': True, 'canRead': True,
                           'runtimeReason': '저장된 대화입니다. 현재 실행 상태는 확인되지 않았습니다.'}}

    def _resume_record_name(self, identity):
        # Host aliases must not provide another route around an unresolved resume.
        return 'resume-' + _digest([identity['agent'], identity['home'], identity['id']]) + '.json'

    def _resume_pending(self, observed):
        if not self.store.root.exists():
            return False
        name = self._resume_record_name(observed['source'])
        pending = self.store.read(name)
        if pending is None:
            if self.store.path(name).exists():
                raise ValueError(RESUME_PENDING_REASON)
            return False
        server = pending.get('server') if isinstance(pending, dict) else None
        if (not isinstance(pending, dict) or set(pending) != {'version', 'threadId', 'path', 'cwd', 'server', 'settings'}
                or pending.get('version') != 1 or pending.get('threadId') != observed['source']['id']
                or not all(isinstance(pending.get(k), str) and pending[k] for k in ('path', 'cwd'))
                or not isinstance(server, dict) or set(server) != {'pid', 'start', 'exe'}
                or type(server.get('pid')) is not int or server['pid'] <= 0
                or not all(isinstance(server.get(k), str) and server[k] for k in ('start', 'exe'))
                or not isinstance(pending.get('settings'), dict)
                or pending['settings'].get('sandbox') not in ('danger-full-access', 'workspace-write', 'read-only')
                or pending['settings'].get('approvalPolicy') not in ('never', 'on-request')):
            raise ValueError(RESUME_PENDING_REASON)
        # A different verified process cannot contain the previous process's loaded thread.
        # Do not clear anything on read/preview; the next explicit cold resume replaces it.
        return server == observed['server']

    def _require_resume_verified(self, observed):
        if self._resume_pending(observed):
            raise ValueError(RESUME_PENDING_REASON)

    def _thread(self, client, identity, cwd):
        loaded, complete = _loaded(client)
        if identity['id'] not in loaded:
            raise ValueError('PC 원본 세션이 현재 서버에 열려 있지 않습니다.')
        result = client.call('thread/read', {'threadId': identity['id'], 'includeTurns': True})
        thread = result.get('thread') if isinstance(result, dict) else None
        if not isinstance(thread, dict) or thread.get('id') != identity['id'] or wc._norm(thread.get('cwd')) != wc._norm(cwd):
            raise ValueError('PC 원본 세션의 작업 폴더가 변경되었습니다.')
        _state(thread)
        return thread

    def _codex(self, source, deadline):
        identity = _identity(source)
        rows = self.reader.database_rows(identity['id'])
        if len(rows) != 1 or rows[0].get('archived') or source.get('cwd') and wc._norm(source['cwd']) != wc._norm(rows[0]['cwd']):
            raise ValueError('PC 원본 세션과 작업 폴더를 확인하지 못했습니다.')
        matches, failures = [], False
        for path in self.reader.endpoints():
            if time.monotonic() >= deadline:
                raise TimeoutError('PC 세션 소유 서버 확인 시간이 초과되었습니다.')
            try:
                with self.proxy_factory(self.reader.binary, path, min(deadline, time.monotonic() + 4)) as client:
                    loaded, complete = _loaded(client)
                    if not complete:
                        raise ValueError('PC 서버 전체 실행 목록을 확인하지 못했습니다.')
                    if identity['id'] not in loaded:
                        continue
                    thread = self._thread(client, identity, rows[0]['cwd'])
                state = wc._json(path.parent / 'server-state.json', self.reader.codex_home)
                process = self.reader.process_reader(state.get('server_pid'))
                if not process:
                    raise ValueError('PC 원본 서버 프로세스를 확인하지 못했습니다.')
                matches.append({'source': identity, 'path': str(path), 'cwd': rows[0]['cwd'], 'thread': thread,
                                'server': {'pid': process['pid'], 'start': process['start'], 'exe': wc._norm(process['exe'])}})
            except (OSError, ValueError, TimeoutError):
                failures = True
        if not matches and not failures:
            raise NotLoaded('PC 원본 세션이 현재 서버에 열려 있지 않습니다.')
        if len(matches) != 1 or failures:
            raise ValueError('PC 세션의 정확한 소유 서버를 확인하지 못했습니다.')
        return matches[0]

    def _project_endpoint(self, cwd):
        """The running project server a stored session under ~/Projects resumes into: the one
        named after its Projects folder (any case). With no server of that name, the only
        `<folder>-…` server that no Projects folder of its own name owns (a project whose
        folder name differs). None otherwise, or when that server is not running."""
        root = wc._windows_path(self.reader.home / 'Projects').rstrip('/') + '/'
        path = wc._windows_path(cwd).rstrip('/')
        if not path.casefold().startswith(root.casefold()):
            return None
        folder = path[len(root):].split('/')[0].casefold()
        try:
            names = sorted(entry.name for entry in (self.reader.codex_home / 'agents-servers').iterdir()
                           if entry.is_dir())[:256]
        except OSError:
            return None
        named = [name for name in names if name.casefold() == folder]
        if not named:
            named = [name for name in names if name.casefold().startswith(folder + '-')
                     and not (self.reader.home / 'Projects' / name).exists()]
        return self.reader.endpoint(named[0]) if len(named) == 1 else None

    def _codex_resume(self, source, prepare=False, deadline=None):
        deadline = time.monotonic() + TERMINAL_SECONDS if deadline is None else deadline
        identity = _identity(source)
        rows = self.reader.database_rows(identity['id'])
        if len(rows) != 1 or rows[0].get('archived') or source.get('cwd') and wc._norm(source['cwd']) != wc._norm(rows[0]['cwd']):
            raise ValueError('PC 저장 기록과 작업 폴더를 확인하지 못했습니다.')
        cwd = rows[0]['cwd']
        if not isinstance(cwd, str) or not Path(wc._windows_path(cwd)).is_dir() or not self.reader.binary.is_file():
            raise ValueError('PC 원본 작업 폴더나 Codex 실행 파일을 확인하지 못했습니다.')
        # Two writers on one thread would interleave its record.
        if self.reader.writer_owners({identity['id']}):
            raise ValueError('이 기록은 PC에서 다른 Codex 실행(codex exec 등)이 쓰고 있습니다. 그 실행이 끝난 뒤 이어가 주세요.')
        endpoint = self._project_endpoint(cwd)
        if endpoint is None:
            # An SSH session is elevated: without its project's own (non-elevated) server, Codex
            # would run an embedded runtime in that session instead.
            raise ValueError('이 기록의 프로젝트 Codex 서버가 PC에서 꺼져 있습니다. 새 세션으로 그 프로젝트를 열어 서버를 켠 뒤 이어갈 수 있습니다.')
        settings = _codex_resume_settings(rows[0])
        argv = [str(self.reader.binary), 'resume', identity['id'], '--remote', 'unix://' + Path(endpoint).as_posix(), '--cd', cwd]
        if prepare:
            # A preview never loads a thread. The explicit terminal action preloads it
            # with its saved permissions; the native remote TUI sends none (terminal_settle).
            with self.store.lock(), self.proxy_factory(self.reader.binary, endpoint, deadline) as client:
                current = self.reader.database_rows(identity['id'])
                keys = ('id', 'cwd', 'archived', 'sandbox_policy', 'approval_mode')
                if len(current) != 1 or any(current[0].get(k) != rows[0].get(k) for k in keys):
                    raise ValueError('PC 세션의 실행 권한이 변경되었습니다. 다시 연결해 주세요.')
                state = wc._json(Path(endpoint).parent / 'server-state.json', self.reader.codex_home)
                process = self.reader.process_reader(state.get('server_pid'))
                if not process:
                    raise ValueError('PC 원본 서버 프로세스를 확인하지 못했습니다.')
                observed = {'source': identity, 'path': str(endpoint), 'cwd': cwd,
                            'server': {'pid': process['pid'], 'start': process['start'], 'exe': wc._norm(process['exe'])}}
                self._verify_endpoint(observed)
                loaded, complete = _loaded(client)
                if not complete:
                    raise ValueError('PC 서버의 실행 목록을 다시 확인해 주세요.')
                if identity['id'] not in loaded:
                    if deadline - time.monotonic() < RESUME_MIN_SECONDS:
                        raise TimeoutError('PC 세션 재개 전에 확인 시간이 부족해 중단했습니다. 다시 연결해 주세요.')
                    name = self._resume_record_name(identity)
                    # Persist BEFORE sending: a lost response or terminated SSH process must
                    # block subsequent warm attaches and chat sends in another RPC process.
                    self.store.save(name, {'version': 1, 'threadId': identity['id'], 'path': str(endpoint),
                                          'cwd': cwd, 'server': observed['server'], 'settings': settings})
                    if deadline - time.monotonic() < RESUME_MIN_SECONDS:
                        # Nothing has been sent yet; a slow local write is not an
                        # ambiguous native result. The operation lock still owns this record.
                        self.store.path(name).unlink()
                        raise TimeoutError('PC 세션 재개 전에 확인 시간이 부족해 중단했습니다.')
                    result = client.call('thread/resume', {'threadId': identity['id'], 'cwd': cwd, **settings})
                    thread = result.get('thread') if isinstance(result, dict) else None
                    if (not isinstance(thread, dict) or thread.get('id') != identity['id']
                            or wc._norm(thread.get('cwd')) != wc._norm(cwd)
                            or not _same_resume_permissions(result, settings)):
                        raise ValueError('PC 세션의 재개와 기존 실행 권한을 확인하지 못했습니다.')
                    self._verify_endpoint(observed)
                    self.store.path(name).unlink()
                    live = _live_permissions(result)
                else:
                    self._require_resume_verified(observed)
                    self._thread(client, identity, cwd)
                    live = self._join(client, identity, settings)
                # The TUI that attaches next sends no permissions: terminal_settle keeps these.
                self._save_settle(identity, observed, live, settings)
                return {'argv': argv, 'cwd': cwd, 'env': {'CODEX_HOME': str(self.reader.codex_home)},
                        'nativeSource': {**identity, 'cwd': cwd, 'kind': source.get('kind')}, 'connectionMode': 'attach',
                        'resumed': True, 'settle': True, 'restricted': _restricted(live)}
        return {'argv': argv, 'cwd': cwd, 'env': {'CODEX_HOME': str(self.reader.codex_home)},
                'nativeSource': {**identity, 'cwd': cwd, 'kind': source.get('kind')}, 'connectionMode': 'attach', 'resumed': True}

    def _join(self, client, identity, settings):
        """Subscribe this connection to the thread and read its live permissions.

        The stored permissions go along, never the cwd: a loaded thread with the same ones, or
        one another client is subscribed to, is joined as it is; one unloaded meanwhile is
        loaded with them rather than with the server's defaults."""
        params = {'threadId': identity['id'], 'approvalPolicy': settings['approvalPolicy'], 'sandbox': settings['sandbox'],
                  **({'config': settings['config']} if settings.get('config') else {})}
        try:
            result = client.call('thread/resume', {**params, 'excludeTurns': True})
        except ValueError:
            # A server that does not take excludeTurns: the same join, with the history.
            result = client.call('thread/resume', params)
        thread = result.get('thread') if isinstance(result, dict) else None
        live = _live_permissions(result)
        if not isinstance(thread, dict) or thread.get('id') != identity['id'] or live is None:
            raise ValueError('PC 세션의 현재 실행 권한을 확인하지 못했습니다.')
        return live

    def _settle_record_name(self, identity):
        return 'settle-' + _digest([identity['agent'], identity['home'], identity['id']]) + '.json'

    def _save_settle(self, identity, observed, live, settings):
        if live is None:
            raise ValueError('PC 세션의 현재 실행 권한을 확인하지 못했습니다.')
        self.store.save(self._settle_record_name(identity), {
            'version': 1, 'threadId': identity['id'], 'path': observed['path'], 'server': observed['server'],
            'expected': live, 'settings': settings, 'createdAt': int(time.time())})

    @staticmethod
    def _settings_notified(client, identity, until):
        """The thread's permissions from its next thread/settings/updated before `until`."""
        while True:
            message = client.notification(until)
            if message is None:
                return None
            params = message.get('params')
            if (message.get('method') == 'thread/settings/updated' and isinstance(params, dict)
                    and params.get('threadId') == identity['id']):
                live = _live_permissions(params.get('threadSettings'))
                if live is not None:
                    return live

    def terminal_settle(self, source):
        """After the board started a terminal's TUI on a thread its plan loaded: keep the
        permissions the plan recorded.

        Subscribed for SETTLE_HOLD_SECONDS, so a TUI that joins meanwhile joins the loaded
        thread as it is. A TUI that came first may have rebuilt it with the server's default
        sandbox: then the recorded permissions are applied again and read back. A change
        notified while subscribed (a /permissions choice in that TUI) is the user's and stays."""
        identity = _identity(source)
        result = {'source': identity, 'settled': None, 'restored': False, 'changed': False,
                  'restricted': False, 'reason': None}
        if identity['agent'] != 'codex':
            return result
        name = self._settle_record_name(identity)
        with self.store.lock():
            record = self.store.read(name)
            if record is None:
                return result
            # Used once: a later terminal starts from its own plan.
            self.store.path(name).unlink()
        failed = {**result, 'settled': False, 'reason': SETTLE_FAILED}
        expected = _live_permissions(record.get('expected')) if isinstance(record, dict) else None
        if (expected is None or record.get('version') != 1 or record.get('threadId') != identity['id']
                or not isinstance(record.get('path'), str) or not isinstance(record.get('server'), dict)
                or not isinstance(record.get('settings'), dict)
                or record['settings'].get('sandbox') not in ('danger-full-access', 'workspace-write', 'read-only')
                or record['settings'].get('approvalPolicy') not in ('never', 'on-request')
                or type(record.get('createdAt')) is not int
                or not 0 <= time.time() - record['createdAt'] <= SETTLE_RECORD_SECONDS):
            return failed
        failed['restricted'] = _restricted(expected)
        hold = time.monotonic() + SETTLE_HOLD_SECONDS
        restored = False
        try:
            self._verify_endpoint({'path': record['path'], 'server': record['server']})
            with self.proxy_factory(self.reader.binary, record['path'], hold + SETTLE_CONFIRM_SECONDS + 5) as client:
                live = self._join(client, identity, record['settings'])
                if live != expected:
                    client.call('thread/settings/update', {'threadId': identity['id'],
                                                           'approvalPolicy': expected['approvalPolicy'],
                                                           'sandboxPolicy': expected['sandbox']})
                    restored = True
                    # Notified only when the settings changed; read back once more otherwise.
                    live = (self._settings_notified(client, identity, time.monotonic() + SETTLE_CONFIRM_SECONDS)
                            or self._join(client, identity, record['settings']))
                    if live != expected:
                        return {**failed, 'restored': True}
                while True:
                    later = self._settings_notified(client, identity, hold)
                    if later is None:
                        break
                    live = later
        except (OSError, ValueError, TimeoutError):
            return {**failed, 'restored': restored}
        return {**result, 'settled': True, 'restored': restored, 'changed': live != expected,
                'restricted': _restricted(live)}

    def _claude_running(self, sid):
        """Whether a live Claude process on this PC owns the session (its registry record).
        A record that cannot be read while its Claude process lives may be this one: unknown."""
        for path in sorted((self.reader.claude_home / 'sessions').glob('[0-9]*.json'))[:256]:
            if not path.stem.isdigit():
                continue
            try:
                row = wc._json(path, self.reader.claude_home)
            except (OSError, ValueError, TypeError):
                process = self.reader.process_reader(int(path.stem))
                if process and self.reader._claude_executable(process.get('exe')):
                    raise ValueError('PC Claude 실행 기록을 읽지 못했습니다. 잠시 뒤 다시 시도해 주세요.')
                continue
            if not isinstance(row, dict) or row.get('sessionId') != sid:
                continue
            process = self.reader.process_reader(int(path.stem))
            if process and (not isinstance(row.get('procStart'), str) or row.get('procStart') == process.get('start')):
                return True
        return False

    def _claude_resume(self, source):
        """A stored Claude conversation no PC process runs, continued in a board terminal."""
        identity = _identity(source)
        if self._claude_running(identity['id']):
            raise ValueError('이 Claude 세션은 PC 터미널에서 실행 중입니다. 그 터미널에서 이어가세요.')
        path = self.reader._transcripts().get(identity['id'])
        if path is None:
            raise ValueError('PC Claude 기록을 확인하지 못했습니다.')
        cwd = next((entry['cwd'] for entry in wc._head(path) if isinstance(entry.get('cwd'), str)), '')
        if source.get('cwd') and wc._norm(source['cwd']) != wc._norm(cwd):
            raise ValueError('PC Claude 기록의 작업 폴더가 바뀌었습니다.')
        binary = self.reader.home / '.local/bin/claude.exe'
        if not cwd or not Path(wc._windows_path(cwd)).is_dir() or not binary.is_file():
            raise ValueError('PC 원본 작업 폴더나 Claude 실행 파일을 확인하지 못했습니다.')
        return {'argv': [str(binary), '--resume', identity['id']], 'cwd': cwd, 'env': {},
                'nativeSource': {**identity, 'cwd': cwd, 'kind': 'interactive'}, 'connectionMode': 'resume', 'resumed': True}

    def _claude(self, source):
        identity = _identity(source)
        matches = []
        for path in sorted((self.reader.claude_home / 'sessions').glob('[0-9]*.json'))[:256]:
            try:
                row = wc._json(path, self.reader.claude_home)
                if row.get('sessionId') != identity['id']:
                    continue
                pid = int(path.stem)
                process = self.reader.process_reader(pid)
                if (not process or row.get('pid') != pid or type(row.get('pid')) is not int
                        or row.get('procStart') != process.get('start')
                        or not re.fullmatch(r'\d{17,20}', row.get('procStart', ''))
                        or str(row.get('pidDomain', '')).casefold() != 'win32:' + self.reader.hostname.casefold()
                        or row.get('kind') not in ('interactive', 'bg')
                        or not self.reader._claude_executable(process.get('exe'))
                        or source.get('cwd') and wc._norm(source['cwd']) != wc._norm(row.get('cwd'))):
                    raise ValueError('PC Claude 프로세스의 신원이 변경되었습니다.')
                matches.append({'row': row, 'process': process, 'source': identity, 'registry': str(path)})
            except (OSError, TypeError):
                raise ValueError('PC Claude 실행 기록을 확인하지 못했습니다.') from None
        if len(matches) != 1:
            raise ValueError('PC Claude 실행의 정확한 소유 프로세스를 확인하지 못했습니다.')
        return matches[0]

    def chat(self, args):
        source, action = args.get('source'), args.get('action')
        identity = _identity(source)
        if action not in ('read', 'capabilities', 'send', 'receipt'):
            raise ValueError('지원하지 않는 PC 대화 요청입니다.')
        if identity['agent'] != 'codex':
            if action not in ('read', 'capabilities'):
                raise ValueError('Claude 입력은 정확한 세션의 보드 터미널을 통해 보냅니다.')
            result = self.reader.read('claude', identity['id'])
            return {**result, 'capabilities': {'canSend': False, 'canRead': True}}
        if action in ('send', 'receipt') and not REQUEST.fullmatch(args.get('requestId', '')):
            raise ValueError('PC 메시지 요청 식별자가 올바르지 않습니다.')
        try:
            observed = self._codex(source, time.monotonic() + 12)
        except (OSError, ValueError, TimeoutError):
            if action not in ('read', 'capabilities'):
                raise
            return {**self.reader.read('codex', identity['id'], '.codex'),
                    'capabilities': {'canSend': False, 'canRead': True, 'reason': '현재 실행 중인 PC 원본 서버를 확인하지 못했습니다.'}}
        thread = observed['thread']
        if action == 'send':
            return self._send(args, observed)
        supplied_ids = args.get('requestIds') or []
        if not isinstance(supplied_ids, list) or len(supplied_ids) > 32:
            raise ValueError('PC 메시지 확인 목록이 올바르지 않습니다.')
        request_ids = list(supplied_ids)
        if args.get('requestId') is not None:
            request_ids.insert(0, args['requestId'])
        if any(not isinstance(value, str) or not REQUEST.fullmatch(value) for value in request_ids):
            raise ValueError('PC 메시지 요청 식별자가 올바르지 않습니다.')
        request_ids = list(dict.fromkeys(request_ids))
        if action == 'receipt':
            request_ids = [args['requestId']]
        receipts = []
        for request_id in request_ids:
            if not isinstance(request_id, str) or not REQUEST.fullmatch(request_id):
                raise ValueError('PC 메시지 요청 식별자가 올바르지 않습니다.')
            receipt = _receipt(identity, request_id, thread)
            saved = None
            if self.store.root.exists():
                try:
                    saved = self.store.read('chat-' + _digest([identity, request_id]) + '.json')
                except (OSError, ValueError):
                    # Only an unconfirmed receipt depends on its record; a confirmed one
                    # merely loses its attachment names.
                    if not receipt['confirmed']:
                        raise
            if not receipt['confirmed'] and saved and saved.get('result'):
                receipt = saved['result']
            # The names of the files a request carried, as a Mac's ledger keeps them.
            if isinstance(saved, dict) and isinstance(saved.get('attachments'), list) and saved['attachments']:
                receipt = {**receipt, 'attachments': saved['attachments']}
            receipts.append(receipt)
        capabilities = {**_capabilities(thread), 'canLaunch': True}
        try:
            self._require_resume_verified(observed)
        except (OSError, ValueError) as exc:
            capabilities.update(canSend=False, canLaunch=False, reason=str(exc))
        messages, older = wc._recent_window(wc._codex_rows(thread['turns']))
        result = {**_state(thread), 'messages': messages, 'olderOmitted': older, 'cwd': observed['cwd'],
                  'modelSettings': wc._model_settings(thread),
                  'capabilities': capabilities, 'readOnly': not capabilities['canSend'], 'receipts': receipts}
        if action == 'receipt':
            result.update(receipts[0])
        return result

    def attachment(self, args, data):
        """Store one upload (windows_attachments.receive) in the work folder this PC records
        for the session, never another path; it starts and sends nothing."""
        identity = _identity(args.get('source'))
        if identity['agent'] == 'codex':
            rows = self.reader.database_rows(identity['id'])
            known = rows[0].get('cwd') if len(rows) == 1 and not rows[0].get('archived') else None
        else:
            known = next(iter(self.reader.claude_rows(target_sid=identity['id'])), {}).get('cwd')
        cwd = args.get('cwd')
        if not isinstance(cwd, str) or not isinstance(known, str) or not known or wc._norm(cwd) != wc._norm(known):
            raise ValueError('첨부할 세션의 실제 작업 폴더를 확인하지 못했습니다. 목록을 새로 읽어 주세요.')
        return wa.receive({**args, 'cwd': known}, data)

    def _attachments(self, args, observed):
        """(native inputs, manifest) of the request's uploads, looked up by ID in the verified
        session cwd as native_chat._send does: an image becomes a native image input, any
        other file a "첨부 파일:" path line (never its contents)."""
        ids = args.get('attachmentIds', [])
        if (not isinstance(ids, list) or len(ids) > wa.MAX_ATTACHMENTS or len(set(map(str, ids))) != len(ids)
                or any(not isinstance(value, str) or not wa.ID.fullmatch(value) for value in ids)):
            raise ValueError('첨부 식별자가 올바르지 않습니다.')
        if ids and not (args.get('source') or {}).get('cwd'):
            raise ValueError('실제 세션의 작업 폴더가 변경되었습니다. 목록을 다시 읽어 주세요.')
        inputs, manifest = [], []
        for identifier in ids:
            item = wa.resolve(observed['cwd'], identifier)
            manifest.append({'id': item['id'], 'name': item['name'], 'type': item['mime'], 'size': item['size'],
                             'kind': 'image' if item['mime'].startswith('image/') else 'file', 'source': 'sessionholic'})
            if item['mime'] in wa.IMAGE_TYPES:
                inputs.append({'type': 'localImage', 'path': item['path']})
            else:
                inputs.append({'type': 'text', 'text': '첨부 파일: ' + json.dumps(item['path'], ensure_ascii=False)})
        return inputs, manifest

    def _send(self, args, observed):
        identity, request_id = observed['source'], args['requestId']
        inputs = args.get('input')
        # The 32 KiB is the user's text; the board's attachment lines come on top of it.
        if (not isinstance(inputs, list) or len(inputs) > 16
                or any(not isinstance(item, dict) or set(item) != {'type', 'text'} or item.get('type') != 'text'
                       or not isinstance(item.get('text'), str) for item in inputs)
                or sum(len(item['text'].encode()) for item in inputs) > 32768):
            raise ValueError('PC 입력은 32 KiB 이하의 텍스트와 첨부 파일을 지원합니다.')
        attached, manifest = self._attachments(args, observed)
        if not attached and not any(item['text'].strip() for item in inputs):
            raise ValueError('PC 입력은 32 KiB 이하의 텍스트와 첨부 파일을 지원합니다.')
        # The digest covers the attachments' inputs: the same request ID with other files is refused.
        inputs = list(inputs) + attached
        digest, name = _digest(inputs), 'chat-' + _digest([identity, request_id]) + '.json'
        named = {'attachments': manifest} if manifest else {}
        with self.store.lock():
            prior = self.store.read(name)
            if prior:
                if prior.get('digest') != digest or (prior.get('binding') is not None and prior['binding'] != binding):
                    raise ValueError('같은 요청 ID로 다른 메시지를 보낼 수 없습니다.')
                return prior['result']
            with self.proxy_factory(self.reader.binary, observed['path'], time.monotonic() + 10) as client:
                self._verify_endpoint(observed)
                self._require_resume_verified(observed)
                thread = self._thread(client, identity, observed['cwd'])
                before = _receipt(identity, request_id, thread)
                if before['confirmed']:
                    return before
                caps = _capabilities(thread)
                if not caps['canSend']:
                    return {**before, 'delivery': 'rejected', 'reason': caps['reason']}
                before = {**before, **named}
                self.store.save(name, {'digest': digest, 'result': before, **named})
                result = before
                try:
                    response = client.call('turn/start', {'threadId': identity['id'], 'input': inputs, 'clientUserMessageId': request_id})
                    turn = response.get('turn') if isinstance(response, dict) else None
                    if isinstance(turn, dict) and isinstance(turn.get('id'), str):
                        result = {**before, 'delivery': 'accepted', 'confirmed': True, 'acknowledged': True, 'turnId': turn['id'], 'reason': None}
                    after = self._thread(client, identity, observed['cwd'])
                    confirmed = _receipt(identity, request_id, after)
                    if confirmed['confirmed']:
                        result = {**confirmed, **named}
                except (OSError, ValueError, TimeoutError):
                    pass
                self.store.save(name, {'digest': digest, 'result': result, **named})
                return result

    def _verify_endpoint(self, observed):
        path = Path(observed['path'])
        current = self.reader.endpoint(path.parent.name)
        state = wc._json(path.parent / 'server-state.json', self.reader.codex_home)
        process = self.reader.process_reader(state.get('server_pid'))
        key = {'pid': process['pid'], 'start': process['start'], 'exe': wc._norm(process['exe'])} if process else None
        if current is None or wc._norm(current) != wc._norm(path) or key != observed['server']:
            raise ValueError('PC 원본 서버가 변경되었습니다. 다시 확인해 주세요.')

    def _observe(self, source):
        identity = _identity(source)
        capabilities = {name: {'supported': False, 'method': None, 'reason': '원본 실행 상태를 확인하지 못했습니다.'} for name in ('interrupt', 'terminate')}
        try:
            if identity['agent'] == 'codex':
                observed = self._codex(source, time.monotonic() + 12)
                state = _state(observed['thread'])
                can_interrupt = state['activeTurnId'] is not None and state['phase'] in ('working', 'needs_input')
                can_end = state['runtimeKnown']
                capabilities = {
                    'interrupt': {'supported': can_interrupt, 'method': 'codex-turn-interrupt', 'reason': None if can_interrupt else '진행 중인 응답이 없습니다.'},
                    'terminate': {'supported': can_end, 'method': 'codex-thread-archive', 'reason': None if can_end else 'PC 실행 상태를 확인하지 못했습니다.',
                                  'effect': ARCHIVE_EFFECT, 'scope': 'session-and-descendants', 'scopeReadback': 'selected-session'}}
                target = {'identity': identity, 'path': observed['path'], 'cwd': wc._norm(observed['cwd']),
                          'server': observed['server'], 'turnId': state['activeTurnId'], 'phase': state['phase']}
                return {**observed, 'target': target, 'phase': state['phase'], 'capabilities': capabilities}
            observed = self._claude(source)
            row = observed['row']
            phase = wc._claude_phase(row.get('status'))
            capabilities['interrupt']['reason'] = '이 Claude 실행 방식은 응답만 중단하는 제어를 제공하지 않습니다.'
            capabilities['terminate'] = {'supported': True, 'method': 'claude-cli-process', 'reason': None,
                'effect': '선택한 Claude CLI 프로세스를 종료합니다. 대화 기록은 유지되며 별도 하위 도구 프로세스의 종료는 확인하지 않습니다.',
                'scope': 'process'}
            target = {'identity': identity, 'process': observed['process'], 'registry': observed['registry'], 'kind': row['kind'], 'cwd': wc._norm(row.get('cwd'))}
            return {**observed, 'target': target, 'phase': phase, 'capabilities': capabilities}
        except (OSError, ValueError, TimeoutError):
            return {'source': identity, 'target': None, 'phase': 'unknown', 'capabilities': capabilities}

    def _view(self, observed):
        payload = {'source': observed['source'], 'revision': _digest(observed['target']),
                   'expiresAt': int(time.time()) + PROOF_TTL, 'nonce': secrets.token_hex(12)}
        data = base64.urlsafe_b64encode(json.dumps(payload, separators=(',', ':')).encode()).decode().rstrip('=')
        proof = data + '.' + hmac.new(self.store.secret(), data.encode(), hashlib.sha256).hexdigest()
        return {**{key: payload[key] for key in ('source', 'revision', 'expiresAt')}, 'phase': observed['phase'],
                'capabilities': observed['capabilities'], 'targetProof': proof}

    def preview(self, source):
        observed = self._observe(source)
        with self.store.lock():
            return self._view(observed)

    def execute(self, args):
        source, action, request_id, proof = args.get('source'), args.get('action'), args.get('requestId'), args.get('targetProof')
        identity = _identity(source)
        if action not in ('interrupt', 'terminate') or not isinstance(request_id, str) or not CONTROL_REQUEST.fullmatch(request_id):
            raise ValueError('PC 세션 제어 요청이 올바르지 않습니다.')
        cleanup_terminals = args.get('cleanupTerminals')
        if cleanup_terminals is not None and (not isinstance(cleanup_terminals, list) or len(cleanup_terminals) > 64
                or any(not isinstance(tid, str) or not re.fullmatch(r'[a-f0-9]{32}', tid) for tid in cleanup_terminals)):
            raise ValueError('정리할 터미널 식별자가 올바르지 않습니다.')
        receipt_only = args.get('receiptOnly') is True
        if not receipt_only and (not isinstance(proof, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,3900}\.[a-f0-9]{64}', proof)):
            raise ValueError('PC 세션 확인 정보가 올바르지 않습니다.')
        digest, name = _digest([identity, action, proof]), 'control-' + _digest(request_id) + '.json'
        binding_keys = ('cwd', 'kind', 'sessionId') if identity['agent'] == 'claude' else ('cwd',)
        binding = {**identity, **{key: source.get(key) for key in binding_keys}}
        with self.store.lock():
            prior = self.store.read(name)
            if receipt_only:
                if (action != 'terminate' or not prior or prior.get('binding') != binding
                        or prior.get('result', {}).get('source') != identity
                        or prior['result'].get('action') != action or prior['result'].get('requestId') != request_id):
                    raise ValueError('기존 종료 결과의 대상을 확인하지 못했습니다. 종료를 다시 요청하지 않았습니다.')
                return {**prior['result'], 'replayed': True}
            if prior:
                if prior.get('digest') != digest:
                    raise ValueError('같은 요청 ID로 다른 세션을 제어할 수 없습니다.')
                return {**prior['result'], 'replayed': True}
            data, signature = proof.split('.')
            if not hmac.compare_digest(signature, hmac.new(self.store.secret(), data.encode(), hashlib.sha256).hexdigest()):
                raise ValueError('PC 세션 확인 정보가 변경되었습니다.')
            payload = json.loads(base64.urlsafe_b64decode(data + '=' * (-len(data) % 4)))
            if payload.get('source') != identity or type(payload.get('expiresAt')) is not int or not time.time() <= payload['expiresAt'] <= time.time() + PROOF_TTL + 2:
                raise ValueError('PC 세션 확인이 만료되었습니다. 다시 확인해 주세요.')
            proof_name = 'used-' + _digest(proof) + '.json'
            used = self.store.read(proof_name)
            if used:
                if used['result'].get('action') != action:
                    raise ValueError('이미 사용한 세션 확인 정보입니다.')
                return {**used['result'], 'replayed': True}
            observed = self._observe(source)
            result = {'source': identity, 'action': action, 'requestId': request_id, 'requestedAt': int(time.time()), 'confirmed': False, 'applied': False, 'status': 'rejected'}
            if cleanup_terminals is not None:
                result['cleanupTerminals'] = cleanup_terminals
            if payload.get('revision') != _digest(observed['target']):
                return {**result, 'status': 'stale', 'reason': '원본 실행이 변경되었습니다. 다시 확인해 주세요.', 'preview': self._view(observed)}
            cap = observed['capabilities'][action]
            if not cap['supported']:
                return {**result, 'reason': cap['reason']}
            pending = {**result, 'applied': None, 'status': 'unconfirmed', 'reason': 'PC 실행 결과를 확인하지 못했습니다. 자동 재실행하지 않습니다.'}
            self.store.save(name, {'digest': digest, 'binding': binding, 'result': pending})
            self.store.save(proof_name, {'result': pending})
            try:
                details = self._apply(observed, action)
                result.update(details)
            except (OSError, ValueError, TimeoutError):
                result = pending
            self.store.save(name, {'digest': digest, 'binding': binding, 'result': result})
            self.store.save(proof_name, {'result': result})
            return result

    def _apply(self, observed, action):
        identity = observed['source']
        if identity['agent'] == 'claude':
            fresh = self._claude({**identity, 'cwd': observed['row'].get('cwd')})
            if fresh['process'] != observed['process']:
                return {'applied': False, 'confirmed': False, 'status': 'stale', 'reason': '원본 프로세스가 변경되어 종료하지 않았습니다.'}
            return {**self.terminate_process(fresh['process']), 'scope': 'process',
                    'effect': observed['capabilities']['terminate']['effect'], 'descendantsConfirmed': False}
        self._verify_endpoint(observed)
        with self.proxy_factory(self.reader.binary, observed['path'], time.monotonic() + 10) as client:
            fresh = self._thread(client, identity, observed['cwd'])
            state = _state(fresh)
            if state['activeTurnId'] != observed['target']['turnId'] or state['phase'] != observed['phase']:
                return {'applied': False, 'confirmed': False, 'status': 'stale', 'reason': '원본 응답이 변경되어 제어하지 않았습니다.'}
            if action == 'interrupt':
                turn = state['activeTurnId']
                client.call('turn/interrupt', {'threadId': identity['id'], 'turnId': turn})
                after = self._thread(client, identity, observed['cwd'])
                exact = next((t for t in after['turns'] if t.get('id') == turn), None)
                confirmed = bool(exact and exact.get('status') in ('interrupted', 'completed', 'failed'))
            else:
                client.call('thread/archive', {'threadId': identity['id']})
                loaded, complete = _loaded(client)
                rows = self.reader.database_rows(identity['id'])
                confirmed = (complete and identity['id'] not in loaded and len(rows) == 1
                             and rows[0].get('archived') == 1 and wc._norm(rows[0].get('cwd')) == wc._norm(observed['cwd']))
            return {'applied': True, 'confirmed': confirmed, 'status': ('interrupted' if action == 'interrupt' else 'terminated') if confirmed else 'unconfirmed',
                    'reason': None if confirmed else 'PC 요청을 보냈지만 원본 상태 변경을 확인하지 못했습니다.',
                    **({'effect': ARCHIVE_EFFECT, 'scope': 'session-and-descendants', 'scopeReadback': 'selected-session',
                        'descendantsConfirmed': False} if action == 'terminate' else {})}

    def _warm_settle(self, identity, observed, deadline):
        """A loaded thread a board TUI attaches to: its live permissions, recorded for
        terminal_settle. None when its stored policy is not one the board can restore."""
        rows = self.reader.database_rows(identity['id'])
        try:
            settings = _codex_resume_settings(rows[0]) if len(rows) == 1 else None
        except ValueError:
            settings = None
        if settings is None:
            return None
        with self.proxy_factory(self.reader.binary, observed['path'], deadline) as client:
            live = self._join(client, identity, settings)
        self._save_settle(identity, observed, live, settings)
        return live

    def terminal_plan(self, source, prepare=False):
        identity = _identity(source)
        settle = None
        if identity['agent'] == 'codex':
            deadline = time.monotonic() + TERMINAL_SECONDS
            try:
                observed = self._codex(source, deadline - RESUME_MIN_SECONDS)
            except NotLoaded:
                return self._codex_resume(source, prepare=prepare, deadline=deadline)
            if prepare:
                with self.store.lock():
                    self._verify_endpoint(observed)
                    self._require_resume_verified(observed)
                    settle = self._warm_settle(identity, observed, deadline)
            else:
                self._verify_endpoint(observed)
                self._require_resume_verified(observed)
            argv = [str(self.reader.binary), 'resume', identity['id'], '--remote', 'unix://' + Path(observed['path']).as_posix(), '--cd', observed['cwd']]
            cwd = observed['cwd']
        else:
            # No process runs it (a background run's record may outlive it): it continues here.
            if not self._claude_running(identity['id']):
                return self._claude_resume(source)
            observed = self._claude(source)
            if observed['row'].get('kind') != 'bg':
                raise ValueError('이 Claude 세션은 원래 대화형 터미널에서 실행 중입니다. 정확한 연결을 지원하는 background 세션만 열 수 있습니다.')
            # The verified owner's registry record names its job. Closing the board terminal
            # ends only this attach client; the run keeps going.
            job = observed['row'].get('jobId')
            if not isinstance(job, str) or not CLAUDE_JOB.fullmatch(job):
                raise ValueError('이 PC Claude background 세션의 터미널 연결 식별자를 확인하지 못했습니다. 원본 PC 터미널에서 이어가세요.')
            binary = self.reader.home / '.local/bin/claude.exe'
            if not binary.is_file():
                raise ValueError('PC Claude 실행 파일을 확인하지 못했습니다.')
            argv, cwd = [str(binary), 'attach', job], observed['row'].get('cwd')
        if not isinstance(cwd, str) or not Path(wc._windows_path(cwd)).is_dir():
            raise ValueError('PC 원본 작업 폴더를 확인하지 못했습니다.')
        return {'argv': argv, 'cwd': cwd, 'env': {'CODEX_HOME': str(self.reader.codex_home)} if identity['agent'] == 'codex' else {},
                'nativeSource': {**identity, 'cwd': cwd, 'kind': source.get('kind')}, 'connectionMode': 'attach',
                **({'settle': True, 'restricted': _restricted(settle)} if settle is not None else {})}

    def resume_preview(self, source):
        identity = _identity(source)
        try:
            if identity['agent'] != 'codex':
                self._claude_resume(source)
            else:
                self.terminal_plan(source)
            return {'source': identity, 'allowed': True, 'blockedByBoard': False, 'reason': None}
        except (OSError, ValueError, TimeoutError) as exc:
            return {'source': identity, 'allowed': False, 'blockedByBoard': False,
                    'reason': str(exc) or '이 PC 기록을 다시 열 수 없습니다.'}


def handle(request, data=None):
    """One request; `data` is an upload's base64 lines (transfer.windows_rpc), for `attachment` only."""
    if not isinstance(request, dict) or not isinstance(request.get('args'), dict):
        raise ValueError('PC 작업 요청 형식이 올바르지 않습니다.')
    op, args = request.get('op'), request['args']
    if (op == 'attachment') != (data is not None):
        raise ValueError('PC 작업 요청 형식이 올바르지 않습니다.')
    runtime = WindowsRuntime()
    if op == 'attachment':
        return runtime.attachment(args, data)
    if op == 'chat':
        return runtime.chat(args)
    if op == 'control_preview':
        return runtime.preview(args.get('source'))
    if op == 'control_execute':
        return runtime.execute(args)
    if op == 'terminal_plan':
        return runtime.terminal_plan(args.get('source'), prepare=args.get('prepare') is True)
    if op == 'terminal_settle':
        return runtime.terminal_settle(args.get('source'))
    if op == 'resume_preview':
        return runtime.resume_preview(args.get('source'))
    if op == 'recover_session':
        return runtime.recover_session(args.get('source'))
    raise ValueError('지원하지 않는 PC 작업 요청입니다.')
