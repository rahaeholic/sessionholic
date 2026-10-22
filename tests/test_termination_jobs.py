"""Durable coordination; no real native control or transport."""
import concurrent.futures
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from termination_jobs import Jobs


class JobsTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.source = dict(host='fixture', agent='codex', home='.codex', id='synthetic', cwd='/fixture')
        self.runner = Mock(return_value={'confirmed': False, 'applied': None})
        self.jobs = Jobs(self.folder.name, self.runner)

    def test_crash_after_claim_restarts_with_receipt_only(self):
        job, created = self.jobs.claim(self.source, 'request-original', {})
        self.assertTrue(created)
        restarted = Jobs(self.folder.name, self.runner)
        with restarted.db() as db:
            db.execute('UPDATE jobs SET due=0')
        restarted.tick()
        self.assertIsNone(self.runner.call_args.args[1])
        self.assertEqual(self.runner.call_args.args[0]['requestId'], job['requestId'])
        self.assertEqual(restarted.pending(self.source)['attempts'], 1)
        self.assertEqual(Path(self.folder.name, 'termination-jobs.sqlite').stat().st_mode & 0o777, 0o600)

    def test_multiple_clients_and_restarts_have_one_native_dispatch(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda n: self.jobs.submit(self.source, f'request-{n}', {}, {'targetProof': 'private-proof'}), range(8)))
        self.assertEqual(len({r['requestId'] for r in results}), 1)
        self.assertEqual(sum(args.args[1] is not None for args in self.runner.call_args_list), 1)
        restarted = Jobs(self.folder.name, self.runner)
        restarted.submit(self.source, 'another-request', {}, {'targetProof': 'another-proof'})
        self.assertIsNone(self.runner.call_args.args[1])
        self.assertNotIn('proof', json.dumps(self.jobs.get(results[0]['requestId'])))

    def test_lost_reply_keeps_job_and_complete_cleanup_is_not_repeated(self):
        self.runner.side_effect = [OSError('lost'), {'confirmed': True, 'cleanup': {'complete': True}}]
        first = self.jobs.submit(self.source, 'request-original', {}, {'targetProof': 'proof'})
        self.assertEqual(first['job']['state'], 'pending')
        second = self.jobs.submit(self.source, 'request-original', {}, {'targetProof': 'proof'})
        self.assertEqual(second['job']['state'], 'complete')
        cached = self.jobs.submit(self.source, 'request-original', {})
        self.assertTrue(cached['historical'])
        self.assertEqual(self.runner.call_count, 2)
        self.assertIsNone(self.runner.call_args.args[1])

    def test_not_started_and_superseded_release_source_but_never_reuse_old_id(self):
        for reason in ('not_started', 'superseded'):
            with self.subTest(reason=reason):
                self.runner.return_value = {'confirmed': False, **({'applied': False, 'dispatchState': reason} if reason == 'not_started' else {'reconcileCode': reason})}
                result = self.jobs.submit(self.source, 'request-' + reason, {}, {'proof': 'x'})
                self.assertEqual(result['job']['state'], reason)
                self.assertIsNone(self.jobs.pending(self.source))
                calls = self.runner.call_count
                self.jobs.submit(self.source, 'request-' + reason, {}, {'proof': 'new'})
                self.assertEqual(self.runner.call_count, calls)
        with self.assertRaises(ValueError):
            self.jobs.submit({**self.source, 'cwd': '/other'}, 'request-not_started', {})

    def test_backoff_and_pending_cleanup_survive_restart(self):
        self.runner.return_value = {'confirmed': True, 'cleanup': {'complete': False, 'remotePending': ['fixture']}}
        first = self.jobs.submit(self.source, 'request-cleanup', {'fixture': 'stamp'}, {})
        self.assertEqual(first['job']['state'], 'pending')
        self.jobs.tick()
        self.assertEqual(self.runner.call_count, 1, 'not due: no tight polling')
        self.assertEqual(Jobs(self.folder.name, self.runner).pending(self.source)['terminals'], {'fixture': 'stamp'})

    def test_stale_preview_secrets_are_not_persisted(self):
        self.runner.return_value = {'confirmed': False, 'applied': False, 'preview': {'targetProof': 'private-proof'}}
        result = self.jobs.submit(self.source, 'request-stale', {}, {})
        self.assertNotIn('preview', result)
        self.assertNotIn('private-proof', json.dumps(self.jobs.get('request-stale')))

    def test_two_store_instances_atomically_claim_same_source(self):
        second = Jobs(self.folder.name, self.runner)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(j.claim, self.source, f'request-{i}', {}) for i, j in enumerate((self.jobs, second))]
            results = [f.result() for f in futures]
        self.assertEqual(sum(created for _, created in results), 1)
        self.assertEqual(results[0][0]['requestId'], results[1][0]['requestId'])
