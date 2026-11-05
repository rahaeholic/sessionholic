import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
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

    def tearDown(self):
        self.temp.cleanup()

    def test_only_chat_and_activity_are_returned_from_dashboard(self):
        with patch.object(woni_bridge, 'urlopen', return_value=Response({'chats': [{'id': 'a'}], 'activity': {'running': 1}, 'actions': ['private']})) as urlopen:
            result = woni_bridge.local_call(home=self.home)
        self.assertEqual(result, {'enabled': True, 'chats': [{'id': 'a'}], 'activity': {'running': 1}})
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
