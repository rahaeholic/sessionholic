import http.client
import copy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import server
import session_history
from history_native import DeleteRefused
from transfer import WorkerError


class HistoryHTTPTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config_patch = patch.object(server, 'CONFIG_DIR', Path(self.temp.name).resolve() / 'config')
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)
        self.host = {'name': 'fixture', 'label': '합성 기기', 'local': True}
        self.board = server.Board([self.host], 60, runner=lambda *a: self.fail('native collection'), live=False)
        self.row = {'id': '11111111-1111-4111-8111-111111111111', 'home': '.codex', 'cwd': '/fixture',
                    'phase': 'closed', 'title': '완료 대화', 'updatedAt': time.time() - 30 * session_history.DAY}
        self.board.state['fixture'].update(ok=True, fetchedAt=time.time(), data={'codex': [self.row], 'claude': []})
        self.httpd = server.make_server('127.0.0.1', 0, self.board, 'fixture-token',
            state_dir=str(Path(self.temp.name).resolve()), terminals=SimpleNamespace(list=lambda: [], close=lambda: None))
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True); self.thread.start()
        self.cookie = None
        status, data, headers = self.request('POST', '/login', {'token': 'fixture-token'})
        self.cookie = headers['Set-Cookie'].split(';')[0]; self.csrf = data['csrfToken']

    def tearDown(self):
        self.httpd.shutdown(); self.thread.join(2); self.httpd.server_close(); self.temp.cleanup()

    def request(self, method, path, body=None, auth=True, csrf=True):
        conn = http.client.HTTPConnection('127.0.0.1', self.httpd.server_address[1])
        headers = {'Content-Type': 'application/json'}
        if auth and self.cookie: headers['Cookie'] = self.cookie
        if csrf and hasattr(self, 'csrf'): headers['X-CSRF-Token'] = self.csrf
        conn.request(method, path, json.dumps(body) if body is not None else None, headers)
        r = conn.getresponse(); payload = json.loads(r.read()); result = (r.status, payload, dict(r.getheaders()))
        conn.close(); return result

    def test_authentication_csrf_and_policy_validation(self):
        self.assertEqual(self.request('GET', '/api/session-history', auth=False)[0], 401)
        policy = {'enabled': False, 'idleDays': 14, 'graceDays': 7}
        self.assertEqual(self.request('POST', '/api/session-history/policy', policy, csrf=False)[0], 403)
        status, data, headers = self.request('POST', '/api/session-history/policy', policy)
        self.assertEqual(status, 200); self.assertEqual(data['policy'], policy)
        self.assertIn('no-store', headers.get('Cache-Control', ''))
        self.assertEqual(self.request('POST', '/api/session-history/policy', {**policy, 'idleDays': 0})[0], 400)
        self.assertEqual(self.request('GET', '/api/session-history/conversation?key=../outside')[0], 400)
        self.assertEqual(self.request('POST', '/api/session-history/preserve', {'key': 'a'*64, 'preserved': True})[0], 400)

    def test_candidates_exclude_active_unknown_fresh_and_offline_sources(self):
        candidates, _ = server.history_candidates(self.httpd, refresh=False)
        self.assertEqual([s['id'] for s in candidates], [self.row['id']])
        for phase in ('working', 'needs_input', 'idle', 'unknown'):
            self.row['phase'] = phase
            self.assertFalse(server.history_candidates(self.httpd, refresh=False)[0])
        self.row['phase'] = 'closed'
        self.board.state['fixture']['ok'] = False
        self.assertFalse(server.history_candidates(self.httpd, refresh=False)[0])
        with patch.object(self.board, 'poll', return_value=False), patch.object(self.httpd.workflow.transfers, 'rpc') as rpc:
            with self.assertRaises(ValueError): server.history_rpc(self.httpd, {'host': 'fixture'}, {'action': 'delete'})
            rpc.assert_not_called()

    def test_original_pin_is_checked_again_before_delete(self):
        source = {'host': 'fixture', 'agent': 'codex', **self.row}
        self.httpd.session_pinned.mark(source, int(time.time()))
        with patch.object(self.board, 'poll', return_value=True), patch.object(self.httpd.workflow.transfers, 'rpc') as rpc:
            with self.assertRaises(ValueError):
                server.history_rpc(self.httpd, source, {'action': 'delete', 'family': [self.row['id']]})
            rpc.assert_not_called()

    def test_persisted_pin_protects_missing_root_and_child_rows(self):
        source = {'host': 'fixture', 'agent': 'codex', **self.row}
        child = {**source, 'id': '22222222-2222-4222-8222-222222222222'}
        export = {'family': [source['id'], child['id']], 'fingerprint': 'a' * 64, 'canDelete': True,
                  'exportVersion': session_history.history_native.EXPORT_VERSION,
                  'lastActivityAt': 1, 'messages': [{'role': 'user', 'text': 'request'}, {'role': 'assistant', 'text': 'answer'}]}
        now = [100 * session_history.DAY]
        self.httpd.session_history.clock = lambda: now[0]
        with patch.object(self.httpd.workflow.transfers, 'rpc', return_value=copy.deepcopy(export)):
            self.httpd.session_history.sweep([source], set())
        now[0] += 8 * session_history.DAY
        self.board.state['fixture']['data']['codex'] = []
        for pinned in [source, child]:
            with self.subTest(pinned=pinned['id']):
                self.httpd.session_pinned.mark(pinned, int(time.time()))
                candidates, protected = server.history_candidates(self.httpd, refresh=False)
                self.assertIn(('fixture', 'codex', '.codex', pinned['id']), protected)
                with patch.object(self.httpd.workflow.transfers, 'rpc') as rpc:
                    self.httpd.session_history.sweep(candidates, protected)
                    rpc.assert_not_called()
                    with patch.object(self.board, 'poll', return_value=True):
                        with self.assertRaises(DeleteRefused):
                            server.history_rpc(self.httpd, source, {'action': 'delete', 'family': export['family']})
                    rpc.assert_not_called()
                self.httpd.session_pinned.unmark(pinned)

    def test_delete_refreshes_owner_and_maps_only_proven_worker_refusal(self):
        source = {'host': 'fixture', 'agent': 'codex', **self.row}
        args = {'action': 'delete', 'family': [source['id']]}
        for stage, expected in [(DeleteRefused.transfer_stage, DeleteRefused), (None, WorkerError)]:
            with self.subTest(stage=stage), patch.object(self.board, 'poll', return_value=True) as poll:
                with patch.object(self.httpd.workflow.transfers, 'rpc', side_effect=WorkerError('fixture', stage)):
                    with self.assertRaises(expected): server.history_rpc(self.httpd, source, args)
                poll.assert_called_once_with(self.host)
        with patch.object(self.board, 'poll', return_value=False):
            with patch.object(self.httpd.workflow.transfers, 'rpc') as rpc:
                with self.assertRaises(DeleteRefused): server.history_rpc(self.httpd, source, args)
                rpc.assert_not_called()

    def test_pin_store_failure_blocks_candidate_collection_and_delete(self):
        source = {'host': 'fixture', 'agent': 'codex', **self.row}
        with patch.object(self.httpd.session_pinned, 'sources', side_effect=server.GoalError('fixture')):
            with self.assertRaises(ValueError): server.history_candidates(self.httpd, refresh=False)
            with patch.object(self.board, 'poll', return_value=True):
                with self.assertRaises(DeleteRefused):
                    server.history_rpc(self.httpd, source, {'action': 'delete', 'family': [source['id']]})


if __name__ == '__main__': unittest.main()
