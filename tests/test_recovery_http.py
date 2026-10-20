"""Loopback recovery API + temporary native DB; no real CLI, SSH or session starts."""
import copy
import http.client
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from urllib.parse import urlencode
from unittest.mock import patch

import launch
import server


HOST = {'name': 'fixture-local', 'label': 'Fixture', 'local': True}
SID = '019aaaaa-0000-7000-8000-000000000001'


class RecoveryFixtureTerminals:
    def __init__(self, records=()):
        self.records = list(records)
        self.creates = 0
        self.inputs = 0

    def list(self):
        return copy.deepcopy(self.records)

    def create(self, *args, **kwargs):
        self.creates += 1
        raise AssertionError('recovery lookup started a terminal')

    def write(self, *args, **kwargs):
        self.inputs += 1
        raise AssertionError('recovery lookup sent input')

    def send_keys(self, *args, **kwargs):
        self.inputs += 1
        raise AssertionError('recovery lookup sent keys')


class RecoveryHttpTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.state = self.root / 'state'
        self.home = self.root / 'native-home'
        self.account = self.home / '.codex'
        self.account.mkdir(parents=True)
        self.project = self.home / 'fixture-project'
        self.project.mkdir()
        self.db = self.account / 'state_5.sqlite'
        with sqlite3.connect(self.db) as db:
            db.execute('CREATE TABLE threads (id TEXT PRIMARY KEY,cwd TEXT,archived INTEGER)')
            db.execute('INSERT INTO threads VALUES (?,?,0)', (SID, str(self.project)))
        self.native = {'host': HOST['name'], 'agent': 'codex', 'id': SID,
                       'home': '.codex', 'cwd': str(self.project)}
        self.ref = {key: self.native[key] for key in ('host', 'agent', 'id', 'home')}
        self.calls = []
        self.httpd = self.thread = None
        self.addCleanup(self.close_server)
        config = patch.object(server, 'CONFIG_DIR', self.root / 'unused-config')
        config.start(); self.addCleanup(config.stop)
        logs = patch.object(server.Handler, 'log_request', lambda *_: None)
        logs.start(); self.addCleanup(logs.stop)
        messages = patch.object(server.Handler, 'log_message', lambda *_: None)
        messages.start(); self.addCleanup(messages.stop)
        terminal = {'id': 'a' * 32, 'nativeSource': self.native, 'mode': 'attach', 'closed': True,
                    'alive': False, 'closedReason': 'released', 'title': 'fixture saved work',
                    'updatedAt': 100, 'openedAt': 200}
        self.start_server([terminal])

    def start_server(self, records=()):
        # The source is deliberately outside the current bounded snapshot. Only the
        # server-held locator and its exact native DB row may bring it back.
        self.board = server.Board([HOST], 60, runner=lambda *_: {'codex': [], 'claude': []}, live=False)
        self.board.poll(HOST)
        self.terms = RecoveryFixtureTerminals(records)
        self.flow = server.Workflow(self.board, self.terms, self.state)
        def rpc(host, operation, args=None, timeout=30):
            self.calls.append((host['name'], operation, copy.deepcopy(args), timeout))
            if operation != 'recover_session':
                raise AssertionError('unexpected native operation: ' + operation)
            return launch.recover_saved_session(args['source'], self.home)
        self.flow.transfers.rpc = rpc
        self.httpd = server.make_server('127.0.0.1', 0, self.board, 'fixture-recovery-token',
            terminals=self.terms, workflow=self.flow, state_dir=self.state, push=False, automatic_work=False)
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        self.thread.start()
        self.port = self.httpd.server_address[1]

    def close_server(self):
        if self.httpd is not None:
            self.httpd.shutdown()
            self.thread.join(2)
            self.httpd.server_close()
            self.httpd = None

    def request(self, method, path, body=None, *, authorized=True):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        headers = {'Authorization': 'Bearer fixture-recovery-token'} if authorized else {}
        if method == 'POST':
            headers.update({'Content-Type': 'application/json', 'X-CSRF-Token': self.httpd.csrf})
        try:
            connection.request(method, path, json.dumps(body) if body is not None else None, headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def recover(self, ref=None, **kwargs):
        return self.request('GET', '/api/session/recover?' + urlencode(self.ref if ref is None else ref), **kwargs)

    def snapshot(self):
        code, snapshot = self.request('GET', '/api/snapshot')
        self.assertEqual(code, 200)
        return snapshot

    def assert_no_execution(self):
        self.assertEqual(self.terms.creates, 0)
        self.assertEqual(self.terms.inputs, 0)
        self.assertTrue(all(call[1] == 'recover_session' for call in self.calls))

    def test_recover_authentication_and_untrusted_browser_references_never_reach_native(self):
        self.assertEqual(self.recover(authorized=False)[0], 401)
        for change in ({'id': '019aaaaa-0000-7000-8000-000000000099'}, {'home': '.codex-other'},
                       {'home': '../.codex'}, {'cwd': str(self.root / 'browser-chosen-folder')}):
            with self.subTest(change=change):
                code, _body = self.recover({**self.ref, **change})
                self.assertEqual(code, 400)
        self.assertEqual(self.calls, [])
        self.assert_no_execution()

    def test_authenticated_lookup_uses_only_the_stored_source_and_never_executes_browser_options(self):
        before = self.db.read_bytes()
        code, data = self.recover({**self.ref, 'argv': 'browser arbitrary command', 'resume': '1'})
        self.assertEqual(code, 200)
        actual = data['session']
        self.assertEqual({key: actual[key] for key in self.native}, self.native)
        self.assertEqual((actual['phase'], actual['runtimeKnown'], actual['recoverySource']), ('unknown', False, True))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0], (HOST['name'], 'recover_session', {'source': self.native}, 20))
        self.assertEqual(self.db.read_bytes(), before)
        self.assert_no_execution()

    def test_outside_list_opened_and_seen_posts_keep_display_marks_without_another_native_read(self):
        self.assertEqual(self.recover()[0], 200)
        self.calls.clear()
        before = int(time.time())
        code, opened = self.request('POST', '/api/session/opened', {'source': self.ref})
        self.assertEqual(code, 200)
        self.assertGreaterEqual(opened['openedAt'], before)
        code, seen = self.request('POST', '/api/session/seen', {'source': self.ref, 'updatedAt': 9999})
        self.assertEqual((code, seen['seenAt']), (200, 100))
        snapshot = self.snapshot()
        self.assertEqual(snapshot['hosts'][0]['data']['codex'], [])
        recovered = snapshot['recoverySessions'][0]
        self.assertEqual((recovered['id'], recovered['seenAt'], recovered['openedAt']), (SID, 100, opened['openedAt']))
        self.assertEqual(self.calls, [])
        self.assert_no_execution()

    def test_recent_recovery_hides_removed_tidied_terminated_and_unreadable_display_metadata(self):
        for suffix, flag in ((2, 'removed'), (3, 'tidied'), (4, 'terminated')):
            source = {**self.native, 'id': '019aaaaa-0000-7000-8000-%012d' % suffix}
            self.flow.recoveries.remember({**source, 'title': flag, 'openedAt': 200,
                **({'explicitTermination': True} if flag == 'terminated' else {})})
            if flag == 'removed':
                self.httpd.session_removed.mark(source, 300)
            elif flag == 'tidied':
                self.httpd.session_tidied.mark(source, 300)
        self.assertEqual([row['id'] for row in self.snapshot()['recoverySessions']], [SID])
        for store in (self.httpd.session_removed, self.httpd.session_tidied):
            with self.subTest(metadata=store.NAME):
                original = store.path.read_bytes()
                store.path.write_bytes(b'fixture-corrupt-display-metadata')
                self.assertEqual(self.snapshot()['recoverySessions'], [])
                store.path.write_bytes(original)
        self.assertEqual(self.calls, [])
        self.assert_no_execution()

    def test_corrupt_locator_store_cannot_authorize_native_lookup_or_resurface_recent_work(self):
        self.flow.recoveries.path.write_bytes(b'fixture-corrupt-recovery-locators')
        code, _data = self.recover()
        self.assertNotEqual(code, 200)
        self.assertEqual(self.snapshot()['recoverySessions'], [])
        self.assertEqual(self.calls, [])
        self.assert_no_execution()

    def test_recovery_locator_survives_http_reload_and_server_restart_after_terminal_trim(self):
        initial = self.snapshot()
        self.assertEqual([row['id'] for row in initial['recoverySessions']], [SID])
        self.assertEqual([row['id'] for row in self.snapshot()['recoverySessions']], [SID])
        self.close_server()
        self.start_server()  # The old display-only terminal history has been trimmed away.
        restarted = self.snapshot()
        self.assertNotEqual(restarted['bootId'], initial['bootId'])
        self.assertEqual(self.terms.records, [])
        self.assertEqual([row['id'] for row in restarted['recoverySessions']], [SID])
        code, result = self.recover()
        self.assertEqual((code, result['session']['id'], result['session']['cwd']), (200, SID, str(self.project)))
        self.assert_no_execution()


if __name__ == '__main__':
    unittest.main()
