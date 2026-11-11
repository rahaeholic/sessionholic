"""Exact recovery outside the bounded list never invents a new native session."""
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
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

    def missing(self):
        from transfer import WorkerError
        return WorkerError('정확한 Claude 저장 대화를 확인하지 못했습니다.', 'recover-missing')

    def test_sweep_forgets_only_a_conversation_gone_on_its_host(self):
        later = time.time() + 2 * self.flow.RECOVERY_GRACE
        self.assertIsNotNone(self.flow.recoveries.get(REF))
        from transfer import WorkerError
        kept = (WorkerError('저장된 세션과 원본 작업 경로가 다릅니다.'), RuntimeError('PC 응답을 확인하지 못했습니다.'),
                OSError('fixture'), None)
        for outcome in kept:
            self.flow.recovery_asked.clear()
            with self.subTest(outcome=outcome), \
                 patch.object(self.flow.transfers, 'rpc', side_effect=outcome,
                              return_value={'source': self.native}) as rpc:
                self.assertEqual(self.flow.prune_recoveries(later), 0)
                rpc.assert_called_once()
            self.assertIsNotNone(self.flow.recoveries.get(REF))
        # A closed terminal record that would save the locator again keeps it.
        self.flow.recovery_asked.clear()
        with patch.object(self.flow.transfers, 'rpc', side_effect=self.missing()):
            self.assertEqual(self.flow.prune_recoveries(later), 0)
        self.assertIsNotNone(self.flow.recoveries.get(REF))
        self.terms.records = []
        self.flow.recovery_asked.clear()
        with patch.object(self.flow.transfers, 'rpc', side_effect=self.missing()) as rpc:
            self.assertEqual(self.flow.prune_recoveries(later), 1)
            rpc.assert_called_once()
            host, op, args = rpc.call_args.args
            self.assertEqual((host['name'], op, args['source']['id']), ('local', 'recover_session', SOURCE['id']))
        self.assertIsNone(self.flow.recoveries.get(REF))
        self.assertFalse(self.terms.calls)

    def test_sweep_never_asks_for_new_listed_offline_closed_or_recently_asked_locators(self):
        self.terms.records = []
        later = time.time() + 2 * self.flow.RECOVERY_GRACE
        with patch.object(self.flow.transfers, 'rpc', side_effect=self.missing()) as rpc:
            self.assertEqual(self.flow.prune_recoveries(time.time()), 0)       # just opened: may be starting
            self.board.state['local']['ok'] = False
            self.assertEqual(self.flow.prune_recoveries(later), 0)             # host offline
            self.board.state['local']['ok'] = True
            self.rows[:] = [SOURCE]
            self.board.poll(HOST)
            self.assertEqual(self.flow.prune_recoveries(later), 0)             # listed
            rpc.assert_not_called()
            self.rows[:] = []
            self.board.poll(HOST)
            self.flow.recovery_asked[json.dumps([REF['host'], REF['agent'], REF['home'], REF['id']])] = later
            self.assertEqual(self.flow.prune_recoveries(later + 60), 0)        # asked a minute ago
            self.flow.recoveries.remember({**self.native, 'explicitTermination': True})
            self.flow.recovery_asked.clear()
            self.assertEqual(self.flow.prune_recoveries(later), 0)             # an explicit end is kept
            rpc.assert_not_called()
        self.assertIsNotNone(self.flow.recoveries.get(REF))

    def test_sweep_asks_a_bounded_batch(self):
        self.terms.records = []
        for index in range(self.flow.RECOVERY_BATCH + 2):
            self.flow.recoveries.remember({**self.native, 'id': SOURCE['id'][:-2] + '%02d' % (index + 10)})
        later = time.time() + 2 * self.flow.RECOVERY_GRACE
        with patch.object(self.flow.transfers, 'rpc', return_value={'source': self.native}) as rpc:
            self.flow.prune_recoveries(later)
        self.assertEqual(rpc.call_count, self.flow.RECOVERY_BATCH)

    def test_opening_a_gone_conversation_forgets_it_and_says_so(self):
        self.terms.records = []
        old = int(time.time() - 2 * self.flow.RECOVERY_GRACE)
        self.flow.recoveries.remember({**self.native, 'openedAt': old})
        from session_recovery import RecoveryError
        with patch.object(self.flow.transfers, 'rpc', side_effect=self.missing()), \
             self.assertRaises(RecoveryError) as caught:
            self.flow.resolve_source(REF, verify=True)
        self.assertEqual((caught.exception.status, caught.exception.payload['code']), (410, 'recovery_session_gone'))
        self.assertIsNone(self.flow.recoveries.get(REF))

    def test_opening_a_new_or_changed_conversation_keeps_it(self):
        self.terms.records = []
        from transfer import WorkerError
        for error in (self.missing(), WorkerError('저장된 세션과 원본 작업 경로가 다릅니다.')):
            with self.subTest(error=str(error)), patch.object(self.flow.transfers, 'rpc', side_effect=error), \
                 self.assertRaises(WorkerError):
                self.flow.resolve_source(REF, verify=True)
            self.assertIsNotNone(self.flow.recoveries.get(REF))

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
