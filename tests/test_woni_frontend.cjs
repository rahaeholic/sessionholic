const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function fixture(call) {
  const nodes = {};
  function element() {
    return { hidden: false, value: '', textContent: '', dataset: {}, children: [], listeners: {},
      scrollHeight: 0, scrollTop: 0, clientHeight: 0, disabled: false,
      addEventListener(name, fn) { this.listeners[name] = fn; },
      setAttribute(name, value) { this[name] = value; },
      click() { return this.listeners.click?.(); },
      append(...items) { this.children.push(...items); },
      replaceChildren(...items) { this.children = items; },
      querySelector() { return this.submit; },
      showModal() { this.open = true; },
      close() { this.open = false; this.listeners.close?.(); } };
  }
  for (const id of ['woni-dialog', 'woni-open', 'woni-input', 'woni-messages', 'woni-status', 'woni-form', 'woni-close', 'woni-files', 'woni-attach', 'woni-attachments', 'woni-send', 'woni-mic', 'woni-chat-tab', 'woni-work-tab', 'woni-work', 'woni-filter', 'woni-filter-label', 'woni-filter-clear', 'woni-reply', 'woni-reply-label', 'woni-reply-clear', 'woni-original']) nodes[id] = element();
  nodes['woni-form'].submit = element();
  const storage = new Map();
  const context = { document: { getElementById: id => nodes[id], createElement: element }, window: {},
    crypto: { randomUUID: () => 'request-123456' }, sessionStorage: {
      getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key) },
    setTimeout: () => 1, clearTimeout: () => {}, AbortController, URLSearchParams, CSS: { escape: value => value } };
  vm.runInNewContext(fs.readFileSync('web/woni.js', 'utf8'), context);
  return { nodes, storage, app: context.window.SessionholicWoni, call };
}

test('실패한 전송을 같은 요청 ID로 재시도하고 서버에서 확인되면 입력을 지운다', async () => {
  let stored = false;
  const sent = [];
  const call = async (url, options) => {
    if (url.includes('availability')) return { enabled: true };
    if (options?.method === 'POST') { sent.push({ ...options.body }); if (sent.length === 1) throw Error('timeout'); stored = true; return {}; }
    return { chats: stored ? [{ id: 'request-123456', text: '안녕', status: 'queued' }] : [], activity: {} };
  };
  const { nodes, app } = fixture(call);
  await app.check(call);
  assert.equal(nodes['woni-open'].hidden, false);
  nodes['woni-open'].listeners.click();
  await new Promise(setImmediate);
  nodes['woni-input'].value = '안녕';
  const submit = () => nodes['woni-form'].listeners.submit({ preventDefault() {} });
  await submit();
  nodes['woni-input'].value = '다른 메시지';
  await submit();
  assert.equal(sent.length, 1);
  nodes['woni-input'].value = '안녕';
  await submit();
  await new Promise(setImmediate);
  assert.deepEqual(sent, [{ text: '안녕', requestId: 'request-123456' }, { text: '안녕', requestId: 'request-123456' }]);
  assert.equal(nodes['woni-input'].value, '');
});

test('답글은 원래 대화 ID를 유지하고 작업 화면에서 같은 대화를 연다', async () => {
  const sent = [];
  const call = async (url, options) => {
    if (url.includes('availability')) return { enabled: true };
    if (options?.method === 'POST') { sent.push(options.body); return {}; }
    if (url.includes('/activity')) return { running: [{ id: 'old-1', text: '작업', status: 'running' }], recent: [], loose: [] };
    return { chats: [{ id: 'old-1', text: '원래 질문', answer: '원이 답변', stream: 's1', streamTitle: '작업 흐름' }], activity: {} };
  };
  const { nodes, app } = fixture(call);
  await app.check(call); nodes['woni-open'].click(); await new Promise(setImmediate);
  const answer = nodes['woni-messages'].children[1];
  answer.children.find(child => child.className === 'woni-message-actions').children[0].click();
  assert.match(nodes['woni-reply-label'].textContent, /원이 답변/);
  nodes['woni-input'].value = '이어서';
  await nodes['woni-form'].listeners.submit({ preventDefault() {} });
  assert.equal(sent[0].replyTo.id, 'old-1'); assert.equal(sent[0].replyTo.part, 'answer');
  nodes['woni-work-tab'].click(); await new Promise(setImmediate);
  assert.equal(nodes['woni-work'].hidden, false);
  assert.ok(nodes['woni-work'].children.some(child => child.className === 'woni-work-card'));
});

test('잠금 뒤 늦게 도착한 작업 응답은 다시 열었을 때 남지 않는다', async () => {
  let finishActivity;
  const call = async url => {
    if (url.includes('availability')) return { enabled: true };
    if (url.includes('/activity')) return new Promise(resolve => { finishActivity = resolve; });
    return { chats: [], activity: {} };
  };
  const { nodes, app } = fixture(call);
  await app.check(call); nodes['woni-open'].click(); await new Promise(setImmediate);
  nodes['woni-work-tab'].click(); await new Promise(setImmediate);
  app.lock(); finishActivity({ running: [{ text: '이전 비밀', status: 'running' }] }); await new Promise(setImmediate);
  await app.check(call); nodes['woni-open'].click(); await new Promise(setImmediate);
  nodes['woni-work-tab'].click();
  assert.equal(JSON.stringify(nodes['woni-work'].children).includes('이전 비밀'), false);
});

test('잠금 후 늦게 온 연결 응답은 원이 버튼을 다시 표시하지 않는다', async () => {
  let finish;
  const call = () => new Promise(resolve => { finish = resolve; });
  const { nodes, app } = fixture(call);
  const checking = app.check(call);
  app.lock();
  finish({ enabled: true });
  await checking;
  assert.equal(nodes['woni-open'].hidden, true);
});

test('Cmd/Ctrl+Enter는 보내고 한글 조합 중 Enter는 보내지 않는다', async () => {
  const sent = [];
  const call = async (url, options) => {
    if (url.includes('availability')) return { enabled: true };
    if (options?.method === 'POST') { sent.push(options.body); return {}; }
    return { chats: [], activity: {} };
  };
  const { nodes, app } = fixture(call);
  nodes['woni-form'].requestSubmit = () => nodes['woni-form'].listeners.submit({ preventDefault() {} });
  await app.check(call);
  nodes['woni-open'].listeners.click();
  await new Promise(setImmediate);
  nodes['woni-input'].value = '안녕';
  let prevented = false;
  const press = (props) => nodes['woni-input'].listeners.keydown({ key: 'Enter', preventDefault() { prevented = true; }, ...props });
  press({ metaKey: true, isComposing: true });
  assert.equal(sent.length, 0);
  press({ metaKey: true, isComposing: false });
  await new Promise(setImmediate);
  assert.equal(prevented, true);
  assert.equal(sent.length, 1);
});

test('붙여 넣은 첨부를 올리고 전송 실패 후 같은 파일과 요청 ID로 재시도한다', async () => {
  const sent = [], uploads = [];
  let confirmed = false;
  const call = async (url, options) => {
    if (url.includes('availability')) return { enabled: true };
    if (options?.method === 'POST') { sent.push({ ...options.body }); if (sent.length === 1) throw Error('timeout'); confirmed = true; return {}; }
    return { chats: confirmed ? [{ id: 'request-123456', text: '파일 설명', status: 'queued' }] : [], activity: {} };
  };
  const { nodes, app } = fixture(call);
  await app.check(call, { upload: async (file) => { uploads.push(file); return { id: '20261106-012345abcdef', name: file.name }; } });
  nodes['woni-open'].listeners.click();
  await new Promise(setImmediate);
  let prevented = false;
  nodes['woni-input'].listeners.paste({ clipboardData: { files: [{ name: '사진.png', size: 5 }] }, preventDefault() { prevented = true; } });
  await new Promise(setImmediate);
  assert.equal(prevented, true);
  assert.equal(uploads.length, 1);
  assert.equal(nodes['woni-attachments'].hidden, false);
  nodes['woni-input'].value = '파일 설명';
  const submit = () => nodes['woni-form'].listeners.submit({ preventDefault() {} });
  await submit(); await submit(); await new Promise(setImmediate);
  assert.equal(sent.length, 2);
  assert.equal(sent[0].requestId, sent[1].requestId);
  assert.deepEqual(Array.from(sent[0].attachments), ['20261106-012345abcdef']);
  assert.equal(nodes['woni-attachments'].hidden, true);
});

test('마이크는 기존 받아쓰기 기능에 연결하고 팝업을 닫으면 중지한다', async () => {
  const calls = [];
  const voice = { supported: () => true, toggle: (field) => calls.push(['toggle', field]), stop: (field) => calls.push(['stop', field]) };
  const call = async () => ({ enabled: true, chats: [], activity: {} });
  const { nodes, app } = fixture(call);
  await app.check(call, { voice });
  assert.equal(nodes['woni-mic'].hidden, false);
  nodes['woni-open'].listeners.click();
  nodes['woni-mic'].listeners.click();
  nodes['woni-close'].listeners.click();
  assert.deepEqual(calls.map((item) => item[0]), ['toggle', 'stop']);
});
