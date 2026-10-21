import unittest
from pathlib import Path
import tempfile
from unittest.mock import patch
import runtime_observation as obs


class RuntimeObservationsTest(unittest.TestCase):
    def setUp(self):
        self.now = 1000
        self.book = obs.RuntimeObservations(clock=lambda: self.now, boot_id='fixture')
        self.source = {'host': 'pc', 'agent': 'codex', 'home': '.codex', 'id': 'one', 'cwd': 'C:\\work'}
        self.working = {'phase': 'working', 'runtimeKnown': True, 'runtimeEvidence': 'exec_writer_lock'}

    def read(self, result=None):
        return self.book.accept(self.source, result or self.working, self.book.begin(), 90)

    def test_only_server_clock_is_used_and_two_same_second_reads_order(self):
        a = self.read({**self.working, 'updatedAt': 99999999999, 'observedAt': -1})
        b = self.read()
        self.assertEqual(b['runtimeObservation']['observedAt'], 1000)
        self.assertEqual(b['runtimeObservation']['expiresAt'], 1090)
        self.assertGreater(b['runtimeObservation']['acceptedSeq'], a['runtimeObservation']['acceptedSeq'])

    def test_reverse_rpc_cannot_replace_newer_runtime(self):
        slow, fast = self.book.begin(), self.book.begin()
        latest = self.book.accept(self.source, self.working, fast, 90)
        stale = self.book.accept(self.source, {'phase': 'closed', 'runtimeKnown': True}, slow, 90)
        self.assertEqual(stale, latest)

    def test_latest_adapter_failure_blocks_an_older_read_without_renewing_known(self):
        confirmed = self.read()['runtimeObservation']
        older_read = self.book.begin()
        self.now += 30
        self.book.reject_scope('pc', 'codex', self.book.begin())
        result = self.book.accept(self.source, self.working, older_read, 90)
        self.assertFalse(result['runtimeObservation']['known'])
        self.assertEqual(result['lastKnown'], confirmed)
        self.assertEqual(result['runtimeObservation']['expiresAt'], confirmed['expiresAt'])
        fresh = self.read()
        self.assertTrue(fresh['runtimeObservation']['known'])

    def test_unknown_keeps_last_known_without_renewing_it(self):
        original = self.read()['runtimeObservation']
        self.now += 80
        unknown = self.read({'phase': 'unknown', 'runtimeKnown': False, 'runtimeEvidence': 'saved_record'})
        self.assertFalse(unknown['runtimeObservation']['known'])
        self.assertEqual(unknown['lastKnown'], original)
        self.now += 20
        self.assertLess(unknown['lastKnown']['expiresAt'], self.now)

    def test_timing_alone_does_not_change_digest_input(self):
        a = self.read()
        self.now += 10
        b = self.read()
        # A confirmed lease renewal carries no new semantic state.
        self.now += 10
        c = self.read()
        self.assertEqual(obs.semantic(b), obs.semantic(c))
        self.assertEqual(obs.semantic(a), obs.semantic(b))

    def test_host_home_cwd_and_claude_conversation_never_share_observations(self):
        self.read()
        for field, value in (('home', '.codex-alt'), ('host', 'mini'), ('cwd', 'C:\\other')):
            source = {**self.source, field: value}
            result = self.book.accept(source, {'runtimeKnown': False}, self.book.begin(), 90)
            self.assertIsNone(result['lastKnown'])
        a = {**self.source, 'agent': 'claude', 'sessionId': 'a'}
        b = {**a, 'sessionId': 'b'}
        self.book.accept(a, self.working, self.book.begin(), 90)
        self.assertIsNone(self.book.accept(b, {}, self.book.begin(), 90)['lastKnown'])

    def test_reused_worker_gets_separate_chat_cache_and_event_watcher(self):
        import chat
        import events
        a = {**self.source, 'agent': 'claude', 'sessionId': 'conversation-a'}
        b = {**a, 'sessionId': 'conversation-b'}
        for source in (a, b):
            self.assertEqual(chat.Chat.state_key(source), events.watch_key(chat.Chat.watch_identity(source)))
        self.assertNotEqual(chat.Chat.state_key(a), chat.Chat.state_key(b))

    def test_restart_has_a_distinct_generation(self):
        other = obs.RuntimeObservations()
        self.assertNotEqual(other.boot_id, self.book.boot_id)

    def test_malformed_adapter_metadata_remains_unknown(self):
        result = self.read({'phase': {}, 'runtimeKnown': True, 'runtimeEvidence': [], 'runtimeReasonCode': 'arbitrary text'})
        self.assertFalse(result['runtimeObservation']['known'])
        self.assertEqual(result['runtimeObservation']['reasonCode'], 'unsupported')

    def test_events_digest_excludes_lease_renewal(self):
        import events
        a = self.read()
        b = {**a, 'runtimeObservation': {**a['runtimeObservation'], 'observedAt': 1030, 'expiresAt': 1120,
                                       'requestSeq': 100, 'acceptedSeq': 100}}
        self.assertEqual(events.state_digest(a), events.state_digest(b))
        b['runtimeObservation']['phase'] = 'idle'
        self.assertNotEqual(events.state_digest(a), events.state_digest(b))

    def test_failed_adapter_preserves_only_that_source_without_renewing_lease(self):
        import server
        host = {'name': 'pc', 'label': 'PC', 'local': False, 'platform': 'windows'}
        payload = {'codex': [{**self.source, **self.working}], 'claude': []}
        board = server.Board([host], 120, runner=lambda *_: payload, live=False)
        board.clock = lambda: self.now
        board.poll(host)
        first = board.state['pc']['data']['codex'][0]['runtimeObservation']
        self.now += 10
        payload = {'codex': [], 'claude': [{'id': 'claude-one'}], 'sourceStatus': {'codex': {'ok': False}}}
        board.poll(host)
        saved = board.state['pc']['data']['codex'][0]
        self.assertEqual(saved['id'], 'one')
        self.assertEqual(saved['lastKnown']['expiresAt'], first['expiresAt'])
        self.assertFalse(saved['runtimeKnown'])
        self.assertEqual(len(board.state['pc']['data']['claude']), 1)
        payload = {'codex': [], 'claude': [], 'sourceStatus': {'codex': {'ok': True}}}
        board.poll(host)
        self.assertEqual(board.state['pc']['data']['codex'], [])

    def test_watcher_renews_without_get_or_fast_digest_and_never_from_cached_receipt(self):
        from types import SimpleNamespace
        import events
        state = {'phase': 'idle', **self.read()}
        board = SimpleNamespace(add_listener=lambda callback: None)
        chat = SimpleNamespace(state=lambda *args, **kwargs: state)
        hub = events.EventHub(board, chat, clock=lambda: self.now)
        watcher = events.Watcher('fixture', self.source)
        stream = events.Stream()
        watcher.streams.add(stream)
        _, digest = hub._read(watcher)
        watcher.digest = digest
        first = stream.observation
        self.assertIsNotNone(first)
        self.now += 31
        hub._read(watcher)  # Same cached observation never gets a new lease notice.
        self.assertIs(stream.observation, first)
        state = {'phase': 'idle', **self.read()}
        _, second = hub._read(watcher)
        watcher.digest = second
        self.assertIsNot(stream.observation, first)
        self.now += 31
        state = {**state, 'stale': True, **self.read()}
        last = stream.observation
        hub._read(watcher)
        self.assertIs(stream.observation, last)

    def test_changed_phase_immediately_replaces_the_event_observation(self):
        from types import SimpleNamespace
        import events
        state = {'phase': 'working', **self.read()}
        hub = events.EventHub(SimpleNamespace(add_listener=lambda _: None),
                              SimpleNamespace(state=lambda *args, **kwargs: state), clock=lambda: self.now)
        watcher = events.Watcher('fixture', self.source)
        stream = events.Stream()
        watcher.streams.add(stream)
        _, watcher.digest = hub._read(watcher)
        self.now += 1
        state = {'phase': 'unknown', **self.read({'phase': 'unknown', 'runtimeKnown': False})}
        hub._read(watcher)
        self.assertFalse(stream.observation['runtimeObservation']['known'])

    def test_poll_raw_phase_matches_a_newer_selected_read(self):
        import server
        host = {'name': 'pc', 'label': 'PC', 'local': False, 'platform': 'windows'}
        board = None
        def runner(*args):
            board.runtime_observations.accept(self.source, self.working,
                                               board.runtime_observations.begin(), 90)
            return {'codex': [{**self.source, 'phase': 'closed', 'runtimeKnown': True}], 'claude': []}
        board = server.Board([host], 120, runner=runner, live=False)
        board.poll(host)
        row = board.state['pc']['data']['codex'][0]
        self.assertEqual(row['phase'], 'working')
        self.assertEqual(row['runtimeObservation']['phase'], row['phase'])

    def test_first_partial_failure_keeps_available_records_as_unconfirmed(self):
        import server
        host = {'name': 'pc', 'label': 'PC', 'local': False, 'platform': 'windows'}
        board = server.Board([host], 120, runner=lambda *_: {
            'codex': [{**self.source, **self.working}], 'claude': [], 'sourceStatus': {'codex': {'ok': False}}}, live=False)
        board.clock = lambda: self.now
        board.poll(host)
        row = board.state['pc']['data']['codex'][0]
        self.assertEqual(row['id'], self.source['id'])
        self.assertEqual(row['phase'], 'unknown')
        self.assertFalse(row['runtimeKnown'])
        self.assertFalse(row['runtimeObservation']['known'])
        self.assertEqual(row['runtimeObservation']['expiresAt'], self.now)


class MacObservationTest(unittest.TestCase):
    def test_real_unchanged_adapter_read_gets_token_but_same_signature(self):
        import collector
        watch = collector.Watch(None, None)
        watch.publish('claude', [{'id': 'one', 'phase': 'idle'}], [], ok=True)
        watch.publish('tmux', [], [], ok=True)
        a = watch.compose()
        watch.publish('claude', [{'id': 'one', 'phase': 'idle'}], [], ok=True)
        b = watch.compose()
        self.assertNotEqual(a['claude'][0]['runtimeReadToken'], b['claude'][0]['runtimeReadToken'])
        self.assertEqual(collector.watch_signatures(a), collector.watch_signatures(b))

    def test_only_actual_read_renews_and_body_revision_does_not_announce_semantic_change(self):
        from types import SimpleNamespace
        import server
        now = [1000]
        host = {'name': 'mac', 'label': 'Mac', 'local': True}
        board = server.Board([host], 120, runner=lambda *_: {}, live=False)
        board.clock = lambda: now[0]
        stream = SimpleNamespace(name='mac', stopping=False, delivered=False)
        board.streams['mac'] = stream
        row = {'id': 'worker', 'cwd': '/work', 'phase': 'working', 'runtimeReadToken': 'a' * 16 + ':1'}
        data = {'claude': [row], 'codex': [], 'sourceStatus': {'claude': {'ok': True}}}
        board.apply_live(stream, data)
        version, revision = board.version, board.observation_revision
        first = board.state['mac']['data']['claude'][0]['runtimeObservation']
        now[0] += 30
        board.apply_live(stream, data)
        self.assertEqual(board.state['mac']['data']['claude'][0]['runtimeObservation'], first)
        self.assertEqual(board.observation_revision, revision)
        fresh = {**data, 'claude': [{**row, 'runtimeReadToken': 'a' * 16 + ':2'}]}
        board.apply_live(stream, fresh)
        new = board.state['mac']['data']['claude'][0]['runtimeObservation']
        self.assertGreater(new['expiresAt'], first['expiresAt'])
        self.assertGreater(board.observation_revision, revision)
        self.assertEqual(board.version, version)
        failed = {**data, 'claude': [], 'sourceStatus': {'claude': {'ok': False}}}
        now[0] += 30
        board.apply_live(stream, failed)
        preserved = board.state['mac']['data']['claude'][0]
        self.assertFalse(preserved['runtimeKnown'])
        self.assertEqual(preserved['lastKnown']['expiresAt'], new['expiresAt'])

    def test_offline_and_legacy_never_claim_new_observation(self):
        import server
        from types import SimpleNamespace
        host = {'name': 'mac', 'label': 'Mac', 'local': True}
        board = server.Board([host], 120, runner=lambda *_: {}, live=False)
        stream = SimpleNamespace(name='mac', stopping=False, delivered=False)
        board.streams['mac'] = stream
        board.apply_live(stream, {'codex': [{'id': 'old', 'home': '.codex', 'phase': 'idle'}], 'claude': []})
        self.assertNotIn('runtimeObservation', board.state['mac']['data']['codex'][0])
        self.assertFalse(obs.native_result({'phase': 'closed', 'appServer': 'off'}, 'codex')['runtimeKnown'])


class ChatObservationTest(unittest.TestCase):
    def setUp(self):
        import chat
        import server
        from test_workflow import FakeTerminals
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        config = patch.object(server, 'CONFIG_DIR', Path(self.temp.name))
        config.start()
        self.addCleanup(config.stop)
        self.host = {'name': 'pc', 'label': 'PC', 'local': False, 'platform': 'windows'}
        self.source = {'host': 'pc', 'agent': 'codex', 'home': '.codex',
                       'id': '019aaaaa-0000-7000-8000-000000000009', 'cwd': 'C:\\fixture', 'phase': 'closed', 'runtimeKnown': True}
        self.reads = 0
        def runner(host, args, timeout):
            if args[0] == 'snapshot':
                return {'codex': [self.source], 'claude': []}
            self.reads += 1
            return {'phase': 'unknown', 'runtimeKnown': False, 'messages': []}
        self.board = server.Board([self.host], 120, runner=runner, live=False)
        self.board.poll(self.host)
        self.flow = server.Workflow(self.board, FakeTerminals(), self.temp.name)
        self.service = chat.Chat(self.board, self.flow, self.temp.name)

    def test_rpc_fallback_never_renews_a_cached_saved_read(self):
        import time
        key = ('pc', 'codex', self.source['id'], '.codex')
        self.board.reads[key] = (time.time(), {'phase': 'working', 'runtimeKnown': True, 'messages': []})
        with patch.object(self.service, 'rpc', side_effect=RuntimeError('fixture disconnected')):
            result = self.service.state({'source': self.source})
        self.assertEqual(self.reads, 1)
        self.assertFalse(result['runtimeKnown'])
        self.assertFalse(result['capability']['supported'])

    def test_older_rpc_matches_newer_phase_but_never_keeps_old_input_capability(self):
        def old_read(*args, **kwargs):
            self.board.runtime_observations.accept(self.source,
                {'phase': 'working', 'runtimeKnown': True, 'runtimeReason': 'new reason'},
                self.board.runtime_observations.begin(), 90)
            return {'phase': 'idle', 'runtimeKnown': True, 'messages': [], 'capabilities': {'canSend': True}}
        with patch.object(self.service, 'rpc', side_effect=old_read):
            result = self.service.state({'source': self.source})
        self.assertEqual(result['phase'], 'working')
        self.assertEqual(result['runtimeReason'], 'new reason')
        self.assertFalse(result['capability']['supported'])

    def test_windows_missing_certainty_is_unknown_and_readonly(self):
        with patch.object(self.service, 'rpc', return_value={
                'phase': 'working', 'messages': [], 'capabilities': {'canSend': True}}):
            result = self.service.state({'source': self.source})
        self.assertEqual(result['phase'], 'unknown')
        self.assertFalse(result['runtimeKnown'])
        self.assertFalse(result['capability']['supported'])


if __name__ == '__main__':
    unittest.main()
