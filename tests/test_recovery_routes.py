"""Exact recovery outside the bounded list never invents a new native session."""
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

import chat
import server
import settings
from test_workflow import FakeTerminals, HOST, SOURCE, PROFILE, OTHER, REF


def setUpModule():
    folder = tempfile.TemporaryDirectory()
    patcher = patch.object(server, 'CONFIG_DIR', Path(folder.name))
    patcher.start()
    unittest.addModuleCleanup(folder.cleanup)
    unittest.addModuleCleanup(patcher.stop)


class ExactRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = patch('settings.load', return_value={**settings.defaults(), 'projects': [], 'profiles': [],
                            'environments': {'default': {'label': '기본', 'variables': {}}}})
        self.config.start()
        self.addCleanup(self.config.stop)
        self.rows = []
        self.board = server.Board([HOST], 60, runner=lambda *_: {'codex': self.rows, 'claude': []}, live=False)
        self.board.poll(HOST)
        self.terms = FakeTerminals()
        self.flow = server.Workflow(self.board, self.terms, self.temp.name)
        self.flow.profiles = lambda: [PROFILE, OTHER]
        self.native = {**REF, 'cwd': SOURCE['cwd']}
        self.terms.records = [{'id': 'a' * 32, 'nativeSource': self.native, 'mode': 'attach',
                               'closed': True, 'alive': False, 'closedReason': 'released'}]
        self.flow.capture_recoveries()

    def test_exact_plan_and_launch_keep_the_old_id_after_restart_and_history_trim(self):
        self.terms.records = []
        flow = server.Workflow(self.board, self.terms, self.temp.name)
        flow.profiles = self.flow.profiles
        fresh = {**self.native, 'phase': 'unknown', 'runtimeKnown': False}
        spec = {'argv': ['/fixture/codex', 'resume', SOURCE['id']], 'cwd': SOURCE['cwd'], 'env': {}}
        with patch.object(flow.transfers, 'rpc', return_value={'source': fresh}) as rpc, \
             patch.object(flow, 'resume_preview', return_value={'allowed': True}), \
             patch('server.shutil.which', return_value='/fixture/tmux'), \
             patch('launch.build_launch', return_value=spec):
            plan = flow.plan({'source': REF, 'target': {'host': 'local', 'agent': 'codex', 'account': PROFILE['id']}})
            self.assertTrue(plan['allowed'])
            self.assertEqual(plan['mode'], 'resume')
            self.assertEqual(self.terms.calls, [])
            result = flow.launch(plan['id'], 'exact-recovery-0001')
        self.assertEqual(result['terminal']['nativeSource']['id'], SOURCE['id'])
        self.assertEqual([call.args[1] for call in rpc.call_args_list], ['recover_session', 'recover_session'])
        self.assertEqual(len(self.terms.calls), 1)

    def test_arbitrary_or_other_account_reference_never_calls_native(self):
        with patch.object(self.flow.transfers, 'rpc') as rpc:
            for changed in ({'id': SOURCE['id'][:-1] + '2'}, {'home': OTHER['home']}, {'host': 'other'}):
                with self.subTest(changed=changed), self.assertRaises(ValueError):
                    self.flow.resolve_source({**REF, **changed}, verify=True)
            rpc.assert_not_called()

    def test_native_cwd_or_account_change_is_rejected(self):
        for changed in ({'cwd': '/other'}, {'home': OTHER['home']}, {'id': SOURCE['id'][:-1] + '2'}):
            with self.subTest(changed=changed), \
                 patch.object(self.flow.transfers, 'rpc', return_value={'source': {**self.native, **changed}}), \
                 self.assertRaises(ValueError):
                self.flow.resolve_source(REF, verify=True)
        self.assertFalse(self.terms.calls)

    def test_offline_and_explicit_termination_cannot_probe_or_resume(self):
        self.board.state['local']['ok'] = False
        with patch.object(self.flow.transfers, 'rpc') as rpc, self.assertRaises(RuntimeError):
            self.flow.resolve_source(REF, verify=True)
        rpc.assert_not_called()
        self.board.state['local']['ok'] = True
        self.flow.recoveries.remember({**self.native, 'explicitTermination': True})
        self.flow.capture_recoveries()
        with patch.object(self.flow.transfers, 'rpc') as rpc, self.assertRaises(ValueError):
            self.flow.resolve_source(REF, verify=True)
        rpc.assert_not_called()

    def test_chat_resolves_saved_source_for_read_without_launch(self):
        service = chat.Chat(self.board, self.flow, self.temp.name)
        self.assertEqual(service.resolve(REF)['cwd'], SOURCE['cwd'])
        self.assertFalse(self.terms.calls)

    def test_reused_claude_worker_never_reconnects_to_another_conversation(self):
        a = {**self.native, 'agent': 'claude', 'id': 'worker-12345678', 'home': None,
             'sessionId': '019aaaaa-0000-7000-8000-000000000011', 'kind': 'background'}
        b = {**a, 'sessionId': a['sessionId'][:-1] + '2'}
        self.flow.recoveries.remember(a)
        self.terms.records = [{'id': 'b' * 32, 'key': 'other-route', 'alive': True, 'nativeSource': b}]
        claude = {'id': 'claude:default', 'agent': 'claude', 'home': '.claude', 'label': '기본', 'available': True}
        self.flow.profiles = lambda: [claude]
        with patch.object(self.flow.transfers, 'rpc', return_value={'source': a}), \
             patch('server.shutil.which', return_value='/fixture/tmux'):
            plan = self.flow.plan({'source': {k: a[k] for k in ('host', 'agent', 'id', 'home', 'sessionId')},
                                  'target': {'host': 'local', 'agent': 'claude', 'account': 'claude:default'}})
        self.assertEqual(plan['mode'], 'attach')
        self.assertNotIn('reconnectId', plan)
        self.assertEqual(plan['source']['sessionId'], a['sessionId'])

    def test_unknown_view_close_never_tidies(self):
        handler = object.__new__(server.Handler)
        handler.server = SimpleNamespace(session_tidied=Mock(), board=self.board)
        handler.tidy_after_close(self.native, ended=False)
        handler.server.session_tidied.mark.assert_not_called()

    def test_windows_idle_connections_are_never_auto_released(self):
        self.board.hosts[0] = {**HOST, 'platform': 'windows'}
        self.rows[:] = [SOURCE]
        self.board.poll(self.board.hosts[0])
        self.terms.idle_codex_attaches = lambda: [{'id': 'a' * 32, 'nativeSource': self.native}]
        self.terms.release_codex_attach = Mock()
        self.assertEqual(self.flow.close_idle_codex_attaches(), [])
        self.terms.release_codex_attach.assert_not_called()
        self.terms.spare_attaches = lambda: [{'id': 'a' * 32, 'host': HOST['name'], 'nativeSource': self.native}]
        self.terms.release_spare_attach = Mock()
        self.assertEqual(self.flow.close_spare_attaches(), [])
        self.terms.release_spare_attach.assert_not_called()
        self.terms.idle_runs = lambda: [{'id': 'a' * 32, 'host': HOST['name']}]
        self.terms.capture = Mock()
        self.assertEqual(self.flow.close_idle_runs(), [])
        self.terms.capture.assert_not_called()


if __name__ == '__main__':
    unittest.main()
