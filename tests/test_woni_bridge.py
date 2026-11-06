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

    def test_activity_removes_unneeded_worker_details(self):
        value = {'running': [{'id': 'chat-1', 'text': '질문', 'steps': [{'tool': '읽기', 'private': 'x'}],
                  'batches': [{'id': '20261106-010101-abcd', 'tasks': [{'n': 1, 'title': '작업', 'status': 'ok', 'pid': 123}]}], 'secret': 'x'}], 'recent': []}
        with patch.object(woni_bridge, 'urlopen', return_value=Response(value)):
            result = woni_bridge.local_call(home=self.home, operation='activity')
        self.assertEqual(result['running'][0]['batches'][0]['tasks'], [{'n': 1, 'title': '작업', 'status': 'ok'}])
        self.assertEqual(result['running'][0]['steps'], [{'tool': '읽기'}])
        self.assertNotIn('secret', result['running'][0])

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
