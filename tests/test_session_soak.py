import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock
import io

spec = importlib.util.spec_from_file_location('session_soak', Path(__file__).parents[1] / 'scripts/session_soak.py')
soak = importlib.util.module_from_spec(spec)
spec.loader.exec_module(soak)


class SessionSoak(unittest.TestCase):
    def test_bounded_observation_records_transport_failure_and_recovery_without_sending(self):
        payload = {'hosts': [{'name': 'pc', 'ok': True, 'data': {'claude': []}}]}
        now = [0]
        calls = []
        def opened(url, **kwargs):
            calls.append(url)
            if len(calls) == 3:
                raise OSError('private-network-detail')
            return io.BytesIO(json.dumps(payload if '/api/snapshot' in url else {'terminals': []}).encode())
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp).resolve() / 'run' / 'log.jsonl'
            with patch.object(soak.urllib.request, 'build_opener', return_value=Mock(open=opened)):
                soak.observe('https://board.example', 'pc', out, duration=120, interval=60,
                             clock=lambda: now[0], sleep=lambda seconds: now.__setitem__(0, now[0] + seconds))
            rows = [json.loads(line) for line in out.read_text().splitlines()]
            self.assertEqual(rows[1]['event'], 'read_unavailable')
            self.assertIn('sample', rows[2])
            self.assertEqual(rows[-1]['elapsedSeconds'], 120)
            self.assertEqual(rows[-1]['samples'], 3)
            self.assertNotIn('private-network-detail', out.read_text())
            self.assertTrue(all('/api/snapshot?refresh=1&host=pc' in url or url.endswith('/api/terminals') for url in calls))

    def test_connection_loss_and_bounded_list_miss_are_not_native_termination(self):
        source = {'host': 'pc', 'agent': 'claude', 'home': None, 'id': 'worker', 'cwd': 'C:\\work', 'sessionId': 'conversation'}
        snapshot = {'hosts': [{'name': 'pc', 'ok': True, 'data': {'claude': [source]}}]}
        terminal = {'id': 'private-terminal', 'host': 'pc', 'nativeSource': source, 'alive': True}
        before = soak.sample(snapshot, {'terminals': [terminal]}, 'pc')
        snapshot['hosts'][0]['data']['claude'] = []
        snapshot['recoverySessions'] = [source]
        after = soak.sample(snapshot, {'terminals': [{**terminal, 'alive': False}]}, 'pc')
        changes = soak.compare(before, after)
        self.assertEqual({r['event'] for r in changes}, {'connection_ended', 'outside_current_list'})
        self.assertTrue(changes[0]['locatable'])
        self.assertEqual(changes[0]['nativeProcessSurvival'], 'not_verified')
        text = json.dumps(after)
        for value in ('private-terminal', 'conversation', 'C:\\work', 'worker'):
            self.assertNotIn(value, text)
        self.assertNotEqual(soak.identity(source), soak.identity({**source, 'sessionId': 'another'}))
        codex = {'host': 'pc', 'agent': 'codex', 'id': 'thread', 'home': '.codex', 'cwd': 'C:\\work', 'source': 'exec'}
        self.assertIsNotNone(soak.identity(codex))
        self.assertNotEqual(soak.identity(codex), soak.identity({**codex, 'home': '.other'}))

    def test_output_is_exclusive_private_and_symlinks_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp).resolve() / 'run' / 'observations.jsonl'
            with soak.private_output(path) as stream:
                stream.write('{}\n')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                soak.private_output(path)
            (Path(tmp).resolve() / 'link').symlink_to(path.parent, target_is_directory=True)
            with self.assertRaises(ValueError):
                soak.private_output(Path(tmp).resolve() / 'link' / 'other.jsonl')
