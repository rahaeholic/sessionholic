from types import SimpleNamespace
import http.client
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
