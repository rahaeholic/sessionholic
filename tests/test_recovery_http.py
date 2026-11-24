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
from session_recovery import RetainedSessions, RecoverySessions


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
        by_id = {row['id']: row for row in self.snapshot()['recoverySessions']}
        self.assertEqual(len(by_id), 3)
        self.assertEqual(by_id['019aaaaa-0000-7000-8000-000000000002']['removedAt'], 300)
        self.assertEqual(by_id['019aaaaa-0000-7000-8000-000000000003']['tidiedAt'], 300)
        self.assertTrue(by_id[SID]['retained'])
        for store in (self.httpd.session_removed, self.httpd.session_tidied):
            with self.subTest(metadata=store.NAME):
                original = store.path.read_bytes()
                store.path.write_bytes(b'fixture-corrupt-display-metadata')
                self.assertEqual(self.snapshot()['recoverySessions'], [])
                store.path.write_bytes(original)
        self.assertEqual(self.calls, [])
        self.assert_no_execution()

    def test_pages_hold_old_released_and_listed_rows_with_bounded_work_and_display_marks(self):
        old = {**self.native, 'id': '019aaaaa-0000-7000-8000-000000000023', 'source': 'cli',
               'title': 'fixture from collection only', 'updatedAt': 3}
        self.board.runner = lambda *_: {'codex': [old], 'claude': []}
        self.board.poll(HOST)
        self.board.runner = lambda *_: {'codex': [], 'claude': []}
        self.board.poll(HOST)
        # More than both the old visible cap and the old retention count.
        observations = [({**self.native, 'id': '019aaaaa-0000-7000-8000-%012d' % (100 + index)}, None, 'released')
                        for index in range(510)]
        self.flow.recoveries.remember_many(observations)
        snapshot = self.snapshot()
        self.assertEqual(len(snapshot['recoverySessions']), RetainedSessions.PAGE_SIZE)
        self.assertEqual(snapshot['hosts'][0]['data']['codex'], [])
        self.assertEqual(self.request('GET', '/api/session/retained?after=abc')[0], 400)
        self.assertEqual(self.request('GET', '/api/session/retained?after=1', authorized=False)[0], 401)

        old_ref = {**self.ref, 'id': old['id']}
        self.assertEqual(self.request('POST', '/api/session/remove',
                                      {'source': old_ref, 'removed': True})[0], 200)
        visible_state = self.snapshot()
        self.assertNotEqual(snapshot['retainedDisplayVersion'], visible_state['retainedDisplayVersion'])
        found = {row['id']: row for row in visible_state['recoverySessions']}
        after = visible_state['retainedNextCursor']
        while after:
            with patch.object(self.flow.recoveries, '_decode', wraps=self.flow.recoveries._decode) as decoded:
                code, page = self.request('GET', '/api/session/retained?' + urlencode({'after': after}))
                self.assertLessEqual(decoded.call_count, RetainedSessions.PAGE_SIZE)
            self.assertEqual(code, 200)
            self.assertEqual(page['displayVersion'], visible_state['retainedDisplayVersion'])
            self.assertTrue(found.keys().isdisjoint(row['id'] for row in page['sessions']))
            found.update({row['id']: row for row in page['sessions']})
            last_cursor, after = after, page['nextCursor']
        self.assertEqual(len(found), 512)
        self.assertEqual(found[SID]['closedReason'], 'released')
        self.assertEqual(found[old['id']]['source'], 'cli')
        self.assertIsInstance(found[old['id']]['removedAt'], int)
        self.assertEqual(self.request('POST', '/api/session/remove',
                                      {'source': old_ref, 'removed': False})[0], 200)
        _, page = self.request('GET', '/api/session/retained?' + urlencode({'after': last_cursor}))
        self.assertIsNone(next(row for row in page['sessions'] if row['id'] == old['id'])['removedAt'])
        self.assertEqual(self.calls, [])
        self.close_server()
        self.start_server()
        self.assertIsNotNone(self.flow.recoveries.get(old_ref))

    def test_retained_manual_view_close_survives_restart_without_a_terminal_row(self):
        current = {'id': 'b' * 32, 'nativeSource': self.native, 'mode': 'attach',
                   'alive': True, 'host': HOST['name'], 'openedAt': 100}
        self.terms.records = [current]

        def close(tid):
            self.assertEqual(tid, current['id'])
            current.update(closed=True, alive=False, closedReason='user', closeEffect='view')

        self.terms.close_terminal = close
        code, result = self.request('POST', '/api/terminal/' + current['id'] + '/close', {})
        self.assertEqual((code, result.get('ok')), (200, True))
        self.close_server()
        self.start_server()
        row = self.snapshot()['recoverySessions'][0]
        self.assertEqual((row['id'], row['closedReason'], row['retained']), (SID, 'view_closed', True))
        self.assert_no_execution()

    def test_legacy_migration_keeps_exact_identity_and_close_markers_without_reimport(self):
        state = self.root / 'migration-state'
        legacy = RecoverySessions(state)
        implicit = {**self.native, 'id': '019aaaaa-0000-7000-8000-000000000034'}
        explicit = {**self.native, 'id': '019aaaaa-0000-7000-8000-000000000035'}
        ambiguous = {**self.native, 'id': '019aaaaa-0000-7000-8000-000000000036'}
        legacy.remember(self.native, 'a' * 32, 'released')
        legacy.remember(implicit, 'b' * 32, 'user')
        legacy.remember({**explicit, 'explicitTermination': True}, 'c' * 32, 'user')
        legacy.remember(ambiguous, 'd' * 32, 'user')
        before = legacy.path.read_bytes()
        catalog = RetainedSessions(state)
        self.assertEqual(catalog.page()[0][0]['source'], self.native)
        for source, tid in ((implicit, 'b'), (explicit, 'c'), (ambiguous, 'e')):
            catalog.remember(source, tid * 32, 'view_closed')
        self.assertEqual(catalog.get(implicit)['provenance']['closedReason'], 'view_closed')
        self.assertEqual(catalog.get(explicit)['provenance']['closure'], 'explicit_termination')
        self.assertEqual(catalog.get(ambiguous)['provenance']['closure'], 'legacy_user_close')
        self.assertEqual(catalog.get(ambiguous)['provenance']['terminalId'], 'd' * 32)
        catalog.remember_many([(explicit, None, None), (ambiguous, None, None)], listed=True)
        self.assertEqual({row['source']['id'] for row in catalog.page()[0]}, {SID, implicit['id']})
        self.assertEqual(legacy.path.read_bytes(), before)
        self.assertTrue(catalog.forget(self.ref))
        self.assertIsNone(RetainedSessions(state).get(self.ref))

    def test_hidden_marks_migrate_once_and_stop_evicting_older_user_choices(self):
        from session_labels import SessionRemoved, SessionSeen

        state = self.root / 'removed-migration'
        old = SessionSeen(state)
        old.path = state / 'session-removed.json'
        old.lock_path = state / 'session-removed.lock'
        old.mark(self.native, 1)
        before = old.path.read_bytes()
        with patch('session_labels.MAX_SEEN', 2):
            removed = SessionRemoved(state)
            for i in range(10, 15):
                removed.mark({**self.native, 'id': '019aaaaa-0000-7000-8000-%012d' % i}, i)
        snapshot = {'hosts': [{'name': HOST['name'], 'data': {'codex': [self.native]}}]}
        def removed_at():
            return SessionRemoved(state).decorate(snapshot)['hosts'][0]['data']['codex'][0]['removedAt']
        self.assertEqual(removed_at(), 1)
        self.assertTrue(removed.unmark(self.native))
        self.assertIsNone(removed_at())
        self.assertEqual(old.path.read_bytes(), before)

    def test_corrupt_locator_store_cannot_authorize_native_lookup_or_resurface_recent_work(self):
        self.flow.recoveries.path.write_bytes(b'fixture-corrupt-recovery-locators')
        code, _data = self.recover()
        self.assertNotEqual(code, 200)
        snapshot = self.snapshot()
        self.assertEqual(snapshot['recoverySessions'], [])
        self.assertFalse(snapshot['retainedCatalog']['ok'])
        self.assertEqual(self.calls, [])
        self.assert_no_execution()

    def test_snapshot_does_not_cache_a_transient_catalog_read_failure(self):
        from session_recovery import RecoveryError

        self.snapshot()
        self.snapshot()  # Warm the snapshot cache before forcing a fresh response.
        self.board.changed(force=True)
        with patch.object(self.flow.recoveries, 'page', side_effect=RecoveryError()):
            self.assertFalse(self.snapshot()['retainedCatalog']['ok'])
        recovered = self.snapshot()
        self.assertTrue(recovered['retainedCatalog']['ok'])
        self.assertEqual(recovered['recoverySessions'][0]['id'], SID)

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
