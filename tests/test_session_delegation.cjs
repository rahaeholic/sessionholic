const { test } = require('node:test');
const assert = require('node:assert/strict');
const { buildRequest } = require('../web/session_delegation.js');

test('delegation is a scoped native request with explicit return location and edit intent', () => {
  const text = buildRequest({ kind: 'review', task: '이 변경 검토', completion: '문제와 근거 반환' });
  assert.match(text, /검토/); assert.match(text, /허용하지 않습니다/); assert.match(text, /부모 대화/);
  assert.match(text, /지원 여부나 권한을 확인할 수 없으면 실행하지 말고/);
  assert.match(text, /추가 발췌 없음/); assert.doesNotMatch(text, /undefined/);
  assert.match(buildRequest({ kind: 'validation', task: '테스트', completion: '결과', allowEdits: true, materials: '선택 발췌' }), /선택 발췌/);
  assert.throws(() => buildRequest({ kind: 'review', task: '', completion: '결과' }));
  assert.throws(() => buildRequest({ kind: 'other', task: '검토', completion: '결과' }));
});
