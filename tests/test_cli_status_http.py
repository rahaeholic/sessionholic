from types import SimpleNamespace
from unittest.mock import Mock, patch
import subprocess

import unittest
import test_goals_http
import server


setUpModule = test_goals_http.setUpModule


class CliStatusHTTPTest(unittest.TestCase):
    setUpBase = test_goals_http.GoalsHTTPTest.setUp
    tearDown = test_goals_http.GoalsHTTPTest.tearDown
    request = test_goals_http.GoalsHTTPTest.request
    def setUp(self):
        self.setUpBase()
        self.monitor = SimpleNamespace(snapshot=Mock(return_value={'hosts': [], 'refreshing': False}))
        self.httpd.cli_monitor = self.monitor

    def test_status_requires_auth_and_refresh_requires_csrf(self):
        response, _ = self.request('GET', '/api/cli-status', authenticated=False)
        self.assertEqual(response.status, 401)
        response, _ = self.request('POST', '/api/cli-status/refresh', {}, csrf=False)
        self.assertEqual(response.status, 403)
        self.monitor.snapshot.assert_not_called()

    def test_routes_only_read_monitor_no_update_or_launch(self):
        with patch.object(self.httpd.workflow, 'launch', side_effect=AssertionError('no launch')):
            response, result = self.request('GET', '/api/cli-status')
            self.assertEqual(response.status, 200)
            self.assertEqual(result['hosts'], [])
            self.monitor.snapshot.assert_called_once_with(refresh=True)
            response, _ = self.request('POST', '/api/cli-status/refresh', {})
            self.assertEqual(response.status, 200)
            self.monitor.snapshot.assert_called_with(force=True)

    def test_remote_transport_only_sends_snapshot_reader(self):
        host = {'name': 'fixture', 'local': False, 'ssh': 'fixture', 'python': 'python3'}
        with patch.object(server.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, b'{"schema":1,"configured":false}', b'')) as run:
            server.run_cli_status(host)
        args, kwargs = run.call_args
        self.assertNotIn('run', args[0])
        self.assertEqual(kwargs['input'], server.CLI_MAINTENANCE.read_bytes())
