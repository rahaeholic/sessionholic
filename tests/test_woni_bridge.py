import io
import http.server
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import woni_bridge


class Response:
    def __init__(self, value):
        self.value = json.dumps(value).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self, *args):
        return self.value


class WoniBridgeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.woni = self.home / '.local/share/ryan-one'
        self.woni.mkdir(parents=True)
        (self.woni / 'config.json').write_text(json.dumps({'token': 'test-credential'}))
        # A request that passes the checks must be answered by a test's fake, never by the real Woni server.
        guard = patch.object(woni_bridge, 'urlopen', side_effect=AssertionError('real Woni call'))
        guard.start(); self.addCleanup(guard.stop)

    def tearDown(self):
        self.temp.cleanup()

    def test_only_chat_and_activity_are_returned_from_dashboard(self):
        with patch.object(woni_bridge, 'urlopen', return_value=Response({'chats': [{'id': 'a'}], 'activity': {'running': 1}, 'actions': ['private']})) as urlopen:
            result = woni_bridge.local_call(home=self.home)
        self.assertEqual(result, {'enabled': True, 'chats': [{'id': 'a', 'attachments': []}], 'activity': {'running': 1}})
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, 'http://127.0.0.1:18772/v1/state')
        self.assertNotIn('test-credential', json.dumps(result))

    def test_send_preserves_request_id_and_never_accepts_other_fields(self):
        data = {'text': '안녕', 'requestId': 'request-123456'}
        with patch.object(woni_bridge, 'urlopen', return_value=Response({'status': 'queued'})) as urlopen:
            woni_bridge.local_call(data, self.home)
        self.assertEqual(json.loads(urlopen.call_args.args[0].data), data)
        self.assertEqual(urlopen.call_args.args[0].get_header('X-woni'), '1')
        with self.assertRaises(ValueError):
            woni_bridge.validate({**data, 'reviewId': 'unexpected'})
        self.assertEqual(woni_bridge.validate({**data, 'attachments': ['20261106-012345abcdef']})['text'], '안녕')
        with self.assertRaises(ValueError):
            woni_bridge.validate({**data, 'attachments': ['../unsafe']})

    def test_reply_and_read_operations_are_narrowly_validated(self):
        data = {'text': '계속', 'requestId': 'request-123456', 'replyTo': {'id': 'chat-1', 'part': 'answer'}}
        with patch.object(woni_bridge, 'urlopen', return_value=Response({'status': 'queued'})) as urlopen:
            woni_bridge.local_call(data, self.home)
        self.assertEqual(json.loads(urlopen.call_args.args[0].data), data)
        for invalid in ({'id': '../x', 'part': 'answer'}, {'id': 'chat-1', 'part': 'other'}, {'id': 'chat-1', 'part': 'answer', 'extra': True}):
            with self.assertRaises(ValueError): woni_bridge.validate({**data, 'replyTo': invalid})
        with patch.object(woni_bridge, 'urlopen', return_value=Response({'id': 'chat-1', 'text': '질문', 'private': 'x'})):
            self.assertEqual(woni_bridge.local_call(home=self.home, operation='chat', query={'id': 'chat-1'}), {'id': 'chat-1', 'text': '질문', 'attachments': []})
        with self.assertRaises(ValueError): woni_bridge.local_call(home=self.home, operation='chat', query={'id': '../unsafe'})
        with self.assertRaises(ValueError): woni_bridge.local_call(home=self.home, operation='worker-result', query={'batch': 'bad', 'n': '1'})

    def test_shared_read_mark_is_passed_through_and_written_narrowly(self):
        # 원이앱에서 확인한 답이 하던일 원이에 새 답으로 계속 남지 않게, 원이 서버의 읽음 기준을 그대로 쓴다.
        with patch.object(woni_bridge, 'urlopen', return_value=Response({'chats': [], 'activity': {}, 'readThrough': 1794000000.5})):
            self.assertEqual(woni_bridge.local_call(home=self.home)['readThrough'], 1794000000.5)
        with patch.object(woni_bridge, 'urlopen', return_value=Response({'chats': [], 'activity': {}, 'readThrough': 'bad'})):
            self.assertNotIn('readThrough', woni_bridge.local_call(home=self.home))
        with patch.object(woni_bridge, 'urlopen', return_value=Response({'readThrough': 1794000001.0, 'private': 'x'})) as urlopen:
            self.assertEqual(woni_bridge.local_call({'through': 1794000001.0}, self.home, operation='read'), {'readThrough': 1794000001.0})
        request = urlopen.call_args.args[0]
        self.assertEqual((request.full_url, json.loads(request.data), request.get_header('X-woni')),
                         ('http://127.0.0.1:18772/v1/read', {'through': 1794000001.0}, '1'))
        for invalid in ({'through': True}, {'through': '1'}, {'through': -1}, {'through': 1, 'extra': 1}, {}):
            with self.assertRaises(ValueError): woni_bridge.local_call(invalid, self.home, operation='read')
        with self.assertRaises(ValueError): woni_bridge.local_call(home=self.home, operation='read')
        with self.assertRaises(ValueError): woni_bridge.local_call({'through': 1.0}, self.home, operation='activity')

    def test_activity_removes_unneeded_worker_details(self):
        value = {'running': [{'id': 'chat-1', 'text': '질문', 'steps': [{'tool': '읽기', 'private': 'x'}],
                  'batches': [{'id': '20261106-010101-abcd', 'tasks': [{'n': 1, 'title': '작업', 'status': 'ok', 'pid': 123}]}], 'secret': 'x'}], 'recent': []}
        with patch.object(woni_bridge, 'urlopen', return_value=Response(value)):
            result = woni_bridge.local_call(home=self.home, operation='activity')
        self.assertEqual(result['running'][0]['batches'][0]['tasks'], [{'n': 1, 'title': '작업', 'status': 'ok'}])
        self.assertEqual(result['running'][0]['steps'], [{'tool': '읽기'}])
        self.assertNotIn('secret', result['running'][0])

    def call(self, operation, value, query=None, data=None, status=None):
        if status:
            error = woni_bridge.HTTPError('http://127.0.0.1:18772', status, 'x', {}, io.BytesIO(json.dumps(value).encode()))
            self.addCleanup(error.close)
            patched = patch.object(woni_bridge, 'urlopen', side_effect=error)
        else:
            patched = patch.object(woni_bridge, 'urlopen', return_value=Response(value))
        with patched as urlopen:
            try:
                return woni_bridge.local_call(data, self.home, operation=operation, query=query), urlopen
            finally:
                self.last = urlopen

    def test_tasks_keep_only_listed_task_fields_and_counts(self):
        task = {'id': 'a' * 24, 'kind': 'delegated', 'title': '제목', 'project': 'P', 'due': '2026-11-12', 'assignee': '민수', 'attention': '확인 필요',
                'taskStatus': 'reported', 'previousStatus': 'open', 'stateEvent': 'xxxx:completion-reported', 'quote': '인용', 'requestQuote': '요청',
                'reportQuote': '보고', 'recordTitle': '녹취', 'recordedAt': '2026-11-10', 'counterpart': '상대', 'sourceId': 'src_1',
                'guessBasis': '근거', 'ownerGuess': '추정', 'archived': False,
                'autoBasis': {'quote': 'q', 'source': 's', 'at': 'a', 'reason': 'r', 'extra': 'x'},
                'candidates': [{'id': 'x'}], 'linkedSession': {'id': 'y'}, 'recordSummary': '요약', 'url': 'https://drive.google.com/x',
                'requestedBy': '나', 'guessName': 'g', 'source': 'slack', 'status': 'open', 'title2': 'x'}
        original = {'actions': [task, 'bad'], 'completedActions': [{'id': 'b' * 24, 'taskStatus': 'done', 'quote': None, 'archived': 'yes'}],
                    'actionCount': 6, 'delegatedCount': 10, 'unassignedCount': 91, 'followupCount': 1, 'dueCount': True, 'autoDoneCount': 2,
                    'completedActionCount': 282, 'archivedCount': 273, 'syncedAt': 1794269917.5, 'driveError': '',
                    'chats': [{'id': 'private'}], 'sessions': [1], 'working': [1], 'attention': [1], 'decisions': [1]}
        result, urlopen = self.call('tasks', original)
        request = urlopen.call_args.args[0]
        self.assertEqual((request.full_url, request.get_header('Authorization'), request.get_header('X-woni-task-roles')),
                         ('http://127.0.0.1:18772/v1/state', 'Bearer test-credential', '1'))
        self.assertEqual(set(result), {'actions', 'completedActions', 'archivedActions', 'counts', 'syncedAt', 'error'})
        for key in ('candidates', 'linkedSession', 'recordSummary', 'url', 'requestedBy', 'guessName', 'source', 'status', 'title2'):
            self.assertNotIn(key, result['actions'][0])
        self.assertEqual(result['actions'][0]['autoBasis'], {'quote': 'q', 'source': 's', 'at': 'a', 'reason': 'r'})
        self.assertEqual(len(result['actions']), 1)
        self.assertEqual(result['actions'][0]['stateEvent'], 'xxxx:completion-reported')
        self.assertEqual(result['completedActions'], [{'id': 'b' * 24, 'taskStatus': 'done'}])  # a missing or malformed field is left out, not guessed
        self.assertEqual(result['archivedActions'], [])
        self.assertEqual(result['counts'], {'mine': 6, 'delegated': 10, 'unassigned': 91, 'followup': 1, 'due': 0,
                                            'autoDone': 2, 'completed': 282, 'archived': 273})
        self.assertEqual((result['syncedAt'], result['error']), (1794269917.5, ''))
        self.assertNotIn('private', json.dumps(result))
        with self.assertRaises(ValueError): woni_bridge.local_call(home=self.home, operation='tasks', query={'x': '1'})
        with self.assertRaises(ValueError): woni_bridge.local_call({'text': '안녕', 'requestId': 'request-123456'}, self.home, operation='tasks')

    def test_notes_and_note_reads_are_bounded_and_filtered(self):
        value = {'items': [{'id': 'n1', 'title': '노트', 'recordedAt': '11월 10일', 'preview': '미리보기', 'evidence': [{'text': 'x'}]}, 3],
                 'total': 586, 'query': '재입고', 'nextOffset': 40, 'syncedAt': 1, 'error': '', 'audio': {'sessionId': 'drive'}}
        result, urlopen = self.call('notes', value, {'q': '재입고', 'offset': '40'})
        self.assertEqual(urlopen.call_args.args[0].full_url, 'http://127.0.0.1:18772/v1/notes?q=%EC%9E%AC%EC%9E%85%EA%B3%A0&offset=40')
        self.assertEqual(result, {'items': [{'id': 'n1', 'title': '노트', 'recordedAt': '11월 10일', 'preview': '미리보기'}],
                                  'total': 586, 'nextOffset': 40, 'query': '재입고', 'error': ''})
        self.assertEqual(self.call('notes', {'items': [], 'total': 0, 'nextOffset': None}, {'offset': 0})[0]['nextOffset'], None)
        for bad in ({'q': 'x' * 301}, {'offset': '-1'}, {'offset': '1000001'}, {'offset': '1.5'}, {'offset': True}, {'q': 'a\nb'}, {'id': 'x'}, {'q': ['a']}):
            with self.assertRaises(ValueError, msg=bad): woni_bridge.local_call(home=self.home, operation='notes', query=bad)
        self.assertEqual(woni_bridge.local_call.__defaults__[2], 'state')
        note = {'id': 'slack-1', 'title': '노트', 'recordedAt': 'r', 'text': '본문', 'nextOffset': None, 'revision': 'rev',
                'url': 'https://drive.google.com/file/d/f/view', 'actions': [1], 'currentActions': [1], 'summary': {}, 'decisions': [], 'audio': {'a': 1}}
        result, urlopen = self.call('note', note, {'id': 'slack-1'})
        self.assertEqual(urlopen.call_args.args[0].full_url, 'http://127.0.0.1:18772/v1/note?id=slack-1')
        self.assertEqual(result, {'id': 'slack-1', 'title': '노트', 'recordedAt': 'r', 'text': '본문', 'nextOffset': None,
                                  'revision': 'rev', 'url': 'https://drive.google.com/file/d/f/view'})
        self.assertNotIn('url', self.call('note', {**note, 'url': 'javascript:alert(1)'}, {'id': 'a.b:c_d-1'})[0])
        for bad in ({}, {'id': ''}, {'id': '../x'}, {'id': 'a' * 201}, {'id': 'a b'}, {'id': 'a', 'q': 'x'}, {'id': 'a', 'offset': 'x'}, {'id': 'a\n'}):
            with self.assertRaises(ValueError, msg=bad): woni_bridge.local_call(home=self.home, operation='note', query=bad)

    def test_transcript_modes_validate_query_and_drop_audio(self):
        row = {'id': 'e1', 'kind': 'transcript', 'label': '전사', 'text': '글', 'start': 3.5, 'end': 9, 'speaker': 'A', 'position': 4,
               'transcriptDoubt': True, 'company': 'c', 'project': 'p', 'segmentId': 's', 'scopeBasis': 'b'}
        page = {'sourceId': 's1', 'text': '원문', 'truncated': False, 'offset': 0, 'nextOffset': None, 'revision': 'r', 'label': '전사 원문', 'audio': {'x': 1}}
        result, _ = self.call('transcript', page, {'id': 's1', 'offset': '100000'})
        self.assertEqual(result, {key: page[key] for key in page if key != 'audio'})
        passages = {'sourceId': 's1', 'passages': [row], 'total': 1, 'nextOffset': None, 'query': '', 'label': '전사 원문', 'revision': 'r', 'audio': {'x': 1}}
        result, urlopen = self.call('transcript', passages, {'id': 's1', 'format': 'passages', 'q': '재입고', 'offset': '100'})
        self.assertIn('format=passages', urlopen.call_args.args[0].full_url)
        self.assertEqual(result['passages'], [{key: row[key] for key in ('id', 'kind', 'label', 'text', 'start', 'end', 'speaker', 'position', 'transcriptDoubt')}])
        self.assertNotIn('audio', result)
        evidence = {**row, 'sourceId': 's1', 'revision': 'r', 'audio': {'x': 1}, 'context': [row, 'bad']}
        result, urlopen = self.call('transcript', evidence, {'id': 's1', 'evidenceId': 'e1', 'context': '2'})
        keys = {'id', 'kind', 'label', 'text', 'start', 'end', 'speaker', 'transcriptDoubt'}
        self.assertEqual(set(result), keys | {'sourceId', 'revision', 'context'})
        self.assertEqual([set(item) for item in result['context']], [keys])
        self.assertEqual(set(self.call('transcript', row, {'id': 's1', 'evidenceId': 'e1', 'context': '0'})[0]), keys)
        for bad in ({}, {'id': 's1', 'format': 'text'}, {'id': 's1', 'q': 'x'}, {'id': 's1', 'context': '1'}, {'id': 's1', 'format': 'passages', 'context': '1'},
                    {'id': 's1', 'evidenceId': 'e1', 'context': '3'}, {'id': 's1', 'evidenceId': 'e1', 'offset': '0'},
                    {'id': 's1', 'evidenceId': 'e1', 'q': 'x'}, {'id': 's1', 'evidenceId': 'e1', 'format': 'passages'},
                    {'id': 's1', 'evidenceId': '../e'}, {'id': 's1', 'evidenceId': 'e' * 101}, {'id': 's1', 'other': '1'}):
            with self.assertRaises(ValueError, msg=bad): woni_bridge.local_call(home=self.home, operation='transcript', query=bad)

    def test_unavailable_slack_transcript_keeps_the_server_message(self):
        with self.assertRaises(ValueError) as caught:
            self.call('transcript', {'error': '원문 전사 파일이 없어요'}, {'id': 'slack-1'}, status=409)
        self.assertEqual(str(caught.exception), '원문 전사 파일이 없어요')

    def test_task_status_is_a_narrow_token_write_and_reply_is_filtered(self):
        data = {'actionId': 'a' * 24, 'status': 'done', 'expectedEvent': '', 'requestId': 'request-123456'}
        config = json.loads((self.woni / 'config.json').read_text())
        (self.woni / 'config.json').write_text(json.dumps({**config, 'readToken': 'read-only-credential'}))
        reply = {'actionId': 'a' * 24, 'taskStatus': 'done', 'stateEvent': 'woni:request-123456', 'result': 'saved', 'owner': {'kind': 'mine'}}
        result, urlopen = self.call('task-status', reply, data=data)
        request = urlopen.call_args.args[0]
        self.assertEqual((request.full_url, json.loads(request.data), request.get_header('X-woni'), request.get_header('Authorization')),
                         ('http://127.0.0.1:18772/v1/task-status', data, '1', 'Bearer test-credential'))
        self.assertEqual(result, {key: reply[key] for key in ('actionId', 'taskStatus', 'stateEvent', 'result')})
        # Reads use the read-only credential when there is one.
        self.assertEqual(self.call('notes', {'items': []})[1].call_args.args[0].get_header('Authorization'), 'Bearer read-only-credential')
        self.assertEqual(self.call('tasks', {})[1].call_args.args[0].get_header('Authorization'), 'Bearer read-only-credential')
        for status in ('open', 'reported'):
            self.call('task-status', reply, data={**data, 'status': status, 'expectedEvent': 'xxxx:completion-reported'})
        invalid = ({'status': 'paused'}, {'status': None}, {'actionId': '../x'}, {'actionId': ''}, {'actionId': 'a' * 101}, {'expectedEvent': 'x' * 301},
                   {'expectedEvent': 'a\nb'}, {'expectedEvent': None}, {'requestId': 'short'}, {'requestId': 'bad id 123456'}, {'requestId': 'a' * 101},
                   {'extra': 1})
        for change in invalid:
            with self.assertRaises(ValueError, msg=change): woni_bridge.local_call({**data, **change}, self.home, operation='task-status')
        with self.assertRaises(ValueError): woni_bridge.local_call({key: data[key] for key in ('actionId', 'status', 'requestId')}, self.home, operation='task-status')
        with self.assertRaises(ValueError): woni_bridge.local_call(home=self.home, operation='task-status')
        with self.assertRaises(ValueError): woni_bridge.local_call(data, self.home, operation='notes')
        with self.assertRaises(ValueError): woni_bridge.local_call(data, self.home, operation='task-status', query={'x': '1'})

    def test_task_status_conflict_is_a_refusal_but_unknown_outcomes_are_not(self):
        data = {'actionId': 'a' * 24, 'status': 'done', 'expectedEvent': '', 'requestId': 'request-123456'}
        with self.assertRaises(ValueError) as caught:
            self.call('task-status', {'error': '다른 곳에서 할 일 상태가 바뀌었어요. 새로고침 후 다시 선택해 주세요'}, data=data, status=409)
        self.assertIn('다른 곳에서', str(caught.exception))
        with self.assertRaises(RuntimeError): self.call('task-status', {'error': '처리 결과를 확인하지 못했어요'}, data=data, status=503)
        with self.assertRaises(RuntimeError): self.call('task-status', {'actionId': 'a' * 24}, data=data)
        with self.assertRaises(RuntimeError): self.call('task-status', {**{'actionId': 'a', 'taskStatus': 'done', 'stateEvent': ''}, 'result': 'maybe'}, data=data)
        with self.assertRaises(RuntimeError): self.call('task-status', ['x'], data=data)
        with patch.object(woni_bridge, 'urlopen', side_effect=woni_bridge.URLError('down')):
            with self.assertRaises(RuntimeError): woni_bridge.local_call(data, self.home, operation='task-status')

    def test_new_operations_work_through_a_remote_host(self):
        config = self.home / 'woni.json'
        config.write_text(json.dumps({'host': 'remote'})); config.chmod(0o600)
        bridge = woni_bridge.WoniBridge([{'name': 'remote', 'host': 'example.invalid'}], config, self.home)
        data = {'actionId': 'a' * 24, 'status': 'open', 'expectedEvent': '', 'requestId': 'request-123456'}
        seen = []
        def run(command, **kwargs):
            seen.append(kwargs['input'].decode())
            return SimpleNamespace(returncode=0, stdout=json.dumps(self.remote_reply).encode())
        with patch('server.collector_command', return_value=['ssh', 'x']), patch.object(woni_bridge.subprocess, 'run', side_effect=run):
            self.remote_reply = {'items': [], 'total': 0, 'nextOffset': None, 'query': '', 'error': ''}
            self.assertEqual(bridge.call(operation='notes', query={'q': 'a'})['total'], 0)
            self.assertTrue(seen[0].startswith("REMOTE_REQUEST = None\nREMOTE_OPERATION = 'notes'\nREMOTE_QUERY = {'q': 'a'}\n"))
            self.remote_reply = {'actionId': 'a', 'taskStatus': 'open', 'stateEvent': 'e', 'result': 'saved'}
            self.assertEqual(bridge.call(data, operation='task-status')['result'], 'saved')
            self.assertIn("REMOTE_REQUEST = " + ascii(data) + "\nREMOTE_OPERATION = 'task-status'", seen[1])
            self.remote_reply = {'failed': True, 'error': '다른 곳에서 바뀌었어요'}
            with self.assertRaises(ValueError): bridge.call(data, operation='task-status')
            self.remote_reply = {'failed': True, 'error': woni_bridge.CONNECT_ERROR, 'unknown': True}
            with self.assertRaises(RuntimeError): bridge.call(data, operation='task-status')
            with self.assertRaises(ValueError): bridge.call(operation='notes', query={'q': 'a'})  # a refusal stays a refusal
            with self.assertRaises(ValueError): bridge.call({**data, 'status': 'gone'}, operation='task-status')
        self.assertEqual(len(seen), 5)

    def test_remote_program_runs_the_new_checks_and_reports_unknown_outcomes(self):
        # The program sent over SSH is this file plus the request. Run it the way the remote host does,
        # against a synthetic server on a free port (never the real Woni server).
        answers = {}
        class Handler(http.server.BaseHTTPRequestHandler):
            def reply(self):
                status, value = answers['next']
                data = json.dumps(value).encode()
                self.send_response(status); self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)
            def do_GET(self):
                self.reply()
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length'])); self.reply()
            def log_message(self, *args):
                pass
        httpd = http.server.HTTPServer(('127.0.0.1', 0), Handler)
        worker = threading.Thread(target=httpd.serve_forever, daemon=True); worker.start()
        source = Path(woni_bridge.__file__).read_text()
        self.assertEqual(source.count('http://127.0.0.1:18772/v1/'), 2)
        source = source.replace('http://127.0.0.1:18772/v1/', 'http://127.0.0.1:%d/v1/' % httpd.server_port)
        def remote(operation, query, data=None, status=200, value=None):
            answers['next'] = (status, value or {})
            program = ("REMOTE_REQUEST = " + ascii(data) + "\nREMOTE_OPERATION = " + ascii(operation) + "\nREMOTE_QUERY = " + ascii(query) + "\n" + source)
            done = woni_bridge.subprocess.run([sys.executable, '-I', '-c', program], capture_output=True, timeout=20, env={'HOME': str(self.home)})
            return json.loads(done.stdout)
        data = {'actionId': 'a' * 24, 'status': 'done', 'expectedEvent': '', 'requestId': 'request-123456'}
        try:
            self.assertEqual(remote('note', {'id': '../x'}), {'failed': True, 'error': '기록을 다시 선택해 주세요.'})
            self.assertEqual(remote('tasks', None, value={'actionCount': 2, 'driveError': '동기화 중'})['error'], '동기화 중')
            self.assertNotIn('failed', remote('tasks', None, value={'actionCount': 2, 'driveError': '동기화 중'}))
            refused = remote('task-status', None, data, 409, {'error': '다른 곳에서 바뀌었어요'})
            self.assertEqual(refused, {'failed': True, 'error': '다른 곳에서 바뀌었어요'})
            unknown = remote('task-status', None, data, 503, {'error': '처리 결과를 확인하지 못했어요'})
            self.assertEqual((unknown['failed'], unknown['unknown']), (True, True))
            saved = {'actionId': 'a' * 24, 'taskStatus': 'done', 'stateEvent': 'woni:x', 'result': 'saved', 'owner': None}
            self.assertEqual(remote('task-status', None, data, 200, saved), {key: saved[key] for key in saved if key != 'owner'})
        finally:
            httpd.shutdown(); worker.join(); httpd.server_close()

    def test_upload_streams_selected_bytes_with_metadata_and_checks_response(self):
        config = self.home / 'woni.json'
        config.write_text(json.dumps({'host': 'air'})); config.chmod(0o600)
        bridge = woni_bridge.WoniBridge([{'name': 'air', 'local': True}], config, self.home)
        def run(command, **kwargs):
            body = kwargs['stdin']
            metadata = json.loads(body.readline())
            self.assertEqual(metadata, {'size': 5, 'name': '한글.txt', 'mime': 'text/plain'})
            self.assertEqual(body.read(), b'hello')
            self.assertNotIn('한글.txt', str(command))
            return SimpleNamespace(returncode=0, stdout=json.dumps({'attachment': {
                'id': '20261106-012345abcdef', 'name': '한글.txt', 'size': 5, 'mime': 'text/plain', 'path': '/private'}}).encode())
        with patch.object(woni_bridge.subprocess, 'run', side_effect=run):
            result = bridge.upload(io.BytesIO(b'hello'), 5, '한글.txt', 'text/plain')
        self.assertEqual(result['attachment']['id'], '20261106-012345abcdef')
        self.assertNotIn('path', result['attachment'])
        with self.assertRaises(ValueError):
            bridge.upload(io.BytesIO(b'bad'), 4, 'x', 'text/plain')
        with self.assertRaises(ValueError):
            bridge.upload(io.BytesIO(b'x'), woni_bridge.MAX_FILE + 1, 'x', 'text/plain')

    def test_upload_round_trip_through_real_local_http_with_synthetic_server(self):
        observed = {}
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                observed.update(path=self.path, headers=dict(self.headers), body=self.rfile.read(int(self.headers['Content-Length'])))
                data = json.dumps({'id': '20261106-012345abcdef', 'name': 'fixture.txt', 'size': 5, 'mime': 'text/plain'}).encode()
                self.send_response(200); self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)
            def log_message(self, *args):
                pass
        httpd = http.server.HTTPServer(('127.0.0.1', 0), Handler)
        worker = threading.Thread(target=httpd.serve_forever, daemon=True); worker.start()
        config = self.home / 'woni.json'
        config.write_text(json.dumps({'host': 'air'})); config.chmod(0o600)
        bridge = woni_bridge.WoniBridge([{'name': 'air', 'local': True}], config, self.home)
        program = woni_bridge.UPLOAD_PROGRAM.replace("'127.0.0.1',18772", "'127.0.0.1'," + str(httpd.server_port))
        try:
            with patch.dict(os.environ, {'HOME': str(self.home)}), patch.object(woni_bridge, 'UPLOAD_PROGRAM', program):
                result = bridge.upload(io.BytesIO(b'hello'), 5, 'fixture.txt', 'text/plain')
        finally:
            httpd.shutdown(); worker.join(); httpd.server_close()
        self.assertEqual(observed['path'], '/v1/attachment')
        self.assertEqual(observed['body'], b'hello')
        self.assertEqual(observed['headers']['Authorization'], 'Bearer test-credential')
        self.assertEqual(result['attachment']['name'], 'fixture.txt')

    def test_explicit_private_host_setting_required(self):
        config = self.home / 'woni.json'
        bridge = woni_bridge.WoniBridge([{'name': 'air', 'local': True}], config, self.home)
        self.assertEqual(bridge.call(), {'enabled': False})
        config.write_text(json.dumps({'host': 'air'}))
        config.chmod(0o644)
        with self.assertRaises(ValueError):
            bridge.host()
        config.chmod(0o600)
        self.assertEqual(bridge.host()['name'], 'air')
        config.write_text(json.dumps({'host': 'other'}))
        with self.assertRaises(ValueError):
            bridge.host()


if __name__ == '__main__':
    unittest.main()
