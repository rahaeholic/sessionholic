"""Loopback HTTP + real receipt DB, with fake terminals and no native services."""
import copy
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import chat
from prompt_receipts import PromptReceiptError
import server


HOST = {'name': 'fixture-local', 'label': 'Fixture', 'local': True}
SESSION_A = '019aaaaa-0000-7000-8000-000000000001'
SESSION_B = '019aaaaa-0000-7000-8000-000000000002'
TERMINAL_ID = 'a' * 32
MENU = '가짜 테스트 질문\n\n계속 진행할까요?\n\n❯ 1. 예\n  2. 아니오\n'
NEXT_MENU = '다음 가짜 질문\n\n어떤 결과를 볼까요?\n\n❯ 1. 요약\n  2. 원문\n'
RID = 'fixture-prompt-integration-001'


class FixtureTerminals:
    """Only a screen and a fake key counter; no tmux or child processes."""
    def __init__(self, native):
        self.rows = [{'id': TERMINAL_ID, 'alive': True, 'closed': False, 'nativeSource': native}]
        self.screens, self.keys, self.captures = [], [], []
        self.keys_error = None

    def list(self):
        return copy.deepcopy(self.rows)

    def capture(self, terminal_id):
        self.captures.append(terminal_id)
        return self.screens.pop(0) if self.screens else '> '

    def send_keys(self, terminal_id, keys):
        self.keys.append((terminal_id, list(keys)))
        if self.keys_error is not None:
            raise self.keys_error
        return {'ok': True}


class PromptReceiptsHttpTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / 'fixture-project'
        self.project.mkdir()
        self.row = {'agent': 'claude', 'id': 'worker-a1', 'sessionId': SESSION_A, 'home': None,
                    'kind': 'background', 'cwd': str(self.project), 'phase': 'idle', 'title': 'fixture'}
        self.board = server.Board([HOST], 60, runner=lambda *_: {'codex': [], 'claude': [self.row]}, live=False)
        self.board.poll(HOST)
        self.terms = FixtureTerminals(self.source())
        self.flow = server.Workflow(self.board, self.terms, self.root)
        config = patch.object(server, 'CONFIG_DIR', self.root / 'unused-config')
        config.start(); self.addCleanup(config.stop)
        settle = patch.object(chat, 'PROMPT_SETTLE', 0)
        settle.start(); self.addCleanup(settle.stop)
        logs = patch.object(server.Handler, 'log_message', lambda *_: None)
        logs.start(); self.addCleanup(logs.stop)
        requests = patch.object(server.Handler, 'log_request', lambda *_: None)
        requests.start(); self.addCleanup(requests.stop)
        self.httpd = server.make_server('127.0.0.1', 0, self.board, 'fixture-http-token',
            terminals=self.terms, workflow=self.flow, state_dir=self.root, push=False, automatic_work=False)
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        rpc = patch.object(self.flow.transfers, 'rpc', side_effect=AssertionError('native RPC used'))
        rpc.start(); self.addCleanup(rpc.stop)
        self.port = self.httpd.server_address[1]
        self.fingerprint = chat.parse_menu(MENU)['fingerprint']

    def close_server(self):
        self.httpd.shutdown()
        self.thread.join(2)
        self.httpd.server_close()

    def source(self):
        return {'host': HOST['name'], **self.row}

    def ref(self):
        return {key: self.source()[key] for key in ('host', 'agent', 'home', 'id', 'sessionId')}

    def switch_session(self, session_id):
        self.row['sessionId'] = session_id
        self.board.poll(HOST)
        self.terms.rows = [{'id': TERMINAL_ID, 'alive': True, 'closed': False, 'nativeSource': self.source()}]

    def restart_chat(self):
        self.httpd.chat = chat.Chat(self.board, self.flow, self.root)

    def auth(self):
        return {'Authorization': 'Bearer fixture-http-token', 'X-CSRF-Token': self.httpd.csrf}

    def post(self, action='status', *, headers=None, request_id=RID, **changes):
        body = {'source': self.ref(), 'action': action, 'requestId': request_id}
        if action == 'answer':
            body.update(fingerprint=self.fingerprint, number=1)
        body.update(changes)
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        try:
            connection.request('POST', '/api/chat/prompt', json.dumps(body),
                {'Content-Type': 'application/json', **(self.auth() if headers is None else headers)})
            response = connection.getresponse()
            raw = response.read()
            body = json.loads(raw) if response.getheader('Content-Type', '').startswith('application/json') else {'body': raw.decode('utf-8')}
            return response.status, body
        finally:
            connection.close()

    def test_status_keeps_existing_authentication_csrf_origin_and_host_guards(self):
        ledger = self.httpd.chat.prompt_receipts
        with patch.object(ledger, 'lookup', wraps=ledger.lookup) as lookup:
            self.assertEqual(self.post(headers={})[0], 401)
            self.assertEqual(self.post(headers={'Authorization': 'Bearer fixture-http-token'})[0], 403)
            self.assertEqual(self.post(headers={**self.auth(), 'X-CSRF-Token': 'old-server-token'})[0], 403)
            self.assertEqual(self.post(headers={**self.auth(), 'Origin': 'https://evil.invalid'})[0], 403)
            self.assertEqual(self.post(headers={**self.auth(), 'Host': 'evil.invalid'})[0], 421)
            lookup.assert_not_called()
            code, result = self.post()
            self.assertEqual((code, result['status']), (200, 'not_found'))
            lookup.assert_called_once()
        self.assertEqual(self.terms.keys, [])
        self.assertEqual(self.terms.captures, [])

    def test_offline_status_reads_saved_receipt_without_terminal_attach_input_or_native_rpc(self):
        ledger = self.httpd.chat.prompt_receipts
        ledger.reserve(self.source(), RID, self.fingerprint, 1, self.terms.rows[0])
        ledger.finish(self.source(), RID, self.fingerprint, 1, self.terms.rows[0], status='answered')
        self.restart_chat()
        self.board.state[HOST['name']]['ok'] = False
        with patch.object(self.terms, 'list', side_effect=AssertionError('terminal list used')), \
                patch.object(self.terms, 'capture', side_effect=AssertionError('screen read')), \
                patch.object(self.terms, 'send_keys', side_effect=AssertionError('input sent')), \
                patch.object(self.flow, 'launch', side_effect=AssertionError('terminal attached')), \
                patch.object(self.board, 'poll', side_effect=AssertionError('native collection used')):
            code, result = self.post()
        self.assertEqual((code, result['status'], result['number']), (200, 'answered', 1))

    def test_http_retry_after_chat_restart_returns_receipt_without_replaying_any_old_prompt(self):
        self.terms.screens = [MENU, NEXT_MENU]
        code, first = self.post('answer')
        self.assertEqual((code, first['status']), (200, 'answered'))
        self.assertEqual(first['prompt']['fingerprint'], chat.parse_menu(NEXT_MENU)['fingerprint'])
        self.restart_chat()
        self.terms.rows = []
        with patch.object(self.terms, 'capture', side_effect=AssertionError('past screen replayed')):
            code, replay = self.post('answer')
        self.assertEqual((code, replay['status'], replay['prompt']), (200, 'answered', None))
        self.assertEqual(len(self.terms.keys), 1)

    def test_reused_worker_keeps_each_conversation_receipt_separate_and_never_replays_prompt(self):
        self.terms.screens = [MENU, '> ']
        self.assertEqual(self.post('answer')[1]['status'], 'answered')
        self.switch_session(SESSION_B)
        self.terms.screens = [MENU, MENU]
        self.assertEqual(self.post('answer')[1]['status'], 'unconfirmed')
        captures = len(self.terms.captures)
        for session_id, expected in ((SESSION_A, 'answered'), (SESSION_B, 'unconfirmed')):
            self.switch_session(session_id)
            code, replay = self.post('answer')
            self.assertEqual((code, replay['status'], replay['prompt']), (200, expected, None))
            self.assertEqual(self.post()[1]['status'], expected)
        self.assertEqual(len(self.terms.keys), 2)
        self.assertEqual(len(self.terms.captures), captures)

    def test_post_send_storage_failure_is_unconfirmed_and_the_saved_reservation_survives_restart(self):
        self.terms.screens = [MENU, '> ']
        with patch.object(self.httpd.chat.prompt_receipts, 'finish', side_effect=PromptReceiptError()):
            code, result = self.post('answer')
        self.assertEqual((code, result['status'], result['reasonCode']), (200, 'unconfirmed', 'receipt_write_failed'))
        self.assertNotIn('dispatchState', result)
        self.restart_chat()
        self.terms.rows = []
        code, retry = self.post('answer')
        self.assertEqual((code, retry['status'], retry['prompt']), (200, 'unconfirmed', None))
        self.assertEqual(self.post()[1]['reasonCode'], 'reserved')
        self.assertEqual(len(self.terms.keys), 1)

    def test_http_conflicting_payload_is_409_without_extra_keys_even_after_terminal_disappears(self):
        self.terms.screens = [MENU, '> ']
        self.assertEqual(self.post('answer')[0], 200)
        self.restart_chat()
        self.terms.rows = []
        for changes in ({'number': 2}, {'fingerprint': 'f' * 64}):
            code, result = self.post('answer', **changes)
            self.assertEqual((code, result['code']), (409, 'prompt_request_conflict'))
            self.assertNotIn('dispatchState', result)
        self.assertEqual(len(self.terms.keys), 1)

    def test_changed_terminal_identity_is_refused_without_creating_a_reservation(self):
        for changes in ({'sessionId': SESSION_B}, {'host': 'another-host'}, {'home': '.claude-other'}):
            with self.subTest(changes=changes):
                self.terms.rows = [{'id': TERMINAL_ID, 'alive': True, 'closed': False,
                                    'nativeSource': {**self.source(), **changes}}]
                code, result = self.post('answer')
                self.assertEqual((code, result['code']), (409, 'terminal_required'))
                self.assertEqual(self.post()[1]['status'], 'not_found')
        self.assertEqual(self.terms.keys, [])
        self.assertEqual(self.terms.captures, [])

    def test_same_transcript_in_another_folder_still_checks_the_current_question(self):
        self.terms.rows[0]['nativeSource']['cwd'] = str(self.root / 'worktree')
        self.terms.screens = [NEXT_MENU]
        code, result = self.post('answer')
        self.assertEqual((code, result['code']), (409, 'prompt_changed'))
        self.assertEqual(self.terms.keys, [])
        self.assertEqual(self.httpd.chat.prompt_receipts.lookup(self.source(), RID)['status'], 'not_found')
        self.terms.screens = [MENU, NEXT_MENU]
        code, result = self.post('answer')
        self.assertEqual((code, result['status']), (200, 'answered'))
        self.assertEqual(len(self.terms.keys), 1)

    def test_uncertain_key_transport_exception_never_turns_into_a_safe_resend(self):
        self.terms.screens = [MENU]
        self.terms.keys_error = ValueError('fixture exception after fake delivery')
        code, first = self.post('answer')
        self.assertEqual((code, first['status']), (200, 'unconfirmed'))
        self.restart_chat()
        self.terms.keys_error = None
        code, replay = self.post('answer')
        self.assertEqual((code, replay['status'], replay['prompt']), (200, 'unconfirmed', None))
        self.assertEqual(len(self.terms.keys), 1)


if __name__ == '__main__':
    unittest.main()
