'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const context = require('../web/session_context.js');
const user = (text, extra = {}) => ({ role: 'user', text, ...extra });
const answer = (text, extra = {}) => ({ role: 'assistant', text, ...extra });

test('final answer remains the selected result after a later commentary message', () => {
  const result = context.project([user('작업 요청'), answer('최종 결과', { phase: 'final_answer' }),
    answer('후속 상태 안내', { phase: 'commentary' })]);
  assert.equal(result.lastRequest.text, '작업 요청');
  assert.equal(result.lastResult.text, '최종 결과');
  assert.equal(result.lastResult.sourceType, 'final_answer');
  assert.equal(result.method, 'extractive');
  assert.equal(result.completed, undefined);
});

test('a new request with no answer never inherits a previous result or next action', () => {
  const result = context.project([user('이전 요청'), answer('완료\n다음: 배포', { phase: 'final_answer' }), user('새 요청')]);
  assert.equal(result.lastRequest.text, '새 요청');
  assert.equal(result.lastResult, null);
  assert.equal(result.next, null);
});

test('commentary alone is progress and an ordinary saved answer remains usable', () => {
  assert.equal(context.project([user('질문'), answer('진행 중', { phase: 'commentary' })]).lastResult, null);
  assert.equal(context.project([user('질문'), answer('저장된 답')]).lastResult.text, '저장된 답');
});

test('only explicit labeled or structured next actions are extracted', () => {
  const result = context.project([user('질문'), answer('결과\n\n다음 단계: 검증을 실행합니다.\n\n완료 근거: 아직 없음')]);
  assert.equal(result.next.text, '검증을 실행합니다.');
  assert.equal(result.next.sourceType, 'explicit_label');
  const structured = context.project([user('질문'), answer('다음: 일반 안내', { work: { next: '명시된 구조화 단계' } })]);
  assert.equal(structured.next.text, '명시된 구조화 단계');
  assert.equal(structured.next.sourceType, 'structured_work');
  for (const text of ['다음으로 무엇을 할까요?', '테스트할까요?', '승인해 주세요.', '아마 배포하면 됩니다.'])
    assert.equal(context.project([user('질문'), answer(text)]).next, null);
});

test('code, quotes and other explicit fields cannot become the next action', () => {
  const text = '```md\n다음: 실행하지 않을 예시\n```\n> 다음: 인용한 내용\n\n실제 결과입니다.';
  assert.equal(context.project([user('질문'), answer(text)]).next, null);
  assert.equal(context.labeledNext('## 다음 행동\n검증할 것\n완료 근거: 성공'), '검증할 것');
  assert.equal(context.labeledNext('````md\n```\n다음: fenced 예시\n````'), '');
});

test('native IDs help identify the message but only bodyHash and recordOffset make an exact anchor', () => {
  const result = context.project([user('질문'), answer('결과', { id: 'item-a', messageId: 'message-a', turnId: 'turn-a' })]);
  assert.equal(result.lastResult.anchor, null);
  assert.equal(result.lastResult.id, 'item-a');
  assert.equal(result.lastResult.messageId, 'message-a');
  assert.equal(result.lastResult.turnId, 'turn-a');
  const exact = context.project([user('질문'), answer('결과', { bodyHash: 'a'.repeat(64), recordOffset: 0 })]);
  assert.deepEqual(exact.lastResult.anchor, { bodyHash: 'a'.repeat(64), recordOffset: 0, role: 'assistant' });
  for (const fields of [{ bodyHash: 'a'.repeat(64) }, { recordOffset: 0 }, { bodyHash: 'bad', recordOffset: 4 },
                        { bodyHash: 'a'.repeat(64), recordOffset: -1 }])
    assert.equal(context.project([answer('답', fields)]).lastResult.anchor, null);
});

test('truncated excerpts and the read range carry their actual limits and time metadata', () => {
  const text = '가'.repeat(5000);
  const result = context.project([user('질문', { ts: 10 }), answer(text, { ts: 20, truncated: true, fullLength: 10000 })],
    { olderOmitted: true, fetchedAt: 30 });
  assert.equal(result.lastResult.text.length, context.LIMITS.result);
  assert.equal(result.lastResult.fullLength, 10000);
  assert.equal(result.lastResult.truncated, true);
  assert.deepEqual(result.scope, { messageCount: 2, requestInRange: true, olderOmitted: true,
    truncated: true, stale: false, offline: false, fetchedAt: 30, firstTs: 10, lastTs: 20 });
});

test('offline or stale reads stay marked and never manufacture a current observation time', () => {
  const result = context.project([answer('마지막 저장 결과', { ts: 10 })], { online: false, fetchedAt: 20 });
  assert.equal(result.scope.offline, true);
  assert.equal(result.scope.stale, true);
  assert.equal(result.scope.fetchedAt, 20);
  assert.equal(result.scope.requestInRange, false);
  assert.equal(context.project([], null).scope.fetchedAt, null);
});

test('projection is pure and does not use mutable cross-session state', () => {
  const rows = Object.freeze([Object.freeze(user('질문')), Object.freeze(answer('결과\n다음: 확인'))]);
  const meta = Object.freeze({ fetchedAt: 100, stale: true });
  const before = JSON.stringify([rows, meta]);
  assert.deepEqual(context.project(rows, meta), context.project(rows, meta));
  assert.equal(JSON.stringify([rows, meta]), before);
  assert.equal(context.project([user('다른 작업')]).lastResult, null);
});

test('browser global works without document, fetch or an automatic-work service', () => {
  const sandbox = vm.createContext({});
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../web/session_context.js'), 'utf8'), sandbox);
  assert.equal(sandbox.SessionholicContext.project([user('질문')]).lastRequest.text, '질문');
  assert.equal(sandbox.SessionholicContext.project([]).scope.messageCount, 0);
});

test('result evidence contains only explicit safe links and file references, with quoted validation unverified', () => {
  const result = context.project([user('현재 요청'), answer('[보고서](https://example.com/report) [소스](/work/app.py:12)\n' +
    '`docs/report.md`\n검증: 단위 테스트 12개 통과\n\n[실행](javascript:alert) [비밀](https://user:password@example.com/)\n' +
    '```md\n[예시](https://example.com/not-a-result)\n```\n> [인용](https://example.com/quote)')]);
  assert.deepEqual(result.evidence.artifacts.map((r) => r.target), ['https://example.com/report', '/work/app.py:12', 'docs/report.md']);
  assert.equal(result.evidence.verification, '단위 테스트 12개 통과');
  assert.equal(result.completed, undefined);
  assert.deepEqual(context.project([answer('[이전](https://example.com/old)'), user('새 요청')]).evidence.artifacts, []);
});
