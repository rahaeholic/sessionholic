const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function fixture(call, storage = new Map()) {
  const nodes = {};
  let scheduled;
  function element() {
    return { hidden: false, value: '', textContent: '', dataset: {}, children: [], listeners: {},
      scrollHeight: 0, scrollTop: 0, clientHeight: 0, disabled: false,
      addEventListener(name, fn) { this.listeners[name] = fn; },
      setAttribute(name, value) { this[name] = value; },
      click() { return this.listeners.click?.(); },
      append(...items) { this.children.push(...items); },
      replaceChildren(...items) { this.children = items; },
      querySelector() { return this.submit; },
      focus() { this.focused = true; },
      showModal() { this.open = true; },
      close() { this.open = false; this.listeners.close?.(); } };
  }
  for (const id of ['woni-dialog', 'woni-open', 'woni-unread', 'woni-input', 'woni-messages', 'woni-status', 'woni-form', 'woni-close', 'woni-files', 'woni-attach', 'woni-attachments', 'woni-send', 'woni-mic', 'woni-chat-tab', 'woni-work-tab', 'woni-work', 'woni-filter', 'woni-filter-label', 'woni-filter-clear', 'woni-reply', 'woni-reply-label', 'woni-reply-clear', 'woni-original']) nodes[id] = element();
  nodes['woni-form'].submit = element();
  const context = { document: { getElementById: id => nodes[id], createElement: element }, window: {},
    crypto: { randomUUID: () => 'request-123456' }, sessionStorage: {
      getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key) },
    setTimeout: fn => { scheduled = fn; return 1; }, clearTimeout: () => { scheduled = null; }, AbortController, URLSearchParams, CSS: { escape: value => value } };
  vm.runInNewContext(fs.readFileSync('web/woni.js', 'utf8'), context);
  return { nodes, storage, app: context.window.SessionholicWoni, call, tick: async () => { const fn = scheduled; scheduled = null; if (fn) await fn(); } };
}

test('원이 창을 열면 기존 초안을 유지하며 채팅 입력창에 포커스한다', async () => {
  const call = async url => url.includes('availability') ? { enabled: true } : { chats: [], activity: {} };
  const { nodes, app } = fixture(call);
  await app.check(call);
  nodes['woni-input'].value = '작성 중인 초안';
  nodes['woni-open'].click();
  assert.equal(nodes['woni-dialog'].open, true);
  assert.equal(nodes['woni-input'].focused, true);
  assert.equal(nodes['woni-input'].value, '작성 중인 초안');
});

test('완료된 답변에 기록된 날짜와 시간을 표시하며 누락된 시간은 추측하지 않는다', async () => {
  const finishedAt = Date.UTC(2026, 10, 6, 4, 23) / 1000;
  const chats = [
    { id: 'known', text: '질문', answer: '기록된 답변', status: 'complete', createdAt: finishedAt - 600, finishedAt },
    { id: 'missing', text: '다른 질문', answer: '시각 없는 답변', status: 'complete', createdAt: finishedAt - 30 },
    { id: 'invalid', text: '질문', answer: '잘못된 시각', status: 'complete', finishedAt: 0 },
  ];
  const call = async url => url.includes('availability') ? { enabled: true } : { chats, activity: {} };
  const { nodes, app } = fixture(call);
  await app.check(call); nodes['woni-open'].click(); await new Promise(setImmediate);
  const answers = nodes['woni-messages'].children.filter(item => item.className === 'woni-message assistant');
  const time = answers[0].children.find(item => item.className === 'woni-message-time');
  assert.equal(time.dateTime, '2026-11-06T04:23:00.000Z');
  assert.match(time.textContent, /2026\.\s*11\.\s*06\..*13:23/);
  assert.match(time['aria-label'], /답변 시각/);
  assert.equal(answers[1].children.some(item => item.className === 'woni-message-time'), false);
  assert.equal(answers[2].children.some(item => item.className === 'woni-message-time'), false);
});

test('팝업을 닫아도 새 답변 배지를 표시하고 확인하면 지운다', async () => {
  let chats = [{ id: 'old', status: 'complete', answer: '기존 답변' }, { id: 'new', status: 'running' }];
  const toasts = [];
  const call = async url => url.includes('availability') ? { enabled: true } : { chats, activity: {} };
  const { nodes, app, tick } = fixture(call);
  await app.check(call, { toast: text => toasts.push(text) }); await new Promise(setImmediate);
  assert.equal(nodes['woni-unread'].hidden, true);
  chats = [chats[0], { id: 'new', status: 'complete', answer: '새 답변' }];
  await tick();
  assert.equal(nodes['woni-unread'].textContent, '1');
  assert.match(nodes['woni-open']['aria-label'], /새 답변 1개/);
  assert.equal(toasts.length, 1);
  await tick(); assert.equal(toasts.length, 1);
  nodes['woni-open'].click(); await new Promise(setImmediate);
  assert.equal(nodes['woni-unread'].hidden, true);
  chats = [...chats, { id: 'newer', status: 'complete', answer: '바로 읽은 답변' }];
  await tick(); assert.equal(nodes['woni-unread'].hidden, true);
});

test('알림을 누르면 로그인 뒤 원이의 해당 대화를 연다', async () => {
  const call = async url => url.includes('availability') ? { enabled: true } : { chats: [{ id: 'chat-42', status: 'complete', answer: '확인' }], activity: {} };
  const { nodes, app } = fixture(call);
  assert.equal(app.openFromNotification('../unsafe'), false);
  assert.equal(app.openFromNotification('chat-42'), true);
  await app.check(call); await new Promise(setImmediate);
  assert.equal(nodes['woni-dialog'].open, true);
  assert.ok(JSON.stringify(nodes['woni-messages'].children).includes('확인'));
});

test('전송을 누르면 응답 전에도 말풍선과 전송 중 상태가 보이고 확인 전 중복 전송되지 않는다', async () => {
  let finishPost, finishRead, count = 0;
  const call = async (url, options) => {
    if (url.includes('availability')) return { enabled: true };
    if (options?.method === 'POST') { count++; return new Promise(resolve => { finishPost = resolve; }); }
    if (count) return new Promise(resolve => { finishRead = resolve; });
    return { chats: [], activity: {} };
  };
  const { nodes, app } = fixture(call);
  await app.check(call); nodes['woni-open'].click(); await new Promise(setImmediate);
  nodes['woni-input'].value = '한 번만 보낼 메시지';
  const submit = () => nodes['woni-form'].listeners.submit({ preventDefault() {} });
  const sending = submit();
  assert.equal(nodes['woni-input'].value, '');
  assert.ok(JSON.stringify(nodes['woni-messages'].children).includes('한 번만 보낼 메시지'));
  assert.ok(JSON.stringify(nodes['woni-messages'].children).includes('전송 중'));
  assert.equal(nodes['woni-send'].disabled, true);
  nodes['woni-input'].value = '다음 초안'; await submit();
  assert.equal(count, 1);
  finishPost({ id: 'request-123456', status: 'queued' }); await sending;
  assert.equal(nodes['woni-send'].disabled, true);
  finishRead({ chats: [{ id: 'request-123456', text: '한 번만 보낼 메시지', status: 'queued' }], activity: {} });
  await new Promise(setImmediate);
  assert.equal(nodes['woni-input'].value, '다음 초안');
  assert.equal(nodes['woni-send'].disabled, false);
  assert.equal(nodes['woni-messages'].children.filter(node => node.className === 'woni-message user').length, 1);
});

test('전송 중 닫았다가 다시 열어도 보낸 내용과 전송 상태가 남는다', async () => {
  let finishPost;
  const call = async (url, options) => {
    if (url.includes('availability')) return { enabled: true };
    if (options?.method === 'POST') return new Promise(resolve => { finishPost = resolve; });
    return { chats: [], activity: {} };
  };
  const { nodes, app } = fixture(call);
  await app.check(call); nodes['woni-open'].click(); await new Promise(setImmediate);
  nodes['woni-input'].value = '닫아도 남는 메시지';
  const sending = nodes['woni-form'].listeners.submit({ preventDefault() {} });
  nodes['woni-close'].click(); nodes['woni-open'].click();
  assert.equal(nodes['woni-input'].value, '');
  assert.ok(JSON.stringify(nodes['woni-messages'].children).includes('닫아도 남는 메시지'));
  assert.equal(nodes['woni-send'].disabled, true);
  finishPost({ id: 'request-123456', status: 'queued' }); await sending;
});

test('전송 중 잠근 뒤 이전 응답은 다시 열린 화면을 바꾸지 않는다', async () => {
  let finishPost;
  const call = async (url, options) => {
    if (url.includes('availability')) return { enabled: true };
    if (options?.method === 'POST') return new Promise(resolve => { finishPost = resolve; });
    return { chats: [], activity: {} };
  };
  const { nodes, app } = fixture(call);
  await app.check(call); nodes['woni-open'].click(); await new Promise(setImmediate);
  nodes['woni-input'].value = '이전 메시지';
  const sending = nodes['woni-form'].listeners.submit({ preventDefault() {} });
  app.lock();
  finishPost({ id: 'request-123456', status: 'queued' }); await sending;
  assert.equal(nodes['woni-open'].hidden, true);
  assert.equal(nodes['woni-messages'].children.length, 0);
});

test('전송이 불확실하면 말풍선에서 같은 요청 ID로 재시도하고 새 초안을 보존한다', async () => {
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
  const outgoing = nodes['woni-messages'].children.find(node => node.className === 'woni-message user');
  const indicator = outgoing.children.find(node => node.className === 'woni-send-status');
  assert.match(indicator.textContent, /전송 여부를 확인하지 못했어요/);
  assert.equal(nodes['woni-send'].disabled, true);
  await indicator.children[0].click();
  await new Promise(setImmediate);
  assert.deepEqual(sent, [{ text: '안녕', requestId: 'request-123456' }, { text: '안녕', requestId: 'request-123456' }]);
  assert.equal(nodes['woni-input'].value, '다른 메시지');
});

test('화면을 새로 열어도 확인되지 않은 메시지를 같은 요청 ID로 복구한다', async () => {
  let stored = false;
  const sent = [];
  const call = async (url, options) => {
    if (url.includes('availability')) return { enabled: true };
    if (options?.method === 'POST') { sent.push({ ...options.body }); if (sent.length === 1) throw Error('timeout'); stored = true; return {}; }
    return { chats: stored ? [{ id: 'request-123456', text: '복구할 메시지', status: 'queued' }] : [], activity: {} };
  };
  const first = fixture(call);
  await first.app.check(call); first.nodes['woni-open'].click(); await new Promise(setImmediate);
  first.nodes['woni-input'].value = '복구할 메시지';
  await first.nodes['woni-form'].listeners.submit({ preventDefault() {} });
  const reopened = fixture(call, first.storage);
  await reopened.app.check(call); reopened.nodes['woni-open'].click(); await new Promise(setImmediate);
  assert.equal(reopened.nodes['woni-input'].value, '');
  const outgoing = reopened.nodes['woni-messages'].children.find(node => node.className === 'woni-message user');
  assert.ok(JSON.stringify(outgoing.children).includes('복구할 메시지'));
  await outgoing.children.find(node => node.className === 'woni-send-status').children[0].click();
  await new Promise(setImmediate);
  assert.equal(sent[0].requestId, sent[1].requestId);
  assert.equal(reopened.storage.has('sessionholic:woni:pending'), false);
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
  await submit();
  const outgoing = nodes['woni-messages'].children.find(node => node.className === 'woni-message user');
  assert.ok(JSON.stringify(outgoing.children).includes('사진.png'));
  await outgoing.children.find(node => node.className === 'woni-send-status').children[0].click();
  await new Promise(setImmediate);
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
