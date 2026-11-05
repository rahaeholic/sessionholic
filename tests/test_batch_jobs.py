"""Only synthetic chats; these tests never contact an agent or send real messages."""
import copy
from pathlib import Path
import tempfile
import unittest

from batch_jobs import BatchJobs
from chat import SendRejected


class Chat:
    def __init__(self):
        self.calls = []
        self.result = {'status': 'accepted', 'receipt': {'confirmed': True}}
        self.receipts = {}

    def source(self, ref, sending=True):
        if not isinstance(ref, dict) or ref.get('id') not in ('first', 'second'):
            raise ValueError('unknown source')
        return {'host': 'fixture', 'agent': 'codex', 'home': '.codex', 'id': ref['id'],
                'cwd': '/fixture/' + ref['id']}, {'name': 'fixture'}

    def identity(self, source):
        return {k: source[k] for k in ('host', 'agent', 'home', 'id')}

    def _direct_session(self, source):
        pass

    def send(self, data):
        self.calls.append(copy.deepcopy(data))
        if isinstance(self.result, Exception): raise self.result
        return copy.deepcopy(self.result)

    def state(self, data):
        return {'receipts': [self.receipts[data['requestId']]] if data['requestId'] in self.receipts else []}


class BatchJobsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.chat = Chat(); self.jobs = BatchJobs(self.tmp.name, self.chat); self.addCleanup(self.jobs.close)
        self.owner = {'id': 'first'}
        self.record = {'id': 'batch-0001', 'tasks': [
            {'source': {'id': name, 'cwd': '/fixture/' + name}, 'identity': name,
             'requestId': 'request-' + name, 'title': name, 'text': '합성 작업 ' + name}
            for name in ('first', 'second')]}

    def status(self):
        return self.jobs.get(self.owner)['record']

    def test_reviewed_plan_survives_browser_and_sends_once_in_order(self):
        self.jobs.start(self.owner, self.record)
        self.jobs.tick(); self.jobs.tick(); self.jobs.tick()
        self.assertEqual([item['text'] for item in self.chat.calls], ['합성 작업 first', '합성 작업 second'])
        self.assertTrue(all(item['requireIdle'] for item in self.chat.calls))
        self.assertFalse(self.status()['running'])
        self.assertEqual([t['status'] for t in self.status()['tasks']], ['accepted', 'accepted'])
        self.jobs.start(self.owner, self.record)
        self.jobs.tick(); self.assertEqual(len(self.chat.calls), 2)
        self.assertEqual(Path(self.jobs.path).stat().st_mode & 0o777, 0o600)

    def test_unknown_stops_and_requires_receipt_or_explicit_skip(self):
        self.chat.result = {'status': 'unknown', 'receipt': {'confirmed': False}}
        self.jobs.start(self.owner, self.record); self.jobs.tick(); self.jobs.tick()
        self.assertEqual(len(self.chat.calls), 1)
        self.assertEqual(self.status()['tasks'][0]['status'], 'unknown')
        with self.assertRaises(ValueError): self.jobs.control(self.owner, 'resume')
        self.chat.receipts['request-first'] = {'requestId': 'request-first', 'confirmed': True, 'status': 'accepted'}
        self.jobs.check(self.owner)
        self.chat.result = {'status': 'accepted', 'receipt': {'confirmed': True}}
        self.jobs.control(self.owner, 'resume'); self.jobs.tick()
        self.assertEqual(len(self.chat.calls), 2)

    def test_restart_marks_interrupted_send_unknown_without_retry(self):
        self.jobs.start(self.owner, self.record)
        owner = self.jobs._owner(self.owner)
        with self.jobs.db() as db:
            job = self.jobs._get(db, owner); job['tasks'][0]['status'] = 'sending'; self.jobs._put(db, owner, job)
        self.jobs.close(); restarted = BatchJobs(self.tmp.name, self.chat); self.addCleanup(restarted.close)
        self.assertEqual(restarted.get(self.owner)['record']['tasks'][0]['status'], 'unknown')
        restarted.tick(); self.assertFalse(self.chat.calls)

    def test_restart_continues_ready_plan_without_a_browser(self):
        self.jobs.start(self.owner, self.record)
        self.jobs.close(); restarted = BatchJobs(self.tmp.name, self.chat); self.addCleanup(restarted.close)
        restarted.tick(); restarted.tick(); restarted.tick()
        self.assertEqual(len(self.chat.calls), 2)
        self.assertFalse(restarted.get(self.owner)['record']['running'])

    def test_a_second_server_cannot_dispatch_the_same_plan(self):
        self.jobs.start(self.owner, self.record)
        second = BatchJobs(self.tmp.name, self.chat); self.addCleanup(second.close)
        self.assertFalse(second.worker_owner)
        second.tick(); self.assertFalse(self.chat.calls)
        self.jobs.tick(); second.tick()
        self.assertEqual(len(self.chat.calls), 1)

    def test_stop_and_cwd_mismatch_prevent_dispatch(self):
        bad = copy.deepcopy(self.record); bad['tasks'][0]['source']['cwd'] = '/fixture/changed'
        with self.assertRaises(ValueError): self.jobs.start(self.owner, bad)
        self.jobs.start(self.owner, self.record); self.jobs.control(self.owner, 'stop'); self.jobs.tick()
        self.assertFalse(self.chat.calls)

    def test_rejection_stops_remaining_and_same_id_cannot_change_payload(self):
        self.jobs.start(self.owner, self.record)
        changed = copy.deepcopy(self.record); changed['tasks'][0]['text'] = '다른 내용'
        with self.assertRaises(ValueError): self.jobs.start(self.owner, changed)
        self.chat.result = SendRejected('busy')
        self.jobs.tick(); self.jobs.tick()
        self.assertEqual(len(self.chat.calls), 1)
        self.assertEqual(self.status()['tasks'][0]['status'], 'rejected')
        self.assertFalse(self.status()['running'])


if __name__ == '__main__': unittest.main()
