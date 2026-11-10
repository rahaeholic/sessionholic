from types import SimpleNamespace
import http.client
import json
import unittest
from unittest.mock import Mock
import test_goals_http

setUpModule = test_goals_http.setUpModule


class WoniHTTPTest(unittest.TestCase):
    setUp = test_goals_http.GoalsHTTPTest.setUp
    tearDown = test_goals_http.GoalsHTTPTest.tearDown
    request = test_goals_http.GoalsHTTPTest.request

    def test_only_authenticated_csrf_checked_requests_can_send(self):
        self.httpd.woni = SimpleNamespace(host=Mock(return_value={'name': 'fixture'}),
                                          call=Mock(return_value={'enabled': True, 'chats': []}))
        data = {'text': '합성 메시지', 'requestId': 'request-fixture'}
        self.assertEqual(self.request('GET', '/api/woni', authenticated=False)[0].status, 401)
        self.assertEqual(self.request('POST', '/api/woni', data, csrf=False)[0].status, 403)
        self.assertEqual(self.request('POST', '/api/woni', data, extra={'Origin': 'https://example.invalid'})[0].status, 403)
        self.httpd.woni.call.assert_not_called()
        self.assertEqual(self.request('GET', '/api/woni?availability=1')[1], {'enabled': True})
        self.assertEqual(self.request('POST', '/api/woni', data)[0].status, 200)
        self.httpd.woni.call.assert_called_once_with(data)

    def test_task_and_note_reads_require_login_and_forward_only_their_query(self):
        secret = 'client_secret=abcdefgh12345678'
        self.httpd.woni = SimpleNamespace(host=Mock(return_value={'name': 'fixture'}),
                                          call=Mock(return_value={'items': [], 'title': secret, 'text': secret, 'error': '', 'label': secret}))
        paths = {'/api/woni/tasks': ('tasks', {}), '/api/woni/notes?q=%EC%9E%AC%EC%9E%85%EA%B3%A0&offset=40': ('notes', {'q': '재입고', 'offset': '40'}),
                 '/api/woni/note?id=slack-1&offset=0': ('note', {'id': 'slack-1', 'offset': '0'}),
                 '/api/woni/transcript?id=s1&format=passages&q=a': ('transcript', {'id': 's1', 'format': 'passages', 'q': 'a'}),
                 '/api/woni/transcript?id=s1&evidenceId=e1&context=1': ('transcript', {'id': 's1', 'evidenceId': 'e1', 'context': '1'})}
        for path in paths:
            self.assertEqual(self.request('GET', path, authenticated=False)[0].status, 401)
        self.httpd.woni.call.assert_not_called()
        for path, (operation, query) in paths.items():
            response, body = self.request('GET', path, csrf=False)
            self.assertEqual(response.status, 200, path)
            self.httpd.woni.call.assert_called_with(operation=operation, query=query)
            # Scrubbed like every other Woni reply: text, title and error, and no other key.
            self.assertEqual((body['title'], body['text'], body['label']), ('[redacted]', '[redacted]', secret))
        self.assertEqual(self.request('GET', '/api/woni/other')[0].status, 404)

    def test_read_errors_keep_the_bridge_messages_and_status(self):
        self.httpd.woni = SimpleNamespace(call=Mock(side_effect=ValueError('원문 전사 파일이 없어요')))
        response, body = self.request('GET', '/api/woni/transcript?id=slack-1')
        self.assertEqual((response.status, body), (400, {'error': '원문 전사 파일이 없어요'}))
        self.httpd.woni.call.side_effect = RuntimeError('원이에 연결하지 못했어요. Air15 연결을 확인해 주세요.')
        response, body = self.request('GET', '/api/woni/tasks')
        self.assertEqual((response.status, body), (409, {'error': '원이에 연결하지 못했어요. Air15 연결을 확인해 주세요.'}))

    def test_task_status_needs_csrf_and_forwards_body_with_its_own_operation(self):
        data = {'actionId': 'a' * 24, 'status': 'done', 'expectedEvent': '', 'requestId': 'request-123456'}
        reply = {'actionId': 'a' * 24, 'taskStatus': 'done', 'stateEvent': 'woni:request-123456', 'result': 'saved'}
        self.httpd.woni = SimpleNamespace(call=Mock(return_value=reply))
        self.assertEqual(self.request('POST', '/api/woni/task-status', data, authenticated=False)[0].status, 401)
        self.assertEqual(self.request('POST', '/api/woni/task-status', data, csrf=False)[0].status, 403)
        self.assertEqual(self.request('POST', '/api/woni/task-status', data, extra={'Origin': 'https://example.invalid'})[0].status, 403)
        self.httpd.woni.call.assert_not_called()
        response, body = self.request('POST', '/api/woni/task-status', data)
        self.assertEqual((response.status, body), (200, reply))
        self.httpd.woni.call.assert_called_with(data, operation='task-status')
        self.httpd.woni.call.side_effect = ValueError('다른 곳에서 할 일 상태가 바뀌었어요. 새로고침 후 다시 선택해 주세요')
        response, body = self.request('POST', '/api/woni/task-status', data)
        self.assertEqual((response.status, body['error'][:6]), (400, '다른 곳에서'))
        self.httpd.woni.call.side_effect = RuntimeError('원이에 연결하지 못했어요. Air15 연결을 확인해 주세요.')
        self.assertEqual(self.request('POST', '/api/woni/task-status', data)[0].status, 409)

    def test_attachment_requires_login_csrf_and_forwards_only_bounded_file(self):
        self.httpd.woni = SimpleNamespace(upload=Mock(side_effect=lambda stream, length, name, mime:
            {'attachment': {'id': '20261106-012345abcdef', 'name': name, 'size': length, 'body': stream.read(length).decode()}}))
        def send(headers):
            connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
            connection.request('POST', '/api/woni/attachment', body=b'hello', headers=headers)
            response = connection.getresponse(); response.read(); connection.close()
            return response.status
        base = {'Content-Type': 'application/octet-stream', 'X-File-Name': 'fixture.txt'}
        self.assertEqual(send(base), 401)
        self.assertEqual(send({**base, 'Cookie': self.cookie}), 403)
        self.httpd.woni.upload.assert_not_called()
        self.assertEqual(send({**base, 'Cookie': self.cookie, 'X-CSRF-Token': self.csrf}), 200)
        args = self.httpd.woni.upload.call_args.args
        self.assertEqual(args[1:], (5, 'fixture.txt', 'application/octet-stream'))
