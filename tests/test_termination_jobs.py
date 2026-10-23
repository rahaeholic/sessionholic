"""Durable coordination; no real native control or transport."""
import concurrent.futures
import json
from pathlib import Path
import tempfile
import threading
import time
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

    def test_slow_host_does_not_delay_other_source_submit_or_background_readback(self):
        for background in (False, True):
            with self.subTest(background=background):
                entered, other_entered, release = threading.Event(), threading.Event(), threading.Event()
                calls = []
                def runner(job, initial):
                    calls.append(initial)
                    if job['source']['host'] == 'slow':
                        entered.set()
                        if not release.wait(3): raise TimeoutError('synthetic wait')
                    else:
                        other_entered.set()
                    return {'confirmed': True, 'cleanup': {'complete': True}}
                self.jobs.runner = runner
                slow, fast = ({**self.source, 'host': host} for host in ('slow', 'fast'))
                with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
                    if background:
                        self.jobs.claim(slow, 'background-slow', {})
                        self.jobs.claim(fast, 'background-fast', {})
                        with self.jobs.db() as db: db.execute('UPDATE jobs SET due=0 WHERE active=1')
                        pending = pool.submit(self.jobs.tick)
                    else:
                        pending = pool.submit(self.jobs.submit, slow, 'initial-slow', {}, {'proof': 'fixture'})
                    try:
                        self.assertTrue(entered.wait(2))
                        if not background:
                            other = pool.submit(self.jobs.submit, fast, 'initial-fast', {}, {'proof': 'fixture'})
                        self.assertTrue(other_entered.wait(2), 'another source must progress before slow host releases')
                    finally:
                        release.set()
                    pending.result(timeout=3)
                    if not background: other.result(timeout=3)
                self.assertEqual(len(calls), 2)
                self.assertEqual(sum(call is not None for call in calls), 0 if background else 2)
                self.assertEqual(self.jobs.source_locks, {}, 'idle source locks are released')

    def test_duplicate_submit_and_tick_wait_for_same_source_initial_result(self):
        entered, release = threading.Event(), threading.Event()
        def runner(job, initial):
            entered.set()
            if not release.wait(3): raise TimeoutError('synthetic wait')
            return {'confirmed': True, 'cleanup': {'complete': True}}
        self.runner.side_effect = runner
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            first = pool.submit(self.jobs.submit, self.source, 'same-initial', {}, {'proof': 'fixture'})
            self.assertTrue(entered.wait(2))
            duplicate = pool.submit(self.jobs.submit, self.source, 'same-duplicate', {}, {'proof': 'fixture'})
            until = time.monotonic() + 2
            while time.monotonic() < until:
                with self.jobs.lock:
                    if next(iter(self.jobs.source_locks.values()))[1] >= 3: break
                time.sleep(.001)
            else:
                release.set()
                self.fail('duplicate did not join the initial request')
            with self.jobs.db() as db: db.execute('UPDATE jobs SET due=0')
            readback = pool.submit(self.jobs.tick)
            release.set()
            results = [first.result(timeout=3), duplicate.result(timeout=3)]
            readback.result(timeout=3)
        self.assertEqual({result['requestId'] for result in results}, {'same-initial'})
        self.assertEqual(self.runner.call_count, 1)
        self.assertEqual(self.jobs.source_locks, {})
