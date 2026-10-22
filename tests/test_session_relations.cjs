'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const relations = require('../web/session_relations.js');
const row = (id, values = {}) => ({ host: 'mini', agent: 'codex', home: '.codex', id, title: id, phase: 'idle', ...values });
const view = (rows, selected) => relations.relationsFor(relations.buildRelationIndex(rows), selected);

test('identity preserves host, agent and account boundaries and only normalizes the default Claude account', () => {
  const base = row('same');
  for (const change of [{ host: 'air' }, { home: '.codex-personal' }, { agent: 'claude', home: null }])
    assert.notEqual(relations.sessionIdentity(base), relations.sessionIdentity({ ...base, ...change }));
  const claude = row('worker-a1', { agent: 'claude', home: null });
  assert.equal(relations.sessionIdentity(claude), relations.sessionIdentity({ ...claude, home: '.claude' }));
  assert.equal(relations.sessionIdentity({ ...claude, home: '.claude-other' }), null);
  assert.equal(relations.sessionIdentity({ ...base, home: null }), null);
  assert.equal(relations.sessionIdentity({ ...base, id: '' }), null);
});

test('a native Codex parent remains linked outside the current filtered view and never crosses scope', () => {
  const parent = row('parent'), child = row('child', { parentSessionId: 'parent' });
  const sameIdElsewhere = row('parent', { host: 'air' }), anotherAccount = row('parent', { home: '.codex-other' });
  const index = relations.buildRelationIndex([parent, child, sameIdElsewhere, anotherAccount]);
  assert.equal(relations.relationsFor(index, child).parent.session, parent);
  assert.equal(relations.relationsFor(index, parent).children[0].session, child);
  assert.equal(relations.relationsFor(index, sameIdElsewhere).children.length, 0);
  assert.equal(relations.relationsFor(index, anotherAccount).children.length, 0);
});

test('typed parent references are validated and explicit cross-scope references stay unresolved', () => {
  const parent = row('parent'), typed = row('child', { parentRef: parent });
  assert.equal(view([parent, typed], typed).parent.status, 'linked');
  for (const parentRef of [{ id: 'parent' }, { ...parent, host: 'air' }, { ...parent, home: '.codex-other' }]) {
    const child = { ...typed, parentRef };
    assert.equal(view([parent, child], child).parent.status, 'unresolved');
  }
});

test('Claude transcript/worker namespace is never inferred from parentSessionId', () => {
  const parent = row('worker-parent', { agent: 'claude', home: null, sessionId: 'transcript-parent' });
  for (const parentSessionId of ['worker-parent', 'transcript-parent']) {
    const child = row('worker-child', { agent: 'claude', home: '.claude', parentSessionId });
    const result = view([parent, child], child);
    assert.equal(result.parent.status, 'unresolved');
    assert.equal(result.parent.reason, 'parent_namespace_unverified');
  }
  const child = row('worker-child', { agent: 'claude', home: '.claude', parentRef: parent });
  assert.equal(view([parent, child], child).parent.status, 'linked');
  const reused = { ...child, parentRef: { ...parent, sessionId: 'another-transcript' } };
  assert.equal(view([parent, reused], reused).parent.reason, 'parent_conversation_mismatch');
  const unspecified = { ...parent }; delete unspecified.sessionId;
  const unverified = { ...child, parentRef: unspecified };
  assert.equal(view([parent, unverified], unverified).parent.reason, 'parent_conversation_unverified');
});

test('missing, duplicate, conflicting and self references stay distinct without choosing a row', () => {
  const child = row('child', { parentThreadId: 'parent' });
  assert.equal(view([child], child).parent.status, 'missing');
  const duplicate = [row('parent'), row('parent')];
  assert.equal(view([...duplicate, child], child).parent.status, 'ambiguous');
  const ambiguousParent = view(duplicate, duplicate[0]);
  assert.equal(ambiguousParent.ambiguous, true);
  assert.equal(ambiguousParent.session, null);
  const conflict = { ...child, parentSessionId: 'another' };
  assert.equal(view([conflict], conflict).parent.reason, 'conflicting_parent_ids');
  const self = row('self', { parentThreadId: 'self' });
  const result = view([self], self);
  assert.equal(result.parent.status, 'self_reference');
  assert.equal(result.children.length, 0);
});

test('multi-level cycles are marked and deep parent chains use bounded iterative walks', () => {
  const a = row('a', { parentThreadId: 'c' }), b = row('b', { parentThreadId: 'a' }), c = row('c', { parentThreadId: 'b' });
  const index = relations.buildRelationIndex([a, b, c]);
  for (const session of [a, b, c]) assert.equal(relations.relationsFor(index, session).parent.status, 'cycle');
  const rows = Array.from({ length: 6000 }, (_, n) => row(String(n), n ? { parentThreadId: String(n - 1) } : {}));
  const deep = relations.buildRelationIndex(rows);
  assert.equal(relations.relationsFor(deep, rows[5999]).parent.status, 'linked');
});

test('snapshot host envelopes supply scope and do not mutate their original rows', () => {
  const parent = Object.freeze({ id: 'parent', home: '.codex' });
  const child = Object.freeze({ id: 'child', home: '.codex', parentThreadId: 'parent' });
  const snapshot = { hosts: [{ name: 'mini', data: { codex: [parent, child], claude: [] } }] };
  const before = JSON.stringify(snapshot), index = relations.buildRelationIndex(snapshot);
  assert.equal(relations.relationsFor(index, row('child')).parent.status, 'linked');
  assert.equal(JSON.stringify(snapshot), before);
});

test('a parent revision changes only for its relevant relationships and child metadata', () => {
  const parent = row('parent'), child = row('child', { parentSessionId: 'parent', phase: 'working' });
  const first = relations.buildRelationIndex([parent, child]);
  const revision = relations.relationRevision(first, parent);
  const reordered = relations.buildRelationIndex([child, parent]);
  assert.equal(relations.relationRevision(reordered, parent), revision);
  const unrelated = relations.buildRelationIndex([parent, child, row('other', { phase: 'working' })]);
  assert.equal(relations.relationRevision(unrelated, parent), revision);
  const done = relations.buildRelationIndex([parent, { ...child, phase: 'done', title: '완료 결과', updatedAt: 300 }]);
  assert.notEqual(relations.relationRevision(done, parent), revision);
  assert.equal(child.phase, 'working');
});

test('browser global works with no DOM or network APIs', () => {
  const context = vm.createContext({});
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../web/session_relations.js'), 'utf8'), context);
  assert.equal(typeof context.SessionholicRelations.buildRelationIndex, 'function');
  assert.equal(context.SessionholicRelations.relationsFor(context.SessionholicRelations.buildRelationIndex([]), row('none')).children.length, 0);
});
