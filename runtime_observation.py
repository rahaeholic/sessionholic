"""Server-clock runtime observations shared by Windows polls and selected reads.

Only an actual adapter read may issue an observation. Cached reads return its original
lease. A later-started request wins even when RPCs finish in the opposite order.
"""
from collections import OrderedDict
import copy
import json
import re
import secrets
import threading
import time

PHASES = frozenset(('working', 'needs_input', 'idle', 'done', 'closed'))
EVIDENCE = frozenset(('project_app_server', 'claude_registry_process', 'exec_writer_lock',
                      'external_cli_open', 'saved_record', 'unsupported'))
TIMING_FIELDS = frozenset(('observedAt', 'expiresAt', 'requestSeq', 'acceptedSeq'))


def semantic(value):
    """Drop observation timing only, keeping phase, certainty, reason and boot changes."""
    if isinstance(value, list):
        return [semantic(item) for item in value]
    if not isinstance(value, dict):
        return value
    return {key: ({k: semantic(v) for k, v in item.items() if k not in TIMING_FIELDS}
                  if key in ('runtimeObservation', 'lastKnown') and isinstance(item, dict) else semantic(item))
            for key, item in value.items() if key != 'runtimeReadToken'}


def native_result(result, agent):
    """A successful native Mac read's recognized phase; saved records prove no runtime."""
    known = (result.get('runtimeKnown') is not False and not result.get('error')
             and not result.get('stale') and result.get('appServer') != 'off'
             and isinstance(result.get('phase'), str) and result['phase'] in PHASES)
    return {**result, 'runtimeKnown': known,
            'runtimeEvidence': ('claude_registry_process' if agent == 'claude' else 'project_app_server') if known else 'unsupported'}


class RuntimeObservations:
    def __init__(self, clock=time.time, boot_id=None, limit=2048):
        self.clock, self.boot_id, self.limit = clock, boot_id or secrets.token_urlsafe(12), limit
        self.lock = threading.Lock()
        self.request_seq = self.accepted_seq = 0
        self.records = OrderedDict()
        self.failures = {}

    @staticmethod
    def key(source):
        identity = {key: source.get(key) for key in ('host', 'agent', 'home', 'id', 'cwd')}
        if identity['agent'] == 'claude':
            identity['home'] = identity['home'] or '.claude'
            identity['sessionId'] = source.get('sessionId')
        return json.dumps(identity, sort_keys=True)

    def begin(self):
        with self.lock:
            self.request_seq += 1
            return self.request_seq

    def current(self, source):
        with self.lock:
            return copy.deepcopy(self.records.get(self.key(source)))

    def reject_scope(self, host, agent, request_seq):
        """A failed adapter advances ordering without extending any confirmed lease."""
        with self.lock:
            scope = (host, agent)
            prior = self.failures.get(scope)
            if prior and prior[0] >= request_seq:
                return
            self.failures[scope] = (request_seq, self.clock())
            for key, record in list(self.records.items()):
                identity = json.loads(key)
                if (identity['host'], identity['agent']) == scope and record['runtimeObservation']['requestSeq'] < request_seq:
                    self.records[key] = self._failure(record, request_seq, self.failures[scope][1])

    def _failure(self, previous, request_seq, at):
        old = previous.get('runtimeObservation') if previous else None
        self.accepted_seq += 1
        observation = {'version': 1, 'known': False, 'phase': 'unknown', 'evidence': 'unsupported',
                       'reasonCode': 'source_read_failed', 'requestSeq': request_seq,
                       'acceptedSeq': self.accepted_seq, 'bootId': self.boot_id, 'observedAt': at,
                       'expiresAt': max(at, old['expiresAt']) if old else at}
        return {'runtimeObservation': observation,
                'lastKnown': copy.deepcopy(old if old and old['known'] else (previous or {}).get('lastKnown')),
                'runtimeReason': '수집에 실패해 마지막 목록을 보여 줍니다.'}

    def accept(self, source, result, request_seq, lease):
        key = self.key(source)
        with self.lock:
            previous = self.records.get(key)
            failure = self.failures.get((source.get('host'), source.get('agent')))
            if failure and request_seq <= failure[0] and (not previous or previous['runtimeObservation']['requestSeq'] < failure[0]):
                previous = self.records[key] = self._failure(previous, failure[0], failure[1])
                while len(self.records) > self.limit:
                    self.records.popitem(last=False)
            if previous and request_seq <= previous['runtimeObservation']['requestSeq']:
                return copy.deepcopy(previous)
            now = self.clock()
            phase = result.get('phase', 'unknown')
            # Explicit unknown never becomes certain just because a process was listed.
            known = result.get('runtimeKnown') is True and isinstance(phase, str) and phase in PHASES
            evidence = result.get('runtimeEvidence', 'unsupported')
            if not isinstance(evidence, str) or evidence not in EVIDENCE:
                evidence = 'unsupported'
            reason_code = result.get('runtimeReasonCode') or evidence
            if not isinstance(reason_code, str) or not re.fullmatch('[a-z][a-z0-9_]{0,79}', reason_code):
                reason_code = evidence
            self.accepted_seq += 1
            observation = {'version': 1, 'known': known, 'phase': phase if known else 'unknown',
                           'evidence': evidence, 'reasonCode': reason_code,
                           'requestSeq': request_seq, 'acceptedSeq': self.accepted_seq, 'bootId': self.boot_id,
                           'observedAt': now, 'expiresAt': now + max(1, lease)}
            last = None
            if previous:
                old = previous['runtimeObservation']
                last = old if old['known'] else previous.get('lastKnown')
            annotation = {'runtimeObservation': observation, 'lastKnown': copy.deepcopy(last) if not known else None,
                          'runtimeReason': result.get('runtimeReason')[:600] if isinstance(result.get('runtimeReason'), str) else None}
            self.records[key] = annotation
            self.records.move_to_end(key)
            while len(self.records) > self.limit:
                self.records.popitem(last=False)
            return copy.deepcopy(annotation)
