/* Extract only evidence already read for the selected conversation. No I/O. */
(function (root, factory) {
  'use strict';
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.SessionholicContext = api;
})(typeof globalThis === 'object' ? globalThis : this, function () {
  'use strict';
  const LIMITS = Object.freeze({ request: 1600, result: 4000, next: 2000 });
  const NEXT_LABELS = new Set(['다음', '다음 단계', '다음 행동', 'next', 'next steps']);
  const OTHER_LABELS = new Set(['제목', '작업명', 'title', '현재', '진행 상황', '현재 작업', 'current',
    '대기 이유', '대기 사유', 'waiting reason', '완료 조건', '완료 기준', 'completion criteria',
    '완료 근거', '검증 결과', 'evidence']);
  const clean = (value) => typeof value === 'string' ? value.replace(/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g, '').trim() : '';
  const stamp = (value) => typeof value === 'number' && Number.isFinite(value) ? value : null;
  function exactAnchor(message) {
    return typeof message.bodyHash === 'string' && /^[a-f0-9]{64}$/.test(message.bodyHash) &&
      Number.isSafeInteger(message.recordOffset) && message.recordOffset >= 0
      ? { bodyHash: message.bodyHash, recordOffset: message.recordOffset, role: message.role } : null;
  }
  function excerpt(message, index, limit, sourceType, value = message.text) {
    const full = clean(value), text = Array.from(full).slice(0, limit).join('');
    const result = { text, ts: stamp(message.ts), index, sourceType, anchor: exactAnchor(message),
      truncated: message.truncated === true || text.length < full.length,
      fullLength: Number.isSafeInteger(message.fullLength) && message.fullLength >= 0 ? message.fullLength : full.length };
    // IDs are useful context, but are never promoted to a history API anchor.
    for (const key of ['id', 'messageId', 'turnId']) if (typeof message[key] === 'string' && message[key].length <= 200) result[key] = message[key];
    return result;
  }
  function labeledNext(text) {
    let active = false, lines = [], found = '', fence = null;
    function flush() { if (active && lines.length) found = clean(lines.join('\n')); }
    for (const raw of clean(text).split('\n')) {
      const stripped = raw.trim(), marker = stripped.match(/^(`{3,}|~{3,})(.*)$/);
      if (marker) {
        if (!fence) { flush(); active = false; lines = []; fence = marker[1]; }
        else if (marker[1][0] === fence[0] && marker[1].length >= fence.length && !marker[2].trim()) fence = null;
        continue;
      }
      if (fence || stripped.startsWith('>')) continue;
      const cleaned = stripped.replace(/^[#*\-\s]+|[#*\-\s]+$/g, '');
      const pair = cleaned.match(/^([^:：]{1,40})\s*[:：]\s*(.*)$/);
      const label = (pair ? pair[1] : cleaned).replace(/^[ *]+|[ *]+$/g, '').toLowerCase();
      if (NEXT_LABELS.has(label)) {
        flush(); active = true; lines = [];
        if (pair && pair[2].replace(/^[ *]+|[ *]+$/g, '')) lines.push(pair[2].replace(/^[ *]+|[ *]+$/g, ''));
      } else if (OTHER_LABELS.has(label)) {
        flush(); active = false; lines = [];
      } else if (active && stripped) {
        if (stripped.startsWith('#')) { flush(); active = false; lines = []; }
        else lines.push(stripped);
      } else if (active) { flush(); active = false; lines = []; }
    }
    flush();
    return found;
  }
  function project(messages, meta = {}) {
    meta = meta && typeof meta === 'object' ? meta : {};
    const rows = (Array.isArray(messages) ? messages : []).map((message, index) => ({ message, index }))
      .filter(({ message }) => message && typeof message === 'object');
    const users = rows.filter(({ message }) => message.role === 'user');
    const request = users[users.length - 1] || null;
    const answers = rows.filter(({ message, index }) => message.role === 'assistant' &&
      (!request || index > request.index) && clean(message.text));
    const finals = answers.filter(({ message }) => message.phase === 'final_answer');
    const normal = answers.filter(({ message }) => message.phase !== 'commentary');
    const result = finals[finals.length - 1] || normal[normal.length - 1] || null;
    const lastRequest = request ? excerpt(request.message, request.index, LIMITS.request, 'user_request') : null;
    const lastResult = result ? excerpt(result.message, result.index, LIMITS.result,
      result.message.phase === 'final_answer' ? 'final_answer' : 'assistant_excerpt') : null;
    let next = null;
    if (result) {
      const structured = clean(result.message.work?.next), labeled = labeledNext(result.message.text);
      if (structured || labeled) next = excerpt(result.message, result.index, LIMITS.next,
        structured ? 'structured_work' : 'explicit_label', structured || labeled);
    }
    const times = rows.map(({ message }) => stamp(message.ts)).filter((value) => value !== null);
    return { method: 'extractive', lastRequest, lastResult, next,
      evidence: result ? artifactEvidence(result.message.text) : { artifacts: [], verification: '' },
      scope: { messageCount: rows.length, requestInRange: !!request, olderOmitted: meta.olderOmitted === true,
        truncated: rows.some(({ message }) => message.truncated === true) || [lastRequest, lastResult, next].some((part) => part?.truncated),
        stale: meta.stale === true || meta.offline === true || meta.online === false,
        offline: meta.offline === true || meta.online === false, fetchedAt: stamp(meta.fetchedAt),
        firstTs: times[0] ?? null, lastTs: times[times.length - 1] ?? null },
      note: '이미 불러온 대화에서 발췌했습니다. 작업 완료 여부는 원문으로 확인하세요.' };
  }
  function artifactEvidence(text) {
    const lines = [], artifacts = [], seen = new Set(); let fence = null;
    for (const raw of clean(text).split('\n')) {
      const marker = raw.trim().match(/^(`{3,}|~{3,})/);
      if (marker) { if (!fence) fence = marker[1]; else if (marker[1][0] === fence[0] && marker[1].length >= fence.length) fence = null; continue; }
      if (!fence && !raw.trim().startsWith('>')) lines.push(raw);
    }
    const add = (label, target) => {
      if (artifacts.length >= 12 || !target || target.length > 2048 || /[\x00-\x20\x7f]/.test(target)) return;
      let type;
      try {
        if (/^https?:\/\//i.test(target)) {
          const url = new URL(target);
          if (!['http:', 'https:'].includes(url.protocol) || !url.hostname || url.username || url.password) return;
          type = 'link'; target = url.href;
        } else if (!/^[a-z][a-z0-9+.-]*:/i.test(target.replace(/^[A-Za-z]:[\\/]/, '/')) &&
          /(?:^\/|^[A-Za-z]:[\\/]|^[.\w-]+\/).*\.[A-Za-z0-9]{1,12}(?::\d+)?$/.test(target)) type = 'file';
        else return;
      } catch { return; }
      const key = `${type}:${target}`;
      if (!seen.has(key)) { seen.add(key); artifacts.push({ type, target, label: clean(label).slice(0, 160) || target }); }
    };
    const visible = lines.join('\n');
    for (const match of visible.matchAll(/(?<!!)\[([^\]\n]{1,160})\]\(([^\s()]+)\)/g)) add(match[1], match[2]);
    for (const match of visible.matchAll(/`([^`\n]{1,2048})`/g)) add(match[1], match[1]);
    let active = false, verification = [];
    for (const raw of lines) {
      const label = raw.trim().replace(/^[#*\s]+|[*\s]+$/g, '');
      const match = label.match(/^(?:검증(?: 결과)?|테스트(?: 결과)?|확인 결과|validation|verification|tests?)(?:\s*[:：]\s*(.*)|\s*)$/i);
      if (match) { active = true; if (match[1]) verification.push(match[1]); }
      else if (active && (!raw.trim() || /^#{1,6}\s/.test(raw.trim()))) active = false;
      else if (active) verification.push(raw.trim());
    }
    return { artifacts, verification: verification.join('\n').slice(0, 1200) };
  }
  return Object.freeze({ project, labeledNext, artifactEvidence, LIMITS });
});
