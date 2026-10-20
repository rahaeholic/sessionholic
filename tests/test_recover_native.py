"""Exact saved-reference lookup only: temporary records, no model or CLI calls."""
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import launch
import transfer
import transfer_worker
import windows_collector as wc
import windows_runtime as wr


class RecoverNativeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.cwd = self.home / 'Projects/fixture'
        self.cwd.mkdir(parents=True)
        self.sid = str(uuid.uuid4())
        self.source = {'host': 'fixture-pc', 'agent': 'codex', 'id': self.sid,
                       'home': '.codex', 'cwd': str(self.cwd)}
        account = self.home / '.codex'
        account.mkdir()
        self.database = account / 'state_5.sqlite'
        with sqlite3.connect(self.database) as db:
            db.execute('CREATE TABLE threads (id TEXT PRIMARY KEY, cwd TEXT, title TEXT, '
                       'created_at INTEGER, updated_at INTEGER, archived INTEGER, source TEXT)')
            db.execute('INSERT INTO threads VALUES (?,?,?,?,?,?,?)',
                       (self.sid, str(self.cwd), 'fixture-title', 1, 1, 0, 'cli'))
        self.reader = wc.WindowsCollector(self.home)
        self.runtime = wr.WindowsRuntime(self.home, reader=self.reader,
                                         proxy_factory=lambda *_: self.fail('native proxy opened'))

    def recover(self, platform, source=None):
        source = self.source if source is None else source
        if platform == 'windows':
            return self.runtime.recover_session(source)['source']
        return launch.recover_saved_session(source, self.home)['source']

    def transcript(self, sid=None, entries=None, project='fixture'):
        sid = sid or self.sid
        path = self.home / '.claude/projects' / project / (sid + '.jsonl')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('\n'.join(json.dumps(row) for row in (entries if entries is not None else [
            {'sessionId': sid, 'cwd': str(self.cwd), 'type': 'user', 'message': {'content': 'private fixture prompt'}},
            {'sessionId': sid, 'type': 'assistant', 'message': {'content': 'private fixture result'}}
        ])) + '\n')
        return path

    def claude_source(self, **changes):
        return {**self.source, 'agent': 'claude', 'home': '.claude', 'kind': 'interactive', **changes}

    def test_exact_codex_outside_top_100_is_recovered_without_any_native_action(self):
        with sqlite3.connect(self.database) as db:
            db.executemany('INSERT INTO threads VALUES (?,?,?,?,?,?,?)', [
                (str(uuid.uuid4()), str(self.cwd), 'newer fixture', 2, index + 2, 0, 'cli')
                for index in range(101)])
        self.assertEqual(len(self.reader.database_rows()), 100)
        self.assertNotIn(self.sid, {row['id'] for row in self.reader.database_rows()})
        before = self.database.read_bytes()
        with patch('subprocess.run', side_effect=AssertionError('CLI started')), \
                patch('subprocess.Popen', side_effect=AssertionError('process started')), \
                patch.object(self.reader, 'endpoints', side_effect=AssertionError('runtime discovered')), \
                patch.object(self.reader, 'writer_owners', side_effect=AssertionError('writer probed')):
            for platform in ('mac', 'windows'):
                with self.subTest(platform=platform):
                    row = self.recover(platform)
                    for key in ('host', 'agent', 'id', 'home', 'cwd'):
                        self.assertEqual(row[key], self.source[key])
                    self.assertEqual((row['phase'], row['runtimeKnown'], row['readOnly']), ('unknown', False, True))
                    self.assertTrue(row['canRead'])
                    self.assertNotIn('argv', row)
                    self.assertNotIn('messages', row)
        self.assertEqual(self.database.read_bytes(), before)
        self.assertFalse(self.runtime.store.root.exists(), 'read-only lookup created a runtime ledger')

    def test_codex_changed_identity_home_cwd_and_archived_fail_closed(self):
        variants = [dict(self.source, id=str(uuid.uuid4())), dict(self.source, home='../.codex'),
                    dict(self.source, cwd=str(self.home / 'elsewhere')), dict(self.source, cwd='relative'),
                    dict(self.source, host='bad host'), dict(self.source, id='not-a-uuid')]
        for platform in ('mac', 'windows'):
            for source in variants:
                with self.subTest(platform=platform, source=source), self.assertRaises(ValueError):
                    self.recover(platform, source)
        with sqlite3.connect(self.database) as db:
            db.execute('UPDATE threads SET archived=1 WHERE id=?', (self.sid,))
        for platform in ('mac', 'windows'):
            with self.subTest(platform=platform), self.assertRaises(ValueError):
                self.recover(platform)

    def test_windows_cannot_select_another_codex_account(self):
        for home in (None, '', '.codex-personal', '.claude'):
            with self.subTest(home=home), self.assertRaises(ValueError):
                self.recover('windows', {**self.source, 'home': home})

    def test_mac_codex_account_link_is_not_followed(self):
        (self.home / '.codex-linked').symlink_to(self.home / '.codex', target_is_directory=True)
        with self.assertRaises(ValueError):
            self.recover('mac', {**self.source, 'home': '.codex-linked'})

    def test_claude_keeps_explicit_worker_alias_kind_home_and_never_starts_cli(self):
        self.transcript()
        with patch('subprocess.run', side_effect=AssertionError('CLI started')), \
                patch('subprocess.Popen', side_effect=AssertionError('process started')), \
                patch.object(self.runtime, '_claude', side_effect=AssertionError('live owner queried')), \
                patch.object(self.reader, '_transcripts', side_effect=AssertionError('bounded index used')), \
                patch.object(self.runtime, '_claude_running', side_effect=AssertionError('running queried')):
            for platform in ('mac', 'windows'):
                for kind in ('interactive', 'background'):
                    for home in (None, '', '.claude'):
                        source = self.claude_source(id='worker-a1', sessionId=self.sid, kind=kind, home=home)
                        with self.subTest(platform=platform, kind=kind, home=home):
                            row = self.recover(platform, source)
                            self.assertEqual((row['id'], row['sessionId'], row['kind'], row['home']),
                                             ('worker-a1', self.sid, kind, home))
                            self.assertEqual((row['phase'], row['runtimeKnown']), ('unknown', False))
                            self.assertNotIn('private fixture', json.dumps(row))
                            self.assertNotIn('argv', row)

    def test_claude_without_explicit_alias_never_guesses_transcript(self):
        self.transcript()
        for platform in ('mac', 'windows'):
            row = self.recover(platform, self.claude_source())
            self.assertEqual(row['id'], self.sid)
            with self.subTest(platform=platform), self.assertRaises(ValueError):
                self.recover(platform, self.claude_source(id='worker-a1'))

    def test_claude_requires_exact_header_id_cwd_and_account(self):
        path = self.transcript()
        valid = path.read_text()
        for platform in ('mac', 'windows'):
            for source in (self.claude_source(home='.claude-other'), self.claude_source(cwd=str(self.home / 'wrong')),
                           self.claude_source(sessionId='../other'), self.claude_source(cwd='relative')):
                with self.subTest(platform=platform, source=source), self.assertRaises(ValueError):
                    self.recover(platform, source)
            for header in ([{'sessionId': str(uuid.uuid4()), 'cwd': str(self.cwd)}],
                           [{'sessionId': self.sid, 'cwd': str(self.home / 'wrong')}],
                           [{'cwd': str(self.cwd)}], [{'sessionId': self.sid}],
                           [{'sessionId': self.sid, 'cwd': str(self.cwd)},
                            {'sessionId': str(uuid.uuid4()), 'cwd': str(self.cwd)}]):
                path.write_text('\n'.join(json.dumps(entry) for entry in header) + '\n')
                with self.subTest(platform=platform, header=header), self.assertRaises(ValueError):
                    self.recover(platform, self.claude_source())
                path.write_text(valid)

    def test_duplicate_or_linked_claude_transcripts_are_not_selected(self):
        path = self.transcript()
        duplicate = self.transcript(project='duplicate')
        for platform in ('mac', 'windows'):
            with self.subTest(platform=platform), self.assertRaises(ValueError):
                self.recover(platform, self.claude_source())
        duplicate.unlink()
        original = path.with_suffix('.saved')
        path.rename(original)
        path.symlink_to(original)
        for platform in ('mac', 'windows'):
            with self.subTest(platform=platform), self.assertRaises(ValueError):
                self.recover(platform, self.claude_source())

    def test_both_worker_dispatchers_expose_only_metadata_recovery(self):
        expected = {'source': {'id': self.sid}}
        self.assertIn('recover_session', transfer.WINDOWS_OPS)
        with patch('launch.recover_saved_session', return_value=expected) as recover:
            self.assertEqual(transfer_worker.rpc({'op': 'recover_session', 'args': {'source': self.source}}), expected)
            recover.assert_called_once_with(self.source)
        with patch('windows_runtime.WindowsRuntime', return_value=self.runtime), \
                patch.object(self.runtime, 'recover_session', return_value=expected) as recover:
            self.assertEqual(wr.handle({'op': 'recover_session', 'args': {'source': self.source}}), expected)
            recover.assert_called_once_with(self.source)


if __name__ == '__main__':
    unittest.main()
