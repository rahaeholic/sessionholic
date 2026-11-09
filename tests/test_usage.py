"""The optional usage provider with fake helpers and fake account names."""
import gzip
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
import http.client

import server
import settings
from usage import UsageMeter

TOKEN = 'usage-fixture-token-0123456789'
CODEX = {'schema_version': 1, 'accounts': [
    {'key': 'alpha', 'status': 'ready', 'windows': {'five_hour': None,
     'weekly': {'remaining': 68, 'resets_at': '2026-10-14T13:00:00Z'}},
     'credits': {'has_credits': True, 'unlimited': False, 'balance': '1250.4'}},
    {'key': 'beta', 'status': 'login_required', 'windows': {}},
    {'key': 'gamma', 'status': 'skipped'}]}
CLAUDE = {'schema_version': 1, 'status': 'ready', 'usage': {'windows': {
    'five_hour': {'used_percentage': 30, 'resets_at': '2026-10-10T06:29:59Z'},
    'seven_day': {'used_percentage': 16, 'resets_at': '2026-10-14T22:59:59Z'}}}}


def setUpModule():
    # A board under test never reads this user's bot sources (nor, through them, a bot ledger).
    import pathlib
    import shutil
    import tempfile
    from unittest import mock
    folder = tempfile.mkdtemp(prefix="sh-config-")
    patcher = mock.patch.object(server, "CONFIG_DIR", pathlib.Path(folder))
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)
    unittest.addModuleCleanup(shutil.rmtree, folder, True)


def config(folder, **extra):
    return {'provider': 'helper', 'resources': str(folder), 'python': str(Path(folder, 'python3')),
            'path': '/usr/bin:/bin', 'codex': '/fixture/codex', 'codexHelper': 'codex-usage.py',
            'codexAccounts': [{'key': 'alpha', 'label': '알파'}, {'key': 'beta', 'label': '베타'}],
            'claude': '/fixture/claude', 'claudeHelper': 'claude-usage.py',
            'intervalSeconds': 300, 'manualIntervalSeconds': 20, **extra}


class UsageMeterTest(unittest.TestCase):
    def meter(self, outputs, now=None):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder)
        for name in ('codex-usage.py', 'claude-usage.py', 'python3'):
            Path(folder, name).write_text('')
        calls, clock = [], [now or 1000.0]
        def runner(argv):
            calls.append(argv)
            return outputs.pop(0) if outputs else None
        meter = UsageMeter(config(folder), runner=runner, clock=lambda: clock[0], threaded=False)
        return meter, calls, clock, folder

    def test_snapshot_converts_helpers_into_remaining_shares(self):
        meter, calls, _, folder = self.meter([CODEX, CLAUDE])
        data = meter.snapshot(refresh=True)
        self.assertEqual(calls[0], [str(Path(folder, 'python3')), str(Path(folder, 'codex-usage.py')), '/fixture/codex',
                                    '--json', '--accounts', 'alpha', 'beta'])
        self.assertEqual(calls[1], [str(Path(folder, 'python3')), str(Path(folder, 'claude-usage.py')), '/fixture/claude'])
        codex, claude = data['providers']
        self.assertEqual((codex['short'], codex['remaining'], codex['status']), ('CX', 68, 'ready'))
        self.assertEqual([row['name'] for row in codex['rows']], ['알파', '베타'])
        self.assertEqual(codex['rows'][0]['credits'], '1,250')
        self.assertEqual(codex['rows'][0]['windows'][0]['remaining'], None)
        self.assertEqual(codex['rows'][1]['note'], '로그인 필요')
        self.assertEqual(codex['rows'][1]['windows'], [])
        # The summary follows the weekly window even when the 5-hour one has less left.
        self.assertEqual((claude['short'], claude['remaining']), ('CC', 84))
        self.assertEqual([(w['label'], w['remaining']) for w in claude['rows'][0]['windows']], [('7일', 84), ('5시간', 70)])
        self.assertIsNotNone(data['fetchedAt'])

    def test_claude_summary_uses_the_5_hour_window_only_without_a_weekly_value(self):
        five_only = {'schema_version': 1, 'status': 'ready', 'usage': {'windows': {
            'five_hour': {'used_percentage': 30, 'resets_at': None}, 'seven_day': None}}}
        meter, _, _, _ = self.meter([CODEX, five_only])
        claude = meter.snapshot(refresh=True)['providers'][1]
        self.assertEqual(claude['remaining'], 70)
        self.assertEqual([w['remaining'] for w in claude['rows'][0]['windows']], [None, 70])

    def test_reads_are_spaced_and_failures_keep_marked_last_good_values(self):
        meter, calls, clock, _ = self.meter([CODEX, CLAUDE, None, {'schema_version': 1, 'status': 'unavailable'}])
        meter.snapshot(refresh=True)
        meter.snapshot(refresh=True)
        self.assertEqual(len(calls), 2, 'a second read within five minutes reuses the cache')
        clock[0] += 10
        meter.snapshot(force=True)
        self.assertEqual(len(calls), 2, 'manual refresh is spaced too')
        clock[0] += 400
        data = meter.snapshot(refresh=True)
        self.assertEqual(len(calls), 4)
        codex, claude = data['providers']
        self.assertEqual((codex['status'], codex['remaining']), ('stale', 68))
        self.assertIn('이전 조회', codex['note'])
        self.assertEqual((claude['status'], claude['remaining']), ('stale', 84))
        clock[0] += 25
        meter.snapshot(force=True)
        self.assertEqual(len(calls), 6, 'a manual refresh after its own interval reads again')
        # A second failure in a row still keeps the last good numbers.
        codex, claude = meter.snapshot()['providers']
        self.assertEqual((codex['status'], codex['remaining']), ('stale', 68))
        self.assertEqual((claude['status'], claude['remaining']), ('stale', 84))

    def test_missing_helpers_are_unavailable_and_never_run(self):
        meter = UsageMeter(config('/nonexistent/helpers'), runner=lambda argv: self.fail('ran'), threaded=False)
        data = meter.snapshot(refresh=True, force=True)
        self.assertEqual((data['available'], data['providers']), (False, []))

    def test_one_configured_helper_runs_alone(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder)
        for name in ('claude-usage.py', 'python3'):
            Path(folder, name).write_text('')
        settings_like = {key: value for key, value in config(folder).items()
                         if key not in ('codex', 'codexHelper', 'codexAccounts')}
        calls = []
        meter = UsageMeter(settings_like, runner=lambda argv: calls.append(argv) or CLAUDE, threaded=False)
        self.assertEqual([p['id'] for p in meter.snapshot(refresh=True)['providers']], ['claude'])
        self.assertEqual(len(calls), 1)

    def test_helper_runner_is_bounded_and_passes_no_secrets(self):
        meter = UsageMeter(config('/nonexistent'), home='/fixture/home')
        with patch('usage.subprocess.run') as run:
            run.return_value = subprocess.CompletedProcess([], 0, stdout=b'{"schema_version": 1, "status": "ready"}')
            with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'fixture', 'OPENAI_API_KEY': 'fixture'}):
                self.assertEqual(meter.run_helper(['x'])['status'], 'ready')
            env = run.call_args.kwargs['env']
            self.assertEqual(set(env), {'PATH', 'HOME', 'USER', 'LANG'})
            self.assertEqual((env['PATH'], env['HOME']), ('/usr/bin:/bin', '/fixture/home'))
            self.assertEqual(run.call_args.kwargs['timeout'], 45)
            self.assertEqual(run.call_args.kwargs['stdin'], subprocess.DEVNULL)
            self.assertEqual(run.call_args.kwargs['stderr'], subprocess.DEVNULL)
            run.return_value = subprocess.CompletedProcess([], 0, stdout=b'x' * 70000)
            self.assertIsNone(meter.run_helper(['x']))
            run.return_value = subprocess.CompletedProcess([], 0, stdout=b'{"schema_version": 2}')
            self.assertIsNone(meter.run_helper(['x']))


class UsageRouteTest(unittest.TestCase):
    def serve(self, usage=None):
        board = server.Board([{'name': 'local', 'label': 'Local', 'local': True}], 60,
                             lambda host, args, timeout: {'claude': [], 'codex': [], 'errors': []})
        state = tempfile.TemporaryDirectory()
        self.addCleanup(state.cleanup)
        httpd = server.make_server('127.0.0.1', 0, board, TOKEN, state_dir=Path(state.name), usage=usage)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return httpd

    def request(self, httpd, method, path, headers=None, body=None):
        connection = http.client.HTTPConnection('127.0.0.1', httpd.server_address[1], timeout=10)
        self.addCleanup(connection.close)
        connection.request(method, path, body=body, headers={'Host': '127.0.0.1', **(headers or {})})
        response = connection.getresponse()
        return response, response.read()

    def test_routes_are_absent_without_a_provider(self):
        httpd = self.serve()
        auth = {'Authorization': 'Bearer ' + TOKEN}
        self.assertEqual(self.request(httpd, 'GET', '/api/usage', auth)[0].status, 404)
        response, _ = self.request(httpd, 'POST', '/api/usage/refresh',
                                   {**auth, 'X-CSRF-Token': httpd.csrf, 'Content-Type': 'application/json'}, b'{}')
        self.assertEqual(response.status, 404)

    def test_usage_reply_is_compressed_like_other_json_and_refresh_is_forced(self):
        reply = {'available': True, 'refreshing': False, 'fetchedAt': 1, 'error': None,
                 'providers': [{'id': 'codex', 'note': '사용량 ' * 300}]}
        meter = UsageMeter(config('/nonexistent'))
        httpd = self.serve(meter)
        auth = {'Authorization': 'Bearer ' + TOKEN}
        with patch.object(meter, 'snapshot', return_value=reply) as snapshot:
            response, data = self.request(httpd, 'GET', '/api/usage', {**auth, 'Accept-Encoding': 'gzip'})
            self.assertEqual((response.status, response.getheader('Content-Encoding')), (200, 'gzip'))
            self.assertEqual(json.loads(gzip.decompress(data)), reply)
            snapshot.assert_called_once_with(refresh=True)
            response, data = self.request(httpd, 'POST', '/api/usage/refresh',
                                          {**auth, 'X-CSRF-Token': httpd.csrf, 'Content-Type': 'application/json'}, b'{}')
            self.assertEqual(response.status, 200)
            snapshot.assert_called_with(force=True)

    def test_settings_section_builds_the_meter(self):
        with tempfile.TemporaryDirectory(dir='/tmp') as folder:
            home = Path(folder).resolve()
            path = home / '.config/sessionholic/config.json'
            path.parent.mkdir(parents=True, mode=0o700)
            path.write_text(json.dumps({'version': 2, 'usage': {
                'provider': 'helper', 'resources': '~/helpers', 'python': '/usr/bin/python3',
                'codex': '/fixture/codex', 'codexHelper': 'codex-usage.py',
                'codexAccounts': [{'key': 'alpha', 'label': '알파'}], 'intervalSeconds': 600}}))
            path.chmod(0o600)
            section = settings.load(home)['usage']
            meter = UsageMeter(section, home=home)
            self.assertEqual((meter.resources, meter.interval, meter.manual_interval), (home / 'helpers', 600, 20))
            self.assertEqual(meter.helpers(), [home / 'helpers/codex-usage.py'])


class ClaudeTokenTest(unittest.TestCase):
    """Claude on the host's long-lived token: one short CLI turn's rate_limit_event."""

    STREAM = b'\n'.join([
        b'{"type":"system","subtype":"init"}', b'not json',
        json.dumps({'type': 'rate_limit_event', 'rate_limit_info': {'status': 'allowed', 'unifiedWindows': {
            'seven_day': {'utilization': 0.31, 'resetsAt': 1794438000, 'status': 'allowed'},
            'five_hour': {'utilization': 0.075, 'resetsAt': 1794222000}}}}).encode(),
        b'{"type":"result","subtype":"success"}'])

    def test_the_last_rate_limit_event_becomes_the_helper_shape(self):
        import usage
        value = usage.token_usage(self.STREAM)
        self.assertEqual(value['status'], 'ready')
        self.assertEqual(value['usage']['windows']['seven_day'], {'used_percentage': 31.0, 'resets_at': '2026-11-11T23:00:00Z'})
        self.assertAlmostEqual(value['usage']['windows']['five_hour']['used_percentage'], 7.5)
        self.assertIsNone(usage.token_usage(b'{"type":"result"}\n'), 'no event, no reading')
        self.assertIsNone(usage.token_usage(json.dumps({'type': 'rate_limit_event', 'rate_limit_info': {
            'unifiedWindows': {'seven_day': {'utilization': True}}}}).encode()))
        provider = UsageMeter(config('/nonexistent')).claude_provider(value)
        self.assertEqual(provider['remaining'], 69)
        self.assertEqual([w['remaining'] for w in provider['rows'][0]['windows']], [69, 92])

    def meter(self, token_values, now=1000.0):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder)
        for name in ('codex-usage.py', 'claude-usage.py', 'python3'):
            Path(folder, name).write_text('')
        helper_calls, token_calls, clock = [], [], [now]

        def runner(argv):
            helper_calls.append(argv)
            return CLAUDE if 'claude-usage.py' in argv[1] else None

        def token_reader():
            token_calls.append(clock[0])
            return token_values.pop(0) if token_values else None
        meter = UsageMeter(config(folder, codexHelper=None), runner=runner, clock=lambda: clock[0], threaded=False,
                           token_reader=token_reader)
        return meter, helper_calls, token_calls, clock

    def test_the_token_answers_first_and_the_helper_only_when_it_cannot(self):
        import usage
        reading = usage.token_usage(self.STREAM)
        meter, helper_calls, token_calls, clock = self.meter([reading])
        claude = next(p for p in meter.snapshot(refresh=True)['providers'] if p['id'] == 'claude')
        self.assertEqual((claude['status'], claude['remaining']), ('ready', 69))
        self.assertEqual(helper_calls, [], 'the claude.ai login helper is not asked')
        # A long-lived token that cannot answer (no token, CLI failure): the helper still can.
        meter, helper_calls, token_calls, clock = self.meter([])
        claude = next(p for p in meter.snapshot(refresh=True)['providers'] if p['id'] == 'claude')
        self.assertEqual(claude['status'], 'ready')
        self.assertEqual(len(helper_calls), 1)

    def test_automatic_reads_are_spaced_and_a_refresh_asks_again(self):
        import usage
        reading = usage.token_usage(self.STREAM)
        meter, helper_calls, token_calls, clock = self.meter([reading, reading, reading])
        meter.snapshot(refresh=True)
        clock[0] += 400
        meter.snapshot(refresh=True)
        self.assertEqual(len(token_calls), 1, 'a page read inside 15 minutes reuses the reading')
        clock[0] += 30
        meter.snapshot(force=True)
        self.assertEqual(len(token_calls), 2, 'a refresh asks again')
        clock[0] += usage.CLAUDE_TOKEN_EVERY + 1
        meter.snapshot(refresh=True)
        self.assertEqual(len(token_calls), 3)

    def test_the_cli_turn_runs_on_the_token_without_settings_tools_or_a_saved_session(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            (home / '.claude').mkdir()
            (home / '.claude/settings.json').write_text(json.dumps({'env': {'CLAUDE_CODE_OAUTH_TOKEN': 'tok'}}))
            cli = home / 'claude'
            cli.write_text('#!/bin/sh\n')
            meter = UsageMeter(config('/nonexistent', claude=str(cli)), home=home)
            with patch('usage.subprocess.run', return_value=subprocess.CompletedProcess([], 0, self.STREAM, b'')) as run:
                self.assertEqual(meter.read_claude_token()['status'], 'ready')
            argv, kwargs = run.call_args.args[0], run.call_args.kwargs
            self.assertEqual(argv[:3], [str(cli), '-p', '.'])
            for flag, value in (('--model', 'haiku'), ('--tools', ''), ('--setting-sources', ''), ('--output-format', 'stream-json')):
                self.assertEqual(argv[argv.index(flag) + 1], value)
            self.assertIn('--no-session-persistence', argv)
            self.assertEqual(kwargs['env']['CLAUDE_CODE_OAUTH_TOKEN'], 'tok')
            self.assertEqual(sorted(kwargs['env']), ['CLAUDE_CODE_OAUTH_TOKEN', 'HOME', 'LANG', 'PATH'])
            with patch('usage.subprocess.run', return_value=subprocess.CompletedProcess([], 1, b'', b'')):
                self.assertIsNone(meter.read_claude_token())
            (home / '.claude/settings.json').write_text('{}')
            with patch('usage.subprocess.run') as run:
                self.assertIsNone(meter.read_claude_token(), 'no token, no call')
                run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
