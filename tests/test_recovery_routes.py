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

    def test_pc_claude_reconnects_after_five_restarts_and_reads_its_own_input_and_reply(self):
        from windows_collector import WindowsCollector
        from windows_runtime import WindowsRuntime

        host = {**HOST, 'name': 'fixture-pc', 'platform': 'windows', 'local': False}
        root = Path(self.temp.name).resolve()
        reader = WindowsCollector(root / 'pc-home', hostname=host['name'], process_reader=lambda pid: None)
        binary = reader.home / '.local/bin/claude.exe'
        binary.parent.mkdir(parents=True)
        binary.touch()
        sid = '019aaaaa-0000-7000-8000-000000000011'
        other_sid = sid[:-1] + '2'
        source = {'host': host['name'], 'agent': 'claude', 'id': 'fixture-worker',
                  'home': '.claude', 'sessionId': sid, 'cwd': str(root), 'kind': 'interactive'}
        other = {**source, 'sessionId': other_sid}
        folder = reader.claude_home / 'projects/fixture'
        folder.mkdir(parents=True)
        old = int(time.time()) - 60

        def append(sid, role, text, stamp, **fields):
            entry = {'sessionId': sid, 'cwd': str(root), 'type': role, 'timestamp': stamp,
                     'uuid': '%s-%s-%s' % (sid, role, stamp), 'message': {'content': text}, **fields}
            with (folder / (sid + '.jsonl')).open('a') as file:
                file.write(json.dumps(entry) + '\n')

        append(sid, 'user', '이전 합성 입력', old)
        append(other_sid, 'user', '다른 대화의 합성 입력', old)
        self.flow.recoveries.remember(source)
        prior = None
        total_pastes = 0
        for turn in range(5):
            with self.subTest(restart=turn + 1):
                board = server.Board([host], 60, runner=lambda *_: {'claude': [], 'codex': []},
                                     state_dir=root, live=False)
                board.poll(host)
                terms = FakeTerminals()
                terms.records = [{'id': '0' * 32, 'alive': True, 'nativeSource': other}]
                terms.capture = Mock(return_value='──────────\n❯ \n──────────')
                terms.paste = Mock()
                terms.send_keys = Mock()
                flow = server.Workflow(board, terms, root)
                runtime = WindowsRuntime(reader=reader, terminate_process=Mock(side_effect=AssertionError('no takeover')))

                def rpc(host, op, args, timeout):
                    selected = args['source']
                    self.assertEqual(selected['sessionId'], sid)
                    if op == 'recover_session':
                        return runtime.recover_session(selected)
                    if op == 'terminal_plan':
                        self.assertIs(args['takeover'], False)
                        result = runtime.terminal_plan(selected, prepare=args.get('prepare', False))
                        self.assertEqual(result['argv'][1:3], ['--resume', sid])
                        return result
                    self.assertEqual(op, 'chat')
                    self.assertIn(args['action'], ('read', 'capabilities'))
                    return runtime.chat(args)

                flow.transfers.rpc = rpc
                flow.transfers.windows_terminal = Mock(return_value=['/fixture/fake-transport'])
                service = chat.Chat(board, flow, root)
                service._settled = Mock(return_value=True)
                elapsed = [0]
                service.verify_clock = lambda: elapsed[0]
                service.sleep = lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds)
                if prior:
                    self.assertEqual(service.send(prior)['status'], 'unknown')
                    terms.paste.assert_not_called()
                connect = {'source': source, 'requestId': 'fixture-connect-%02d' % turn}
                with patch('server.shutil.which', return_value='/fixture/tmux'):
                    connection = service.connect(connect)
                    self.assertEqual(service.connect(connect), connection)
                self.assertEqual(connection['source']['sessionId'], sid)
                self.assertNotEqual(connection['terminalId'], terms.records[0]['id'])
                flow.transfers.windows_terminal.assert_called_once()
                self.assertEqual(len(terms.calls), 1)

                rid = 'fixture-send-%02d' % turn
                body = '합성 입력 %d' % turn
                stamp = int(time.time())
                append(sid, 'user', body, stamp)  # Same text in the same second, before sending.
                send = {'source': source, 'requestId': rid, 'text': body}
                result = service.send(send)['receipt']
                self.assertEqual((result['status'], result['acknowledged'], result['readbackConfirmed'],
                                  result['responseObserved']), ('unknown', True, False, False))
                self.assertEqual(service.send(send)['receipt'], result)

                def receipt():
                    service._forget_state(source)
                    state = service.state({'source': source, 'requestId': rid})
                    self.assertEqual(state['route']['sessionId'], sid)
                    return state['receipts'][0]

                append(other_sid, 'user', body, stamp + 1)
                append(other_sid, 'assistant', '다른 대화 응답', stamp + 1)
                append(sid, 'assistant', '입력과 무관한 합성 응답', stamp + 1)
                self.assertEqual(receipt()['status'], 'unknown')
                append(sid, 'queue-operation', '', stamp + 2, operation='enqueue', content=body)
                queued = receipt()
                self.assertEqual((queued['status'], queued['deliveryVerified'], queued['readbackConfirmed'],
                                  queued['responseObserved']), ('queued', True, False, False))
                append(sid, 'user', body, stamp + 3)
                read = receipt()
                self.assertEqual((read['status'], read['readbackConfirmed'], read['responseObserved']),
                                 ('accepted', True, False))
                append(sid, 'assistant', '같은 대화 합성 후속 응답', stamp + 4)
                self.assertTrue(receipt()['responseObserved'])
                board.state[host['name']]['ok'] = False
                stale = receipt()
                self.assertTrue(stale['readbackConfirmed'])
                self.assertFalse(stale['currentSnapshotConfirmed'])
                terms.paste.assert_called_once_with(connection['terminalId'], body, submit=True)
                terms.send_keys.assert_not_called()
                runtime.terminate_process.assert_not_called()
                prior = send
                total_pastes += terms.paste.call_count
        self.assertEqual(total_pastes, 5)

    def test_unknown_view_close_never_tidies(self):
        handler = object.__new__(server.Handler)
        handler.server = SimpleNamespace(session_tidied=Mock(), board=self.board)
        handler.tidy_after_close(self.native, ended=False)
        handler.server.session_tidied.mark.assert_not_called()

    def missing(self):
        from transfer import WorkerError
        return WorkerError('정확한 Claude 저장 대화를 확인하지 못했습니다.', 'recover-missing')

    def test_sweep_forgets_only_a_conversation_gone_on_its_host(self):
        self.flow.recoveries.forget(REF)
        self.flow.recoveries.remember(self.native, 'a' * 32)
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
        self.flow.recoveries.forget(REF)
        self.flow.recoveries.remember(self.native, 'a' * 32)
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
        self.flow.recoveries.forget(REF)
        self.flow.recoveries.remember(self.native, 'a' * 32)
        self.terms.records = []
        old = int(time.time() - 2 * self.flow.RECOVERY_GRACE)
        self.flow.recoveries.remember({**self.native, 'openedAt': old})
        from session_recovery import RecoveryError
        with patch.object(self.flow.transfers, 'rpc', side_effect=self.missing()), \
             self.assertRaises(RecoveryError) as caught:
            self.flow.resolve_source(REF, verify=True)
        self.assertEqual((caught.exception.status, caught.exception.payload['code']), (410, 'recovery_session_gone'))
        self.assertIsNone(self.flow.recoveries.get(REF))

    def test_missing_probe_cannot_forget_a_record_changed_during_the_probe(self):
        self.flow.recoveries.forget(REF)
        self.flow.recoveries.remember(self.native, 'a' * 32)
        self.terms.records = []
        old = int(time.time() - 2 * self.flow.RECOVERY_GRACE)
        self.flow.recoveries.remember({**self.native, 'openedAt': old})

        def probe(*args, **kwargs):
            self.flow.recoveries.remember({**self.native, 'explicitTermination': True})
            raise self.missing()

        from transfer import WorkerError
        with patch.object(self.flow.transfers, 'rpc', side_effect=probe), self.assertRaises(WorkerError):
            self.flow.resolve_source(REF, verify=True)
        self.assertEqual(self.flow.recoveries.get(REF)['provenance']['closure'], 'explicit_termination')

    def test_capture_batches_valid_rows_and_retries_an_atomic_write_failure(self):
        other = {**self.native, 'id': self.native['id'][:-1] + '2'}
        self.terms.records = [
            {'id': 'a' * 32, 'nativeSource': self.native, 'title': 'updated fixture'},
            {'id': 'b' * 32, 'nativeSource': {**self.native, 'cwd': '/fixture/conflict'}},
            {'id': 'c' * 32, 'nativeSource': other},
        ]
        store = self.flow.recoveries
        before = store.page()
        put = store._put

        def fail_second(db, row):
            if row['source']['id'] == other['id']:
                raise OSError('fixture failed second upsert')
            put(db, row)

        with patch.object(store, '_put', side_effect=fail_second):
            self.flow.capture_recoveries()
        self.assertEqual(store.page(), before)
        with patch.object(store, '_database', wraps=store._database) as database, \
                patch.object(store, '_put', wraps=put) as upsert:
            self.flow.capture_recoveries()
        database.assert_called_once_with(write=True)
        self.assertEqual(upsert.call_count, 2)
        self.assertEqual(store.get(REF)['title'], 'updated fixture')
        self.assertEqual(store.get(other)['provenance']['terminalId'], 'c' * 32)

    def test_opening_a_new_or_changed_conversation_keeps_it(self):
        self.flow.recoveries.forget(REF)
        self.flow.recoveries.remember(self.native, 'a' * 32)
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
