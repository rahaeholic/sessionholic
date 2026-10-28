"""Synthetic native responses only; no SSH, model calls, signals or real sessions."""
import base64
import copy
import hashlib
import json
from pathlib import Path
import struct
import sys
import tempfile
import time
import threading
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import windows_attachments as wa
import windows_collector as wc
import windows_runtime as wr


class FakeReader:
    def __init__(self, home):
        self.home = home
        self.codex_home = home / '.codex'
        self.claude_home = home / '.claude'
        self.binary = home / 'AppData/Local/Programs/OpenAI/Codex/bin/codex.exe'
        self.hostname = 'fixture-pc'
        self.sid = str(uuid.uuid4())
        self.cwd = home / 'Projects/fixture'
        self.cwd.mkdir(parents=True)
        self.path = self.codex_home / 'agents-servers/fixture/app.sock'
        self.path.parent.mkdir(parents=True)
        self.path.touch()
        self.binary.parent.mkdir(parents=True)
        self.binary.touch()
        (self.path.parent / 'server-state.json').write_text(json.dumps({'server_pid': 12}))
        self.processes = {12: {'pid': 12, 'start': '134362358929022995', 'exe': str(self.binary)}}
        self.row = {'id': self.sid, 'cwd': str(self.cwd), 'archived': 0,
                    'sandbox_policy': json.dumps({'type': 'disabled'}), 'approval_mode': 'never'}
        self.paths = [self.path]
        self.calls = []
        self.loaded = [self.sid]
        self.thread = {'id': self.sid, 'cwd': str(self.cwd), 'source': 'cli', 'canAcceptDirectInput': True,
                       'status': {'type': 'idle', 'activeFlags': []}, 'turns': []}
        self.fail_send = False
        self.fail_after = False
        self.send_calls = 0
        self.mutate_hook = None
        self.resume_hook = None

    def endpoints(self):
        return self.paths

    def writer_owners(self, ids):
        return {sid: self.processes[12] for sid in ids if sid in getattr(self, 'held', ())}

    def project(self, cwd):
        relative = Path(cwd).relative_to(self.home / 'Projects') if str(cwd).startswith(str(self.home / 'Projects')) else Path(cwd)
        return relative.parts[0] if relative.parts else '기타'

    def _transcripts(self):
        folder = self.claude_home / 'projects/fixture'
        return {path.stem: path for path in folder.glob('*.jsonl')} if folder.is_dir() else {}

    def endpoint(self, project):
        return next((path for path in self.paths if path.parent.name == project), None)

    def database_rows(self, sid=None):
        return [dict(self.row)] if sid in (None, self.sid) else []

    def claude_rows(self, target_sid=None):
        return [dict(row) for row in getattr(self, 'claude_list', []) if target_sid in (None, row['id'])]

    def process_reader(self, pid):
        return copy.deepcopy(self.processes.get(pid))

    def parent_reader(self, deadline):
        return dict(getattr(self, 'parents', {}))

    def arguments_reader(self, process, deadline):
        return copy.deepcopy(getattr(self, 'arguments', {}).get(process['pid']))

    def _claude_executable(self, exe):
        return Path(exe).name == 'claude.exe' and Path(exe).is_file()

    def read(self, *args):
        return {'messages': [], 'readOnly': True, 'phase': 'unknown', 'runtimeKnown': False}

    def proxy(self, binary, path, deadline):
        owner = self
        class Client:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def call(self, method, params):
                owner.calls.append((method, copy.deepcopy(params)))
                if method == 'thread/loaded/list':
                    if owner.fail_after and owner.send_calls:
                        raise TimeoutError('synthetic lost response')
                    return {'data': list(owner.loaded)}
                if method == 'thread/read':
                    return {'thread': copy.deepcopy(owner.thread)}
                if method == 'model/list':
                    result = getattr(owner, 'catalog', {'data': [], 'nextCursor': None})
                    if isinstance(result, Exception):
                        raise result
                    return copy.deepcopy(result)
                if method == 'thread/compact/start':
                    if getattr(owner, 'compact_timeout', False):
                        raise TimeoutError('synthetic lost compact reply')
                    return {}
                if method == 'thread/resume':
                    owner.loaded.append(params['threadId'])
                    modes = {'danger-full-access': 'dangerFullAccess', 'workspace-write': 'workspaceWrite',
                             'read-only': 'readOnly'}
                    sandbox = {'type': modes[params['sandbox']]}
                    if params['sandbox'] == 'read-only':
                        sandbox['networkAccess'] = False
                    elif params['sandbox'] == 'workspace-write':
                        config = params.get('config', {})
                        for key, native, default in [('network_access', 'networkAccess', False),
                                                     ('writable_roots', 'writableRoots', []),
                                                     ('exclude_slash_tmp', 'excludeSlashTmp', False),
                                                     ('exclude_tmpdir_env_var', 'excludeTmpdirEnvVar', False)]:
                            sandbox[native] = config.get('sandbox_workspace_write.' + key, default)
                    result = {'thread': copy.deepcopy(owner.thread), 'approvalPolicy': params['approvalPolicy'],
                              'sandbox': sandbox}
                    return owner.resume_hook(result) if owner.resume_hook else result
                if method == 'turn/start':
                    owner.send_calls += 1
                    if owner.fail_send:
                        raise TimeoutError('synthetic lost response')
                    turn = {'id': 'turn-sent', 'status': 'inProgress', 'items': [
                        {'type': 'userMessage', 'clientId': params['clientUserMessageId'], 'content': params['input']}]}
                    owner.thread['turns'].append(turn)
                    owner.thread['status']['type'] = 'active'
                    return {'turn': {'id': 'turn-sent'}}
                if method == 'turn/interrupt':
                    for turn in owner.thread['turns']:
                        if turn['id'] == params['turnId']:
                            turn['status'] = 'interrupted'
                    owner.thread['status']['type'] = 'idle'
                    if owner.mutate_hook:
                        owner.mutate_hook(method)
                    return {}
                if method == 'thread/archive':
                    owner.loaded = []
                    owner.row['archived'] = 1
                    if owner.mutate_hook:
                        owner.mutate_hook(method)
                    return {}
                raise AssertionError('unexpected native operation: ' + method)
        return Client()


class WindowsRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.reader = FakeReader(self.home)
        self.kills = []
        self.runtime = wr.WindowsRuntime(reader=self.reader, proxy_factory=self.reader.proxy,
            terminate_process=lambda process: self.kills.append(process) or {'applied': True, 'confirmed': True, 'status': 'terminated'})
        self.source = {'host': 'fixture-pc', 'agent': 'codex', 'id': self.reader.sid, 'home': '.codex', 'cwd': str(self.reader.cwd)}

    def send(self, request='request-1234', text='합성 입력'):
        return self.runtime.chat({'action': 'send', 'source': self.source, 'requestId': request,
                                  'input': [{'type': 'text', 'text': text}]})

    def active(self):
        self.reader.thread['status']['type'] = 'active'
        self.reader.thread['turns'] = [{'id': 'turn-original', 'status': 'inProgress', 'items': []}]

    def model_command(self, command, value=None, request='model-request-1234'):
        return self.runtime.chat({'action': 'command', 'source': self.source, 'command': command,
                                  'value': value, 'requestId': request})

    def models(self):
        self.reader.thread.update(model='example-one', reasoningEffort='high')
        self.reader.catalog = {'data': [
            {'model': name, 'displayName': name, 'defaultReasoningEffort': 'low',
             'supportedReasoningEfforts': [{'reasoningEffort': value} for value in efforts]}
            for name, efforts in [('example-one', ['low', 'high']), ('example-two', ['low'])]], 'nextCursor': None}

    def test_model_command_lists_account_models_and_next_idle_send_uses_selection(self):
        self.models()
        catalog = self.model_command('models')
        self.assertEqual([row['model'] for row in catalog['models']], ['example-one', 'example-two'])
        self.assertEqual(catalog['current'], {'model': 'example-one', 'effort': 'high'})
        chosen = self.model_command('model', 'example-two')
        self.assertEqual(chosen['next'], {'model': 'example-two', 'effort': 'low'})
        self.assertEqual(chosen['current']['model'], 'example-one')
        self.assertEqual(self.reader.send_calls, 0)
        state = self.runtime.chat({'action': 'read', 'source': self.source})
        self.assertEqual(state['modelSettings']['next'], chosen['next'])
        self.assertTrue(self.send()['confirmed'])
        sent = next(params for method, params in self.reader.calls if method == 'turn/start')
        self.assertEqual(set(sent), {'threadId', 'input', 'clientUserMessageId', 'model', 'effort'})
        self.assertEqual((sent['model'], sent['effort']), ('example-two', 'low'))

    def test_model_preferences_survive_restart_and_replay_does_not_revert_new_choice(self):
        self.models()
        first = self.model_command('model', 'example-two')
        self.model_command('model', 'example-one', request='model-request-5678')
        self.runtime = wr.WindowsRuntime(reader=self.reader, proxy_factory=self.reader.proxy)
        self.assertEqual(self.model_command('model', 'example-two'), first)
        self.assertEqual(self.model_command('models')['next']['model'], 'example-one')
        with self.assertRaises(ValueError):
            self.model_command('model', 'example-one')

    def test_slow_model_catalog_does_not_block_other_session_input(self):
        self.models()
        entered, release = threading.Event(), threading.Event()
        proxy = self.reader.proxy
        class SlowCatalog:
            def __init__(self, *args): self.client = proxy(*args)
            def __enter__(self): self.client.__enter__(); return self
            def __exit__(self, *args): return self.client.__exit__(*args)
            def call(self, method, params):
                if method == 'model/list':
                    entered.set()
                    if not release.wait(3): raise TimeoutError('synthetic catalog wait')
                return self.client.call(method, params)
        self.runtime.proxy_factory = SlowCatalog
        other = copy.deepcopy(self.reader)
        other.sid = other.row['id'] = other.thread['id'] = str(uuid.uuid4())
        other.loaded = [other.sid]
        runtime = wr.WindowsRuntime(reader=other, proxy_factory=other.proxy)
        results, errors = [], []
        def read():
            try: results.append(self.model_command('models'))
            except Exception as exc: errors.append(exc)
        thread = threading.Thread(target=read)
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            result = runtime.chat({'action': 'send', 'source': {**self.source, 'id': other.sid},
                'requestId': 'other-request-1234', 'input': [{'type': 'text', 'text': 'synthetic'}]})
            self.assertTrue(result['confirmed'])
            self.assertEqual(other.send_calls, 1)
        finally:
            release.set(); thread.join(3)
        self.assertFalse(errors)
        self.assertEqual(len(results), 1)

    def test_unknown_model_receipt_reads_settings_without_reapplying_or_reverting(self):
        self.models()
        for index, (command, value) in enumerate([('model', 'example-two'), ('effort', 'low')]):
            save = self.runtime.store.save
            def fail_final(name, data):
                if data.get('result', {}).get('status') == 'configured':
                    raise OSError('synthetic receipt storage failure')
                return save(name, data)
            request = 'recovery-000' + str(index)
            with patch.object(self.runtime.store, 'save', side_effect=fail_final):
                with self.assertRaises(OSError): self.model_command(command, value, request)
                result = self.model_command(command, value, request)
                self.assertEqual(result['status'], 'configured')
                self.assertEqual(result['next'][command], value)
            self.runtime = wr.WindowsRuntime(reader=self.reader, proxy_factory=self.reader.proxy)
            self.model_command('model', 'example-one', 'later-model-000' + str(index))
            if command == 'effort': self.model_command('effort', 'high', 'later-effort-0001')
            self.reader.calls.clear()
            result = self.model_command(command, value, request)
            self.assertEqual(result['status'], 'rejected')
            self.assertEqual(result['next']['model'], 'example-one')
            self.assertNotIn('model/list', [method for method, _ in self.reader.calls])
            self.assertEqual(self.reader.send_calls, 0)

    def test_unknown_model_receipt_with_failed_settings_write_requires_new_explicit_request(self):
        self.models()
        save = self.runtime.store.save
        def fail_settings(name, data):
            if name.startswith('model-settings-'): raise OSError('synthetic settings failure')
            return save(name, data)
        with patch.object(self.runtime.store, 'save', side_effect=fail_settings):
            with self.assertRaises(OSError): self.model_command('model', 'example-two')
        result = self.model_command('model', 'example-two')
        self.assertEqual(result['status'], 'rejected')
        self.assertEqual(result['next'], {})
        self.assertEqual(self.reader.send_calls, 0)
        self.assertEqual(self.model_command('model', 'example-two', 'fresh-control-0001')['status'], 'configured')

    def test_model_effort_validation_and_running_turn_preserves_its_configuration(self):
        self.models()
        for command, value in [('model', '../../auth.json'), ('effort', 'ultra'), ('models', 'extra')]:
            with self.assertRaises(ValueError):
                self.model_command(command, value)
        self.assertEqual(self.model_command('effort', 'low')['next'], {'model': 'example-one', 'effort': 'low'})
        self.active()
        self.model_command('model', 'example-two', request='model-request-5678')
        self.send()
        sent = next(params for method, params in self.reader.calls if method == 'turn/start')
        self.assertNotIn('model', sent)
        self.assertNotIn('effort', sent)

    def test_model_catalog_incomplete_or_unavailable_does_not_store_choice(self):
        self.models()
        for catalog in [TimeoutError('synthetic catalog timeout'), {'data': [], 'nextCursor': 'cycle'},
                        {'data': 'malformed'}, {'data': [], 'nextCursor': 5}]:
            self.reader.catalog = catalog
            with self.assertRaises((ValueError, TimeoutError)):
                self.model_command('model', 'example-one')
        self.assertFalse(list(self.runtime.store.root.glob('model-settings-*.json')))
        self.assertEqual(self.reader.send_calls, 0)

    def test_model_settings_are_bound_to_session_and_work_folder(self):
        self.models()
        self.model_command('model', 'example-two')
        original = dict(self.source)
        self.reader.sid = self.source['id'] = str(uuid.uuid4())
        self.reader.row['id'] = self.reader.thread['id'] = self.source['id']
        self.reader.loaded = [self.source['id']]
        self.assertEqual(self.model_command('models')['next'], {})
        self.source = original
        self.reader.sid = self.reader.row['id'] = self.reader.thread['id'] = self.source['id']
        self.reader.loaded = [self.source['id']]
        self.reader.row['cwd'] = self.reader.thread['cwd'] = self.source['cwd'] = str(self.home / 'other')
        with self.assertRaises(ValueError):
            self.send()
        self.assertEqual(self.reader.send_calls, 0)

    def test_malformed_model_settings_block_before_native_send(self):
        self.models()
        self.model_command('model', 'example-two')
        path = next(self.runtime.store.root.glob('model-settings-*.json'))
        value = json.loads(path.read_text())
        value['selected']['effort'] = 'invalid'
        path.write_text(json.dumps(value))
        with self.assertRaises(ValueError):
            self.send()
        self.assertEqual(self.reader.send_calls, 0)

    def test_model_endpoint_change_and_unknown_runtime_never_configure(self):
        self.models()
        original = self.runtime._verify_endpoint
        def changed(observed):
            self.reader.processes[12]['start'] = '134362358929099999'
            return original(observed)
        with patch.object(self.runtime, '_verify_endpoint', side_effect=changed):
            with self.assertRaises(ValueError):
                self.model_command('model', 'example-two')
        self.reader.thread['canAcceptDirectInput'] = False
        with self.assertRaises(ValueError):
            self.model_command('model', 'example-two')
        self.assertFalse(list(self.runtime.store.root.glob('model-settings-*.json')))

    def test_compact_is_idle_only_and_lost_reply_is_never_repeated(self):
        self.active()
        with self.assertRaises(ValueError):
            self.model_command('compact')
        self.reader.thread.update(status={'type': 'idle'}, turns=[])
        self.reader.compact_timeout = True
        first = self.model_command('compact')
        self.assertEqual(first['status'], 'unknown')
        self.assertEqual(self.model_command('compact'), first)
        self.assertEqual(sum(method == 'thread/compact/start' for method, _ in self.reader.calls), 1)
        self.assertEqual(self.reader.send_calls, 0)

    def execute(self, preview, action='terminate', request='request-1234', source=None):
        return self.runtime.execute({'source': source or self.source, 'action': action,
                                     'requestId': request, 'targetProof': preview['targetProof']})

    def claude(self, kind='interactive'):
        sid = str(uuid.uuid4())
        binary = self.home / '.local/bin/claude.exe'
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.touch()
        process = {'pid': 45, 'start': '134362358929022995', 'exe': str(binary)}
        self.reader.processes[45] = process
        path = self.reader.claude_home / 'sessions/45.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {'sessionId': sid, 'pid': 45, 'procStart': process['start'], 'pidDomain': 'win32:fixture-pc',
               'kind': kind, 'cwd': str(self.reader.cwd), 'status': 'idle'}
        path.write_text(json.dumps(row))
        source = {'host': 'fixture-pc', 'agent': 'claude', 'id': sid, 'sessionId': sid, 'home': '.claude',
                  'kind': 'background' if kind == 'bg' else 'interactive', 'cwd': str(self.reader.cwd)}
        return source, row, path

    def test_live_read_exposes_actual_capability_and_launch_support(self):
        value = self.runtime.chat({'action': 'read', 'source': self.source})
        self.assertTrue(value['capabilities']['canSend'])
        self.assertTrue(value['capabilities']['canLaunch'])
        self.assertFalse(value['readOnly'])
        self.assertFalse(self.runtime.store.root.exists())
        self.assertEqual(self.reader.send_calls, 0)

    def test_live_read_keeps_the_boards_window(self):
        self.assertIs(self.runtime.chat({'action': 'read', 'source': self.source})['olderOmitted'], False)
        self.reader.thread['turns'] = [{'id': 'turn-%d' % number, 'status': 'completed', 'items': [
            {'id': 'input-%d' % number, 'type': 'userMessage', 'content': [{'type': 'text', 'text': '합성 입력 %d' % number}]},
            {'id': 'command-%d' % number, 'type': 'commandExecution', 'command': 'dir'},
            {'id': 'answer-%d' % number, 'type': 'agentMessage', 'text': '합성 답변 %d' % number}]} for number in range(100)]
        value = self.runtime.chat({'action': 'read', 'source': self.source})
        self.assertEqual((len(value['messages']), value['messages'][0]['text'], value['olderOmitted']), (120, '합성 입력 60', True))

    def test_live_read_names_the_threads_model_and_effort(self):
        self.reader.thread.update(model='example-model', reasoningEffort='xhigh')
        value = self.runtime.chat({'action': 'read', 'source': self.source})
        # As a Mac read (native_chat.model_settings); a PC keeps no board choice for the next input.
        self.assertEqual(value['modelSettings'], {'model': 'example-model', 'effort': 'xhigh', 'next': {}})

    def test_claude_read_names_the_newest_responses_model_and_effort(self):
        reader = wc.WindowsCollector(self.home, hostname='fixture-pc', process_reader=lambda pid: None)
        sid = str(uuid.uuid4())
        folder = self.home / '.claude/projects/fixture'
        folder.mkdir(parents=True)
        (folder / (sid + '.jsonl')).write_text(''.join(json.dumps(entry) + '\n' for entry in [
            {'type': 'user', 'message': {'content': '합성 요청'}},
            {'type': 'assistant', 'effort': 'xhigh', 'message': {'model': 'claude-opus-5-5', 'content': [{'type': 'text', 'text': '합성 답변'}]}}]))
        value = wr.WindowsRuntime(reader=reader).chat(
            {'action': 'read', 'source': {'host': 'fixture-pc', 'agent': 'claude', 'id': sid, 'home': '.claude'}})
        self.assertEqual(value['modelSettings'], {'model': 'claude-opus-5-5', 'effort': 'xhigh', 'observed': 'last_response'})
        self.assertEqual(([m['text'] for m in value['messages']], value['olderOmitted']), (['합성 요청', '합성 답변'], False))

    def test_stored_or_unloaded_session_remains_readable_without_execution(self):
        self.reader.loaded = []
        value = self.runtime.chat({'action': 'read', 'source': self.source})
        self.assertTrue(value['readOnly'])
        self.assertFalse(value['capabilities']['canSend'])
        self.assertEqual(self.reader.send_calls, 0)

    def test_direct_input_must_be_native_confirmed(self):
        for value in (None, False):
            self.reader.thread['canAcceptDirectInput'] = value
            result = self.send()
            self.assertEqual(result['delivery'], 'rejected')
        self.assertEqual(self.reader.send_calls, 0)

    def test_remote_and_unknown_status_do_not_allow_input(self):
        for change in ({'source': 'remote'}, {'turns': [{'id': 'new', 'status': 'new-future-status'}]}):
            saved = copy.deepcopy(self.reader.thread)
            self.reader.thread.update(change)
            self.assertEqual(self.send()['delivery'], 'rejected')
            self.reader.thread = saved
        self.assertEqual(self.reader.send_calls, 0)

    def test_live_read_explains_unsupported_and_conflicting_status_without_allowing_input(self):
        for change, code in (({'turns': [{'id': 'future', 'status': 'new-status'}]}, 'turn_status_unsupported'),
                             ({'turns': [{'id': 'active', 'status': 'inProgress'}]}, 'status_inconsistent'),
                             ({'status': {'type': 'future-status'}}, 'status_unsupported')):
            before = copy.deepcopy(self.reader.thread)
            self.reader.thread.update(change)
            result = self.runtime.chat({'action': 'read', 'source': self.source})
            self.assertEqual((result['phase'], result['runtimeKnown'], result['runtimeReasonCode']), ('unknown', False, code))
            self.assertTrue(result['runtimeReason'])
            self.assertFalse(result['capabilities']['canSend'])
            self.reader.thread = before
        known = self.runtime.chat({'action': 'read', 'source': self.source})
        self.assertEqual((known['runtimeKnown'], known['runtimeReason'], known['runtimeEvidence']), (True, None, 'project_app_server'))
        self.assertEqual(self.reader.send_calls, 0)

    def test_send_exact_once_receipt_and_no_message_in_ledger(self):
        first = self.send(text='sensitive-fixture-body')
        replay = self.send(text='sensitive-fixture-body')
        self.assertTrue(first['confirmed'])
        self.assertTrue(first['readbackConfirmed'])
        self.assertEqual(replay, first)
        self.assertEqual(self.reader.send_calls, 1)
        saved = ''.join(path.read_text() for path in self.runtime.store.root.glob('*.json'))
        self.assertNotIn('sensitive-fixture-body', saved)
        mutation = next(params for method, params in self.reader.calls if method == 'turn/start')
        self.assertEqual(set(mutation), {'threadId', 'input', 'clientUserMessageId'})
        self.assertEqual(mutation['threadId'], self.source['id'])

    def test_same_request_different_input_is_rejected(self):
        self.send()
        with self.assertRaises(ValueError):
            self.send(text='다른 합성 입력')
        self.assertEqual(self.reader.send_calls, 1)

    def test_lost_send_response_is_durable_unknown_and_not_retried(self):
        self.reader.fail_send = True
        first = self.send()
        self.reader.fail_send = False
        second = self.send()
        self.assertEqual(first['delivery'], 'unknown')
        self.assertEqual(second, first)
        self.assertEqual(self.reader.send_calls, 1)

    def test_acknowledged_send_with_lost_readback_stays_accepted(self):
        self.reader.fail_after = True
        result = self.send()
        self.assertTrue(result['confirmed'])
        self.assertFalse(result['readbackConfirmed'])
        self.assertEqual(result['delivery'], 'accepted')

    def test_receipt_reads_native_client_message_identity(self):
        self.send()
        result = self.runtime.chat({'action': 'read', 'source': self.source, 'requestIds': ['request-1234']})
        self.assertEqual(result['receipts'][0]['turnId'], 'turn-sent')
        self.assertTrue(result['receipts'][0]['confirmed'])
        self.assertEqual(result['messages'][0]['clientId'], 'request-1234')

    def test_delivered_message_identity_survives_lost_receipt_file(self):
        self.send()
        for path in self.runtime.store.root.glob('chat-*.json'):
            path.unlink()
        before = self.reader.send_calls
        result = self.runtime.chat({'action': 'read', 'source': self.source, 'requestId': 'request-1234'})
        self.assertTrue(result['receipts'][0]['confirmed'])
        self.assertEqual(result['messages'][0]['clientId'], 'request-1234')
        self.assertEqual(self.reader.send_calls, before)

    def stored(self, name, data):
        return wa.store(str(self.reader.cwd), name, data)

    def send_files(self, ids, request='request-1234', text='합성 입력', source=None):
        return self.runtime.chat({'action': 'send', 'source': source or self.source, 'requestId': request,
                                  'input': [{'type': 'text', 'text': text}] if text else [], 'attachmentIds': ids})

    def test_32_files_are_sent_once_and_33_is_rejected_before_native_write(self):
        files = [self.stored(f'file-{n}.txt', b'fixture') for n in range(33)]
        result = self.send_files([file['id'] for file in files[:32]])
        self.assertEqual(len(result['attachments']), 32)
        mutation = next(params for method, params in self.reader.calls if method == 'turn/start')
        self.assertEqual(len(mutation['input']), 33)
        with self.assertRaises(ValueError):
            self.send_files([file['id'] for file in files], request='request-too-many')
        self.assertEqual(self.reader.send_calls, 1)

    def test_files_go_once_as_native_images_and_path_lines_with_their_names(self):
        image = self.stored('screen.png', b'\x89PNG\r\n\x1a\nfixture-image')
        memo = self.stored('memo.txt', b'fixture-memo')
        first = self.send_files([image['id'], memo['id']])
        self.assertTrue(first['confirmed'])
        mutation = next(params for method, params in self.reader.calls if method == 'turn/start')
        # As native_chat._send on a Mac: the image as a native input, any other file as its path.
        self.assertEqual(mutation['input'], [
            {'type': 'text', 'text': '합성 입력'}, {'type': 'localImage', 'path': image['path']},
            {'type': 'text', 'text': '첨부 파일: ' + json.dumps(memo['path'], ensure_ascii=False)}])
        self.assertEqual([(item['name'], item['kind'], item['type']) for item in first['attachments']],
                         [('screen.png', 'image', 'image/png'), ('memo.txt', 'file', 'text/plain')])
        self.assertTrue(all(set(item) == {'id', 'name', 'type', 'size', 'kind', 'source'} for item in first['attachments']))
        # The same request is answered from its record; other files under its ID are refused.
        self.assertEqual(self.send_files([image['id'], memo['id']]), first)
        with self.assertRaisesRegex(ValueError, '같은 요청 ID'):
            self.send_files([image['id']])
        self.assertEqual(self.reader.send_calls, 1)
        # A later read names the files with its receipt; the record keeps no contents, paths or words.
        read = self.runtime.chat({'action': 'read', 'source': self.source, 'requestIds': ['request-1234']})
        self.assertEqual(read['receipts'][0]['attachments'], first['attachments'])
        self.assertEqual(read['messages'][0]['attachments'][0]['kind'], 'image')
        saved = ''.join(path.read_text() for path in self.runtime.store.root.glob('*.json'))
        for value in ('fixture-image', 'fixture-memo', image['path'], memo['path'], '합성 입력'):
            self.assertNotIn(value, saved)

    def test_a_message_of_files_alone_goes_and_bad_attachment_lists_never_do(self):
        image = self.stored('screen.png', b'\x89PNG\r\n\x1a\nfixture')
        refused = ([image['id'], image['id']], ['%032x' % number for number in range(9)], ['../outside'],
                   [1], 'not-a-list', ['0' * 32], [])
        for ids in refused:
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                self.send_files(ids, text='')
        with self.assertRaises(ValueError):
            self.send_files([image['id']], source={key: value for key, value in self.source.items() if key != 'cwd'})
        self.assertEqual(self.reader.send_calls, 0)
        self.assertTrue(self.send_files([image['id']], text='')['confirmed'])
        mutation = next(params for method, params in self.reader.calls if method == 'turn/start')
        self.assertEqual(mutation['input'], [{'type': 'localImage', 'path': image['path']}])

    def test_images_are_offered_exactly_when_direct_input_is(self):
        capabilities = self.runtime.chat({'action': 'read', 'source': self.source})['capabilities']
        self.assertEqual((capabilities['canSend'], capabilities['canAttachImages']), (True, True))
        self.reader.thread['canAcceptDirectInput'] = False
        capabilities = self.runtime.chat({'action': 'read', 'source': self.source})['capabilities']
        self.assertEqual((capabilities['canSend'], capabilities['canAttachImages']), (False, False))

    def test_an_upload_lands_only_in_the_work_folder_the_pc_records_for_its_session(self):
        data = b'\x89PNG\r\n\x1a\nfixture'
        args = {'source': self.source, 'cwd': str(self.reader.cwd), 'name': 'shot.png',
                'sha256': hashlib.sha256(data).hexdigest()}
        chunks = [base64.b64encode(data).decode()]
        stored = self.runtime.attachment(args, chunks)
        self.assertEqual(Path(stored['path']).read_bytes(), data)
        self.assertEqual(wa.resolve(str(self.reader.cwd), stored['id']), stored)
        other = self.home / 'Projects/other'
        other.mkdir()
        with self.assertRaises(ValueError):
            self.runtime.attachment({**args, 'cwd': str(other)}, chunks)
        self.assertEqual(list(other.iterdir()), [])
        # A Claude session's folder is the one its list row names.
        sid = str(uuid.uuid4())
        claude = {'host': 'fixture-pc', 'agent': 'claude', 'id': sid, 'sessionId': sid, 'home': '.claude'}
        with self.assertRaises(ValueError):
            self.runtime.attachment({**args, 'source': claude}, chunks)
        self.reader.claude_list = [{'id': sid, 'cwd': str(self.reader.cwd)}]
        self.assertTrue(Path(self.runtime.attachment({**args, 'source': claude}, chunks)['path']).is_file())
        self.reader.row['archived'] = 1
        with self.assertRaises(ValueError):
            self.runtime.attachment(args, chunks)
        # Only an upload carries a file, and an upload always does.
        with self.assertRaises(ValueError):
            wr.handle({'op': 'chat', 'args': {'source': self.source, 'action': 'read'}}, chunks)
        with self.assertRaises(ValueError):
            wr.handle({'op': 'attachment', 'args': args})

    def test_duplicate_endpoint_refuses_mutation(self):
        self.reader.paths.append(self.reader.path)
        with self.assertRaises(ValueError):
            self.send()
        self.assertEqual(self.reader.send_calls, 0)

    def test_cwd_mismatch_refuses_mutation(self):
        self.reader.thread['cwd'] += '-different'
        with self.assertRaises(ValueError):
            self.send()
        self.assertEqual(self.reader.send_calls, 0)

    def test_new_turn_between_preview_and_execute_is_stale(self):
        self.active()
        preview = self.runtime.preview(self.source)
        self.reader.thread['turns'][0]['id'] = 'turn-new'
        result = self.execute(preview, 'interrupt')
        self.assertEqual(result['status'], 'stale')
        self.assertFalse(any(method == 'turn/interrupt' for method, params in self.reader.calls))

    def test_codex_interrupt_exact_turn_and_readback(self):
        self.active()
        preview = self.runtime.preview(self.source)
        first = self.execute(preview, 'interrupt')
        second = self.execute(preview, 'interrupt')
        self.assertTrue(first['confirmed'])
        self.assertEqual(first['status'], 'interrupted')
        self.assertTrue(second['replayed'])
        calls = [params for method, params in self.reader.calls if method == 'turn/interrupt']
        self.assertEqual(calls, [{'threadId': self.source['id'], 'turnId': 'turn-original'}])

    def test_archive_explicit_descendant_scope_and_exact_readback(self):
        preview = self.runtime.preview(self.source)
        cap = preview['capabilities']['terminate']
        self.assertEqual(cap['method'], 'codex-thread-archive')
        self.assertEqual(cap['scope'], 'session-and-descendants')
        self.assertIn('하위 에이전트', cap['effect'])
        result = self.execute(preview)
        self.assertEqual(result['status'], 'terminated')
        self.assertTrue(result['confirmed'])
        calls = [(method, params) for method, params in self.reader.calls if method in ('thread/archive', 'thread/unsubscribe')]
        self.assertEqual(calls, [('thread/archive', {'threadId': self.source['id']})])
        self.assertFalse(self.kills)

    def test_archive_without_db_readback_is_unconfirmed(self):
        self.reader.mutate_hook = lambda method: self.reader.row.update(archived=0)
        result = self.execute(self.runtime.preview(self.source))
        self.assertEqual(result['status'], 'unconfirmed')
        self.assertFalse(result['confirmed'])
        self.assertTrue(result['applied'])

    def test_delayed_archive_receipt_recovers_without_second_mutation(self):
        self.reader.mutate_hook = lambda method: self.reader.row.update(archived=0)
        first = self.execute(self.runtime.preview(self.source))
        self.assertFalse(first['confirmed'])
        self.reader.row['archived'] = 1
        with patch.object(self.runtime, '_observe') as observe, patch.object(self.runtime, '_apply') as apply:
            result = self.runtime.execute({'source': self.source, 'action': 'terminate', 'requestId': first['requestId'],
                                           'receiptOnly': True, 'verifyCleanup': True})
        self.assertTrue(result['confirmed']); self.assertTrue(result['cleanupEligible'])
        self.assertFalse(result['descendantsConfirmed'])
        observe.assert_not_called(); apply.assert_not_called()
        self.assertEqual(sum(method == 'thread/archive' for method, _ in self.reader.calls), 1)

    def test_later_completed_turn_also_prevents_old_receipt_cleanup(self):
        first = self.execute(self.runtime.preview(self.source))
        self.reader.thread['turns'] = [{'id': 'new-completed-turn', 'status': 'completed'}]
        result = self.runtime.execute({'source': self.source, 'action': 'terminate', 'requestId': first['requestId'],
                                       'receiptOnly': True, 'verifyCleanup': True})
        self.assertEqual(result['reconcileCode'], 'execution_changed')
        self.assertFalse(result['cleanupEligible'])

    def test_receipt_missing_tombstone_prevents_late_original_dispatch(self):
        preview = self.runtime.preview(self.source)
        with patch.object(self.runtime, '_apply') as apply:
            result = self.runtime.execute({'source': self.source, 'action': 'terminate', 'requestId': 'request-1234',
                                           'receiptOnly': True, 'sealMissing': True})
            late = self.execute(preview)
        self.assertEqual(result['dispatchState'], 'not_started')
        self.assertEqual(late['dispatchState'], 'not_started')
        apply.assert_not_called()

    def test_confirmed_receipt_cannot_clean_new_endpoint_or_reloaded_thread(self):
        first = self.execute(self.runtime.preview(self.source))
        args = {'source': self.source, 'action': 'terminate', 'requestId': first['requestId'], 'receiptOnly': True, 'verifyCleanup': True}
        self.reader.loaded = [self.source['id']]
        result = self.runtime.execute(args)
        self.assertTrue(result['confirmed'], 'retain historical execution success')
        self.assertFalse(result['cleanupEligible'])
        self.assertEqual(result['reconcileCode'], 'execution_changed')
        self.reader.processes[12]['start'] += '-new'
        result = self.runtime.execute(args)
        self.assertFalse(result['cleanupEligible'])
        self.assertEqual(result['reconcileCode'], 'execution_changed')

    def test_cleanup_receipt_only_never_archives_or_observes_again(self):
        first = self.execute(self.runtime.preview(self.source))
        args = {'source': self.source, 'action': 'terminate', 'requestId': first['requestId'], 'receiptOnly': True}
        with patch.object(self.runtime, '_observe') as observe, patch.object(self.runtime, '_apply') as apply:
            result = self.runtime.execute(args)
            self.assertTrue(result['replayed'])
            self.assertTrue(result['confirmed'])
            self.assertIsInstance(result['requestedAt'], int)
            for change in ({'requestId': 'unknown-request'}, {'source': {**self.source, 'cwd': 'C:/other'}}):
                with self.assertRaises(ValueError):
                    self.runtime.execute({**args, **change})
            observe.assert_not_called()
            apply.assert_not_called()

    def test_archive_without_unloaded_readback_is_unconfirmed(self):
        self.reader.mutate_hook = lambda method: self.reader.loaded.append(self.source['id'])
        result = self.execute(self.runtime.preview(self.source))
        self.assertEqual(result['status'], 'unconfirmed')

    def test_server_restart_invalidates_preview(self):
        preview = self.runtime.preview(self.source)
        self.reader.processes[12]['start'] = '134362358929099999'
        result = self.execute(preview)
        self.assertEqual(result['status'], 'stale')
        self.assertFalse(any(method == 'thread/archive' for method, params in self.reader.calls))

    def test_proof_tampering_expiry_wrong_source_rejected(self):
        preview = self.runtime.preview(self.source)
        original = preview['targetProof']
        preview['targetProof'] = original[:-1] + ('a' if original[-1] != 'a' else 'b')
        with self.assertRaises(ValueError):
            self.execute(preview)
        preview['targetProof'] = original
        with patch.object(wr.time, 'time', return_value=time.time() + 60):
            with self.assertRaises(ValueError):
                self.execute(preview)
        with self.assertRaises(ValueError):
            self.execute(preview, source={**self.source, 'host': 'other-host'})

    def test_used_proof_does_not_repeat_with_new_request_id(self):
        preview = self.runtime.preview(self.source)
        first = self.execute(preview)
        second = self.execute(preview, request='other-request')
        self.assertTrue(first['confirmed'])
        self.assertTrue(second['replayed'])
        self.assertEqual(sum(method == 'thread/archive' for method, params in self.reader.calls), 1)

    def test_codex_terminal_plan_preserves_account_and_existing_endpoint(self):
        plan = self.runtime.terminal_plan(self.source)
        self.assertEqual(plan['argv'], [str(self.reader.binary), 'resume', self.source['id'], '--remote',
                                       'unix://' + self.reader.path.as_posix(), '--cd', str(self.reader.cwd)])
        self.assertEqual(plan['env'], {'CODEX_HOME': str(self.reader.codex_home)})
        self.assertEqual(plan['nativeSource']['id'], self.source['id'])
        self.assertEqual(self.reader.send_calls, 0)

    def test_terminal_attach_preserves_readonly_and_workspace_permissions(self):
        policies = [
            {'type': 'read-only', 'network_access': False},
            {'type': 'workspace-write', 'network_access': True,
             'writable_roots': [str(self.home / 'extra folder')],
             'exclude_slash_tmp': True, 'exclude_tmpdir_env_var': True},
        ]
        for policy in policies:
            with self.subTest(policy=policy):
                self.reader.loaded = []
                self.reader.row.update(sandbox_policy=json.dumps(policy), approval_mode='on-request')
                # Neither these browser fields nor default launch configuration are authority.
                plan = self.runtime.terminal_plan({**self.source, 'sandbox_policy': '{"type":"disabled"}',
                                                   'approval_mode': 'never'}, prepare=True)
                self.assertNotIn('--sandbox', plan['argv'])
                params = [p for method, p in self.reader.calls if method == 'thread/resume'][-1]
                self.assertEqual(params['sandbox'], policy['type'])
                self.assertEqual(params['approvalPolicy'], 'on-request')
                if policy['type'] == 'workspace-write':
                    config = params['config']
                    prefix = 'sandbox_workspace_write.'
                    self.assertEqual(config[prefix + 'network_access'], policy['network_access'])
                    self.assertEqual(config[prefix + 'writable_roots'], policy['writable_roots'])
                    self.assertIs(config[prefix + 'exclude_slash_tmp'], True)
                else:
                    self.assertNotIn('config', params)
        self.assertEqual(self.reader.send_calls, 0)

    def test_native_managed_workspace_policy_is_preserved_without_expanding_paths(self):
        entries = [{'path': {'type': 'special', 'value': {'kind': 'root'}}, 'access': 'read'},
                   {'path': {'type': 'path', 'path': str(self.reader.cwd)}, 'access': 'write'}]
        entries += [{'path': {'type': 'special', 'value': {'kind': kind}}, 'access': 'write'}
                    for kind in ('slash_tmp', 'tmpdir')]
        entries += [{'path': {'type': 'path', 'path': str(self.reader.cwd / name)},
                     'access': 'read', 'missing_path_behavior': 'skip'}
                    for name in ('.git', '.agents', '.codex', '.aws')]
        policy = {'type': 'managed', 'file_system': {'type': 'restricted', 'entries': entries},
                  'network': 'restricted'}
        self.reader.row['sandbox_policy'] = json.dumps(policy)
        self.reader.loaded = []
        self.runtime.terminal_plan(self.source, prepare=True)
        params = [p for method, p in self.reader.calls if method == 'thread/resume'][-1]
        self.assertEqual(params['sandbox'], 'workspace-write')
        self.assertIs(params['config']['sandbox_workspace_write.network_access'], False)
        self.assertEqual(params['config']['sandbox_workspace_write.writable_roots'], [])
        # A custom deny or additional rule must never be flattened into a broader preset.
        for changed in [entries + [{'path': {'type': 'path', 'path': str(self.home / 'private')}, 'access': 'deny'}],
                        entries[:-1], entries + [entries[0]]]:
            policy['file_system']['entries'] = changed
            self.reader.row['sandbox_policy'] = json.dumps(policy)
            self.reader.loaded = []
            with self.assertRaisesRegex(ValueError, '기존 실행 권한'):
                self.runtime.terminal_plan(self.source)

    def test_unknown_or_missing_native_permissions_never_fall_back_to_defaults(self):
        for policy, approval in [(None, 'never'), ('not-json', 'never'), ('[]', 'never'),
                                 ('{"type":"future-policy"}', 'never'),
                                 ('{"type":"disabled","deny":"extra"}', 'never'),
                                 ('{"type":"disabled"}', None), ('{"type":"disabled"}', 'unsupported'),
                                 ('{"type":"read-only","network_access":true}', 'never'),
                                 ('{"type":"read-only","network_access":"false"}', 'never')]:
            self.reader.row.update(sandbox_policy=policy, approval_mode=approval)
            self.reader.loaded = []
            with self.subTest(policy=policy, approval=approval), self.assertRaisesRegex(ValueError, '기존 실행 권한'):
                self.runtime.terminal_plan(self.source)
            self.assertFalse(self.runtime.resume_preview(self.source)['allowed'])
        self.assertFalse(any(method == 'thread/resume' for method, params in self.reader.calls))
        self.assertEqual(self.reader.send_calls, 0)

    def test_terminal_previews_never_load_or_change_the_stored_thread(self):
        self.reader.loaded = []
        self.runtime.terminal_plan(self.source, prepare=False)
        self.runtime.resume_preview(self.source)
        self.assertEqual(self.reader.loaded, [])
        self.assertFalse(any(method == 'thread/resume' for method, params in self.reader.calls))

    def test_resume_permissions_require_an_exact_native_readback(self):
        settings = {'sandbox': 'danger-full-access', 'approvalPolicy': 'never'}
        for result in [{'approvalPolicy': 'on-request', 'sandbox': {'type': 'dangerFullAccess'}},
                       {'approvalPolicy': 'never', 'sandbox': {'type': 'workspaceWrite'}},
                       {'approvalPolicy': 'never'}, None]:
            self.assertFalse(wr._same_resume_permissions(result, settings))
        self.assertTrue(wr._same_resume_permissions({'approvalPolicy': 'never',
                                                    'sandbox': {'type': 'dangerFullAccess'}}, settings))

    def test_failed_resume_persists_and_blocks_later_attach_preview_and_send(self):
        for failure in ('sandbox', 'approval', 'id', 'cwd', 'timeout', 'error'):
            with self.subTest(failure=failure):
                self.reader.loaded = []
                self.reader.calls = []
                def fail(result):
                    if failure == 'timeout':
                        raise TimeoutError('synthetic lost response after load')
                    if failure == 'error':
                        raise OSError('synthetic disconnected proxy after load')
                    if failure == 'sandbox':
                        result['sandbox'] = {'type': 'readOnly', 'networkAccess': False}
                    elif failure == 'approval':
                        result['approvalPolicy'] = 'on-request'
                    elif failure == 'id':
                        result['thread']['id'] = str(uuid.uuid4())
                    elif failure == 'cwd':
                        result['thread']['cwd'] = str(self.home / 'other')
                    return result
                self.reader.resume_hook = fail
                with self.assertRaises((ValueError, TimeoutError, OSError)):
                    self.runtime.terminal_plan(self.source, prepare=True)
                self.assertEqual(self.reader.loaded, [self.reader.sid])
                # Every SSH RPC constructs a new runtime, possibly with a host alias.
                self.runtime = wr.WindowsRuntime(reader=self.reader, proxy_factory=self.reader.proxy)
                alias = {**self.source, 'host': 'another-alias'}
                for prepare in (False, True):
                    with self.assertRaisesRegex(ValueError, '재개 권한 확인'):
                        self.runtime.terminal_plan(alias, prepare=prepare)
                self.assertFalse(self.runtime.resume_preview(alias)['allowed'])
                caps = self.runtime.chat({'action': 'capabilities', 'source': alias})['capabilities']
                self.assertFalse(caps['canSend'])
                self.assertFalse(caps['canLaunch'])
                with self.assertRaisesRegex(ValueError, '재개 권한 확인'):
                    self.send()
                self.assertEqual(self.reader.send_calls, 0)
                self.assertEqual(sum(method == 'thread/resume' for method, params in self.reader.calls), 1)

    def failed_resume(self):
        self.reader.loaded = []
        self.reader.resume_hook = lambda result: None
        with self.assertRaises(ValueError):
            self.runtime.terminal_plan(self.source, prepare=True)
        self.reader.resume_hook = None

    def test_failed_resume_recovers_only_after_unload_or_verified_process_change(self):
        self.failed_resume()
        # The loaded path cannot evade the marker if another client wins the cold-load race.
        with patch.object(self.runtime, '_codex', side_effect=wr.NotLoaded()):
            with self.assertRaisesRegex(ValueError, '재개 권한 확인'):
                self.runtime.terminal_plan(self.source, prepare=True)
        self.reader.loaded = []
        self.runtime.terminal_plan(self.source, prepare=True)
        self.assertIsNone(self.runtime.store.read(self.runtime._resume_record_name(wr._identity(self.source))))
        self.assertTrue(self.runtime.resume_preview(self.source)['allowed'])
        self.failed_resume()
        name = self.runtime._resume_record_name(wr._identity(self.source))
        before = self.runtime.store.read(name)
        self.reader.processes[12]['start'] = '134362358929099999'
        self.assertTrue(self.runtime.resume_preview(self.source)['allowed'])
        self.assertEqual(self.runtime.store.read(name), before)  # previews never clear records
        self.assertTrue(self.runtime.terminal_plan(self.source, prepare=True))
        self.assertTrue(self.send()['confirmed'])

    def test_resume_marker_write_failure_stops_before_native_mutation(self):
        self.reader.loaded = []
        with patch.object(self.runtime.store, 'save', side_effect=OSError('synthetic disk failure')):
            with self.assertRaises(OSError):
                self.runtime.terminal_plan(self.source, prepare=True)
        self.assertEqual(self.reader.loaded, [])
        self.assertFalse(any(method == 'thread/resume' for method, params in self.reader.calls))

    def test_corrupt_resume_record_blocks_warm_attach_and_send(self):
        self.runtime.store.prepare()
        name = self.runtime._resume_record_name(wr._identity(self.source))
        for record in (None, {}, {'version': 999}, {'server': None}):
            self.runtime.store.save(name, record)
            with self.assertRaisesRegex(ValueError, '재개 권한 확인'):
                self.runtime.terminal_plan(self.source, prepare=True)
            with self.assertRaisesRegex(ValueError, '재개 권한 확인'):
                self.send()
        self.assertEqual(self.reader.send_calls, 0)

    def test_successful_resume_clears_marker_only_after_verifying_server_identity(self):
        self.reader.loaded = []
        def restart(result):
            self.reader.processes[12]['start'] = '134362358929099999'
            return result
        self.reader.resume_hook = restart
        with self.assertRaisesRegex(ValueError, '서버가 변경'):
            self.runtime.terminal_plan(self.source, prepare=True)
        self.assertIsNotNone(self.runtime.store.read(self.runtime._resume_record_name(wr._identity(self.source))))

    def test_resume_shares_observation_deadline_and_refuses_a_late_mutation(self):
        clock, deadlines = [100.0], []
        def cold(source, deadline):
            deadlines.append(deadline)
            clock[0] = 111.0
            raise wr.NotLoaded()
        def proxy(binary, path, deadline):
            deadlines.append(deadline)
            return self.reader.proxy(binary, path, deadline)
        self.reader.loaded = []
        self.runtime.proxy_factory = proxy
        with patch.object(wr.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(self.runtime, '_codex', side_effect=cold):
            with self.assertRaisesRegex(TimeoutError, '시간이 부족'):
                self.runtime.terminal_plan(self.source, prepare=True)
        self.assertEqual(deadlines, [109.0, 115.0])
        self.assertFalse(any(method == 'thread/resume' for method, params in self.reader.calls))

    def test_slow_marker_write_leaves_no_pending_state_if_nothing_was_sent(self):
        clock = [100.0]
        self.reader.loaded = []
        save = self.runtime.store.save
        def slow(name, value):
            save(name, value)
            clock[0] = 110.0
        with patch.object(wr.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(self.runtime.store, 'save', side_effect=slow):
            with self.assertRaisesRegex(TimeoutError, '시간이 부족'):
                self.runtime.terminal_plan(self.source, prepare=True)
        self.assertIsNone(self.runtime.store.read(self.runtime._resume_record_name(wr._identity(self.source))))
        self.assertEqual(self.reader.loaded, [])
        self.assertFalse(any(method == 'thread/resume' for method, params in self.reader.calls))
        # A later PC-side load must not be blocked by an unsent request.
        self.reader.loaded = [self.reader.sid]
        self.assertTrue(self.runtime.resume_preview(self.source)['allowed'])

    def test_database_change_before_resume_refuses_without_loading(self):
        self.reader.loaded = []
        original = self.reader.database_rows
        calls = []
        def changed(sid):
            calls.append(sid)
            if len(calls) == 3:  # discovery, initial policy, final recheck
                self.reader.row['approval_mode'] = 'on-request'
            return original(sid)
        with patch.object(self.reader, 'database_rows', side_effect=changed):
            with self.assertRaisesRegex(ValueError, '실행 권한이 변경'):
                self.runtime.terminal_plan(self.source, prepare=True)
        self.assertEqual(self.reader.loaded, [])

    def test_resume_and_send_use_the_same_nonblocking_operation_lock(self):
        self.reader.loaded = []
        with self.runtime.store.lock():
            with self.assertRaisesRegex(ValueError, '다른 작업을 처리'):
                self.runtime.terminal_plan(self.source, prepare=True)
            self.reader.loaded = [self.reader.sid]
            with self.assertRaisesRegex(ValueError, '다른 작업을 처리'):
                self.send()
            self.reader.loaded = []
        self.assertEqual(self.reader.loaded, [])
        self.assertEqual(self.reader.send_calls, 0)

    def test_claude_external_interactive_process_can_terminate(self):
        source, row, path = self.claude()
        preview = self.runtime.preview(source)
        self.assertTrue(preview['capabilities']['terminate']['supported'])
        result = self.execute(preview, source=source)
        self.assertTrue(result['confirmed'])
        self.assertEqual([item['pid'] for item in self.kills], [45])

    def test_claude_shell_is_foreground_idle_and_other_registry_states_keep_their_meaning(self):
        source, row, path = self.claude()
        for status, phase in (('shell', 'idle'), ('busy', 'working'), ('waiting', 'needs_input'), ('future-state', 'unknown')):
            path.write_text(json.dumps({**row, 'status': status}))
            self.assertEqual(self.runtime.preview(source)['phase'], phase)
        self.assertEqual(self.reader.send_calls, 0)
        self.assertEqual(self.kills, [])

    def test_claude_pid_reuse_is_stale(self):
        source, row, path = self.claude()
        preview = self.runtime.preview(source)
        self.reader.processes[45]['start'] = '134362358929099999'
        result = self.execute(preview, source=source)
        self.assertEqual(result['status'], 'stale')
        self.assertFalse(self.kills)

    def test_claude_domain_executable_cwd_or_duplicate_owner_refused(self):
        source, row, path = self.claude()
        for field, value in [('pidDomain', 'win32:other-machine'), ('procStart', '134362358929099999'), ('cwd', str(self.home / 'other'))]:
            path.write_text(json.dumps({**row, field: value}))
            self.assertFalse(self.runtime.preview(source)['capabilities']['terminate']['supported'])
        path.write_text(json.dumps(row))
        self.reader.processes[45]['exe'] = str(self.home / 'unrelated.exe')
        self.assertFalse(self.runtime.preview(source)['capabilities']['terminate']['supported'])
        self.assertFalse(self.kills)

    def test_claude_background_without_native_attach_id_and_interactive_refused(self):
        source, row, path = self.claude('bg')
        with self.assertRaisesRegex(ValueError, '연결 식별자'):
            self.runtime.terminal_plan(source)
        path.write_text(json.dumps({**row, 'kind': 'interactive'}))
        with self.assertRaises(ValueError):
            self.runtime.terminal_plan(source)

    def test_a_running_claude_background_session_attaches_by_its_job_id(self):
        source, row, path = self.claude('bg')
        job = source['id'][:8]
        path.write_text(json.dumps({**row, 'jobId': job}))
        plan = self.runtime.terminal_plan(source)
        self.assertEqual(plan['argv'], [str(self.home / '.local/bin/claude.exe'), 'attach', job])
        self.assertEqual((plan['cwd'], plan['env'], plan['connectionMode']), (str(self.reader.cwd), {}, 'attach'))
        self.assertEqual((plan['nativeSource']['id'], plan['nativeSource']['kind']), (source['id'], 'background'))
        self.assertNotIn('resumed', plan)
        # Not a job id `claude attach` takes: no attach to some other run.
        for value in (source['id'], 'ABCD1234', '1234567', '12345678 ', 'a1b2c3d4e', 12345678):
            path.write_text(json.dumps({**row, 'jobId': value}))
            with self.assertRaisesRegex(ValueError, '연결 식별자'):
                self.runtime.terminal_plan(source)
        # Mid-update the run keeps its renamed old image while no claude.exe exists yet.
        path.write_text(json.dumps({**row, 'jobId': job}))
        old = self.home / '.local/bin/claude.exe.old.1792000000000.45'
        old.touch()
        self.reader.processes[45]['exe'] = str(old)
        (self.home / '.local/bin/claude.exe').unlink()
        with patch.object(self.reader, '_claude_executable', lambda exe: Path(exe).is_file()), \
                self.assertRaisesRegex(ValueError, '실행 파일'):
            self.runtime.terminal_plan(source)
        self.reader.processes[45]['exe'] = str(self.home / '.local/bin/claude.exe')
        # A record another process now owns is no live run: its old job id is never attached.
        (self.home / '.local/bin/claude.exe').touch()
        path.write_text(json.dumps({**row, 'jobId': job, 'procStart': '134362358929099999'}))
        with self.assertRaises(ValueError):
            self.runtime.terminal_plan(source)

    def test_native_vendor_remote_nested_sources_are_rejected(self):
        for value in ('vendorRemote', {'vendorRemote': {}}, {'wrapper': [{'vendorRemote': 'x'}]}):
            self.reader.thread['threadSource'] = value
            self.assertEqual(self.send()['delivery'], 'rejected')
        self.assertEqual(self.reader.send_calls, 0)

    def test_single_request_id_is_included_in_read_receipts(self):
        self.send()
        result = self.runtime.chat({'action': 'read', 'source': self.source, 'requestId': 'request-1234'})
        self.assertEqual(len(result['receipts']), 1)
        self.assertTrue(result['receipts'][0]['confirmed'])

    def test_instance_state_directories_are_isolated(self):
        with patch.object(wr, 'STORE_RELATIVE', '.local/state/dev-fixture/windows'):
            other = wr.WindowsRuntime(reader=self.reader, proxy_factory=self.reader.proxy)
        self.assertNotEqual(other.store.root, self.runtime.store.root)
        self.assertEqual(other.store.root, self.home / '.local/state/dev-fixture/windows')
        with patch.object(wr, 'STORE_RELATIVE', '../escaped'):
            with self.assertRaises(ValueError):
                wr.Store(self.home)

    def test_resume_preview_distinguishes_loaded_from_stored(self):
        self.assertTrue(self.runtime.resume_preview(self.source)['allowed'])
        self.reader.loaded = []
        # Stored, its project's server running: it resumes into that server.
        self.assertTrue(self.runtime.resume_preview(self.source)['allowed'])
        self.reader.paths = []
        result = self.runtime.resume_preview(self.source)
        self.assertFalse(result['allowed'])
        self.assertFalse(result['blockedByBoard'])
        self.assertIn('서버가 PC에서 꺼져', result['reason'])

    def test_a_stored_codex_session_resumes_into_its_projects_running_server_only(self):
        self.reader.loaded = []
        plan = self.runtime.terminal_plan(self.source, prepare=True)
        self.assertEqual(plan['argv'], [str(self.reader.binary), 'resume', self.reader.sid, '--remote',
                                        'unix://' + self.reader.path.as_posix(), '--cd', str(self.reader.cwd)])
        resumes = [params for method, params in self.reader.calls if method == 'thread/resume']
        self.assertEqual(len(resumes), 1)
        self.assertEqual(resumes[0]['sandbox'], 'danger-full-access')
        self.assertEqual(resumes[0]['approvalPolicy'], 'never')
        self.assertEqual((plan['resumed'], plan['connectionMode']), (True, 'attach'))
        self.assertEqual(plan['env'], {'CODEX_HOME': str(self.reader.codex_home)})
        self.reader.loaded = []  # Each topology below starts with a stored thread again.
        # The folder's own server, in any case; while it exists, never another one.
        other = self.reader.codex_home / 'agents-servers/fixture-us/app.sock'
        other.parent.mkdir(parents=True)
        other.touch()
        self.reader.paths = [other]
        with self.assertRaisesRegex(ValueError, '꺼져'):
            self.runtime.terminal_plan(self.source)
        self.reader.path.parent.rename(self.reader.path.parent.with_name('FIXTURE'))
        upper = self.reader.codex_home / 'agents-servers/FIXTURE/app.sock'
        self.reader.paths = [upper, other]
        self.assertIn('unix://' + upper.as_posix(), self.runtime.terminal_plan(self.source)['argv'])
        self.reader.loaded = []
        # With none of its name, the only <folder>-… server is the folder's (a project named apart)...
        upper.parent.rename(upper.parent.with_name('retired'))
        self.reader.paths = [other]
        self.assertIn('unix://' + other.as_posix(), self.runtime.terminal_plan(self.source)['argv'])
        self.reader.loaded = []
        # ...unless a project folder of that name owns it, or another such server exists.
        (self.home / 'Projects/fixture-us').mkdir()
        with self.assertRaisesRegex(ValueError, '꺼져'):
            self.runtime.terminal_plan(self.source)
        (self.home / 'Projects/fixture-us').rmdir()
        twin = self.reader.codex_home / 'agents-servers/fixture-eu'
        twin.mkdir()
        with self.assertRaisesRegex(ValueError, '꺼져'):
            self.runtime.terminal_plan(self.source)
        twin.rmdir()
        # Outside ~/Projects no server is guessed from the folder name.
        outside = self.home / 'Elsewhere/fixture'
        outside.mkdir(parents=True)
        self.reader.row['cwd'] = str(outside)
        with self.assertRaisesRegex(ValueError, '꺼져'):
            self.runtime.terminal_plan({**self.source, 'cwd': str(outside)})
        self.reader.row['cwd'] = str(self.reader.cwd)
        # Never an embedded runtime in the elevated SSH session.
        self.reader.paths = []
        with self.assertRaisesRegex(ValueError, '꺼져'):
            self.runtime.terminal_plan(self.source)
        # A thread another Codex process writes (a codex exec) is never resumed alongside it.
        self.reader.paths = [self.reader.path]
        self.reader.held = {self.reader.sid}
        with self.assertRaisesRegex(ValueError, '다른 Codex 실행'):
            self.runtime.terminal_plan(self.source)
        self.reader.held = set()
        self.reader.row['archived'] = 1
        with self.assertRaises(ValueError):
            self.runtime.terminal_plan(self.source)

    def test_an_unreadable_claude_record_of_a_live_claude_process_blocks_a_resume(self):
        source, row, registry = self.claude()
        transcript = self.reader.claude_home / 'projects/fixture' / (source['id'] + '.jsonl')
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text(json.dumps({'type': 'user', 'cwd': str(self.reader.cwd), 'sessionId': source['id']}) + '\n')
        registry.write_text('{')  # being rewritten, say
        with self.assertRaisesRegex(ValueError, '실행 기록을 읽지 못했습니다'):
            self.runtime.terminal_plan(source)
        self.reader.processes.pop(45)  # that process is gone: the record cannot be this run
        self.assertTrue(self.runtime.terminal_plan(source)['resumed'])

    def test_a_background_run_whose_process_ended_resumes_like_any_stored_conversation(self):
        source, row, registry = self.claude(kind='bg')
        self.assertEqual(source['kind'], 'background')
        transcript = self.reader.claude_home / 'projects/fixture' / (source['id'] + '.jsonl')
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text(json.dumps({'type': 'user', 'cwd': str(self.reader.cwd), 'sessionId': source['id']}) + '\n')
        self.reader.processes.pop(45)  # its registry record stays
        self.assertEqual(self.runtime.terminal_plan(source)['argv'][1:],
                         ['--resume', source['id'], '--permission-mode', 'bypassPermissions'])

    def test_a_stored_claude_conversation_no_pc_process_runs_resumes_in_a_board_terminal(self):
        source, row, registry = self.claude()
        transcript = self.reader.claude_home / 'projects/fixture' / (source['id'] + '.jsonl')
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text(json.dumps({'type': 'user', 'cwd': str(self.reader.cwd), 'sessionId': source['id']}) + '\n')
        with self.assertRaisesRegex(ValueError, '실행 중'):
            self.runtime.terminal_plan(source)
        self.assertFalse(self.runtime.resume_preview(source)['allowed'])
        # The process ended (its record may stay): the conversation continues here.
        self.reader.processes.pop(45)
        plan = self.runtime.terminal_plan(source)
        # Resumed in the mode the PC launcher starts Claude in, not the settings default (auto).
        self.assertEqual(plan['argv'], [str(self.home / '.local/bin/claude.exe'), '--resume', source['id'],
                                        '--permission-mode', 'bypassPermissions'])
        self.assertEqual((plan['cwd'], plan['env'], plan['resumed']), (str(self.reader.cwd), {}, True))
        self.assertEqual(plan['nativeSource']['kind'], 'interactive')
        self.assertTrue(self.runtime.resume_preview(source)['allowed'])
        registry.unlink()
        transcript.unlink()
        with self.assertRaisesRegex(ValueError, '기록'):
            self.runtime.terminal_plan(source)

    def board_resume_left_running(self, legacy=False):
        """A Claude run the board's SSH terminal started (`claude --resume`) whose terminal is gone."""
        import transfer
        source, row, registry = self.claude()
        transcript = self.reader.claude_home / 'projects/fixture' / (source['id'] + '.jsonl')
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text(json.dumps({'type': 'user', 'cwd': str(self.reader.cwd), 'sessionId': source['id']}) + '\n')
        self.reader.processes.pop(45)
        spec = self.runtime.terminal_plan(source)
        self.reader.processes[45] = {'pid': 45, 'start': row['procStart'], 'exe': str(self.home / '.local/bin/claude.exe')}
        if legacy:  # launched before the board kept the launcher's permission mode
            spec = {**spec, 'argv': spec['argv'][:3]}
        # The command Mini's board terminal sends (transfer.windows_terminal), as the PC's PowerShell receives it.
        script = transfer.windows_terminal_script(spec['cwd'], spec['argv'], spec['env'])
        self.assertEqual(script, wr._terminal_script(spec['cwd'], spec['argv']))
        shell = transfer.windows_encoded_command(script).split(' ')
        self.assertEqual(shell[:3], ['powershell.exe', '-NoProfile', '-EncodedCommand'])
        self.reader.processes[40] = {'pid': 40, 'start': '134362358929022990',
                                     'exe': 'C:\\WINDOWS\\System32\\WindowsPowerShell\\v1.0\\powershell.exe'}
        self.reader.parents = {45: 40}
        self.reader.arguments = {40: ['C:\\WINDOWS\\System32\\WindowsPowerShell\\v1.0\\powershell.exe'] + shell[1:]}
        return source, row, registry

    def test_the_boards_own_idle_resume_left_after_its_terminal_is_gone_is_replaced_on_open(self):
        for legacy in (False, True):
            self.kills.clear()
            source, row, registry = self.board_resume_left_running(legacy)
            # Without the board's word that no terminal of it is open, the run is never touched.
            with self.assertRaisesRegex(ValueError, '대화형 터미널'):
                self.runtime.terminal_plan(source)
            # The plan says it replaces the run; only opening the terminal ends it.
            plan = self.runtime.terminal_plan(source, takeover=True)
            self.assertEqual((plan['replaces'], plan['resumed'], plan['argv'][1:3]), (True, True, ['--resume', source['id']]))
            self.assertFalse(self.kills)
            ended = self.reader.processes[45]
            self.runtime.terminate_process = lambda process: (self.kills.append(process), self.reader.processes.pop(45),
                                                              {'applied': True, 'confirmed': True})[-1]
            opened = self.runtime.terminal_plan(source, prepare=True, takeover=True)
            self.assertEqual(self.kills, [ended])
            self.assertEqual(opened['argv'], [str(self.home / '.local/bin/claude.exe'), '--resume', source['id'],
                                              '--permission-mode', 'bypassPermissions'])
            self.assertNotIn('replaces', opened)
            registry.unlink()

    def test_only_the_boards_own_idle_resume_of_the_same_session_is_replaced(self):
        cases = {
            'busy': lambda row, path: path.write_text(json.dumps({**row, 'status': 'busy'})),
            'another session': lambda row, path: self.reader.arguments[40].__setitem__(3, base64.b64encode(
                base64.b64decode(self.reader.arguments[40][3]).decode('utf-16le').replace(row['sessionId'], str(uuid.uuid4()))
                .encode('utf-16le')).decode()),
            'another script': lambda row, path: self.reader.arguments[40].__setitem__(3, base64.b64encode(
                "$ErrorActionPreference='Stop'; claude".encode('utf-16le')).decode()),
            'a user shell': lambda row, path: self.reader.arguments.__setitem__(40, ['powershell.exe']),
            'unreadable': lambda row, path: self.reader.arguments.pop(40),
            'not powershell': lambda row, path: self.reader.processes[40].update(exe='C:\\Windows\\explorer.exe'),
            'a newer parent': lambda row, path: self.reader.processes[40].update(start='134362358929099999'),
            'no parent': lambda row, path: self.reader.parents.clear(),
        }
        for name, change in cases.items():
            with self.subTest(name):
                self.kills.clear()
                source, row, registry = self.board_resume_left_running()
                change(row, registry)
                for prepare in (False, True):
                    with self.assertRaisesRegex(ValueError, '대화형 터미널'):
                        self.runtime.terminal_plan(source, prepare=prepare, takeover=True)
                self.assertFalse(self.kills)
                registry.unlink()
        # An end the PC cannot confirm opens nothing.
        self.kills.clear()
        source, row, registry = self.board_resume_left_running()
        self.runtime.terminate_process = lambda process: self.kills.append(process) or {'applied': True, 'confirmed': False}
        with self.assertRaisesRegex(ValueError, '닫지 못했습니다'):
            self.runtime.terminal_plan(source, prepare=True, takeover=True)
        self.assertEqual(len(self.kills), 1)

    @unittest.skipIf(sys.platform == 'win32', 'non-Windows guard')
    def test_windows_process_termination_never_executes_on_non_windows(self):
        with self.assertRaises(ValueError):
            wr._terminate_windows({'pid': 123, 'start': 'x', 'exe': 'fixture'})

    def test_runtime_refuses_shell_upload_and_control_without_proof(self):
        for op in ('shell', 'upload', 'start_app_server', 'new_session'):
            with self.assertRaises(ValueError):
                wr.handle({'op': op, 'args': {}})
        with self.assertRaises(ValueError):
            self.runtime.execute({'source': self.source, 'action': 'terminate', 'requestId': 'request-1234'})

    def test_symlink_state_folder_is_refused(self):
        target = self.home / 'other-state'
        target.mkdir()
        self.runtime.store.root.parent.mkdir(parents=True)
        self.runtime.store.root.symlink_to(target, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.runtime.preview(self.source)

    def test_long_websocket_input_uses_64_bit_frame_length(self):
        proxy = object.__new__(wr.NativeProxy)
        proxy.deadline = time.monotonic() + 1
        output = []
        proxy._write = output.append
        value = {'input': '한' * 20000}
        proxy._send(value)
        self.assertEqual(output[0][1], 0xff)
        self.assertEqual(struct.unpack('!Q', output[0][2:10])[0], len(json.dumps(value).encode()))

    def test_readonly_collector_never_gains_mutation_permissions(self):
        self.assertNotIn('turn/start', wc.Proxy.ALLOWED)
        self.assertNotIn('thread/archive', wc.Proxy.ALLOWED)
        self.assertNotIn('thread/compact/start', wc.Proxy.ALLOWED)
        self.assertIn('turn/start', wr.NativeProxy.ALLOWED)
        self.assertIn('thread/archive', wr.NativeProxy.ALLOWED)

    def test_real_native_proxy_dispatches_model_catalog_and_compact_but_refuses_arbitrary_methods(self):
        proxy = object.__new__(wr.NativeProxy)
        proxy.sequence = 0
        writes = []
        proxy._send = writes.append
        proxy._message = lambda: {'id': proxy.sequence, 'result': {'fixture': True}}
        for method, params in [('model/list', {'limit': 100}),
                               ('thread/compact/start', {'threadId': self.source['id']})]:
            self.assertEqual(proxy.call(method, params), {'fixture': True})
            self.assertEqual(writes[-1]['method'], method)
            self.assertEqual(writes[-1]['params'], params)
        with self.assertRaises(ValueError):
            proxy.call('command/exec', {'command': 'must-not-run'})
        self.assertEqual(len(writes), 2)

    def test_loaded_list_paginates_and_truncation_does_not_prove_absence(self):
        class Paged:
            def __init__(self, pages):
                self.pages, self.calls = pages, []
            def call(self, method, args):
                self.calls.append(args)
                return self.pages[len(self.calls) - 1]
        client = Paged([{'data': ['first'], 'nextCursor': 'page-2'}, {'data': ['target'], 'nextCursor': None}])
        self.assertEqual(wr._loaded(client), ({'first', 'target'}, True))
        self.assertEqual(client.calls[1]['cursor'], 'page-2')
        partial = Paged([{'data': [], 'nextCursor': 'page-' + str(index)} for index in range(3)])
        self.assertEqual(wr._loaded(partial), (set(), False))

    def test_archive_partial_loaded_list_does_not_claim_termination(self):
        original = wr._loaded
        def loaded(client):
            if self.reader.row['archived']:
                return set(), False
            return original(client)
        with patch.object(wr, '_loaded', side_effect=loaded):
            result = self.execute(self.runtime.preview(self.source))
        self.assertTrue(result['applied'])
        self.assertFalse(result['confirmed'])
        self.assertEqual(result['scopeReadback'], 'selected-session')
        self.assertFalse(result['descendantsConfirmed'])

    def test_terminate_checks_identity_and_waits_on_same_windows_handle(self):
        class Function:
            def __init__(self, fn):
                self.fn = fn
            def __call__(self, *args):
                return self.fn(*args)
        calls = []
        start = 134362358929022995
        def times(handle, created, *args):
            self.assertEqual(handle, 99)
            created._obj.dwHighDateTime = start >> 32
            created._obj.dwLowDateTime = start & 0xffffffff
            return True
        def image(handle, flags, output, length):
            self.assertEqual(handle, 99)
            output.value = 'C:\\Users\\fixture\\claude.exe'
            return True
        class Kernel:
            OpenProcess = Function(lambda rights, inherit, pid: calls.append(('open', pid)) or 99)
            CloseHandle = Function(lambda handle: calls.append(('close', handle)))
            GetProcessTimes = Function(times)
            QueryFullProcessImageNameW = Function(image)
            TerminateProcess = Function(lambda handle, code: calls.append(('terminate', handle)) or True)
            WaitForSingleObject = Function(lambda handle, ms: calls.append(('wait', handle)) or 0)
        expected = {'pid': 45, 'start': str(start), 'exe': 'C:\\Users\\fixture\\claude.exe'}
        with patch.object(wr.os, 'name', 'nt'), patch.object(wr.ctypes, 'WinDLL', return_value=Kernel(), create=True):
            result = wr._terminate_windows(expected)
            self.assertTrue(result['confirmed'])
            self.assertIn(('terminate', 99), calls)
            self.assertIn(('wait', 99), calls)
            calls.clear()
            result = wr._terminate_windows({**expected, 'start': 'different'})
            self.assertEqual(result['status'], 'stale')
            self.assertFalse(any(call[0] == 'terminate' for call in calls))
            self.assertEqual(calls[-1], ('close', 99))


FULL = {'approvalPolicy': 'never', 'sandbox': {'type': 'dangerFullAccess'}}
# The project server's default sandbox (scripts/windows/codex-agents-server.py: workspace-write).
DEFAULT_SANDBOX = {'type': 'workspaceWrite', 'writableRoots': [], 'networkAccess': False,
                   'excludeTmpdirEnvVar': False, 'excludeSlashTmp': False}


class SettleServer:
    """One PC project server's resume rules for one thread (Codex app-server
    resume_running_thread): a loaded thread nobody is subscribed to is rebuilt from its record
    by a resume whose values differ from it, keeping only the approval policy and taking the
    server's default sandbox when the request carries none. Otherwise a resume joins the loaded
    thread as it is. A board terminal's `codex resume --remote` TUI sends no permissions."""

    def __init__(self, reader):
        self.reader, self.loaded, self.live, self.subscribers = reader, False, None, 0
        self.notes, self.calls, self.fail_update, self.reject_exclude, self.on_wait = [], [], False, False, None

    @staticmethod
    def requested(params):
        modes = {'danger-full-access': 'dangerFullAccess', 'workspace-write': 'workspaceWrite', 'read-only': 'readOnly'}
        sandbox = {'type': modes[params['sandbox']]}
        if params['sandbox'] == 'workspace-write':
            config = params.get('config', {})
            sandbox.update(writableRoots=config.get('sandbox_workspace_write.writable_roots', []),
                           networkAccess=config.get('sandbox_workspace_write.network_access', False),
                           excludeTmpdirEnvVar=config.get('sandbox_workspace_write.exclude_tmpdir_env_var', False),
                           excludeSlashTmp=config.get('sandbox_workspace_write.exclude_slash_tmp', False))
        elif params['sandbox'] == 'read-only':
            sandbox['networkAccess'] = False
        return {'approvalPolicy': params['approvalPolicy'], 'sandbox': sandbox}

    def tui_attach(self):
        """The TUI's resume: no permissions, and a cwd spelled differently from the live one."""
        if not self.loaded or self.subscribers == 0:
            approval = self.live['approvalPolicy'] if self.live else self.reader.row['approval_mode']
            self.live = {'approvalPolicy': approval, 'sandbox': dict(DEFAULT_SANDBOX)}
        self.loaded = True
        self.subscribers += 1

    def proxy(self, binary, path, deadline):
        server = self

        class Client:
            subscribed = False

            def __enter__(self):
                return self

            def __exit__(self, *args):
                if self.subscribed:
                    server.subscribers -= 1

            def call(self, method, params):
                server.calls.append((method, copy.deepcopy(params)))
                if method == 'thread/loaded/list':
                    return {'data': [server.reader.sid] if server.loaded else []}
                if method == 'thread/read':
                    return {'thread': copy.deepcopy(server.reader.thread)}
                if method == 'thread/resume':
                    if server.reject_exclude and 'excludeTurns' in params:
                        raise ValueError('PC 서버가 읽기 요청을 거부했습니다.')
                    wanted = server.requested(params)
                    if not server.loaded or wanted != server.live and server.subscribers == 0:
                        server.live = wanted
                    server.loaded = True
                    if not self.subscribed:
                        self.subscribed = True
                        server.subscribers += 1
                    return {'thread': copy.deepcopy(server.reader.thread), **copy.deepcopy(server.live)}
                if method == 'thread/settings/update':
                    if server.fail_update:
                        raise ValueError('PC 서버가 읽기 요청을 거부했습니다.')
                    new = {'approvalPolicy': params['approvalPolicy'], 'sandbox': params['sandboxPolicy']}
                    if new != server.live:
                        server.live = copy.deepcopy(new)
                        server.notes.append({'method': 'thread/settings/updated', 'params': {
                            'threadId': params['threadId'],
                            'threadSettings': {'approvalPolicy': new['approvalPolicy'], 'sandboxPolicy': new['sandbox'],
                                               'model': 'fixture-model'}}})
                    return {}
                raise AssertionError('unexpected native operation: ' + method)

            def notification(self, until):
                if server.on_wait:
                    hook, server.on_wait = server.on_wait, None
                    hook()
                return server.notes.pop(0) if server.notes else None
        return Client()


class TerminalSettleTest(unittest.TestCase):
    """A board terminal on a PC Codex thread keeps the thread's permissions after its TUI joins."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.reader = FakeReader(Path(self.temp.name).resolve())
        self.server = SettleServer(self.reader)
        self.runtime = wr.WindowsRuntime(reader=self.reader, proxy_factory=self.server.proxy)
        self.source = {'host': 'fixture-pc', 'agent': 'codex', 'id': self.reader.sid, 'home': '.codex',
                       'cwd': str(self.reader.cwd)}

    def methods(self, name):
        return [params for method, params in self.server.calls if method == name]

    def test_a_tui_that_joins_before_settle_is_brought_back_to_the_stored_permissions(self):
        # 2026-10-20: never + full access became never + workspace-write once the TUI attached.
        plan = self.runtime.terminal_plan(self.source, prepare=True)
        self.assertTrue(plan['settle'])
        self.assertFalse(plan['restricted'])
        self.assertEqual(self.server.subscribers, 0)  # the plan's connection is gone
        self.server.tui_attach()
        self.assertEqual(self.server.live['sandbox'], DEFAULT_SANDBOX)
        result = self.runtime.terminal_settle(self.source)
        self.assertEqual((result['settled'], result['restored'], result['changed']), (True, True, False))
        self.assertEqual(self.server.live, FULL)
        self.assertEqual(self.methods('thread/settings/update'), [
            {'threadId': self.reader.sid, 'approvalPolicy': 'never', 'sandboxPolicy': {'type': 'dangerFullAccess'}}])
        # The join carries the stored permissions and never the cwd (a cwd mismatch would rebuild).
        join = self.methods('thread/resume')[-1]
        self.assertNotIn('cwd', join)
        self.assertEqual((join['sandbox'], join['approvalPolicy'], join['excludeTurns']), ('danger-full-access', 'never', True))

    def test_settle_subscribed_first_lets_the_tui_join_without_changing_anything(self):
        self.runtime.terminal_plan(self.source, prepare=True)
        self.server.on_wait = self.server.tui_attach  # the TUI joins while settle holds
        result = self.runtime.terminal_settle(self.source)
        self.assertEqual((result['settled'], result['restored']), (True, False))
        self.assertEqual(self.server.live, FULL)
        self.assertEqual(self.methods('thread/settings/update'), [])
        self.assertEqual(self.server.subscribers, 1)  # the TUI stays; settle let go

    def test_a_thread_unloaded_before_settle_is_loaded_with_the_stored_permissions(self):
        self.runtime.terminal_plan(self.source, prepare=True)
        self.server.loaded, self.server.live = False, None
        result = self.runtime.terminal_settle(self.source)
        self.assertTrue(result['settled'])
        self.assertEqual(self.server.live, FULL)
        self.assertEqual(self.methods('thread/settings/update'), [])

    def test_an_update_the_server_refuses_is_reported_not_retried(self):
        self.runtime.terminal_plan(self.source, prepare=True)
        self.server.tui_attach()
        self.server.fail_update = True
        result = self.runtime.terminal_settle(self.source)
        self.assertFalse(result['settled'])
        self.assertEqual(result['reason'], wr.SETTLE_FAILED)
        self.assertEqual(len(self.methods('thread/settings/update')), 1)

    def test_an_update_without_a_notification_is_read_back_by_another_join(self):
        self.runtime.terminal_plan(self.source, prepare=True)
        self.server.tui_attach()
        update = SettleServer.proxy
        def quiet(binary, path, deadline):
            client = update(self.server, binary, path, deadline)
            client.notification = lambda until: self.server.notes.clear()
            return client
        self.runtime.proxy_factory = quiet
        result = self.runtime.terminal_settle(self.source)
        self.assertTrue(result['settled'])
        self.assertEqual(len(self.methods('thread/resume')), 3)  # the plan's load, the join, the readback

    def test_a_permissions_choice_made_while_subscribed_stays(self):
        self.runtime.terminal_plan(self.source, prepare=True)
        chosen = {'type': 'readOnly', 'networkAccess': False}
        def choose():
            self.server.tui_attach()
            self.server.live = {'approvalPolicy': 'on-request', 'sandbox': chosen}
            self.server.notes.append({'method': 'thread/settings/updated', 'params': {
                'threadId': self.reader.sid, 'threadSettings': {'approvalPolicy': 'on-request', 'sandboxPolicy': chosen}}})
        self.server.on_wait = choose
        result = self.runtime.terminal_settle(self.source)
        self.assertEqual((result['settled'], result['changed'], result['restored']), (True, True, False))
        self.assertEqual(self.server.live['sandbox'], chosen)
        self.assertEqual(self.methods('thread/settings/update'), [])

    def test_a_record_is_used_once_and_only_shortly_after_its_plan(self):
        self.assertIsNone(self.runtime.terminal_settle(self.source)['settled'])  # no plan: nothing to keep
        self.runtime.terminal_plan(self.source, prepare=True)
        self.assertTrue(self.runtime.terminal_settle(self.source)['settled'])
        self.assertIsNone(self.runtime.terminal_settle(self.source)['settled'])
        self.server.loaded, self.server.live = False, None
        self.runtime.terminal_plan(self.source, prepare=True)
        with patch.object(wr.time, 'time', return_value=time.time() + wr.SETTLE_RECORD_SECONDS + 5):
            result = self.runtime.terminal_settle(self.source)
        self.assertFalse(result['settled'])
        self.assertEqual(self.methods('thread/settings/update'), [])

    def test_a_restarted_project_server_is_not_settled(self):
        self.runtime.terminal_plan(self.source, prepare=True)
        self.reader.processes[12]['start'] = '134362358929099999'
        resumes = len(self.methods('thread/resume'))
        self.assertFalse(self.runtime.terminal_settle(self.source)['settled'])
        self.assertEqual(len(self.methods('thread/resume')), resumes)

    def test_never_asking_inside_a_sandbox_is_kept_and_flagged(self):
        # No automatic widening: the stored combination returns, and the board says what it means.
        self.reader.row['sandbox_policy'] = json.dumps({'type': 'workspace-write'})
        plan = self.runtime.terminal_plan(self.source, prepare=True)
        self.assertTrue(plan['restricted'])
        self.server.tui_attach()
        result = self.runtime.terminal_settle(self.source)
        self.assertTrue(result['settled'])
        self.assertTrue(result['restricted'])
        self.assertEqual(self.server.live['sandbox']['type'], 'workspaceWrite')

    def test_a_loaded_thread_is_recorded_as_it_runs(self):
        self.server.loaded, self.server.subscribers = True, 1  # a PC TUI holds it
        chosen = {'type': 'readOnly', 'networkAccess': False}
        self.server.live = {'approvalPolicy': 'on-request', 'sandbox': chosen}
        plan = self.runtime.terminal_plan(self.source, prepare=True)
        self.assertTrue(plan['settle'])
        self.assertEqual(self.server.live['sandbox'], chosen)  # its live choice, not the stored one
        self.server.tui_attach()
        result = self.runtime.terminal_settle(self.source)
        self.assertEqual((result['settled'], result['restored']), (True, False))
        self.assertEqual(self.server.live['sandbox'], chosen)

    def test_a_loaded_thread_with_a_policy_the_board_cannot_restore_is_not_settled(self):
        self.server.loaded, self.server.live = True, FULL
        self.reader.row['sandbox_policy'] = json.dumps({'type': 'future-policy'})
        plan = self.runtime.terminal_plan(self.source, prepare=True)
        self.assertNotIn('settle', plan)
        self.assertEqual(self.methods('thread/resume'), [])
        self.assertIsNone(self.runtime.terminal_settle(self.source)['settled'])

    def test_a_server_without_exclude_turns_is_joined_with_the_history(self):
        self.runtime.terminal_plan(self.source, prepare=True)
        self.server.tui_attach()
        self.server.reject_exclude = True
        self.assertTrue(self.runtime.terminal_settle(self.source)['settled'])
        self.assertNotIn('excludeTurns', self.methods('thread/resume')[-1])

    def test_settle_is_an_rpc_and_holds_no_operation_lock(self):
        self.runtime.terminal_plan(self.source, prepare=True)
        def other_operation():
            with self.runtime.store.lock():  # a chat send meanwhile is not refused
                pass
        self.server.on_wait = other_operation
        with patch.object(wr, 'WindowsRuntime', return_value=self.runtime):
            self.assertTrue(wr.handle({'op': 'terminal_settle', 'args': {'source': self.source}})['settled'])

    def test_the_native_proxy_reads_notifications_whole_and_waits_only_between_messages(self):
        proxy = object.__new__(wr.NativeProxy)
        proxy.deadline, proxy.pending, proxy.frames = time.monotonic() + 5, bytearray(), __import__('queue').Queue()
        def frame(value):
            raw = json.dumps(value).encode()
            return bytes([0x81, len(raw)]) + raw
        note = {'method': 'thread/settings/updated', 'params': {'threadId': 'x'}}
        request = {'id': 7, 'method': 'item/commandExecution/requestApproval', 'params': {}}
        both = frame(request) + frame(note)
        proxy.frames.put(both[:5])
        proxy.frames.put(both[5:])
        self.assertEqual(proxy.notification(time.monotonic() + 1), note)  # the request is skipped
        self.assertIsNone(proxy.notification(time.monotonic() + 0.05))
        proxy.frames.put(b'')
        with self.assertRaises(ValueError):
            proxy.notification(time.monotonic() + 1)


if __name__ == '__main__':
    unittest.main()
