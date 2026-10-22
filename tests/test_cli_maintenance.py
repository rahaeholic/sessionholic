import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import cli_maintenance as cm
from cli_monitor import CliMonitor, normalize


class MaintenanceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.old = self.root / 'original-package'
        self.old.write_text('live binary and helpers')
        self.launcher = self.root / 'codex'
        self.launcher.symlink_to(self.old)
        self.row = {'path': str(self.launcher), 'installer': 'npm', 'node': '/usr/bin/node', 'npm': '/tools/npm'}
        cm.atomic_json(self.root / 'config.json', {'schema': 1, 'codex': self.row,
                       'claude': {'path': '/tools/claude'}, 'schedule': {'daily': True}})

    def test_staged_update_switches_only_after_validation_and_preserves_original(self):
        commands = []
        def execute(args, **kwargs):
            commands.append([str(arg) for arg in args])
            self.assertEqual(self.launcher.resolve(), self.old)
            return ''
        with patch.object(cm, 'command', side_effect=execute), patch.object(cm, 'version', return_value='0.164.0'):
            cm.update_npm(self.row, self.root, '0.163.0', '0.164.0')
        self.assertEqual(self.old.read_text(), 'live binary and helpers')
        self.assertEqual(self.launcher.readlink(), self.root / 'codex-releases/0.164.0/bin/codex')
        self.assertIn('@openai/codex@0.164.0', commands[0])
        self.assertIn('--ignore-scripts', commands[0])
        self.assertNotIn(str(self.old), commands[0])

    def test_bad_candidate_or_concurrent_launcher_change_does_not_replace_pointer(self):
        with patch.object(cm, 'command'), patch.object(cm, 'version', return_value='0.162.0'):
            with self.assertRaises(ValueError):
                cm.update_npm(self.row, self.root, '0.163.0', '0.164.0')
        self.assertEqual(self.launcher.resolve(), self.old)
        other = self.root / 'manual-new'
        def probe(*args):
            self.launcher.unlink()
            self.launcher.symlink_to(other)
            return '0.164.0'
        with patch.object(cm, 'command'), patch.object(cm, 'version', side_effect=probe):
            with self.assertRaises(ValueError):
                cm.update_npm(self.row, self.root, '0.163.0', '0.164.0')
        self.assertEqual(self.launcher.readlink(), other)

    def test_equal_or_newer_never_installs(self):
        with patch.object(cm, 'command') as command:
            cm.update_npm(self.row, self.root, '0.164.0', '0.164.0')
            cm.update_npm(self.row, self.root, '0.165.0', '0.164.0')
        command.assert_not_called()

    def test_partial_failure_keeps_last_success_and_native_channel(self):
        cm.atomic_json(self.root / 'status.json', {'schema': 1, 'tools': {
            'codex': {'version': '0.163.0', 'lastSuccessAt': '2026-10-20T00:00:00Z'}}})
        with patch.object(cm, 'version', side_effect=lambda row, tool: '0.163.0' if tool == 'codex' else '2.1.306'), \
                patch.object(cm, 'latest_codex', side_effect=OSError()), \
                patch.object(cm, 'native_policy', return_value={'native': True, 'autoUpdates': True, 'channel': 'stable'}), \
                patch.object(cm, 'command') as command:
            self.assertEqual(cm.maintain(self.root), 1)
        data = cm.snapshot(self.root)
        self.assertEqual(data['tools']['codex']['status'], 'failed')
        self.assertEqual(data['tools']['codex']['lastSuccessAt'], '2026-10-20T00:00:00Z')
        self.assertEqual(data['tools']['claude']['status'], 'current')
        self.assertEqual(data['tools']['claude']['channel'], 'stable')
        command.assert_called_once_with(['/tools/claude', '--safe-mode', 'update'], timeout=600)

    def test_snapshot_has_no_commands_network_or_writes(self):
        with patch.object(cm, 'command') as command, patch.object(cm, 'atomic_json') as write, \
                patch.object(cm, 'latest_codex') as network:
            self.assertTrue(cm.snapshot(self.root)['configured'])
            self.assertFalse(cm.snapshot(self.root / 'missing')['configured'])
        command.assert_not_called()
        write.assert_not_called()
        network.assert_not_called()

    def test_lock_excludes_another_run_and_releases_on_exit(self):
        with cm.lock(self.root) as first:
            with cm.lock(self.root) as second:
                self.assertTrue(first)
                self.assertFalse(second)
        with cm.lock(self.root) as third:
            self.assertTrue(third)

    def test_native_disabled_updater_is_not_forced_or_reconfigured(self):
        with patch.object(cm, 'version', return_value='0.163.0'), patch.object(cm, 'latest_codex', return_value='0.163.0'), \
                patch.object(cm, 'native_policy', return_value={'native': True, 'autoUpdates': False, 'channel': 'stable'}), \
                patch.object(cm, 'command') as command:
            self.assertEqual(cm.maintain(self.root), 1)
        command.assert_not_called()


class MonitorTest(unittest.TestCase):
    def test_offline_keeps_last_good_and_monitor_only_is_included(self):
        now = [1000000]
        raw = {'schema': 1, 'configured': True, 'finishedAt': '1970-01-12T13:46:40Z',
               'schedule': {'daily': True}, 'tools': {'codex': {'version': '0.163.0', 'status': 'current'}}}
        monitor = CliMonitor([{'name': 'pro', 'monitorOnly': True}], lambda host: raw, clock=lambda: now[0], threaded=False)
        first = monitor.snapshot(refresh=True)['hosts'][0]
        self.assertEqual(first['status'], 'ready')
        self.assertTrue(first['monitorOnly'])
        monitor.runner = lambda host: (_ for _ in ()).throw(OSError())
        now[0] += 61
        after = monitor.snapshot(refresh=True)['hosts'][0]
        self.assertEqual(after['status'], 'stale')
        self.assertEqual(after['data']['tools']['codex']['version'], '0.163.0')

    def test_read_cache_and_manual_refresh_are_bounded(self):
        now, calls = [0], []
        monitor = CliMonitor([{'name': 'mini'}], lambda host: calls.append(host) or {'schema': 1, 'configured': False},
                             clock=lambda: now[0], threaded=False)
        monitor.snapshot()
        self.assertEqual(calls, [])
        monitor.snapshot(refresh=True)
        monitor.snapshot(force=True)
        self.assertEqual(len(calls), 1)
        now[0] = 10
        monitor.snapshot(force=True)
        self.assertEqual(len(calls), 2)

    def test_remote_payload_is_allowlisted(self):
        row = normalize({'schema': 1, 'configured': True, 'secret': 'not exposed',
                         'tools': {'codex': {'version': '<script>', 'status': 'arbitrary', 'path': '/private'}}})
        self.assertIsNone(row['tools']['codex']['version'])
        self.assertEqual(row['tools']['codex']['status'], 'unknown')
        self.assertNotIn('secret', json.dumps(row))
        self.assertNotIn('/private', json.dumps(row))


if __name__ == '__main__':
    unittest.main()
