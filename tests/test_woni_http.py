from types import SimpleNamespace
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
