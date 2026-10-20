"""Temporary SQLite and fake writers only; no native CLI, SSH or model is used."""
from concurrent.futures import ThreadPoolExecutor
import copy
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import prompt_receipts as receipts


SOURCE = {'host': 'fixture-mini', 'agent': 'claude', 'home': None, 'id': 'worker-a1',
          'sessionId': '019aaaaa-0000-7000-8000-000000000001', 'cwd': '/fixture/project'}
TERMINAL = {'id': 'a' * 32, 'nativeSource': SOURCE}
RID = 'fixture-prompt-request-001'
FINGERPRINT = 'b' * 64


class PromptReceiptsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ledger = receipts.PromptReceipts(self.root)

    def reserve(self, *, ledger=None, source=None, request_id=RID, fingerprint=FINGERPRINT,
                number=1, terminal=None):
        return (ledger or self.ledger).reserve(source or SOURCE, request_id, fingerprint, number, terminal or TERMINAL)

    def finish(self, *, ledger=None, source=None, status='answered', reason_code=None):
        return (ledger or self.ledger).finish(source or SOURCE, RID, FINGERPRINT, 1, TERMINAL,
                                             status=status, reason_code=reason_code)

    def test_lookup_is_lazy_and_never_creates_a_ledger(self):
        result = self.ledger.lookup(SOURCE, RID)
        self.assertEqual(result, {'requestId': RID, 'status': 'not_found', 'number': None, 'reasonCode': 'not_found'})
        self.assertFalse(self.ledger.directory.exists())
        self.assertFalse(self.ledger.cancel_not_sent(SOURCE, RID, FINGERPRINT, 1, TERMINAL))
        self.assertFalse(self.ledger.directory.exists())

    def test_reservation_is_committed_before_input_and_survives_new_instance(self):
        first = self.reserve()
        self.assertTrue(first['reserved'])
        self.assertEqual(first['status'], 'unconfirmed')
        other = receipts.PromptReceipts(self.root)
        self.assertEqual(other.lookup(SOURCE, RID)['reasonCode'], 'reserved')
        self.assertFalse(self.reserve(ledger=other)['reserved'])
        with sqlite3.connect(self.ledger.path) as db:
            self.assertEqual(db.execute('SELECT status FROM receipts').fetchall(), [('unconfirmed',)])
        self.assertEqual(stat.S_IMODE(self.ledger.directory.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.ledger.path.stat().st_mode), 0o600)

    def test_answered_receipt_survives_restart_and_cannot_be_downgraded_or_cancelled(self):
        self.reserve()
        result = self.finish()
        self.assertEqual((result['status'], result['number']), ('answered', 1))
        restarted = receipts.PromptReceipts(self.root)
        self.assertEqual(self.finish(ledger=restarted, status='unconfirmed'), result)
        self.assertFalse(restarted.cancel_not_sent(SOURCE, RID, FINGERPRINT, 1, TERMINAL))
        self.assertFalse(self.reserve(ledger=restarted)['reserved'])

    def test_signature_conflict_includes_choice_fingerprint_and_exact_terminal(self):
        self.reserve()
        for change in ({'number': 2}, {'fingerprint': 'c' * 64},
                       {'terminal': {**TERMINAL, 'id': 'd' * 32}}):
            with self.subTest(change=change), self.assertRaises(receipts.PromptReceiptError) as raised:
                self.reserve(**change)
            self.assertEqual((raised.exception.status, raised.exception.code), (409, 'prompt_request_conflict'))
        self.assertEqual(self.ledger.lookup(SOURCE, RID)['status'], 'unconfirmed')

    def test_lookup_can_validate_original_choice_without_terminal_or_menu(self):
        self.reserve()
        self.finish()
        restarted = receipts.PromptReceipts(self.root)
        result = restarted.lookup(SOURCE, RID, FINGERPRINT, 1)
        self.assertEqual(result['status'], 'answered')
        for fingerprint, number in (('c' * 64, 1), (FINGERPRINT, 2)):
            with self.subTest(fingerprint=fingerprint, number=number), self.assertRaises(receipts.PromptReceiptError) as raised:
                restarted.lookup(SOURCE, RID, fingerprint, number)
            self.assertEqual((raised.exception.status, raised.exception.code), (409, 'prompt_request_conflict'))
        with self.assertRaises(receipts.PromptReceiptError) as raised:
            restarted.lookup(SOURCE, RID, FINGERPRINT)
        self.assertEqual(raised.exception.status, 400)
        # New reservations still validate the exact terminal, even after payload lookup matched.
        with self.assertRaises(receipts.PromptReceiptError):
            self.reserve(terminal={**TERMINAL, 'id': 'e' * 32})

    def test_changed_terminal_native_conversation_is_rejected_before_reservation(self):
        for change in ({'sessionId': '019aaaaa-0000-7000-8000-000000000002'},
                       {'cwd': '/another/project'}, {'host': 'another-host'}):
            with self.subTest(change=change), self.assertRaises(receipts.PromptReceiptError) as raised:
                self.reserve(terminal={**TERMINAL, 'nativeSource': {**SOURCE, **change}})
            self.assertEqual(raised.exception.status, 400)
        self.assertFalse(self.ledger.path.exists())

    def test_claude_changed_conversation_and_cwd_have_distinct_keys(self):
        self.reserve()
        for change in ({'sessionId': '019aaaaa-0000-7000-8000-000000000002'}, {'cwd': '/another/project'}):
            source = {**SOURCE, **change}
            terminal = {**TERMINAL, 'nativeSource': source}
            with self.subTest(change=change):
                self.assertEqual(self.ledger.lookup(source, RID)['status'], 'not_found')
                self.assertTrue(self.reserve(source=source, terminal=terminal)['reserved'])
        self.assertEqual(self.ledger.lookup({**SOURCE, 'home': '.claude'}, RID)['status'], 'unconfirmed')

    def test_codex_host_and_home_are_distinct_and_its_terminal_must_match(self):
        base = {'host': 'fixture-mini', 'agent': 'codex', 'id': SOURCE['sessionId'],
                'home': '.codex', 'cwd': SOURCE['cwd']}
        for change in ({}, {'home': '.codex-personal'}, {'host': 'fixture-pc'}):
            source = {**base, **change}
            self.assertTrue(self.reserve(source=source, terminal={**TERMINAL, 'nativeSource': source})['reserved'])
        with self.assertRaises(receipts.PromptReceiptError):
            self.reserve(source=base, terminal={**TERMINAL, 'nativeSource': {**base, 'home': '.codex-other'}})

    def test_same_fingerprint_new_explicit_request_is_not_confused_with_retry(self):
        self.reserve()
        self.finish()
        self.assertTrue(self.reserve(request_id=RID + '-new')['reserved'])
        self.assertFalse(self.reserve()['reserved'])

    def test_only_unfinished_reserved_record_can_be_cancelled_after_proven_not_sent(self):
        self.reserve()
        self.assertTrue(self.ledger.cancel_not_sent(SOURCE, RID, FINGERPRINT, 1, TERMINAL))
        self.assertEqual(self.ledger.lookup(SOURCE, RID)['status'], 'not_found')
        self.assertTrue(self.reserve()['reserved'])
        self.finish(status='unconfirmed', reason_code='screen_unconfirmed')
        self.assertFalse(self.ledger.cancel_not_sent(SOURCE, RID, FINGERPRINT, 1, TERMINAL))
        self.assertFalse(self.reserve()['reserved'])

    def test_limit_blocks_new_keys_but_keeps_existing_unknown_and_answered_records(self):
        limited = receipts.PromptReceipts(self.root, max_records=1)
        self.reserve(ledger=limited)
        with self.assertRaises(receipts.PromptReceiptError) as raised:
            self.reserve(ledger=limited, request_id=RID + '-another')
        self.assertEqual(raised.exception.code, 'prompt_receipts_full')
        self.assertFalse(self.reserve(ledger=limited)['reserved'])
        self.finish(ledger=limited)
        with self.assertRaises(receipts.PromptReceiptError):
            self.reserve(ledger=limited, request_id=RID + '-another')

    def test_concurrent_instances_only_one_reserves_an_existing_database_request(self):
        # Initialize before racing so this checks the durable request transaction itself.
        self.reserve(request_id=RID + '-initial')
        ledgers = [receipts.PromptReceipts(self.root) for _ in range(8)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda ledger: self.reserve(ledger=ledger), ledgers))
        self.assertEqual(sum(item['reserved'] for item in results), 1)

    def test_final_storage_failure_keeps_committed_reservation_and_blocks_replay(self):
        self.reserve()
        self.ledger.path.chmod(0o644)
        with self.assertRaises(receipts.PromptReceiptError):
            self.finish()
        self.ledger.path.chmod(0o600)
        restarted = receipts.PromptReceipts(self.root)
        self.assertEqual(restarted.lookup(SOURCE, RID)['status'], 'unconfirmed')
        self.assertFalse(self.reserve(ledger=restarted)['reserved'])

    def test_corrupt_oversized_or_empty_database_does_not_create_a_fresh_request(self):
        for kind in ('corrupt', 'oversized', 'empty'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as root:
                ledger = receipts.PromptReceipts(root)
                self.reserve(ledger=ledger)
                if kind == 'corrupt':
                    ledger.path.write_bytes(b'corrupt' * 100)
                elif kind == 'empty':
                    ledger.path.write_bytes(b'')
                else:
                    with ledger.path.open('r+b') as stream:
                        stream.truncate(receipts.MAX_DATABASE_BYTES + 1)
                for action in (lambda: ledger.lookup(SOURCE, RID),
                               lambda: self.reserve(ledger=ledger, request_id=RID + '-new')):
                    with self.assertRaises(receipts.PromptReceiptError):
                        action()

    def test_symlink_hardlink_and_unsafe_journal_fail_closed(self):
        self.reserve()
        outside = self.root / 'outside'
        outside.write_bytes(b'untouched')
        outside.chmod(0o600)
        journal = self.ledger.path.with_name(self.ledger.path.name + '-journal')
        journal.symlink_to(outside)
        with self.assertRaises(receipts.PromptReceiptError):
            self.reserve(request_id=RID + '-new')
        self.assertEqual(outside.read_bytes(), b'untouched')
        journal.unlink()
        linked = self.root / 'linked'
        os.link(self.ledger.path, linked)
        with self.assertRaises(receipts.PromptReceiptError):
            self.reserve(request_id=RID + '-new')
        linked.unlink()
        self.ledger.path.unlink()
        self.ledger.path.symlink_to(outside)
        with self.assertRaises(receipts.PromptReceiptError):
            self.ledger.lookup(SOURCE, RID)

    def test_sqlite_full_commit_and_no_raw_identifiers_or_prompt_text_are_persisted(self):
        source = {**SOURCE, 'cwd': '/PRIVATE-FIXTURE-PATH', 'question': 'PRIVATE-FIXTURE-QUESTION',
                  'input': 'PRIVATE-FIXTURE-INPUT', 'token': 'PRIVATE-FIXTURE-TOKEN'}
        terminal = {**TERMINAL, 'nativeSource': source}
        self.reserve(source=source, terminal=terminal)
        raw = self.ledger.path.read_bytes()
        for value in ('PRIVATE-FIXTURE', SOURCE['id'], SOURCE['sessionId'], RID, FINGERPRINT):
            self.assertNotIn(value.encode(), raw)
        with self.ledger._connection() as db:
            self.assertEqual(db.execute('PRAGMA synchronous').fetchone()[0], 2)
            self.assertEqual(db.execute('PRAGMA journal_mode').fetchone()[0], 'delete')
            page_size = db.execute('PRAGMA page_size').fetchone()[0]
            pages = db.execute('PRAGMA max_page_count').fetchone()[0]
            self.assertLessEqual(page_size * pages, receipts.MAX_DATABASE_BYTES)

    def test_small_byte_cap_blocks_before_another_reservation_commit(self):
        with patch('prompt_receipts.MAX_DATABASE_BYTES', 16 * 1024):
            reserved = 0
            for index in range(1000):
                try:
                    self.reserve(request_id=RID + '-' + str(index))
                except receipts.PromptReceiptError:
                    break
                reserved += 1
            self.assertGreater(reserved, 0)
            self.assertLess(reserved, 1000)
            self.assertLessEqual(self.ledger.path.stat().st_size, 16 * 1024)
            self.assertFalse(self.reserve(request_id=RID + '-0')['reserved'])

    def test_low_disk_and_missing_finish_record_fail_closed(self):
        with self.assertRaises(receipts.PromptReceiptError):
            self.finish()
        self.reserve()
        with patch('prompt_receipts.sqlite3.connect', side_effect=sqlite3.OperationalError('full')), \
                patch('prompt_receipts.shutil.disk_usage', return_value=type('Usage', (), {'free': 0})()):
            with self.assertRaises(receipts.PromptReceiptError) as raised:
                self.reserve(request_id=RID + '-new')
        self.assertIn('디스크', str(raised.exception))

    def test_subprocess_crashes_never_replay_reserved_or_written_choice(self):
        script = '''
import json, os, pathlib, sys
from prompt_receipts import PromptReceipts
root, phase, payload = sys.argv[1:]
source, terminal, rid, fingerprint = json.loads(payload)
ledger = PromptReceipts(root)
result = ledger.reserve(source, rid, fingerprint, 1, terminal)
if result['reserved']:
    if phase == 'after_reserve': os._exit(71)
    fd = os.open(str(pathlib.Path(root)/'fake-writer-count'), os.O_WRONLY|os.O_CREAT|os.O_APPEND, 0o600)
    os.write(fd, b'one choice\\n'); os.fsync(fd); os.close(fd)
    if phase == 'after_send': os._exit(72)
    status = 'answered'
    if phase == 'before_finish': os._exit(73)
    ledger.finish(source, rid, fingerprint, 1, terminal, status=status)
'''
        payload = json.dumps([SOURCE, TERMINAL, RID, FINGERPRINT])
        for phase, code, writes in (('after_reserve', 71, 0), ('after_send', 72, 1), ('before_finish', 73, 1)):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as root:
                args = [sys.executable, '-c', script, root, phase, payload]
                crashed = subprocess.run(args, capture_output=True, text=True, timeout=10)
                self.assertEqual(crashed.returncode, code, crashed.stderr)
                # A second process sees the committed reservation and never reaches the fake writer.
                replay = subprocess.run([*args[:4], 'finish', payload], capture_output=True, text=True, timeout=10)
                self.assertEqual(replay.returncode, 0, replay.stderr)
                writer = Path(root) / 'fake-writer-count'
                self.assertEqual(len(writer.read_text().splitlines()) if writer.exists() else 0, writes)
                self.assertEqual(receipts.PromptReceipts(root).lookup(SOURCE, RID)['status'], 'unconfirmed')


if __name__ == '__main__':
    unittest.main()
