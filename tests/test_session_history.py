import copy
from contextlib import redirect_stdout
import gzip
import io
import json
import sqlite3
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import session_history as history
import history_native as native


class RetentionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.now = 100 * history.DAY
        self.source = {'host': 'fixture', 'agent': 'codex', 'home': '.codex',
                       'id': '11111111-1111-4111-8111-111111111111', 'cwd': '/fixture', 'title': '합성 대화'}
        self.export = {'messages': [{'role': 'user', 'text': '영상 검수', 'ts': 1},
                                    {'role': 'assistant', 'text': '검수 결과입니다.', 'ts': 2}],
                       'family': [self.source['id']], 'fingerprint': 'a' * 64,
                       'lastActivityAt': self.now - 20 * history.DAY, 'canDelete': True, 'sourceBytes': 90000}
        self.export['exportVersion'] = native.EXPORT_VERSION
        self.calls = []
        self.fail_delete = False
        def rpc(source, args):
            self.calls.append(copy.deepcopy(args))
            if args['action'] == 'export':
                return copy.deepcopy(self.export)
            if self.fail_delete:
                raise RuntimeError('response lost')
            return {'confirmed': True, 'family': self.export['family'], 'reclaimedBytes': None}
        self.store = history.Histories(self.root, rpc, clock=lambda: self.now)
        self.key = history.key(self.source)

    def tearDown(self):
        self.temp.cleanup()

    def sweep(self, protected=None):
        self.store.sweep([self.source], protected or set())

    def row(self):
        return self.store.status()['records'][0]

    def test_verified_whole_text_then_full_grace_and_restart(self):
        self.export['messages'] = [{'role': 'user' if i % 2 == 0 else 'assistant',
                                    'text': str(i) + ' · ' + '대화' * 5000, 'ts': i} for i in range(204)]
        self.sweep()
        self.assertEqual(self.row()['state'], 'pending')
        self.assertNotIn('delete', [c['action'] for c in self.calls])
        restored = history.Histories(self.root, self.store.rpc, clock=lambda: self.now)
        messages, offset = [], 0
        while offset is not None:
            page = restored.conversation(self.key, offset)
            messages.extend(page['messages']); offset = page['nextOffset']
        self.assertEqual([m['text'] for m in messages], [m['text'] for m in self.export['messages']])
        self.now += 7 * history.DAY - 1
        self.sweep(); self.assertEqual(self.row()['state'], 'pending')
        self.now += 2
        self.sweep(); self.assertEqual(self.row()['state'], 'deleted')
        self.assertIsNone(self.row()['reclaimedBytes'])
        self.sweep(); self.assertEqual([c['action'] for c in self.calls].count('delete'), 1)

    def test_changed_history_restarts_the_grace_and_preserves_updated_text(self):
        self.sweep(); first = self.row()['deleteAfter']
        self.now += 8 * history.DAY
        self.export['fingerprint'] = 'b' * 64
        self.export['messages'].append({'role': 'user', 'text': '새 요청', 'ts': 3})
        self.sweep()
        self.assertGreater(self.row()['deleteAfter'], first)
        self.assertEqual(self.store.conversation(self.key)['messages'][-1]['text'], '새 요청')
        self.assertNotIn('delete', [c['action'] for c in self.calls])

    def test_reuse_and_child_protection_cancel_grace(self):
        child = '22222222-2222-4222-8222-222222222222'
        self.export['family'].append(child)
        self.sweep(); self.now += 8 * history.DAY
        self.sweep({('fixture', 'codex', '.codex', child)})
        self.assertEqual(self.row()['state'], 'waiting')
        self.sweep()
        self.assertEqual(self.row()['deleteAfter'], self.now + 7 * history.DAY)
        self.export['lastActivityAt'] = self.now
        self.sweep(); self.assertEqual(self.row()['state'], 'waiting')
        self.assertNotIn('delete', [c['action'] for c in self.calls])

    def test_manual_preservation_and_disabled_policy_block_deletion(self):
        self.sweep(); self.store.preserve(self.key, True)
        self.now += 100 * history.DAY
        self.sweep(); self.assertEqual(self.row()['state'], 'preserved')
        self.store.preserve(self.key, False)
        self.assertEqual(self.row()['deleteAfter'], self.now + 7 * history.DAY)
        self.store.policy({'enabled': False, 'idleDays': 14, 'graceDays': 7})
        before = len(self.calls); self.sweep(); self.assertEqual(len(self.calls), before)

    def test_preserved_child_record_blocks_parent_delete(self):
        child = {**self.source, 'id': '22222222-2222-4222-8222-222222222222'}
        self.store.sweep([child], set())
        self.store.preserve(history.key(child), True)
        self.export['family'].append(child['id'])
        self.now += 8 * history.DAY
        self.sweep()
        self.assertNotIn('delete', [c['action'] for c in self.calls])

    def test_failed_summary_or_archive_verification_never_deletes(self):
        self.export['messages'] = [{'role': 'user', 'text': '아직 답변 없음'}]
        self.sweep(); self.assertEqual(self.row()['state'], 'blocked')
        self.export['messages'].append({'role': 'assistant', 'text': '답변'})
        self.sweep()
        (self.store.root / (self.key + '.json.gz')).write_bytes(b'broken')
        self.now += 8 * history.DAY
        self.sweep()
        self.assertNotIn('delete', [c['action'] for c in self.calls])

    def test_delete_intent_is_persisted_and_response_loss_is_not_replayed(self):
        self.sweep(); self.now += 8 * history.DAY; self.fail_delete = True
        self.sweep(); self.assertEqual(self.row()['state'], 'uncertain')
        self.sweep()
        self.store.preserve(self.key, False)
        self.sweep()
        self.assertEqual([c['action'] for c in self.calls].count('delete'), 1)

    def test_no_binary_even_from_remote_text_and_no_workspace_files(self):
        binary = 'A' * 2048
        self.export['messages'][0]['text'] += '\ndata:image/png;base64,' + binary
        self.export['messages'][1]['image'] = binary
        self.sweep()
        raw = gzip.decompress((self.store.root / (self.key + '.json.gz')).read_bytes()).decode()
        self.assertNotIn(binary, raw)
        self.assertNotIn('image', self.store.conversation(self.key)['messages'][1])
        self.assertEqual((self.store.root / 'journal.json').stat().st_mode & 0o777, 0o600)

    def test_summary_uses_parent_conversation_and_not_a_childs_last_answer(self):
        self.export['messages'].extend([{'role': 'user', 'text': '하위 요청', 'threadId': 'child'},
                                        {'role': 'assistant', 'text': '하위의 다른 결과', 'threadId': 'child'}])
        self.sweep()
        self.assertEqual(self.row()['summary']['lastAnswer'], '검수 결과입니다.')
        self.assertEqual(self.row()['summary']['messageCount'], 4)

    def test_claude_history_is_archived_without_unsafe_delete(self):
        self.source.update(agent='claude', home=None)
        self.export.update(canDelete=False, reason='원본 유지')
        self.key = history.key(self.source)
        self.sweep(); self.now += 100 * history.DAY; self.sweep()
        self.assertEqual(self.row()['state'], 'archived')
        self.assertNotIn('delete', [c['action'] for c in self.calls])

    def test_paths_policy_and_symlinks_are_rejected(self):
        for value in ({'enabled': True, 'idleDays': 0, 'graceDays': 7},
                      {'enabled': True, 'idleDays': True, 'graceDays': 7}):
            with self.assertRaises(ValueError): self.store.policy(value)
        with self.assertRaises(ValueError): self.store.conversation('../outside')
        self.store.root.symlink_to(self.root / 'outside')
        with self.assertRaises(ValueError): self.store.status()

    def test_preserved_and_uncertain_rows_do_not_take_the_eight_slots(self):
        original = self.store.rpc
        def rpc(source, args):
            return {**original(source, args), 'family': [source['id']]}
        self.store.rpc = rpc
        skipped = []
        for i in range(8):
            source = {**self.source, 'id': 'preserved-' + str(i)}
            self.store.sweep([source], set())
            self.store.preserve(history.key(source), True)
            skipped.append(source)
            self.now += 1
        self.sweep()
        self.now += 8 * history.DAY
        self.store.sweep(skipped + [self.source], set())
        target = next(r for r in self.store.status()['records'] if r['key'] == self.key)
        self.assertEqual(target['state'], 'deleted')
        self.assertEqual([c['action'] for c in self.calls].count('delete'), 1)
        with self.store.storage():
            data = self.store._load()
            for source in skipped:
                data['records'][history.key(source)].update(state='uncertain', preserved=False)
            self.store._write(data)
        before = len(self.calls)
        fresh = {**self.source, 'id': 'fresh'}
        self.store.sweep(skipped + [fresh], set())
        self.assertEqual(len(self.calls), before + 1)

    def test_preserved_family_blocks_a_new_child_without_starving_other_records(self):
        child = {**self.source, 'id': 'child'}
        self.export['family'].append('child')
        self.sweep(); self.store.preserve(self.key, True)
        self.store.sweep([child], set())
        row = next(r for r in self.store.status()['records'] if r['key'] == history.key(child))
        self.assertEqual(row['state'], 'waiting')
        self.assertEqual(row['createdAt'], None)
        with self.store.storage():
            self.assertEqual(self.store._load()['records'][history.key(child)]['attemptedAt'], self.now)

    def test_policy_changed_during_export_controls_new_grace_and_eligibility(self):
        original_rpc = self.store.rpc
        for policy, expected in [({'enabled': True, 'idleDays': 14, 'graceDays': 30}, 'pending'),
                                 ({'enabled': True, 'idleDays': 365, 'graceDays': 7}, 'waiting'),
                                 ({'enabled': False, 'idleDays': 14, 'graceDays': 7}, None)]:
            with self.subTest(policy=policy), tempfile.TemporaryDirectory() as directory:
                def rpc(source, args):
                    if args['action'] == 'export':
                        store.policy(policy)
                    return original_rpc(source, args)
                store = history.Histories(Path(directory).resolve(), rpc, clock=lambda: self.now)
                store.sweep([self.source], set())
                rows = store.status()['records']
                self.assertEqual(rows[0]['state'] if rows else None, expected)
                if expected == 'pending':
                    self.assertEqual(rows[0]['deleteAfter'], self.now + 30 * history.DAY)
                    self.now += 8 * history.DAY
                    store.sweep([self.source], set())
                    self.assertEqual(store.status()['records'][0]['state'], 'pending')

    def test_extractor_upgrade_rearchives_then_restarts_grace(self):
        self.sweep()
        with self.store.storage():
            data = self.store._load(); data['records'][self.key].pop('exportVersion')
            self.store._write(data)
        self.now += 8 * history.DAY
        self.export['messages'].append({'role': 'assistant', 'text': '이전에 빠졌던 리뷰 결과'})
        self.sweep()
        self.assertEqual(self.row()['state'], 'pending')
        self.assertEqual(self.row()['deleteAfter'], self.now + 7 * history.DAY)
        self.assertEqual(self.store.conversation(self.key)['messages'][-1]['text'], '이전에 빠졌던 리뷰 결과')
        self.assertNotIn('delete', [c['action'] for c in self.calls])
        self.export.pop('exportVersion')
        self.now += 8 * history.DAY; self.sweep()
        self.assertEqual(self.row()['state'], 'blocked')
        self.assertNotIn('delete', [c['action'] for c in self.calls])

    def test_proven_pre_delete_refusal_retries_only_after_a_new_full_grace(self):
        self.sweep(); self.now += 8 * history.DAY
        original = self.store.rpc
        def refused(source, args):
            if args['action'] == 'delete':
                raise native.DeleteRefused('fixture pre-dispatch refusal')
            return original(source, args)
        self.store.rpc = refused; self.sweep()
        self.assertEqual(self.row()['state'], 'waiting')
        self.assertIsNone(self.row()['deleteAfter'])
        self.store.rpc = original; self.sweep()
        self.assertEqual(self.row()['state'], 'pending')
        self.assertEqual(self.row()['deleteAfter'], self.now + 7 * history.DAY)
        self.now += 8 * history.DAY; self.sweep()
        self.assertEqual(self.row()['state'], 'deleted')

    def test_preserve_release_never_claims_an_unverified_archive(self):
        self.export['messages'] = []
        self.sweep(); self.assertEqual(self.row()['state'], 'blocked')
        self.store.preserve(self.key, True); self.store.preserve(self.key, False)
        self.assertEqual(self.row()['state'], 'blocked')
        self.assertIsNone(self.row()['archiveBytes'])
        self.export['messages'] = [{'role': 'user', 'text': 'request'}, {'role': 'assistant', 'text': 'answer'}]
        self.sweep()
        (self.store.root / (self.key + '.json.gz')).write_bytes(b'broken')
        self.store.preserve(self.key, True); self.store.preserve(self.key, False)
        self.assertEqual(self.row()['state'], 'blocked')
        self.sweep()
        self.assertEqual(self.row()['state'], 'pending')
        self.assertEqual(len(self.store.conversation(self.key)['messages']), 2)

    def test_unexpected_collection_error_does_not_kill_future_sweeps(self):
        attempts = []
        self.store.stop = SimpleNamespace(wait=Mock(side_effect=[False, False, True]))
        def collect():
            attempts.append(1)
            if len(attempts) == 1: raise AttributeError('private fixture detail must not be exposed')
            self.assertNotIn('private fixture', self.store.error)
            return [], set()
        with patch.object(history.threading, 'Thread') as thread:
            self.store.start(collect)
            thread.call_args.kwargs['target']()
        self.assertEqual(len(attempts), 2)
        self.assertIsNone(self.store.error)


class NativeHistoryTest(unittest.TestCase):
    def codex_client(self):
        sid = '11111111-1111-4111-8111-111111111111'
        class Client:
            initialized = {'userAgent': 'codex-cli 0.163.0'}
            calls = []
            status = 'notLoaded'
            answer = '전체 답변' * 4000
            queued = []
            goal = None
            children = []
            pinned = []
            parent = None
            extra_items = []
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def call(self, method, params):
                self.calls.append((method, copy.deepcopy(params)))
                if method == 'thread/read':
                    return {'thread': {'id': params['threadId'], 'status': {'type': self.status}, 'updatedAt': 1,
                                       'parentThreadId': self.parent,
                                       'isPinned': any(row['id'] == params['threadId'] for row in self.pinned)}}
                if method == 'thread/list':
                    if params.get('isPinned'): return {'data': self.pinned}
                    return {'data': self.children if not params['archived'] else []}
                if method == 'thread/turns/list':
                    return {'data': [{'status': 'completed', 'startedAt': 1, 'completedAt': 2, 'itemsView': 'full',
                      'items': [{'id': 'user', 'type': 'userMessage', 'content': [{'type': 'text', 'text': '요청'},
                          {'type': 'image', 'url': 'data:image/png;base64,SECRET_IMAGE'}]},
                                {'id': 'answer', 'type': 'agentMessage', 'text': self.answer}] + self.extra_items}]}
                if method == 'thread/queue/list': return {'data': self.queued}
                if method == 'thread/goal/get': return {'goal': self.goal}
                if method == 'thread/loaded/list': return {'data': [] if self.status == 'notLoaded' else [sid]}
                if method == 'thread/delete': return {}
                raise AssertionError(method)
        return {'agent': 'codex', 'id': sid, 'home': '.codex'}, Client()

    def test_codex_full_text_native_delete_and_activity_guards(self):
        source, client = self.codex_client()
        with patch.object(native.native, '_client', return_value=client):
            result = native.handle({'action': 'export', 'source': source})
            self.assertEqual(result['messages'][-1]['text'], client.answer)
            self.assertNotIn('SECRET_IMAGE', json.dumps(result))
            self.assertTrue(result['canDelete'])
            self.assertTrue(all(p['sourceKinds'] == native.SOURCE_KINDS for m, p in client.calls if m == 'thread/list'))
            original = result['fingerprint']
            for attr, value in [('queued', [{'id': 'queued'}]), ('goal', {'status': 'active'}), ('goal', {'status': 'paused'}),
                                ('status', 'idle'), ('pinned', [{'id': source['id']}])]:
                old = getattr(client, attr); setattr(client, attr, value)
                with self.subTest(guard=attr), self.assertRaises(ValueError):
                    native.handle({'action': 'delete', 'source': source, 'fingerprint': original})
                setattr(client, attr, old)
            client.answer += '변경'
            with self.assertRaises(ValueError):
                native.handle({'action': 'delete', 'source': source, 'fingerprint': original})
            result = native.handle({'action': 'export', 'source': source})
            done = native.handle({'action': 'delete', 'source': source, 'fingerprint': result['fingerprint']})
            self.assertTrue(done['confirmed'])
            self.assertEqual([m for m, p in client.calls].count('thread/delete'), 1)

    def test_codex_unordered_descendants_and_unrelated_thread_refused(self):
        source, client = self.codex_client()
        client.children = [{'id': 'grandchild', 'parentThreadId': 'child'},
                           {'id': 'child', 'parentThreadId': source['id']}]
        with patch.object(native.native, '_client', return_value=client):
            result = native.handle({'action': 'export', 'source': source})
            self.assertEqual(set(result['family']), {source['id'], 'child', 'grandchild'})
            client.children.append({'id': 'foreign', 'parentThreadId': 'other'})
            with self.assertRaises(ValueError): native.handle({'action': 'export', 'source': source})

    def test_unverified_native_version_exports_but_never_deletes(self):
        source, client = self.codex_client()
        client.initialized = {'userAgent': 'codex-cli 9.9.9'}
        with patch.object(native.native, '_client', return_value=client):
            result = native.handle({'action': 'export', 'source': source})
            self.assertTrue(result['messages'])
            self.assertFalse(result['canDelete'])
            with self.assertRaises(ValueError):
                native.handle({'action': 'delete', 'source': source, 'fingerprint': result['fingerprint']})
        self.assertNotIn('thread/delete', [m for m, p in client.calls])

    def test_reviews_questions_and_user_answers_are_kept_but_unknown_items_refuse_export(self):
        source, client = self.codex_client()
        client.extra_items = [
            {'type': 'enteredReviewMode', 'id': 'r1', 'review': '리뷰 요청'},
            {'type': 'exitedReviewMode', 'id': 'r2', 'review': '전체 리뷰 결과'},
            {'type': 'agentMessage', 'id': 'q1', 'text': '질문입니다.',
             'questions': [{'title': '어떤 방식?', 'options': ['첫째', '둘째']}]},
            {'type': 'functionCallOutput', 'id': 'a1', 'name': 'request_user_input', 'output': '둘째로 해줘'},
            {'type': 'functionCallOutput', 'id': 'tool', 'name': 'exec', 'output': 'omit tool output'},
            {'type': 'reasoning', 'id': 'hidden', 'content': ['omit reasoning']},
        ]
        with patch.object(native.native, '_client', return_value=client):
            result = native.handle({'action': 'export', 'source': source})
            dialogue = '\n'.join(m['text'] for m in result['messages'])
            for text in ['리뷰 요청', '전체 리뷰 결과', '어떤 방식?', '첫째', '둘째로 해줘']:
                self.assertIn(text, dialogue)
            self.assertTrue(result['canDelete'])
            self.assertNotIn('omit', dialogue)
            client.extra_items.append({'type': 'futureVisibleMessage', 'text': 'must not lose'})
            with self.assertRaises(ValueError): native.handle({'action': 'export', 'source': source})
            with self.assertRaises(native.DeleteRefused):
                native.handle({'action': 'delete', 'source': source, 'fingerprint': result['fingerprint']})
        self.assertNotIn('thread/delete', [m for m, p in client.calls])

    def test_parent_or_descendant_family_is_export_only_until_cascade_is_verified(self):
        source, client = self.codex_client()
        with patch.object(native.native, '_client', return_value=client):
            for parent, children in [('parent', []), (None, [{'id': 'child', 'parentThreadId': source['id']}])]:
                client.parent, client.children = parent, children
                result = native.handle({'action': 'export', 'source': source})
                self.assertFalse(result['canDelete'])
                self.assertTrue(result['messages'])
                with self.assertRaises(native.DeleteRefused):
                    native.handle({'action': 'delete', 'source': source, 'fingerprint': result['fingerprint']})

    def test_native_refusal_proof_stops_at_the_destructive_rpc_boundary(self):
        source, client = self.codex_client()
        with patch.object(native.native, '_client', return_value=client):
            result = native.handle({'action': 'export', 'source': source})
            original = client.call
            for failure_method, exception in [('thread/read', native.collector.RpcError('read failed')),
                                              ('thread/loaded/list', AttributeError('new native shape')),
                                              ('thread/delete', OSError('response lost after dispatch'))]:
                def call(method, params):
                    if method == failure_method: raise exception
                    return original(method, params)
                client.call = call
                expected = OSError if failure_method == 'thread/delete' else native.DeleteRefused
                with self.assertRaises(expected) as raised:
                    native.handle({'action': 'delete', 'source': source, 'fingerprint': result['fingerprint']})
                if failure_method == 'thread/delete':
                    self.assertNotIsInstance(raised.exception, native.DeleteRefused)

    def test_scan_disagreement_blocks_delete_and_invalid_selector_is_proven_refusal(self):
        source, client = self.codex_client()
        original = client.call
        with patch.object(native.native, '_client', return_value=client):
            result = native.handle({'action': 'export', 'source': source})
            def call(method, params):
                if method == 'thread/list' and params.get('useStateDbOnly') is False and not params['archived']:
                    return {'data': [{'id': 'hidden-child', 'parentThreadId': source['id']}]}
                return original(method, params)
            client.call = call
            with self.assertRaises(native.DeleteRefused):
                native.handle({'action': 'delete', 'source': source, 'fingerprint': result['fingerprint']})
        self.assertNotIn('thread/delete', [m for m, p in client.calls])
        with self.assertRaises(native.DeleteRefused): native.handle({'action': 'delete', 'source': None})

    def test_helper_serializes_pre_dispatch_refusal_proof(self):
        import transfer_worker
        output = io.StringIO()
        with patch.object(transfer_worker, 'rpc', side_effect=native.DeleteRefused('fixture')):
            with patch('sys.argv', ['fixture.pyz']), patch('sys.stdin', SimpleNamespace(buffer=io.BytesIO(b'{}'))):
                with redirect_stdout(output): transfer_worker._main()
        reply = json.loads(output.getvalue())
        self.assertFalse(reply['ok'])
        self.assertEqual(reply['stage'], native.DeleteRefused.transfer_stage)

    def test_codex_all_turn_pages_are_exported_in_order_and_short_items_are_refused(self):
        source, client = self.codex_client()
        original = client.call
        def call(method, params):
            response = original(method, params)
            if method == 'thread/turns/list':
                page = int(params.get('cursor', '0'))
                response['data'][0]['items'] = [{'id': str(page), 'type': 'agentMessage', 'text': 'page-' + str(page)}]
                response['nextCursor'] = str(page + 1) if page < 2 else None
            return response
        client.call = call
        with patch.object(native.native, '_client', return_value=client):
            result = native.handle({'action': 'export', 'source': source})
            self.assertEqual([m['text'] for m in result['messages']], ['page-0', 'page-1', 'page-2'])
            def short(method, params):
                response = call(method, params)
                if method == 'thread/turns/list': response['data'][0]['itemsView'] = 'notLoaded'
                return response
            client.call = short
            with self.assertRaises(ValueError): native.handle({'action': 'export', 'source': source})

    def test_legacy_fallback_also_refuses_turns_without_full_items(self):
        source, client = self.codex_client()
        original = client.call
        def call(method, params):
            if method == 'thread/turns/list': raise native.collector.RpcError('fixture legacy')
            result = original(method, params)
            if method == 'thread/read' and params.get('includeTurns'):
                result['thread']['turns'] = [{'id': 'partial', 'itemsView': 'notLoaded', 'items': []}]
            return result
        client.call = call
        with patch.object(native.native, '_client', return_value=client), patch.object(native.collector, 'paging_unsupported', return_value=True):
            with self.assertRaises(ValueError): native.handle({'action': 'export', 'source': source})

    def test_archived_native_stores_must_confirm_no_queue_or_unfinished_goal(self):
        source, _ = self.codex_client()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve(); account = root / '.codex'; account.mkdir()
            schemas = [('state_5.sqlite', 'CREATE TABLE threads (id TEXT, archived INTEGER)',
                        'INSERT INTO threads VALUES (?,1)'),
                       ('queue_1.sqlite', 'CREATE TABLE queued_items (thread_id TEXT)', None),
                       ('goals_1.sqlite', 'CREATE TABLE thread_goals (thread_id TEXT, status TEXT)', None)]
            for filename, schema, insert in schemas:
                with sqlite3.connect(account / filename) as db:
                    db.execute(schema)
                    if insert: db.execute(insert, (source['id'],))
            self.assertTrue(native._archived_clear(source, root, source['id']))
            with sqlite3.connect(account / 'goals_1.sqlite') as db:
                db.execute('INSERT INTO thread_goals VALUES (?,?)', (source['id'], 'paused'))
            self.assertFalse(native._archived_clear(source, root, source['id']))
            with sqlite3.connect(account / 'goals_1.sqlite') as db: db.execute('DELETE FROM thread_goals')
            with sqlite3.connect(account / 'queue_1.sqlite') as db:
                db.execute('INSERT INTO queued_items VALUES (?)', (source['id'],))
            self.assertFalse(native._archived_clear(source, root, source['id']))
            (account / 'queue_1.sqlite').unlink()
            self.assertFalse(native._archived_clear(source, root, source['id']))

    def test_claude_full_transcript_and_children_omit_all_attachment_payloads(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            sid = '11111111-1111-4111-8111-111111111111'
            project = root / '.claude/projects/fixture'; project.mkdir(parents=True)
            path = project / (sid + '.jsonl')
            rows = [{'type': 'user' if i % 2 == 0 else 'assistant', 'timestamp': '2026-01-01T00:00:00Z',
                     'message': {'content': [{'type': 'text', 'text': str(i) + '원문' * 2000}]}} for i in range(202)]
            rows[0]['message']['content'].append({'type': 'image', 'source': {'type': 'base64', 'data': 'SECRET_IMAGE_BYTES'}})
            path.write_text('\n'.join(json.dumps(r, ensure_ascii=False) for r in rows) + '\n')
            children = project / sid / 'subagents'; children.mkdir(parents=True)
            (children / 'agent-fixture.jsonl').write_text(json.dumps(rows[1]))
            result = native.handle({'action': 'export', 'source': {'agent': 'claude', 'id': sid, 'sessionId': sid}}, home=root)
            self.assertEqual(len(result['messages']), 203)
            self.assertIn('agent-fixture', result['family'])
            self.assertIn('[첨부: image]', result['messages'][0]['text'])
            self.assertNotIn('SECRET_IMAGE_BYTES', json.dumps(result))
            self.assertFalse(result['canDelete'])
            with self.assertRaises(ValueError):
                native.handle({'action': 'delete', 'source': {'agent': 'claude', 'id': sid}, 'fingerprint': result['fingerprint']}, home=root)

    def test_unknown_text_shape_and_malformed_history_are_not_silently_dropped(self):
        with self.assertRaises(ValueError): native.blocks([{'type': 'future_text', 'value': 'must be preserved'}])
        self.assertEqual(native.blocks('x' * 5000), 'x' * 5000, 'long ordinary text must never be guessed to be binary')
        self.assertEqual(native.blocks([{'type': 'document', 'filename': 'fixture.pdf', 'source': {'data': 'private'}}]),
                         '[첨부: document · fixture.pdf]')


if __name__ == '__main__':
    unittest.main()
