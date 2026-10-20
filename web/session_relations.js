/* Explicit session relationships only. No I/O, filtering, navigation or launch. */
(function (root, factory) {
  'use strict';
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.SessionholicRelations = api;
})(typeof globalThis === 'object' ? globalThis : this, function () {
  'use strict';
  const text = (value, limit) => typeof value === 'string' && value.length > 0 && value.length <= limit && !/[\x00-\x1f\x7f]/.test(value);
  function reference(session) {
    if (!session || !text(session.host, 40) || !['codex', 'claude'].includes(session.agent) || !text(session.id, 128)) return null;
    let home = session.home;
    if (session.agent === 'claude') {
      if (home != null && home !== '' && home !== '.claude') return null;
      home = '.claude';
    } else if (!text(home, 128)) return null;
    const ref = { host: session.host, agent: session.agent, home, id: session.id };
    if (session.agent === 'claude' && session.sessionId != null) {
      if (!text(session.sessionId, 64) || !/^[0-9A-Za-z-]{8,64}$/.test(session.sessionId)) return null;
      ref.sessionId = session.sessionId;
    }
    return ref;
  }
  function sessionIdentity(session) {
    const ref = reference(session);
    return ref ? JSON.stringify([ref.host, ref.agent, ref.home, ref.id]) : null;
  }
  function rowsOf(input) {
    if (Array.isArray(input)) return input;
    const rows = [];
    for (const host of input?.hosts || []) {
      for (const agent of ['codex', 'claude']) {
        for (const session of host.data?.[agent] || []) rows.push({ ...session, host: host.name, agent });
      }
    }
    return rows;
  }
  function parentReference(session) {
    const own = reference(session);
    if (!own) return { status: 'unresolved', reason: 'invalid_identity' };
    if (session.parentRef != null) {
      const ref = reference(session.parentRef);
      if (!ref) return { status: 'unresolved', reason: 'invalid_parent_ref' };
      if (['host', 'agent', 'home'].some((key) => ref[key] !== own[key])) return { status: 'unresolved', ref, reason: 'scope_mismatch' };
      return { status: 'declared', ref };
    }
    const ids = [...new Set(['parentThreadId', 'parent_thread_id', 'parentSessionId', 'parent_session_id']
      .map((key) => session[key]).filter((id) => text(id, 128)))];
    if (!ids.length) return { status: 'none' };
    if (session.agent !== 'codex') return { status: 'unresolved', reason: 'parent_namespace_unverified', parentId: ids[0] };
    if (ids.length !== 1) return { status: 'ambiguous', reason: 'conflicting_parent_ids' };
    return { status: 'declared', ref: { ...own, id: ids[0] } };
  }
  function buildRelationIndex(input) {
    const byIdentity = new Map(), parentOf = new Map(), childrenOf = new Map();
    for (const session of rowsOf(input)) {
      const key = sessionIdentity(session);
      if (key) byIdentity.set(key, [...(byIdentity.get(key) || []), session]);
    }
    for (const [key, sessions] of byIdentity) {
      if (sessions.length !== 1) {
        parentOf.set(key, { status: 'ambiguous', reason: 'duplicate_identity' });
        continue;
      }
      const declared = parentReference(sessions[0]);
      if (declared.status !== 'declared') { parentOf.set(key, declared); continue; }
      const identity = sessionIdentity(declared.ref), matches = byIdentity.get(identity) || [];
      if (matches.length === 1 && identity !== key && declared.ref.agent === 'claude' &&
          !declared.ref.sessionId && matches[0].sessionId && matches[0].id !== matches[0].sessionId) {
        parentOf.set(key, { status: 'unresolved', identity, ref: declared.ref,
          reason: 'parent_conversation_unverified', session: null });
        continue;
      }
      if (matches.length === 1 && declared.ref.sessionId &&
          (matches[0].sessionId || matches[0].id) !== declared.ref.sessionId) {
        parentOf.set(key, { status: 'unresolved', identity, ref: declared.ref, reason: 'parent_conversation_mismatch', session: null });
        continue;
      }
      const status = identity === key ? 'self_reference' : !matches.length ? 'missing' : matches.length > 1 ? 'ambiguous' : 'linked';
      parentOf.set(key, { status, identity, ref: declared.ref, session: status === 'linked' ? matches[0] : null,
        reason: status === 'ambiguous' ? 'duplicate_parent' : status === 'self_reference' ? 'self_reference' : null });
    }
    // Iterative walks keep deep native parent chains from overflowing the browser stack.
    const visited = new Set(), cycles = new Set();
    for (const start of byIdentity.keys()) {
      const path = [], positions = new Map();
      let key = start;
      while (key && !visited.has(key)) {
        if (positions.has(key)) { for (const item of path.slice(positions.get(key))) cycles.add(item); break; }
        positions.set(key, path.length); path.push(key);
        const parent = parentOf.get(key);
        key = parent?.status === 'linked' ? parent.identity : null;
      }
      for (const item of path) visited.add(item);
    }
    for (const key of cycles) parentOf.set(key, { ...parentOf.get(key), status: 'cycle', reason: 'cycle' });
    for (const [key, relation] of parentOf) {
      if (!relation.identity || relation.status === 'self_reference' || relation.status === 'unresolved') continue;
      const session = byIdentity.get(key)?.[0];
      if (!session || byIdentity.get(key).length !== 1) continue;
      childrenOf.set(relation.identity, [...(childrenOf.get(relation.identity) || []),
        { identity: key, session, status: relation.status, ref: reference(session) }]);
    }
    for (const children of childrenOf.values()) children.sort((a, b) => a.identity.localeCompare(b.identity));
    return { byIdentity, parentOf, childrenOf };
  }
  function relationsFor(index, sessionOrKey) {
    const identity = typeof sessionOrKey === 'string' ? sessionOrKey : sessionIdentity(sessionOrKey);
    const sessions = index?.byIdentity?.get(identity) || [];
    return { identity, session: sessions.length === 1 ? sessions[0] : null, ambiguous: sessions.length > 1,
      parent: { ...(index?.parentOf?.get(identity) || { status: 'none' }) },
      children: [...(index?.childrenOf?.get(identity) || [])] };
  }
  function summary(session) {
    return session ? [sessionIdentity(session), session.sessionId || null, session.title || '', session.displayTitle || '',
      session.phase || 'unknown', session.runtimeKnown ?? null, session.runtimeReason || '', session.updatedAt ?? null,
      session.agentRole || '', session.readOnly ?? null] : null;
  }
  function relationRevision(index, sessionOrKey) {
    const view = relationsFor(index, sessionOrKey);
    return JSON.stringify([view.identity, view.ambiguous, view.parent.status, view.parent.reason || null,
      view.parent.identity || null, summary(view.parent.session),
      view.children.map((child) => [child.identity, child.status, summary(child.session)])]);
  }
  return Object.freeze({ sessionIdentity, buildRelationIndex, relationsFor, relationRevision });
});
