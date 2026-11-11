const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function fixture(call, storage = new Map(), host = undefined, extra = {}) {
  const nodes = {};
  // Timers by id: the chat refresh and a search delay can wait at once; tick() runs all that are waiting.
  const timers = new Map(), delays = new Map(); let timerId = 0;
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
  for (const id of ['woni-dialog', 'woni-open', 'woni-unread', 'woni-input', 'woni-messages', 'woni-status', 'woni-form', 'woni-close', 'woni-files', 'woni-attach', 'woni-attachments', 'woni-send', 'woni-mic', 'woni-chat-tab', 'woni-work-tab', 'woni-work', 'woni-filter', 'woni-filter-label', 'woni-filter-clear', 'woni-reply', 'woni-reply-label', 'woni-reply-clear', 'woni-reply-suggestions', 'woni-original', 'woni-tasks-tab', 'woni-tasks-badge', 'woni-notes-tab', 'woni-tasks', 'woni-notes', 'woni-notice', 'woni-find', 'woni-find-open', 'woni-side', 'woni-main', 'woni-menu', 'woni-scrim']) nodes[id] = element();
  nodes['woni-form'].submit = element();
  const context = { document: { getElementById: id => nodes[id], createElement: element }, window: { RyanOneHost: host },
    crypto: { randomUUID: () => 'request-123456' }, sessionStorage: {
      getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key) },
    setTimeout: (fn, ms) => { timers.set(++timerId, fn); delays.set(timerId, ms); return timerId; }, clearTimeout: id => { timers.delete(id); }, AbortController, URLSearchParams, CSS: { escape: value => value }, ...extra };
  vm.runInNewContext(fs.readFileSync('web/woni.js', 'utf8'), context);
  const tick = async () => { const waiting = [...timers.values()]; timers.clear(); for (const fn of waiting) await fn(); };
  return { nodes, storage, app: context.window.SessionholicWoni, call, tick, delays };
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

test('원이 답 말풍선 끝에 답한 시각만 작게 달고, 메뉴에는 날짜까지 남기며 누락된 시각은 추측하지 않는다', async () => {
  // 라이언 2026-11-10 17:25 "원이 답변 시간도 표시" — 00:26의 '원이 답에 시각 없음'을 뒤집었다(앱 1.42.0과 같음).
  const finishedAt = Date.UTC(2026, 10, 6, 4, 23) / 1000;
  const chats = [
    { id: 'known', text: '질문', answer: '기록된 답변', status: 'complete', createdAt: finishedAt - 600, finishedAt },
    // known의 답(finishedAt) 뒤에 물은 질문이다. 앞에 두면 known의 답이 떨어진 답이 되어 맨 아래로 간다(2026-11-11 줄 규칙).
    { id: 'missing', text: '다른 질문', answer: '시각 없는 답변', status: 'complete', createdAt: finishedAt + 30 },
    { id: 'invalid', text: '질문', answer: '잘못된 시각', status: 'complete', finishedAt: 0 },
  ];
  const call = async url => url.includes('availability') ? { enabled: true } : { chats, activity: {} };
  const { nodes, app } = fixture(call);
  await app.check(call); nodes['woni-open'].click(); await new Promise(setImmediate);
  const answers = nodes['woni-messages'].children.filter(item => item.className === 'woni-message assistant');
  const bubbleTime = answer => answer.children.find(item => item.className === 'woni-answer-time');
  const time = bubbleTime(answers[0]);
  assert.equal(time.dateTime, '2026-11-06T04:23:00.000Z');
  assert.equal(time.textContent, '오후 1:23');  // 시각만(KST), 흐름 이름 없음
  assert.equal(time['aria-label'], '답한 시각 오후 1:23');
  assert.equal(answers[0].children.indexOf(time), 2);  // 이름(화면 읽기용)·말풍선 바로 뒤
  assert.equal(bubbleTime(answers[1]), undefined);
  assert.equal(bubbleTime(answers[2]), undefined);
  // 날짜까지 든 시각은 말풍선을 눌러 여는 메뉴에 그대로 있다.
  const menuTime = answer => answer.children.find(item => item.className === 'woni-message-actions')?.children.find(item => item.className === 'woni-message-time');
  assert.match(menuTime(answers[0]).textContent, /2026\.\s*11\.\s*06\..*13:23/);
  assert.equal(menuTime(answers[1]), undefined);
});

test('답 시각은 먼저 알림·진행 중·실패 줄에는 달지 않고, 질문과 다른 날 끝난 답은 날짜를 붙인다', async () => {
  const asked = Date.UTC(2026, 10, 9, 14, 50) / 1000;  // KST 11/9 오후 11:50
  const chats = [
    { id: 'late', text: '밤 질문', answer: '자정 넘어 답', status: 'complete', createdAt: asked, finishedAt: asked + 1200 },
    { id: 'alert', kind: 'alert', answer: '발주 마감이에요.', status: 'complete', createdAt: asked + 1300, finishedAt: asked + 1300 },
    { id: 'work', text: '진행 중 질문', status: 'running', createdAt: asked + 1400 },
    { id: 'fail', text: '실패 질문', status: 'failed', createdAt: asked + 1500, finishedAt: asked + 1600 },
  ];
  const call = async url => url.includes('availability') ? { enabled: true } : { chats, activity: {} };
  const { nodes, app } = fixture(call);
  await app.check(call); nodes['woni-open'].click(); await new Promise(setImmediate);
  const answers = nodes['woni-messages'].children.filter(item => item.className === 'woni-message assistant');
  const times = answers.map(answer => answer.children.find(item => item.className === 'woni-answer-time')?.textContent ?? null);
  assert.deepEqual(times, ['11/10 오전 12:10', null, null, null]);
  assert.deepEqual(answers.map(answer => answer.dataset.state ?? null), [null, null, 'working', 'failed']);
});

test('창 안 알림은 2.5초(40자 넘으면 3.5초, 되돌리기는 5초) 뒤 사라진다', async () => {
  const call = async url => url.includes('availability') ? { enabled: true } : { chats: [], activity: {} };
  const { nodes, app, delays } = fixture(call);
  assert.equal(app.toastMs('복사했어요.'), 2500);
  assert.equal(app.toastMs('가'.repeat(40)), 2500);
  assert.equal(app.toastMs('가'.repeat(41)), 3500);
  assert.equal(app.toastMs('짧은 알림', true), 5000);
  await app.check(call); nodes['woni-open'].click(); await new Promise(setImmediate);
  // 메뉴의 새로고침은 창 안 알림을 띄운다.
  nodes['woni-side'].children.find(node => node.dataset.side === 'do:refresh').click();
  assert.equal([...delays.values()].at(-1), 2500);  // 누른 즉시 건 타이머가 알림을 닫는다
  assert.equal(nodes['woni-notice'].hidden, false);
  assert.equal(nodes['woni-notice'].textContent, '새로 불러오고 있어요.');
});

test('닷츠처럼 원이 말에는 이름·시각을 달지 않고 날짜와 먼저 알림만 가운데 줄로 보인다', async () => {
  // 라이언 2026-11-10: "말하는 모델이 닷츠니까 굳이 자기 아이디는 안 올리잖아. 깔끔해"
  const now = Date.now() / 1000;
  const chats = [
    { id: 'ask', text: '오늘 할 일 정리해 줘', answer: '세 가지예요.', status: 'complete', createdAt: now - 120, finishedAt: now - 100, stream: 's1', streamTitle: '오늘 정리' },
    { id: 'alert', kind: 'alert', text: '원이가 먼저 알림', answer: '발주 마감이 오늘이에요.', status: 'complete', createdAt: now - 60, finishedAt: now - 60 },
  ];
  const call = async url => url.includes('availability') ? { enabled: true } : { chats, activity: {} };
  const { nodes, app } = fixture(call);
  await app.check(call); nodes['woni-open'].click(); await new Promise(setImmediate);
  const list = () => nodes['woni-messages'].children;
  const clock = new Intl.DateTimeFormat('ko-KR', { timeZone: 'Asia/Seoul', hour: 'numeric', minute: '2-digit', hour12: true });
  const dividers = list().filter(node => node.className === 'woni-divider').map(node => node.textContent);
  assert.deepEqual(dividers, ['오늘', `${clock.format(new Date((now - 60) * 1000))} · 먼저 알림`]);
  const [user, answer] = list().filter(node => node.className?.startsWith('woni-message '));
  // 이름은 화면 읽기 프로그램에만 남는다.
  assert.equal(user.children.some(node => node.tagName === 'strong' || node.className === ''), false);
  assert.equal(user.children[0].className, 'sr-only'); assert.equal(answer.children[0].textContent, '원이');
  const line = user.children.find(node => node.className === 'woni-message-line');
  assert.equal(line.children[0].className, 'woni-sent-time');
  assert.equal(line.children[0].textContent, clock.format(new Date((now - 120) * 1000)));
  assert.equal(answer.children.some(node => node.className === 'woni-sent-time'), false);
  // 메뉴는 말풍선을 눌러야 열린다(흐름 이름도 그 안에).
  assert.equal(answer.className, 'woni-message assistant');
  answer.listeners.click({ target: { closest: () => null } });
  const opened = list().find(node => node.dataset.chatId === 'ask' && node.className.includes('assistant'));
  assert.equal(opened.className, 'woni-message assistant is-open');
  const labels = opened.children.find(node => node.className === 'woni-message-actions').children.map(node => node.textContent);
  assert.deepEqual(labels.slice(0, 3), ['답글', '복사', '이 흐름만 보기 · 오늘 정리']);
  opened.listeners.click({ target: { closest: () => null } });
  assert.equal(list().find(node => node.dataset.chatId === 'ask' && node.className.includes('assistant')).className, 'woni-message assistant');
  // 말풍선 속 링크·버튼을 누른 것은 메뉴를 열지 않는다.
  answer.listeners.click({ target: { closest: () => ({}) } });
  assert.equal(list().some(node => node.className?.includes('is-open')), false);
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

test('Cmd/Ctrl+Enter도 보내고 한글 조합 중 Enter는 보내지 않는다', async () => {
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

test('컴퓨터에서는 Enter로 보내고 Shift+Enter·한글 조합 중 Enter·폰의 Enter는 줄바꿈으로 둔다', async () => {
  const run = async (extra = {}) => {
    const sent = [];
    const call = async (url, options) => {
      if (url.includes('availability')) return { enabled: true };
      if (options?.method === 'POST') { sent.push(options.body); return {}; }
      return { chats: [], activity: {} };
    };
    const f = fixture(call, new Map(), undefined, extra);
    f.nodes['woni-form'].requestSubmit = () => f.nodes['woni-form'].listeners.submit({ preventDefault() {} });
    await f.app.check(call); f.nodes['woni-open'].click(); await new Promise(setImmediate);
    f.nodes['woni-input'].value = '안녕';
    const press = (props) => { let prevented = false; f.nodes['woni-input'].listeners.keydown({ key: 'Enter', preventDefault() { prevented = true; }, ...props }); return prevented; };
    return { sent, press, input: f.nodes['woni-input'] };
  };
  const desk = await run();
  assert.equal(desk.press({ shiftKey: true }), false);  // 줄바꿈은 그대로
  assert.equal(desk.press({ isComposing: true }), false);  // 한글 확정용 Enter는 막지도 보내지도 않는다
  assert.equal(desk.press({ keyCode: 229 }), false);
  assert.equal(desk.sent.length, 0);
  assert.equal(desk.press({}), true);
  await new Promise(setImmediate);
  assert.equal(desk.sent.length, 1);
  // 한글 확정 Enter: 조합 중 keydown → compositionend → 다시 온 Enter keydown이 정확히 한 번 보낸다.
  const ime = await run();
  ime.input.listeners.compositionstart();
  assert.equal(ime.press({ isComposing: true, keyCode: 229 }), false);
  ime.input.listeners.compositionend();
  assert.equal(ime.press({}), true);
  await new Promise(setImmediate);
  assert.equal(ime.sent.length, 1);
  const phone = await run({ matchMedia: query => ({ matches: query === '(pointer: coarse)' }) });
  assert.equal(phone.press({}), false);
  assert.equal(phone.sent.length, 0);
  assert.equal(phone.press({ ctrlKey: true }), true);  // 단축키는 어디서나 보낸다
  await new Promise(setImmediate);
  assert.equal(phone.sent.length, 1);
});

test('≡ 메뉴는 화면·최근 흐름 5개·오늘 정리·새로고침·설정을 담고, 막·Esc로 닫으며 ≡로 초점을 돌려준다', async () => {
  const now = Date.now() / 1000, sent = [];
  const items = Array.from({ length: 7 }, (_, n) => ({ id: `flow-${n}`, title: `흐름 ${n}`, count: 3, running: n === 0 ? 1 : 0, unread: n === 1 ? 2 : 0, lastAt: now - n * 60 }));
  const call = async (url, options) => {
    if (url.includes('availability')) return { enabled: true };
    if (options?.method === 'POST') { sent.push(options.body); return {}; }
    if (url === '/api/woni/streams') return { items, total: items.length };
    if (url.startsWith('/api/woni/chats?')) return { chats: [], hasMore: false };
    if (url === '/api/woni/tasks') return { actions: [], completedActions: [], archivedActions: [], counts: { due: 2, followup: 1 } };
    return { chats: [{ id: 'c1', text: '질문', answer: '답', status: 'complete', createdAt: now - 60, finishedAt: now - 50 }], activity: {} };
  };
  const { nodes, app } = fixture(call);
  await app.check(call); nodes['woni-open'].click(); await new Promise(setImmediate); await new Promise(setImmediate);
  const dialog = nodes['woni-dialog'], menu = nodes['woni-menu'];
  const sideKeys = () => nodes['woni-side'].children.filter(node => node.dataset?.side).map(node => node.dataset.side);
  assert.deepEqual(sideKeys(), ['view:chat', 'view:tasks', 'view:notes', 'view:work', 'view:find',
    'stream:flow-0', 'stream:flow-1', 'stream:flow-2', 'stream:flow-3', 'stream:flow-4', 'streams:all', 'do:today', 'do:refresh', 'do:settings']);
  const item = key => nodes['woni-side'].children.find(node => node.dataset?.side === key);
  assert.equal(item('view:chat')['aria-current'], 'page');
  assert.equal(item('view:tasks').children[1].children[1].textContent, '3');  // 확인 필요 수
  assert.equal(item('stream:flow-0').children[1].textContent, '진행 중');
  // 열기: 창 내용은 밀리고(CSS) 닫힐 때까지 누를 수 없다.
  menu.click();
  assert.equal(dialog.dataset.drawer, 'open'); assert.equal(menu['aria-expanded'], 'true'); assert.equal(nodes['woni-main'].inert, true);
  // Esc는 흐름·찾기·창보다 메뉴를 먼저 닫는다.
  let prevented = false;
  dialog.listeners.keydown({ key: 'Escape', preventDefault() { prevented = true; } });
  assert.equal(prevented, true); assert.equal(dialog.dataset.drawer, 'closed'); assert.equal(nodes['woni-main'].inert, false);
  assert.equal(menu.focused, true); assert.equal(dialog.open, true);
  menu.click(); nodes['woni-scrim'].click();
  assert.equal(dialog.dataset.drawer, 'closed');
  // 흐름을 누르면 메뉴를 닫고 그 흐름을 연다.
  menu.click(); item('stream:flow-2').click(); await new Promise(setImmediate);
  assert.equal(dialog.dataset.drawer, 'closed');
  assert.equal(nodes['woni-filter-label'].textContent, '흐름: 흐름 2');
  assert.equal(item('stream:flow-2')['aria-current'], 'true');
  // 화면 이동: 할 일로, 다시 대화로(흐름도 풀린다).
  item('view:tasks').click();
  assert.equal(nodes['woni-tasks'].hidden, false); assert.equal(item('view:tasks')['aria-current'], 'page');
  item('view:chat').click();
  assert.equal(nodes['woni-messages'].hidden, false); assert.equal(nodes['woni-filter'].hidden, true);
  // 오늘 정리 부탁은 앱과 같은 말을 보낸다.
  item('do:today').click(); await new Promise(setImmediate);
  assert.equal(sent.at(-1).text, '지금 하던일에서 진행 중인 작업과 내가 챙길 일을 정리해 줘.');
  // 잠그면 메뉴도 닫히고 흐름 목록을 비운다.
  menu.click(); app.lock();
  assert.equal(dialog.dataset.drawer, 'closed');
  assert.equal(sideKeys().some(key => key.startsWith('stream:')), false);
});

// 라이언 2026-11-11 "하던일에서 최근작업흐름에 새로고침을 안하면 진행중 딱지가 바로바로 안붙는다"
test('최근 작업 흐름의 진행 중 딱지는 새로고침 없이 질문이 시작·끝날 때 바로 붙고 떨어진다', async () => {
  const now = Date.now() / 1000;
  const flows = running => [
    { id: 'flow-a', title: '흐름 A', count: 2, running, unread: running ? 0 : 1, lastAt: now },
    { id: 'flow-b', title: '흐름 B', count: 1, running: 0, unread: 0, lastAt: now - 60 }];
  let serverRunning = 0, chats = [{ id: 'c1', stream: 'flow-a', text: '질문', answer: '답', status: 'complete', createdAt: now - 120, finishedAt: now - 110 }];
  const reads = [];
  const call = async url => {
    if (url.includes('availability')) return { enabled: true };
    if (url === '/api/woni/streams') { reads.push(serverRunning); return { items: flows(serverRunning), total: 2 }; }
    if (url === '/api/woni/tasks') return { actions: [], completedActions: [], archivedActions: [], counts: {} };
    return { chats, activity: {} };
  };
  const { nodes, app, tick } = fixture(call);
  await app.check(call); nodes['woni-open'].click(); await new Promise(setImmediate); await new Promise(setImmediate);
  const item = key => nodes['woni-side'].children.find(node => node.dataset?.side === key);
  const text = node => [node?.textContent || '', ...(node?.children || []).map(text)].join('');
  const badge = key => text(item(key).children[1]).replace('새 답 ', '');
  assert.equal(badge('stream:flow-a'), '1');  // 새 답 1개, 진행 중 아님
  const before = reads.length;
  // 새 질문이 흐름 A에서 돌기 시작: 흐름 목록을 다시 읽기 전(목록은 아직 0)에도 4초 갱신의 대화로 바로 딱지가 붙는다.
  chats = [...chats, { id: 'c2', stream: 'flow-a', text: '새 질문', status: 'running', createdAt: now }];
  await tick();
  assert.equal(badge('stream:flow-a'), '진행 중');
  assert.equal(badge('stream:flow-b'), '');
  // 1분을 기다리지 않고 흐름 목록도 바로 다시 읽는다.
  assert.equal(reads.length, before + 1);
  // 같은 상태가 이어지면 매 갱신마다 다시 읽지 않는다.
  serverRunning = 1; await tick(); await new Promise(setImmediate);
  assert.equal(reads.length, before + 1);
  assert.equal(badge('stream:flow-a'), '진행 중');
  // 답이 끝나면 바로 다시 읽어 딱지를 떼고 새 답 수를 단다.
  serverRunning = 0;
  chats = [chats[0], { ...chats[1], answer: '새 답', status: 'complete', finishedAt: now + 5 }];
  await tick(); await new Promise(setImmediate);
  assert.equal(reads.length, before + 2);
  assert.equal(badge('stream:flow-a'), '1');
  // 찾기 화면의 흐름 줄도 같은 기준으로 진행 중을 보인다.
  chats = [...chats, { id: 'c3', stream: 'flow-b', text: '또 질문', status: 'queued', createdAt: now + 6 }];
  nodes['woni-find-open'].click(); await new Promise(setImmediate);
  await tick(); await new Promise(setImmediate);
  assert.equal(badge('stream:flow-b'), '진행 중');
  const findRow = nodes['woni-find'].children[0].children[1].children.find(row => row.dataset?.stream === 'flow-b');
  assert.ok(findRow.children[1].children.some(node => node.className === 'woni-find-running'));
});

// 라이언 2026-11-11 "최근작업흐름에 옆에 1이 뜨는데 클릭해도 안 없어지는데"
function readFixture({ marks = true } = {}) {
  const now = Date.now() / 1000, start = now - 100, posts = [];
  const server = { read: start, marks: {} };
  let chats = [{ id: 'old', stream: 'flow-a', text: '옛 질문', answer: '옛 답', status: 'complete', createdAt: start - 20, finishedAt: start - 10 }];
  const unreadIn = id => chats.filter(chat => chat.stream === id && chat.status === 'complete' && chat.answer &&
    chat.finishedAt > Math.max(server.read, server.marks[id] || 0)).length;
  const call = async (url, options) => {
    if (url.includes('availability')) return { enabled: true };
    if (url === '/api/woni/read') {
      posts.push(options.body);
      if (options.body.stream) server.marks[options.body.stream] = Math.max(server.marks[options.body.stream] || 0, options.body.through);
      else server.read = Math.max(server.read, options.body.through);
      return { readThrough: server.read, ...(marks ? { streamRead: { ...server.marks } } : {}) };
    }
    if (url === '/api/woni/streams') return { items: ['flow-a', 'flow-b'].map((id, n) => ({ id, title: `흐름 ${id.slice(-1).toUpperCase()}`, count: 1, running: 0, unread: unreadIn(id), lastAt: now - n })), total: 2 };
    if (url.startsWith('/api/woni/chats?')) return { chats: [], hasMore: false };
    if (url === '/api/woni/tasks') return { actions: [], completedActions: [], archivedActions: [], counts: {} };
    return { chats, activity: {}, readThrough: server.read, ...(marks ? { streamRead: { ...server.marks } } : {}) };
  };
  const f = fixture(call);
  const plain = value => JSON.parse(JSON.stringify(value));  // vm 안 객체를 비교할 수 있게
  const item = key => f.nodes['woni-side'].children.find(node => node.dataset?.side === key);
  const text = node => [node?.textContent || '', ...(node?.children || []).map(text)].join('');
  const count = key => text(item(key).children[1]).replace('새 답 ', '');
  const settle = async () => { for (let i = 0; i < 4; i++) await new Promise(setImmediate); };
  return { ...f, start, get posts() { return plain(posts); }, server, item, count, settle, setChats: next => { chats = next(chats); } };
}

test('흐름을 열어 읽으면 다른 흐름에 먼저 끝난 안 읽은 답이 있어도 그 흐름의 새 답 수가 바로 지워진다', async () => {
  const r = readFixture();
  await r.app.check(r.call); r.nodes['woni-open'].click(); await r.settle();
  r.item('stream:flow-a').click(); await r.settle();
  // 흐름 A를 보는 사이 B 답(먼저 끝남)과 A 답이 끝난다.
  r.setChats(chats => [...chats,
    { id: 'b1', stream: 'flow-b', text: 'B 질문', answer: 'B 답', status: 'complete', createdAt: r.start + 1, finishedAt: r.start + 10 },
    { id: 'a1', stream: 'flow-a', text: 'A 질문', answer: 'A 답', status: 'complete', createdAt: r.start + 2, finishedAt: r.start + 20 }]);
  await r.tick(); await r.settle();
  // 전체 기준은 못 본 B 답 앞에서 멈추고, A는 흐름 읽음으로 남긴다.
  assert.deepEqual(r.posts, [{ through: r.start + 20, stream: 'flow-a' }]);
  assert.equal(r.count('stream:flow-a'), '');
  assert.equal(r.count('stream:flow-b'), '1');
  assert.equal(r.nodes['woni-unread'].textContent, '1');  // 원이 버튼의 새 답도 B 하나
  // 전체 대화로 돌아와 B까지 보면 전체 기준이 앞으로 가고 메뉴의 수도 바로 다시 읽는다.
  r.item('view:chat').click(); await r.settle();
  assert.deepEqual(r.posts.at(-1), { through: r.start + 20 });
  assert.equal(r.count('stream:flow-b'), '');
  assert.equal(r.nodes['woni-unread'].hidden, true);
});

test('메뉴에서 흐름을 누르면 목록을 다시 읽기 전에도 그 흐름의 새 답 수를 지우고, 읽음을 남긴 뒤 목록을 바로 다시 읽는다', async () => {
  const r = readFixture();
  await r.app.check(r.call); r.nodes['woni-open'].click(); await r.settle();
  r.item('stream:flow-b').click(); await r.settle();
  // 흐름 B를 보는 사이 A에서 질문이 돌다가 끝난다.
  const a1 = { id: 'a1', stream: 'flow-a', text: 'A 질문', status: 'running', createdAt: r.start + 2 };
  r.setChats(chats => [...chats, a1]); await r.tick(); await r.settle();
  r.setChats(chats => [...chats.slice(0, -1), { ...a1, answer: 'A 답', status: 'complete', finishedAt: r.start + 20 }]); await r.tick(); await r.settle();
  assert.equal(r.posts.some(body => !body.stream), false);  // B 화면에선 A 답을 전체 읽음으로 넘기지 않는다
  assert.equal(r.count('stream:flow-a'), '1');
  r.item('stream:flow-a').click();
  assert.equal(r.count('stream:flow-a'), '');  // 누르는 즉시
  await r.settle();
  assert.equal(r.posts.at(-1).through, r.start + 20);  // 다른 흐름에 남은 답이 없으면 전체 기준으로 남는다
  r.item('stream:flow-b').click(); await r.settle();
  assert.equal(r.count('stream:flow-a'), '');  // 다시 읽은 목록에서도 0
});

test('흐름별 읽음을 모르는 이전 원이 서버에는 흐름 읽음을 보내지 않는다', async () => {
  const r = readFixture({ marks: false });
  r.setChats(chats => [...chats,
    { id: 'b1', stream: 'flow-b', text: 'B 질문', answer: 'B 답', status: 'complete', createdAt: r.start + 1, finishedAt: r.start + 10 },
    { id: 'a1', stream: 'flow-a', text: 'A 질문', answer: 'A 답', status: 'complete', createdAt: r.start + 2, finishedAt: r.start + 20 }]);
  await r.app.check(r.call); await r.settle();
  r.nodes['woni-open'].click(); await r.settle(); r.item('stream:flow-a').click(); await r.settle();
  await r.tick(); await r.settle();
  assert.equal(r.posts.some(body => body.stream), false);
});

test('대화가 하나도 없으면 빈 화면 안내와 오늘 정리 버튼을 보여 준다', async () => {
  const call = async url => url.includes('availability') ? { enabled: true } : { chats: [], activity: {} };
  const { nodes, app } = fixture(call);
  await app.check(call); nodes['woni-open'].click(); await new Promise(setImmediate);
  const welcome = nodes['woni-messages'].children.find(node => node.className === 'woni-welcome');
  assert.equal(welcome.children[0].textContent, '무엇을 도와드릴까요?');
  assert.equal(welcome.children[2].textContent, '오늘 정리 부탁');
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

test('원이앱에서 읽은 답은 하던일 원이에서 새 답변으로 세지 않고, 여기서 읽으면 같은 기준에 남긴다', async () => {
  let readThrough = 100;
  let chats = [{ id: 'old', status: 'complete', answer: '읽은 답', finishedAt: 90 }, { id: 'unseen', status: 'complete', answer: '아직 안 읽은 답', finishedAt: 110 }];
  const toasts = [], reads = [];
  const call = async (url, options) => {
    if (url.includes('availability')) return { enabled: true };
    if (url === '/api/woni/read') { reads.push({ ...options.body }); readThrough = Math.max(readThrough, options.body.through); return { readThrough }; }
    return { chats, activity: {}, readThrough };
  };
  const { nodes, app, tick } = fixture(call);
  await app.check(call, { toast: text => toasts.push(text) }); await new Promise(setImmediate);
  assert.equal(nodes['woni-unread'].textContent, '1');  // 공유 기준보다 늦게 끝난 답은 처음 확인에도 표시
  assert.equal(toasts.length, 0);
  readThrough = 115;  // 원이앱에서 읽음
  await tick();
  assert.equal(nodes['woni-unread'].hidden, true);
  chats = [...chats, { id: 'next', status: 'complete', answer: '새 답', finishedAt: 130 }];
  await tick();
  assert.equal(nodes['woni-unread'].textContent, '1');
  assert.equal(toasts.length, 1);
  nodes['woni-open'].click(); await new Promise(setImmediate);
  assert.equal(nodes['woni-unread'].hidden, true);
  assert.deepEqual(reads, [{ through: 130 }]);
  await tick(); await new Promise(setImmediate);
  assert.equal(reads.length, 1);  // 이미 남긴 기준은 다시 보내지 않는다
});

test('원이 답의 추천 답장(👉)을 버튼으로 보여 주고 누르면 그 말을 바로 보낸다', async () => {
  const sent = [];
  const chats = [
    { id: 'older', text: '앞 질문', status: 'complete', answer: '앞 답\n\n👉 예전 제안', finishedAt: 10 },
    { id: 'latest', text: '질문', status: 'complete', answer: '정리했어요.\n\n👉 응, 정리해 줘 | 장소 후보부터 봐 줘\n🔊 정리했어요.', finishedAt: 20 },
  ];
  const call = async (url, options) => {
    if (url.includes('availability')) return { enabled: true };
    if (url === '/api/woni' && options?.method === 'POST') { sent.push(JSON.parse(JSON.stringify(options.body))); return { id: options.body.requestId, status: 'queued' }; }
    if (options?.method === 'POST') return {};
    return { chats, activity: {} };
  };
  const { nodes, app } = fixture(call);
  await app.check(call); nodes['woni-open'].click(); await new Promise(setImmediate);
  nodes['woni-input'].value = '쓰던 초안';
  const answers = nodes['woni-messages'].children.filter(node => node.className === 'woni-message assistant');
  assert.deepEqual(answers.map(node => node.children[1].textContent), ['앞 답', '정리했어요.']);
  const boxes = nodes['woni-messages'].children.filter(node => node.className === 'woni-suggestions');
  assert.equal(boxes.length, 1);  // 가장 최근 답에만
  assert.deepEqual(boxes[0].children.map(button => button.textContent), ['응, 정리해 줘', '장소 후보부터 봐 줘']);
  assert.equal(boxes[0].children[0].type, 'button');
  await boxes[0].children[0].click();
  // 추천은 늘 그 답에 단 답글로 보낸다(2026-11-11 늦게 끝난 답이 맨 아래로 — 앱 1.44.0과 같게)
  assert.deepEqual(sent, [{ text: '응, 정리해 줘', requestId: 'request-123456', replyTo: { id: 'latest', part: 'answer' } }]);
  assert.equal(nodes['woni-input'].value, '쓰던 초안');
  assert.equal(nodes['woni-messages'].children.some(node => node.className === 'woni-suggestions'), false);  // 확인 전 두 번 보내지 않게 숨긴다
});


test('이전 원이 답에 답글을 고르면 답글 띠 아래에 그 답의 추천이 뜨고 누르면 그 답에 단 답글로 보낸다', async () => {
  // 라이언 2026-11-10: "이전 답변에 답글 남길 때도 추천답변 뜨게 해줘" (Ryan One 앱 1.38.1과 같게)
  const sent = [];
  const chats = [
    { id: 'older', text: '앞 질문', status: 'complete', answer: '앞 답이에요.\n\n👉 일정 잡아 줘 | 기한 알림 고쳐 줘\n🔊 앞 답 요약', finishedAt: 10 },
    { id: 'alert-1', kind: 'alert', text: '원이가 먼저 알림', status: 'complete', answer: '발주 마감이 오늘이에요.\n\n👉 발주서 열어 줘', finishedAt: 15 },
    { id: 'latest', text: '질문', status: 'complete', answer: '정리했어요.\n\n👉 응, 정리해 줘 | 장소 후보부터 봐 줘', finishedAt: 20 },
  ];
  const call = async (url, options) => {
    if (url.includes('availability')) return { enabled: true };
    if (url === '/api/woni' && options?.method === 'POST') { sent.push(JSON.parse(JSON.stringify(options.body))); return { id: options.body.requestId, status: 'queued' }; }
    if (options?.method === 'POST') return {};
    return { chats, activity: {} };
  };
  const { nodes, app } = fixture(call);
  await app.check(call); nodes['woni-open'].click(); await new Promise(setImmediate);
  const picks = nodes['woni-reply-suggestions'];
  const menu = id => nodes['woni-messages'].children.find(node => node.dataset.chatId === id && node.className.includes('assistant'))
    .children.find(node => node.className === 'woni-message-actions').children;
  assert.equal(picks.hidden, true);
  // 말풍선 아래 추천은 지금처럼 가장 최근 답에만
  assert.equal(nodes['woni-messages'].children.filter(node => node.className === 'woni-suggestions').length, 1);
  menu('older')[0].click();
  assert.equal(nodes['woni-reply'].hidden, false);
  assert.equal(nodes['woni-reply-label'].textContent, '원이 답변에 답글 · 앞 답이에요.');  // 👉·🔊 줄은 띠에서 뺀다
  assert.equal(picks.hidden, false);
  assert.deepEqual(picks.children.map(button => button.textContent), ['일정 잡아 줘', '기한 알림 고쳐 줘']);
  assert.equal(picks.children[0].type, 'button');
  nodes['woni-input'].value = '쓰던 초안';
  await picks.children[0].click();
  assert.deepEqual(sent, [{ text: '일정 잡아 줘', requestId: 'request-123456', replyTo: { id: 'older', part: 'answer' } }]);
  assert.equal(nodes['woni-input'].value, '쓰던 초안');
  assert.equal(nodes['woni-reply'].hidden, true);
  assert.equal(picks.hidden, true);  // 확인 전 두 번 보내지 않게 숨긴다
  const outgoing = nodes['woni-messages'].children.find(node => node.className === 'woni-message user' && !node.dataset.chatId);
  assert.ok(JSON.stringify(outgoing.children).includes('↩ 답글 · 앞 답이에요.'));
});

test('먼저 알림에도 답글 추천이 뜨고, 최근 답·라이언 메시지·취소한 답글에는 띠 아래 추천을 두지 않는다', async () => {
  const sent = [];
  const chats = [
    { id: 'alert-1', kind: 'alert', text: '원이가 먼저 알림', status: 'complete', answer: '발주 마감이 오늘이에요.\n\n👉 발주서 열어 줘', finishedAt: 15 },
    { id: 'plain', text: '그냥 질문', status: 'complete', answer: '추천 없는 답', finishedAt: 18 },
    { id: 'latest', text: '질문', status: 'complete', answer: '정리했어요.\n\n👉 응, 정리해 줘 | 장소 후보부터 봐 줘', finishedAt: 20 },
  ];
  const call = async (url, options) => {
    if (url.includes('availability')) return { enabled: true };
    if (url === '/api/woni' && options?.method === 'POST') { sent.push(JSON.parse(JSON.stringify(options.body))); return { id: options.body.requestId, status: 'queued' }; }
    if (options?.method === 'POST') return {};
    return { chats, activity: {} };
  };
  const { nodes, app } = fixture(call);
  await app.check(call); nodes['woni-open'].click(); await new Promise(setImmediate);
  const picks = nodes['woni-reply-suggestions'];
  const message = (id, role) => nodes['woni-messages'].children.find(node => node.dataset.chatId === id && node.className.includes(role));
  const reply = (id, role) => message(id, role).children.find(node => node.className === 'woni-message-actions').children[0].click();
  reply('alert-1', 'assistant');
  assert.deepEqual(picks.children.map(button => button.textContent), ['발주서 열어 줘']);
  assert.equal(picks.hidden, false);
  nodes['woni-reply-clear'].click();
  assert.equal(picks.hidden, true);
  reply('plain', 'assistant');
  assert.equal(picks.hidden, true);
  reply('latest', 'user');
  assert.equal(nodes['woni-reply-label'].textContent, '라이언 메시지에 답글 · 질문');
  assert.equal(picks.hidden, true);
  // 최근 답은 말풍선 아래에 이미 있으니 띠 아래에 또 두지 않고, 그 칩을 누르면 그 답에 단 답글로 간다.
  reply('latest', 'assistant');
  assert.equal(picks.hidden, true);
  const box = nodes['woni-messages'].children.find(node => node.className === 'woni-suggestions');
  await box.children[1].click();
  assert.deepEqual(sent, [{ text: '장소 후보부터 봐 줘', requestId: 'request-123456', replyTo: { id: 'latest', part: 'answer' } }]);
  assert.equal(nodes['woni-reply'].hidden, true);
});

test('Android 호스트가 있을 때만 정확한 대화로 이동하고 기존 웹은 유지한다', async () => {
  const calls = [];
  const call = async url => url.includes('availability') ? { enabled: true } : { chats: [], activity: {} };
  const host = { version: 1, openWoni(id) { calls.push(id); return true; } };
  const { nodes, app } = fixture(call, new Map(), host);
  await app.check(call);
  nodes['woni-open'].click();
  assert.deepEqual(calls, ['']);
  assert.notEqual(nodes['woni-dialog'].open, true);
  app.openFromNotification('chat-42');
  assert.deepEqual(calls, ['', 'chat-42']);
  assert.notEqual(nodes['woni-dialog'].open, true);
});

test('작성 중인 웹 초안이나 미확인 전송은 네이티브 이동 대신 웹에서 이어 간다', async () => {
  const call = async url => url.includes('availability') ? { enabled: true } : { chats: [], activity: {} };
  for (const pending of [false, true]) {
    const storage = new Map();
    const calls = [];
    const { nodes, app } = fixture(call, storage, { version: 1, openWoni(id) { calls.push(id); return true; } });
    await app.check(call);
    if (pending) storage.set('sessionholic:woni:pending', JSON.stringify({ text: '미확인 요청', requestId: 'pending-001', attachments: ['file-1'], replyTo: { id: 'chat-1', part: 'answer' } }));
    else nodes['woni-input'].value = '기존 초안';
    nodes['woni-open'].click();
    assert.equal(nodes['woni-dialog'].open, true);
    assert.equal(nodes['woni-input'].value, pending ? '' : '기존 초안');
    assert.equal(app.hasDraft(), true);
    assert.deepEqual(calls, []);
  }
});

test('Android 호스트 거절과 예외는 일반 웹 대화로 돌아간다', async () => {
  const call = async url => url.includes('availability') ? { enabled: true } : { chats: [], activity: {} };
  for (const host of [undefined, { version: 2, openWoni() { throw Error(); } },
    { version: 1, openWoni() { return false; } }, { version: 1, openWoni() { throw Error(); } }]) {
    const { nodes, app } = fixture(call, new Map(), host);
    await app.check(call); nodes['woni-open'].click();
    assert.equal(nodes['woni-dialog'].open, true);
  }
});


test('답글 대상만 남은 웹 입력도 호스트로 이동시키지 않는다', async () => {
  const calls = [];
  const host = { version: 0, openWoni(id) { calls.push(id); return true; } };
  const call = async url => url.includes('availability') ? { enabled: true } : { chats: [{ id: 'chat-one', text: '질문', answer: '답변' }], activity: {} };
  const { nodes, app } = fixture(call, new Map(), host);
  await app.check(call); nodes['woni-open'].click(); await new Promise(setImmediate);
  nodes['woni-messages'].children[1].children.find(child => child.className === 'woni-message-actions').children[0].click();
  nodes['woni-close'].click(); host.version = 1;
  assert.equal(nodes['woni-input'].value, '');
  assert.equal(app.hasDraft(), true);
  nodes['woni-open'].click();
  assert.equal(nodes['woni-dialog'].open, true);
  assert.deepEqual(calls, []);
});

// ---- 할 일·기록 (docs/plan/2026-11-10-woni-panels.md) ----
function walk(node, pred, out = []) {
  if (!node || typeof node !== 'object') return out;
  if (pred(node)) out.push(node);
  for (const child of node.children || []) walk(child, pred, out);
  return out;
}
const textOf = node => [node.textContent || '', ...(node.children || []).map(textOf)].join('');
const byClass = (root, name) => walk(root, node => typeof node.className === 'string' && node.className.split(' ').includes(name));
const buttonOf = (root, label) => walk(root, node => node.type === 'button' && textOf(node) === label)[0];
const settle = () => new Promise(setImmediate);
function taskData() {
  return {
    actions: [
      { id: 'a1', kind: 'mine', title: '재입고 일정 공유하기', project: '물류', due: '11/12', taskStatus: 'open', stateEvent: 'e1', quote: '목요일까지 공유할게요', recordTitle: '주간 회의', recordedAt: '2026-11-08 10:00', counterpart: '김팀장', sourceId: 'rec-1', requestedBy: '김팀장', flow: '김팀장 → 라이언' },
      { id: 'd1', kind: 'delegated', title: '촬영 견적 받기', assignee: '박매니저', attention: '완료 보고 · 확인 필요', taskStatus: 'reported', previousStatus: 'open', stateEvent: 'd1:completion-reported', reportQuote: '견적 받았어요', project: '마케팅' },
      { id: 'd2', kind: 'delegated', title: '상세페이지 수정', attention: '기다리는 중', taskStatus: 'open', stateEvent: '' },
      { id: 'u1', kind: 'unassigned', title: '샘플 발송', attention: '담당 확인 · 박매니저일 가능성', guessBasis: '박매니저가 보내겠다고 말함', taskStatus: 'open', stateEvent: '' },
    ],
    completedActions: [
      { id: 'c1', kind: 'mine', title: '세금계산서 발행', taskStatus: 'done', previousStatus: 'open', stateEvent: 'c1:done', autoBasis: { quote: '발행했습니다', source: 'Slack 재무 채널', at: 1, reason: '보낸 글' } },
      { id: 'c2', kind: 'delegated', title: '박스 발주', taskStatus: 'done', previousStatus: 'reported', stateEvent: 'c2:done' },
    ],
    archivedActions: [{ id: 'r1', kind: 'delegated', title: '오래된 맡긴 일', taskStatus: 'open', stateEvent: '' }],
    counts: { mine: 1, delegated: 2, unassigned: 1, followup: 1, due: 1, autoDone: 1, completed: 2, archived: 1 }, syncedAt: 1, error: '',
  };
}
async function openTasks(call, options = {}) {
  let n = 0;
  const f = fixture(call, options.storage || new Map(), undefined, { crypto: { randomUUID: () => `req-${++n}-abcdef` }, ...(options.extra || {}) });
  await f.app.check(call, { toast: text => (options.toasts || []).push(text) }); f.nodes['woni-open'].click(); await settle();
  f.nodes['woni-tasks-tab'].click(); await settle();
  const rows = () => byClass(f.nodes['woni-tasks'], 'woni-task');
  const titles = () => rows().map(row => textOf(byClass(row, 'woni-task-title')[0]));
  const toggle = label => walk(f.nodes['woni-tasks'], node => node['aria-pressed'] !== undefined && textOf(node).startsWith(label))[0];
  return { ...f, rows, titles, toggle };
}

test('할 일 탭은 종류·보기·찾기로 나누고 앱과 같은 줄과 안내를 보여 준다', async () => {
  const call = async url => url.includes('availability') ? { enabled: true } : url === '/api/woni/tasks' ? taskData() : { chats: [], activity: {} };
  const { nodes, rows, titles, toggle } = await openTasks(call);
  assert.equal(nodes['woni-tasks'].hidden, false); assert.equal(nodes['woni-messages'].hidden, true);
  assert.equal(nodes['woni-tasks-tab']['aria-pressed'], 'true');
  assert.equal(nodes['woni-tasks-badge'].hidden, false); assert.equal(nodes['woni-tasks-badge'].textContent, '2');  // due 1 + followup 1
  assert.equal(nodes['woni-tasks-tab']['aria-label'], '할 일, 확인 필요 2개');
  assert.deepEqual(byClass(nodes['woni-tasks'], 'woni-kinds')[0].children.map(textOf), ['내가 할 일1', '내가 맡긴 일2', '담당 확인1']);
  assert.deepEqual(titles(), ['재입고 일정 공유하기']);
  assert.equal(textOf(byClass(rows()[0], 'woni-task-meta')[0]), '물류 · 11/12');
  // 누가 누구에게 맡긴 일인지(Ryan One 1.46.0): 서버가 준 flow
  assert.equal(textOf(byClass(rows()[0], 'woni-task-line')[0]), '김팀장 → 라이언');
  assert.equal(rows()[0].children[0]['aria-label'], '완료 처리: 재입고 일정 공유하기');
  toggle('내가 맡긴 일').click();
  assert.deepEqual(titles(), ['촬영 견적 받기', '상세페이지 수정']);
  const lines = byClass(nodes['woni-tasks'], 'woni-task-line');
  // flow가 없는 옛 서버 응답도 맡긴 일은 '라이언 → 받는 사람'으로 보인다
  assert.deepEqual(lines.map(textOf), ['라이언 → 박매니저 · 완료 보고 · 확인 필요', '라이언 → 받을 사람 확인 필요 · 기다리는 중']);
  assert.equal(lines[0].className.includes('is-attention'), true); assert.equal(lines[1].className.includes('is-attention'), false);
  assert.match(textOf(byClass(nodes['woni-tasks'], 'woni-note-hint')[0]), /^확인 필요 1개 · 완료 보고와 마감이 된 일을 먼저 보여줘요/);
  assert.equal(rows()[0].children[0]['aria-label'], '결과 확인 완료: 촬영 견적 받기');
  toggle('담당 확인').click();
  // Ryan One 1.40.1: 담당 확인도 끝났거나 필요 없으면 체크한다. 목록 위에 보관 기준 안내.
  assert.equal(rows()[0].children[0].disabled, false);
  assert.equal(rows()[0].children[0]['aria-label'], '완료 처리: 샘플 발송');
  assert.equal(textOf(byClass(nodes['woni-tasks'], 'woni-note-hint')[0]), '누가 할지 모르는 일이에요. 라이언 일로 보이는 것부터 보여줘요. 끝났거나 필요 없으면 체크하세요. 그냥 두면 모르는 일은 3일, 라이언 일로 보이는 일은 7일 뒤 보관으로 옮겨요.');
  assert.equal(textOf(byClass(rows()[0], 'woni-task-line')[0]), '박매니저일 가능성');
  toggle('내가 할 일').click(); toggle('완료').click();
  assert.deepEqual(titles(), ['세금계산서 발행']);
  assert.equal(textOf(byClass(rows()[0], 'woni-task-auto')[0]), '원이 확인 · “발행했습니다”');
  assert.equal(rows()[0].children[0]['aria-label'], '완료 취소: 세금계산서 발행');
  assert.match(textOf(byClass(nodes['woni-tasks'], 'woni-note-hint')[0]), /^'원이 확인' 표시는/);
  toggle('보관 1').click();
  assert.deepEqual(titles(), ['오래된 맡긴 일']);  // 보관은 종류와 상관없이 모두
  assert.equal(textOf(byClass(nodes['woni-tasks'], 'woni-note-hint')[0]), "녹음한 지 14일이 넘은 일, 담당 확인에서 며칠 지난 일이라 목록과 알림에서 뺐어요(마감이 남은 일은 그대로 둬요). 아직 필요한 일이면 앱에서 열어 '담당 구분 수정'으로 꺼내 주세요.");
  toggle('남은 일').click();
  const search = walk(nodes['woni-tasks'], node => node.type === 'search')[0];
  search.value = '없는 말'; search.listeners.input();
  assert.equal(rows().length, 0);
  assert.ok(textOf(nodes['woni-tasks']).includes('찾는 할 일이 없어요'));
  search.value = '물류'; search.listeners.input();
  assert.deepEqual(titles(), ['재입고 일정 공유하기']);
});

test('완료 체크는 정확한 본문을 보내고, 결과를 모르면 같은 변경을 같은 requestId로 다시 보낸다', async () => {
  let data = taskData(), reads = 0;
  const sent = [], toasts = [];
  let answer = async () => { throw Error('timeout'); };
  const call = async (url, options) => {
    if (url.includes('availability')) return { enabled: true };
    if (url === '/api/woni/tasks') { reads++; return data; }
    if (url === '/api/woni/task-status') { sent.push(JSON.parse(JSON.stringify(options.body))); return answer(options.body); }
    return { chats: [], activity: {} };
  };
  const storage = new Map();
  const { rows, titles, toggle } = await openTasks(call, { toasts, storage });
  let release;
  answer = () => new Promise((_, reject) => { release = () => reject(Error('network')); });
  rows()[0].children[0].click();
  assert.deepEqual(Object.keys(sent[0]).sort(), ['actionId', 'expectedEvent', 'requestId', 'status']);
  assert.deepEqual({ ...sent[0], requestId: undefined }, { actionId: 'a1', status: 'done', expectedEvent: 'e1', requestId: undefined });
  assert.match(sent[0].requestId, /^[A-Za-z0-9_-]{8,100}$/);
  assert.equal(textOf(byClass(rows()[0], 'woni-task-meta')[0]), '저장 중…');
  assert.equal(rows()[0].children[0].disabled, true);
  rows()[0].children[0].click(); assert.equal(sent.length, 1);  // 저장 중에는 그 항목 조작을 막는다
  release(); await settle();
  assert.deepEqual(toasts, ['저장 여부를 확인하지 못했어요. 다시 누르면 같은 요청으로 확인해요.']);
  assert.ok(JSON.parse(storage.get('sessionholic:woni:task-requests')).a1);
  answer = async body => { data = taskData(); const [task] = data.actions.splice(0, 1); data.completedActions.unshift({ ...task, taskStatus: 'done', stateEvent: 'a1:done', previousStatus: 'open' });
    return { actionId: body.actionId, taskStatus: 'done', stateEvent: 'a1:done', result: 'saved' }; };
  const before = reads;
  rows()[0].children[0].click(); await settle();
  assert.equal(sent[1].requestId, sent[0].requestId);
  assert.equal(toasts.at(-1), '‘재입고 일정 공유하기’ 완료');
  assert.equal(JSON.parse(storage.get('sessionholic:woni:task-requests')).a1, undefined);
  assert.ok(reads > before);  // 성공하면 목록을 다시 받는다
  assert.equal(titles().length, 0);
  toggle('완료').click();
  assert.deepEqual(titles(), ['재입고 일정 공유하기', '세금계산서 발행']);
  answer = async () => { throw Object.assign(Error('다른 곳에서 할 일 상태가 바뀌었어요. 새로고침 후 다시 선택해 주세요'), { status: 400 }); };
  rows()[0].children[0].click(); await settle();
  assert.deepEqual({ ...sent[2], requestId: undefined }, { actionId: 'a1', status: 'open', expectedEvent: 'a1:done', requestId: undefined });
  assert.notEqual(sent[2].requestId, sent[0].requestId);  // 다른 변경은 새 요청
  assert.equal(toasts.at(-1), '다른 곳에서 할 일 상태가 바뀌었어요. 새로고침 후 다시 선택해 주세요');
  answer = async body => ({ actionId: body.actionId, taskStatus: 'done', stateEvent: 'x', result: 'unconfirmed' });
  rows()[1].children[0].click(); await settle();
  assert.equal(toasts.at(-1), '이전 변경은 저장을 확인하지 못했어요. 현재 상태를 불러왔으니 필요하면 다시 눌러 주세요.');
});

test('할 일 상세는 근거·담당·버튼을 보여 주고 노트 읽기 뒤 뒤로 가면 같은 상세로 돌아온다', async () => {
  const sent = [], toasts = [];
  const call = async (url, options) => {
    if (url.includes('availability')) return { enabled: true };
    if (url === '/api/woni/tasks') return taskData();
    if (url === '/api/woni/task-status') { sent.push({ ...options.body }); return { actionId: options.body.actionId, taskStatus: options.body.status, stateEvent: 'n', result: 'saved' }; }
    if (url.startsWith('/api/woni/note?')) return { id: 'rec-1', title: '주간 회의', recordedAt: '2026-11-08 10:00', text: '## 요약\n재입고는 목요일', nextOffset: null, revision: 'v1', url: '' };
    return { chats: [], activity: {} };
  };
  const { nodes, rows, toggle } = await openTasks(call, { toasts });
  const detail = () => nodes['woni-tasks'].children[1];
  nodes['woni-tasks'].scrollTop = 120;
  byClass(rows()[0], 'woni-task-open')[0].click();
  assert.equal(detail().hidden, false); assert.equal(nodes['woni-tasks'].children[0].hidden, true);
  assert.equal(textOf(byClass(detail(), 'woni-detail-title')[0]), '재입고 일정 공유하기');
  assert.equal(textOf(byClass(detail(), 'woni-detail-meta')[0]), '물류 · 11/12 · 남은 일');
  assert.ok(buttonOf(detail(), '완료로 표시'));
  assert.ok(textOf(detail()).includes('“목요일까지 공유할게요”'));
  assert.ok(textOf(detail()).includes('주간 회의 · 2026-11-08 10:00 · 김팀장'));
  buttonOf(detail(), '노트 읽기').click(); await settle();
  assert.equal(nodes['woni-notes'].hidden, false); assert.equal(nodes['woni-notes-tab']['aria-pressed'], 'true');
  assert.equal(textOf(byClass(nodes['woni-notes'], 'woni-detail-title')[0]), '주간 회의');
  const back = byClass(nodes['woni-notes'], 'woni-back')[0];
  assert.equal(back.textContent, '할 일');
  back.click();
  assert.equal(nodes['woni-tasks'].hidden, false); assert.equal(detail().hidden, false);
  assert.equal(textOf(byClass(detail(), 'woni-detail-title')[0]), '재입고 일정 공유하기');
  byClass(detail(), 'woni-back')[0].click();
  assert.equal(detail().hidden, true); assert.equal(nodes['woni-tasks'].scrollTop, 120);  // 목록 스크롤 유지
  toggle('내가 맡긴 일').click();
  byClass(rows()[0], 'woni-task-open')[0].click();  // 완료 보고된 맡긴 일
  assert.ok(textOf(detail()).includes('맡긴 사람: 라이언'));
  assert.ok(textOf(detail()).includes('할 사람: 박매니저'));
  assert.ok(textOf(detail()).includes('완료 보고 근거: 견적 받았어요'));
  assert.equal(buttonOf(detail(), '완료 보고 받음'), undefined);
  buttonOf(detail(), '다시 기다리는 중으로').click(); await settle();
  assert.deepEqual([sent[0].actionId, sent[0].status], ['d1', 'open']);
  assert.equal(toasts.at(-1), '‘촬영 견적 받기’ 남은 일로 되돌렸어요');
  byClass(detail(), 'woni-back')[0].click();
  byClass(rows().find(row => row.dataset.taskId === 'd2'), 'woni-task-open')[0].click();
  buttonOf(detail(), '완료 보고 받음').click(); await settle();
  assert.deepEqual([sent[1].actionId, sent[1].status], ['d2', 'reported']);
  assert.equal(toasts.at(-1), '‘상세페이지 수정’ 완료 보고 · 결과 확인 필요');
  byClass(detail(), 'woni-back')[0].click();
  toggle('완료').click();
  byClass(rows()[0], 'woni-task-open')[0].click();  // 보고를 받았다가 완료한 맡긴 일
  buttonOf(detail(), '완료 되돌리기').click(); await settle();
  assert.deepEqual([sent[2].actionId, sent[2].status], ['c2', 'reported']);
  byClass(detail(), 'woni-back')[0].click();
  toggle('내가 할 일').click();
  byClass(rows()[0], 'woni-task-open')[0].click();
  const auto = byClass(detail(), 'woni-auto')[0];
  assert.match(textOf(auto), /원이가 끝낸 근거를 확인해 완료로 체크했어요“발행했습니다”Slack 재무 채널틀렸으면 아래 '완료 되돌리기'를 눌러 주세요/);
  buttonOf(detail(), '완료 되돌리기').click(); await settle();
  assert.deepEqual([sent[3].actionId, sent[3].status], ['c1', 'open']);
});

test('변경 전에 떠난 목록 응답과 잠금 뒤 늦은 응답은 화면에 남지 않는다', async () => {
  const waiting = [], toasts = [];
  let post;
  const call = async (url, options) => {
    if (url.includes('availability')) return { enabled: true };
    if (url === '/api/woni/task-status') return new Promise(resolve => { post = resolve; });
    if (url === '/api/woni/tasks' || url.startsWith('/api/woni/notes?')) return new Promise(resolve => waiting.push({ url, resolve }));
    return { chats: [], activity: {} };
  };
  const answer = (url, value) => waiting.splice(0).forEach(item => item.url.startsWith(url) ? item.resolve(value) : waiting.push(item));
  let n = 0;
  const f = fixture(call, new Map(), undefined, { crypto: { randomUUID: () => `req-${++n}-abcdef` } });
  await f.app.check(call, { toast: text => toasts.push(text) }); f.nodes['woni-open'].click(); await settle();
  f.nodes['woni-tasks-tab'].click(); await settle();
  answer('/api/woni/tasks', taskData()); await settle();
  const rows = () => byClass(f.nodes['woni-tasks'], 'woni-task');
  assert.equal(rows()[0].dataset.taskId, 'a1');
  f.nodes['woni-tasks-tab'].click(); await settle();  // 다시 읽는 중
  const stale = waiting.splice(0);
  rows()[0].children[0].click();
  post({ actionId: 'a1', taskStatus: 'done', stateEvent: 'a1:done', result: 'saved' }); await settle();
  stale.forEach(item => item.resolve(taskData())); await settle();  // a1이 아직 남은 일이던 이전 응답
  assert.equal(rows().some(row => row.dataset.taskId === 'a1'), false);
  // 잠금 뒤 늦게 온 할 일·노트·상태 변경 응답은 버린다.
  waiting.splice(0);
  f.nodes['woni-notes-tab'].click(); f.nodes['woni-tasks-tab'].click(); await settle();
  assert.equal(waiting.length, 2);
  answer('/api/woni/tasks', taskData()); await settle();
  walk(f.nodes['woni-tasks'], node => node['aria-pressed'] !== undefined && textOf(node).startsWith('내가 맡긴 일'))[0].click();
  rows()[0].children[0].click();  // 맡긴 일 상태 변경이 진행 중
  assert.equal(textOf(byClass(rows()[0], 'woni-task-meta')[0]), '저장 중…');
  const count = toasts.length;
  f.app.lock();
  const secret = taskData(); secret.actions[1].title = '이전 비밀 할 일'; secret.counts.due = 5;
  answer('/api/woni/tasks', secret);
  answer('/api/woni/notes', { items: [{ id: 'n1', title: '이전 비밀 노트' }], total: 1, nextOffset: null });
  post({ actionId: 'd1', taskStatus: 'done', stateEvent: 'd1:done', result: 'saved' });
  await settle();
  assert.equal(toasts.length, count);  // 늦은 상태 변경 결과는 알리지 않는다
  assert.equal(f.nodes['woni-notice'].hidden, true);
  await f.app.check(call, { toast: text => toasts.push(text) }); f.nodes['woni-open'].click(); await settle();
  assert.equal(JSON.stringify(f.nodes['woni-tasks'].children).includes('이전 비밀'), false);
  assert.equal(JSON.stringify(f.nodes['woni-notes'].children).includes('이전 비밀'), false);
  assert.equal(f.nodes['woni-tasks-badge'].hidden, true);
  assert.equal(toasts.length, count);
});

test('원이 창을 열기만 해도 할 일 배지를 받고, 창이 열린 동안 알림은 창 안에도 띄운다', async () => {
  const call = async (url, options) => {
    if (url.includes('availability')) return { enabled: true };
    if (url === '/api/woni/tasks') return taskData();
    if (url === '/api/woni/task-status') return { actionId: options.body.actionId, taskStatus: 'done', stateEvent: 'n', result: 'saved' };
    return { chats: [], activity: {} };
  };
  const toasts = [];
  const f = fixture(call, new Map(), undefined, { crypto: { randomUUID: () => 'req-only-abcdef' } });
  await f.app.check(call, { toast: text => toasts.push(text) });
  assert.equal(f.nodes['woni-tasks-badge'].hidden, true);
  f.nodes['woni-open'].click(); await settle();
  assert.equal(f.nodes['woni-tasks-badge'].hidden, false);  // 할 일 탭을 열기 전에도
  assert.equal(f.nodes['woni-tasks-badge'].textContent, '2');
  f.nodes['woni-tasks-tab'].click(); await settle();
  byClass(f.nodes['woni-tasks'], 'woni-task')[0].children[0].click(); await settle();
  assert.equal(toasts.at(-1), '‘재입고 일정 공유하기’ 완료');
  assert.equal(f.nodes['woni-notice'].hidden, false);  // 보드 알림은 모달 아래에 깔리므로
  assert.equal(f.nodes['woni-notice'].textContent, '‘재입고 일정 공유하기’ 완료');
  await f.tick();
  assert.equal(f.nodes['woni-notice'].hidden, true);
});

function noteCall(log, overrides = {}) {
  return async (url, options) => {
    if (url.includes('availability')) return { enabled: true };
    if (!url.startsWith('/api/woni/note') && !url.startsWith('/api/woni/transcript')) return { chats: [], activity: {} };
    log.push(url);
    const query = Object.fromEntries(new URL(url, 'http://x').searchParams);
    if (url.startsWith('/api/woni/notes?')) {
      if (query.q) return { items: [{ id: 'rec-9', title: '재입고 회의', recordedAt: '11월 9일', preview: '재입고 일정' }], total: 1, nextOffset: null, query: query.q, error: '' };
      return query.offset === '0'
        ? { items: [{ id: 'rec-1', title: '주간 회의', recordedAt: '11월 8일', preview: '재고 · 촬영' }, { id: 'slack-1', title: 'Slack 대화', recordedAt: '11월 7일', preview: '샘플' }], total: 3, nextOffset: 2, query: '', error: '' }
        : { items: [{ id: 'rec-3', title: '거래처 통화', recordedAt: '11월 6일', preview: '견적' }], total: 3, nextOffset: null, query: '', error: '' };
    }
    if (url.startsWith('/api/woni/note?')) {
      if (overrides.note) return overrides.note(query);
      return query.offset === '0'
        ? { id: query.id, title: '주간 회의', recordedAt: '11월 8일', text: '## 요약\n첫 부분', nextOffset: 10, revision: 'v1', url: 'https://drive.google.com/file/d/fake' }
        : { id: query.id, title: '주간 회의', recordedAt: '11월 8일', text: '\n이어진 부분', nextOffset: null, revision: overrides.revision || 'v1', url: 'https://drive.google.com/file/d/fake' };
    }
    if (query.id.startsWith('slack-')) throw Object.assign(Error('Slack에서 온 기록이라 녹취 원문이 없어요.'), { status: 409 });
    const all = [
      { id: 'p1', kind: 'transcript', label: '전사', text: '재입고는 목요일이에요.', start: 65, speaker: '김팀장' },
      { id: 'p2', kind: 'transcript', label: '전사', text: '촬영은 다음 주', start: 3723, transcriptDoubt: true },
      { id: 'p3', kind: 'transcript', label: '전사', text: '견적은 받았어요', start: 4000, speaker: '라이언' },
    ];
    const rows = query.q ? all.filter(row => row.text.includes(query.q)) : query.offset === '0' ? all.slice(0, 2) : all.slice(2);
    return { sourceId: query.id, passages: rows, total: query.q ? rows.length : 3, nextOffset: !query.q && query.offset === '0' ? 2 : null, query: query.q, label: '전사 원문', revision: 'r1' };
  };
}
async function openNotes(call, extra = {}) {
  const f = fixture(call, new Map(), undefined, extra);
  await f.app.check(call); f.nodes['woni-open'].click(); await settle();
  f.nodes['woni-notes-tab'].click(); await settle();
  return { ...f, cards: () => byClass(f.nodes['woni-notes'], 'woni-note-card').map(card => textOf(byClass(card, 'woni-note-title')[0])) };
}

test('기록 탭은 정리 노트를 보여 주고 찾기는 늦춰 요청하며 더 보기로 이어 받는다', async () => {
  const log = [];
  const { nodes, cards, tick } = await openNotes(noteCall(log));
  assert.deepEqual(log, ['/api/woni/notes?q=&offset=0']);
  assert.equal(textOf(nodes['woni-notes']).includes('정리 노트 3개'), true);
  assert.deepEqual(cards(), ['주간 회의', 'Slack 대화']);
  const card = byClass(nodes['woni-notes'], 'woni-note-card')[0];
  assert.deepEqual(card.children.map(textOf), ['11월 8일', '주간 회의', '재고 · 촬영']);
  const more = buttonOf(nodes['woni-notes'], '노트 더 보기');
  assert.equal(more.hidden, false);
  more.click(); await settle();
  assert.equal(log.at(-1), '/api/woni/notes?q=&offset=2');
  assert.deepEqual(cards(), ['주간 회의', 'Slack 대화', '거래처 통화']);
  assert.equal(more.hidden, true);
  const search = walk(nodes['woni-notes'], node => node.type === 'search')[0];
  search.value = '재입'; search.listeners.input(); search.value = '재입고'; search.listeners.input();
  assert.equal(log.length, 2);  // 입력 중에는 아직 요청하지 않는다
  await tick(); await settle();
  assert.deepEqual(log.slice(2), ['/api/woni/notes?q=%EC%9E%AC%EC%9E%85%EA%B3%A0&offset=0']);
  assert.deepEqual(cards(), ['재입고 회의']);
});

test('노트 본문은 대화와 같은 렌더러로 그리고 이어 읽기는 같은 판일 때만 붙인다', async () => {
  const log = [], rendered = [];
  const MessageFormat = { render(node, text) { rendered.push(text); node.replaceChildren({ textContent: `MF:${text}`, children: [] }); } };
  const { nodes } = await openNotes(noteCall(log), { MessageFormat });
  byClass(nodes['woni-notes'], 'woni-note-card')[0].click(); await settle();
  assert.equal(log.at(-1), '/api/woni/note?id=rec-1&offset=0');
  assert.equal(nodes['woni-notes'].children[0].hidden, true);
  assert.deepEqual(rendered, ['## 요약\n첫 부분']);
  const link = walk(nodes['woni-notes'], node => node.className === 'woni-link')[0];
  assert.equal(link.href, 'https://drive.google.com/file/d/fake'); assert.equal(link.target, '_blank'); assert.match(link.rel, /noopener/);
  const back = byClass(nodes['woni-notes'], 'woni-back')[0];
  assert.equal(back.textContent, '기록');
  buttonOf(nodes['woni-notes'], '이어 읽기').click(); await settle();
  assert.equal(log.at(-1), '/api/woni/note?id=rec-1&offset=10');
  assert.equal(rendered.at(-1), '## 요약\n첫 부분\n이어진 부분');
  assert.equal(buttonOf(nodes['woni-notes'], '이어 읽기').hidden, true);
  back.click();
  assert.equal(nodes['woni-notes'].children[0].hidden, false);
  // 읽는 사이 판이 바뀌면 붙이지 않고 알린다. https가 아닌 주소는 링크로 만들지 않는다.
  const changed = await openNotes(noteCall([], { revision: 'v2' }), { MessageFormat });
  byClass(changed.nodes['woni-notes'], 'woni-note-card')[0].click(); await settle();
  buttonOf(changed.nodes['woni-notes'], '이어 읽기').click(); await settle();
  assert.ok(textOf(changed.nodes['woni-notes']).includes('읽는 사이 노트가 변경됐어요. 새로고침해 주세요.'));
  const unsafe = await openNotes(noteCall([], { note: query => ({ id: query.id, title: '노트', text: '본문', nextOffset: null, revision: 'v1', url: 'javascript:alert(1)' }) }));
  byClass(unsafe.nodes['woni-notes'], 'woni-note-card')[0].click(); await settle();
  assert.equal(walk(unsafe.nodes['woni-notes'], node => node.className === 'woni-link').length, 0);
});

test('녹취 원문은 화자·분:초·전사 불확실을 보이고 찾기·더 보기를 하며 원문이 없으면 서버 문구를 보여 준다', async () => {
  const log = [];
  const { nodes, tick } = await openNotes(noteCall(log));
  byClass(nodes['woni-notes'], 'woni-note-card')[0].click(); await settle();
  buttonOf(nodes['woni-notes'], '녹취 원문 보기').click(); await settle();
  assert.equal(log.at(-1), '/api/woni/transcript?id=rec-1&format=passages&q=&offset=0');
  const metas = () => byClass(nodes['woni-notes'], 'woni-passage-meta').map(textOf);
  assert.deepEqual(metas(), ['김팀장 · 1:05', '화자 미상 · 1:02:03전사 불확실']);
  assert.deepEqual(byClass(nodes['woni-notes'], 'woni-passage-text').map(textOf), ['재입고는 목요일이에요.', '촬영은 다음 주']);
  assert.equal(textOf(byClass(nodes['woni-notes'], 'woni-passages')[0].children[0]), '전사 원문 · 구간 3개');
  assert.equal(buttonOf(nodes['woni-notes'], '녹취 원문 보기').hidden, true);
  buttonOf(nodes['woni-notes'], '원문 더 보기').click(); await settle();
  assert.equal(log.at(-1), '/api/woni/transcript?id=rec-1&format=passages&q=&offset=2');
  assert.equal(metas().length, 3);
  const search = walk(byClass(nodes['woni-notes'], 'woni-passages')[0], node => node.type === 'search')[0];
  search.value = '견적'; search.listeners.input(); await tick(); await settle();
  assert.equal(log.at(-1), '/api/woni/transcript?id=rec-1&format=passages&q=%EA%B2%AC%EC%A0%81&offset=0');
  assert.deepEqual(metas(), ['라이언 · 1:06:40']);
  search.value = '없는 말'; search.listeners.input(); await tick(); await settle();
  assert.ok(textOf(nodes['woni-notes']).includes('해당 원문이 없어요.'));
  byClass(nodes['woni-notes'], 'woni-back')[0].click();
  byClass(nodes['woni-notes'], 'woni-note-card')[1].click(); await settle();  // Slack 기록
  assert.equal(search.value, '');  // 다른 노트는 새로 찾는다
  buttonOf(nodes['woni-notes'], '녹취 원문 보기').click(); await settle();
  assert.ok(textOf(nodes['woni-notes']).includes('Slack에서 온 기록이라 녹취 원문이 없어요.'));
  assert.equal(metas().length, 0);
});

// ---- 담당 확인 체크 (Ryan One 1.40.1) ----
test('담당 확인도 목록 체크와 상세의 완료 버튼으로 끝낼 수 있다', async () => {
  const sent = [], toasts = [];
  const call = async (url, options) => {
    if (url.includes('availability')) return { enabled: true };
    if (url === '/api/woni/tasks') return taskData();
    if (url === '/api/woni/task-status') { sent.push({ ...options.body }); return { actionId: options.body.actionId, taskStatus: 'done', stateEvent: 'u1:done', result: 'saved' }; }
    return { chats: [], activity: {} };
  };
  const { nodes, rows, toggle } = await openTasks(call, { toasts });
  toggle('담당 확인').click();
  byClass(rows()[0], 'woni-task-open')[0].click();
  const detail = nodes['woni-tasks'].children[1];
  assert.ok(buttonOf(detail, '완료로 표시'));
  assert.ok(textOf(detail).includes('추정 근거: 박매니저가 보내겠다고 말함'));
  byClass(detail, 'woni-back')[0].click();
  rows()[0].children[0].click(); await settle();
  assert.deepEqual(sent, [{ actionId: 'u1', status: 'done', expectedEvent: '', requestId: 'req-1-abcdef' }]);
  assert.equal(toasts.at(-1), '‘샘플 발송’ 완료');
});

// ---- 이전 대화·흐름·대화 찾기 (Ryan One 1.40.0) ----
// A synthetic Woni list of `total` chats; the state holds the last 60, like the server.
function history(total = 100, streamOf = () => null) {
  const all = Array.from({ length: total }, (_, seq) => {
    const stream = streamOf(seq);
    return { id: `c${seq}`, seq, text: `질문 ${seq}`, answer: `답 ${seq}`, status: 'complete', createdAt: 1000 + seq, finishedAt: 1001 + seq,
             ...(stream ? { stream, streamTitle: '합성 흐름' } : {}) };
  });
  return { all, state: () => ({ chats: all.slice(-60), chatTotal: all.length, chatsHasMore: all.length > 60, activity: {} }) };
}
async function openHistory(call) {
  const f = fixture(call);
  // The list's height follows what it holds, so keeping the reading place can be measured.
  Object.defineProperty(f.nodes['woni-messages'], 'scrollHeight', { get() { return this.children.length * 100; } });
  await f.app.check(call); f.nodes['woni-open'].click(); await settle();
  const ids = () => f.nodes['woni-messages'].children.filter(node => node.className?.startsWith('woni-message user')).map(node => node.dataset.chatId);
  const top = () => f.nodes['woni-messages'].children[0];
  return { ...f, ids, top };
}

test('대화 목록 맨 위에 닿으면 앞 40건을 이어 받아 순서대로 합치고 보던 자리를 지킨다', async () => {
  const data = history(100), urls = [];
  let fail = false;
  const call = async url => {
    if (url.includes('availability')) return { enabled: true };
    if (url.startsWith('/api/woni/chats?')) {
      urls.push(url);
      if (fail) { const error = new Error('원이에 연결하지 못했어요.'); error.status = 409; throw error; }
      const before = data.all.findIndex(chat => chat.id === new URLSearchParams(url.split('?')[1]).get('before'));
      return { chats: data.all.slice(Math.max(0, before - 40), before), hasMore: before > 40, total: 100 };
    }
    return url === '/api/woni' ? data.state() : { chats: [], activity: {} };
  };
  const { nodes, ids, top, tick } = await openHistory(call);
  assert.deepEqual(ids().slice(0, 2), ['c40', 'c41']);
  assert.equal(urls.length, 0);  // 창을 열기만 해서는 부르지 않는다
  assert.equal(textOf(top()), '이전 대화 더 보기');
  const messages = nodes['woni-messages'], place = () => messages.children.findIndex(node => node.dataset.chatId === 'c40');
  const before = place();
  messages.scrollTop = 30; messages.listeners.scroll(); await settle();
  assert.deepEqual(urls, ['/api/woni/chats?before=c40&limit=40']);
  assert.deepEqual(ids().slice(0, 3), ['c0', 'c1', 'c2']); assert.equal(ids().length, 100);
  assert.equal(ids().at(-1), 'c99');
  assert.equal(place() - before, 79);  // 말풍선 80개가 위에 붙고 '더 보기' 줄이 빠졌다
  assert.equal(messages.scrollTop, 30 + 79 * 100);  // 그만큼 내려 보던 대화(c40)에 머문다
  // c0까지 받았으면 더 부르지 않는다.
  messages.scrollTop = 0; messages.listeners.scroll(); await settle();
  assert.equal(urls.length, 1); assert.equal(top().className, 'woni-divider');
  // 60건 창에서 밀려난 대화도 남는다.
  data.all.push({ id: 'c100', seq: 100, text: '새 질문', answer: '새 답', status: 'complete', createdAt: 2000, finishedAt: 2001 });
  await tick(); await settle();
  assert.equal(ids().length, 101); assert.ok(ids().includes('c40'));
});

test('이전 대화를 못 받으면 다시 시도 버튼만 두고 스크롤로 저절로 다시 부르지 않는다', async () => {
  const data = history(100), urls = [];
  let fail = true;
  const call = async url => {
    if (url.includes('availability')) return { enabled: true };
    if (url.startsWith('/api/woni/chats?')) {
      urls.push(url);
      if (fail) { const error = new Error('대화를 찾지 못했어요'); error.status = 400; throw error; }
      return { chats: data.all.slice(20, 40), hasMore: true, total: 100 };
    }
    return url === '/api/woni' ? data.state() : { chats: [], activity: {} };
  };
  const { nodes, ids, top } = await openHistory(call);
  const messages = nodes['woni-messages'];
  messages.scrollTop = 0; messages.listeners.scroll(); await settle();
  assert.equal(top().className, 'woni-older is-error');
  assert.equal(textOf(top()), '대화를 찾지 못했어요다시 시도');
  messages.listeners.scroll(); messages.listeners.scroll(); await settle();
  assert.equal(urls.length, 1);
  fail = false;
  buttonOf(top(), '다시 시도').click(); await settle();
  assert.equal(urls.length, 2); assert.equal(ids()[0], 'c20');
  assert.equal(textOf(top()), '이전 대화 더 보기');
});

test('원이 답 아래 흐름 이름을 달고 누르면 그 흐름을 처음부터 열며 Esc는 흐름만 닫는다', async () => {
  const now = Date.now() / 1000, urls = [];
  const flow = { stream: 'fixture-flow', streamTitle: '합성 흐름' };
  const early = Array.from({ length: 3 }, (_, n) => ({ id: `f${n}`, seq: n, text: `처음 질문 ${n}`, answer: `처음 답 ${n}`, status: 'complete', createdAt: now - 900 + n, ...flow }));
  const chats = [
    { id: 'w1', seq: 10, text: '흐름 질문', answer: '흐름 답', status: 'complete', createdAt: now - 300, finishedAt: now - 290, ...flow },
    { id: 'n1', seq: 11, text: '흐름 없는 질문', answer: '흐름 없는 답', status: 'complete', createdAt: now - 200 },
    { id: 'a1', seq: 12, kind: 'alert', text: '알림', answer: '먼저 알린 말', status: 'complete', createdAt: now - 150, ...flow },
    { id: 'r1', seq: 13, text: '일하는 질문', status: 'running', progress: '찾는 중', createdAt: now - 100, ...flow },
    { id: 'x1', seq: 14, text: '실패 질문', status: 'failed', createdAt: now - 50, ...flow },
  ];
  const call = async url => {
    if (url.includes('availability')) return { enabled: true };
    if (url.startsWith('/api/woni/chats?')) { urls.push(url); return { chats: [...early, chats[0], chats[2], chats[3], chats[4]], hasMore: false, total: 7, streamTitle: '합성 흐름' }; }
    return url === '/api/woni' ? { chats, chatTotal: 15, chatsHasMore: true, activity: {} } : { chats: [], activity: {} };
  };
  const { nodes, ids } = await openHistory(call);
  const tags = () => byClass(nodes['woni-messages'], 'woni-stream-tag');
  const owner = tag => walk(nodes['woni-messages'], node => node.children?.includes(tag))[0];
  assert.deepEqual(tags().map(textOf), ['# 합성 흐름', '# 합성 흐름', '# 합성 흐름']);
  assert.deepEqual(tags().map(tag => owner(tag).className), ['woni-message assistant', 'woni-message assistant', 'woni-message assistant']);
  assert.deepEqual(tags().map(tag => textOf(byClass(owner(tag), 'woni-message-body')[0])), ['흐름 답', '찾는 중', '답변을 완료하지 못했어요. 원이 앱에서 상태를 확인해 주세요.']);
  assert.equal(tags()[0]['aria-label'], '흐름 합성 흐름만 보기');
  tags()[0].click(); await settle();
  assert.deepEqual(urls, ['/api/woni/chats?stream=fixture-flow&limit=200']);
  assert.equal(nodes['woni-filter-clear'].focused, true);  // 누른 이름이 사라져도 Esc가 창에 닿게
  assert.deepEqual(ids(), ['f0', 'f1', 'f2', 'w1', 'r1', 'x1']);
  assert.equal(tags().length, 0);  // 흐름 안에서는 달지 않는다
  assert.equal(nodes['woni-messages'].children[0].textContent, '이 흐름의 첫 대화 · 모두 7개');
  assert.equal(nodes['woni-filter'].hidden, false); assert.equal(nodes['woni-filter-label'].textContent, '흐름: 합성 흐름');
  let prevented = 0;
  nodes['woni-dialog'].listeners.keydown({ key: 'Escape', preventDefault: () => prevented++ });
  assert.equal(prevented, 1); assert.equal(nodes['woni-filter'].hidden, true); assert.equal(nodes['woni-dialog'].open, true);
  assert.deepEqual(ids(), ['w1', 'n1', 'r1', 'x1']);
  nodes['woni-dialog'].listeners.keydown({ key: 'Escape', preventDefault: () => prevented++ });
  assert.equal(prevented, 1);  // 닫을 흐름이 없으면 원래대로 창이 닫힌다
  // '이 흐름만 보기' 메뉴도 같은 흐름을 처음부터 연다.
  const answer = nodes['woni-messages'].children.find(node => node.dataset.chatId === 'w1' && node.className.includes('assistant'));
  buttonOf(answer, '이 흐름만 보기 · 합성 흐름').click(); await settle();
  assert.equal(urls.length, 2); assert.equal(ids()[0], 'f0');
  // '전체 대화'는 대화 화면에 남아 모든 대화를 보인다.
  nodes['woni-filter-clear'].click();
  assert.equal(nodes['woni-filter'].hidden, true); assert.equal(nodes['woni-messages'].hidden, false); assert.deepEqual(ids(), ['w1', 'n1', 'r1', 'x1']);
});

test('찾기 화면은 흐름 목록과 대화 찾기를 보여 주고 결과를 누르면 그 자리로 간다', async () => {
  const data = history(100, seq => seq % 2 ? 'fixture-flow' : null), urls = [];
  let far = false;
  const streams = { items: [
    { id: 'fixture-flow', title: '합성 흐름', count: 50, running: 1, unread: 0, lastAt: Date.now() / 1000, lastText: '맡긴 말', lastAnswer: '답 첫 줄', lastStatus: 'running' },
    { id: 'other-flow', title: '다른 흐름', count: 2, running: 0, unread: 2, lastAt: 1000, lastText: '질문', lastAnswer: '', lastStatus: 'failed' },
    { id: 'third-flow', title: '세 번째', count: 1, running: 0, unread: 0, lastAt: 1000, lastText: '질문', lastAnswer: '끝난 답', lastStatus: 'complete' }], total: 3 };
  const call = async url => {
    if (url.includes('availability')) return { enabled: true };
    urls.push(url);
    if (url === '/api/woni/streams') return streams;
    if (url.startsWith('/api/woni/chat-search?')) return { q: '재고 확인', total: 3, nextOffset: null,
      streams: [{ id: 'fixture-flow', title: '재고 흐름', count: 4, lastAt: 1 }],
      items: [{ id: 'c70', stream: 'fixture-flow', streamTitle: '합성 흐름', createdAt: 1070, question: '재고 확인 부탁', snippet: '…재고 3개 확인했어요…' },
              { id: 'c5', kind: 'chat', createdAt: 1005, question: '오래된 재고', snippet: '' },
              { id: 'c3', stream: 'fixture-flow', streamTitle: '합성 흐름', createdAt: 1003, question: '아주 오래된 재고', snippet: '' }] };
    if (url.startsWith('/api/woni/chats?through=')) {
      if (far) return { chats: [], hasMore: true, total: 500, tooFar: true };
      return { chats: data.all.slice(4, 40), hasMore: true, total: 100 };  // the server adds the 3 chats before it
    }
    if (url.startsWith('/api/woni/chats?stream=')) return { chats: data.all.filter(chat => chat.stream), hasMore: false, total: 50 };
    return url === '/api/woni' ? data.state() : { chats: [], activity: {} };
  };
  const { nodes, ids, tick } = await openHistory(call);
  nodes['woni-find-open'].click(); await settle();
  assert.equal(nodes['woni-find'].hidden, false); assert.equal(nodes['woni-messages'].hidden, true); assert.equal(nodes['woni-form'].hidden, true);
  assert.equal(nodes['woni-find-open']['aria-pressed'], 'true');
  const find = nodes['woni-find'];
  const rows = () => byClass(find, 'woni-find-row');
  assert.ok(urls.includes('/api/woni/streams'));
  assert.equal(textOf(byClass(find, 'woni-find-heading')[0]), '흐름');
  assert.deepEqual(rows().map(row => textOf(byClass(row, 'woni-find-preview')[0])), ['맡긴 말', '답을 받지 못했어요 · 질문', '끝난 답']);
  assert.equal(byClass(rows()[0], 'woni-find-running').length, 1);
  assert.equal(textOf(byClass(rows()[1], 'woni-find-unread')[0]), '새 답 2개');
  assert.equal(textOf(byClass(rows()[1], 'woni-find-time')[0]).length > 0, true);
  // 흐름을 열고 뒤로 가면 흐름 목록으로 돌아온다.
  rows()[0].click(); await settle();
  assert.equal(nodes['woni-messages'].hidden, false); assert.equal(ids().length, 50);
  let cancelled = 0;
  nodes['woni-dialog'].listeners.cancel({ preventDefault: () => cancelled++ });
  assert.equal(cancelled, 1); assert.equal(nodes['woni-find'].hidden, false); assert.equal(nodes['woni-filter'].hidden, true);
  assert.equal(walk(nodes['woni-find'], node => node.type === 'search')[0].focused, true);
  // 찾기 화면에서 한 번 더 뒤로 가면 대화로, 그다음에야 창이 닫힌다.
  nodes['woni-dialog'].listeners.cancel({ preventDefault: () => cancelled++ });
  assert.equal(cancelled, 2); assert.equal(nodes['woni-find'].hidden, true); assert.equal(nodes['woni-messages'].hidden, false);
  nodes['woni-dialog'].listeners.cancel({ preventDefault: () => cancelled++ });
  assert.equal(cancelled, 2);
  nodes['woni-find-open'].click(); await settle();
  // 낱말을 적으면 0.3초 뒤 찾는다(흐름 이름 일치 먼저, 찾은 말은 굵게).
  const input = walk(find, node => node.type === 'search')[0];
  input.value = '재고 확인'; input.listeners.input();
  assert.equal(urls.some(url => url.startsWith('/api/woni/chat-search')), false);
  await tick(); await settle();
  assert.ok(urls.includes('/api/woni/chat-search?q=%EC%9E%AC%EA%B3%A0+%ED%99%95%EC%9D%B8&offset=0&limit=20'));
  assert.deepEqual(byClass(find, 'woni-find-heading').map(textOf), ['흐름 1', '대화 3건']);
  assert.deepEqual(byClass(rows()[1], 'woni-match').map(textOf), ['재고', '확인', '재고', '확인']);
  assert.equal(textOf(byClass(rows()[1], 'woni-find-question')[0]), '나: 재고 확인 부탁');
  assert.equal(textOf(byClass(rows()[2], 'woni-find-stream')[0]), '흐름 없음');
  // 받아 둔 대화면 그 자리로 가서 강조한다.
  rows()[1].click(); await settle();
  assert.equal(nodes['woni-messages'].hidden, false);
  assert.ok(nodes['woni-messages'].children.find(node => node.dataset.chatId === 'c70').className.includes('is-found'));
  // 받지 않은 대화는 그 대화까지 이어 받는다.
  nodes['woni-find-open'].click(); await settle();
  rows()[2].click(); await settle();
  assert.ok(urls.includes('/api/woni/chats?through=c5&before=c40'));
  assert.equal(ids()[0], 'c4'); assert.ok(nodes['woni-messages'].children.find(node => node.dataset.chatId === 'c5').className.includes('is-found'));
  // 너무 멀면 그 흐름을 처음부터 열어 그 자리로.
  far = true;
  nodes['woni-find-open'].click(); await settle();
  rows()[3].click(); await settle(); await settle();
  assert.ok(urls.includes('/api/woni/chats?through=c3&before=c4'));
  assert.ok(urls.includes('/api/woni/chats?stream=fixture-flow&limit=200'));
  assert.equal(nodes['woni-filter-label'].textContent, '흐름: 합성 흐름');
  assert.ok(nodes['woni-messages'].children.find(node => node.dataset.chatId === 'c3').className.includes('is-found'));
});

test('잠그면 이어 받은 대화·흐름·찾기 결과를 모두 버리고 늦은 응답도 남기지 않는다', async () => {
  const data = history(100);
  let finish;
  const call = async url => {
    if (url.includes('availability')) return { enabled: true };
    if (url.startsWith('/api/woni/chats?')) return new Promise(resolve => { finish = resolve; });
    if (url === '/api/woni/streams') return { items: [{ id: 'secret-flow', title: '비밀 흐름', count: 1 }], total: 1 };
    return url === '/api/woni' ? data.state() : { chats: [], activity: {} };
  };
  const { nodes, app, ids } = await openHistory(call);
  nodes['woni-find-open'].click(); await settle();
  assert.ok(textOf(nodes['woni-find']).includes('비밀 흐름'));
  nodes['woni-chat-tab'].click();
  nodes['woni-messages'].scrollTop = 0; nodes['woni-messages'].listeners.scroll(); await settle();
  app.lock(); finish({ chats: [{ id: 'late', seq: 0, text: '늦은 비밀', answer: '늦은 답' }], hasMore: false });
  await settle();
  assert.equal(textOf(nodes['woni-find']).includes('비밀 흐름'), false);
  await app.check(call); nodes['woni-open'].click(); await settle();
  assert.equal(ids().includes('late'), false); assert.equal(ids()[0], 'c40');
});

// ---- 늦게 온 답은 맨 아래에, 그 질문에 단 답글로 (라이언 2026-11-11 08:10) ----
// "작업 중인 게 위에 있다고 해서 거기에 최종 답 가져와 버리면은 … 위에 온 답변을 내가 놓치게 되잖아"
const plainRows = rows => JSON.parse(JSON.stringify(rows));
const shape = rows => plainRows(rows).map(row => `${row.id}:${row.kind}${row.detached ? '(떨어짐)' : ''}`);

test('줄 함수: 질문과 바로 붙은 답은 한 덩어리, 작업 중이면 질문 줄만', () => {
  const { app } = fixture(async () => ({}));
  assert.deepEqual(plainRows(app.chatRows([{ id: 'a', seq: 1, text: '질문', answer: '답', status: 'complete', createdAt: 100, finishedAt: 160 }])),
    [{ kind: 'merged', id: 'a', at: 100, detached: false, key: 'a:merged' }]);
  assert.deepEqual(plainRows(app.chatRows([{ id: 'a', seq: 1, text: '질문', status: 'running', progress: '찾는 중', createdAt: 100 }])),
    [{ kind: 'question', id: 'a', at: 100, detached: false, key: 'a:question' }]);
  assert.deepEqual(plainRows(app.chatRows([{ id: 'a', seq: 1, text: '질문', status: 'queued', createdAt: 100 }])).map(row => row.kind), ['question']);
  assert.deepEqual(plainRows(app.chatRows([])), []);
});

test('줄 함수: 먼저 보낸 질문의 답이 늦게 끝나면 다른 대화 아래로 떨어져 맨 아래에 온다', () => {
  const { app } = fixture(async () => ({}));
  const at = (h, m) => Date.UTC(2026, 10, 11, h - 9, m) / 1000;  // KST
  const chats = [
    { id: 'A', seq: 1, text: 'A 질문', answer: 'A 답', status: 'complete', createdAt: at(10, 0), finishedAt: at(10, 5) },
    { id: 'B', seq: 2, text: 'B 질문', answer: 'B 답', status: 'complete', createdAt: at(10, 1), finishedAt: at(10, 2) },
  ];
  const rows = plainRows(app.chatRows(chats));
  assert.deepEqual(rows, [
    { kind: 'question', id: 'A', at: at(10, 0), detached: true, key: 'A:question' },
    { kind: 'merged', id: 'B', at: at(10, 1), detached: false, key: 'B:merged' },
    { kind: 'answer', id: 'A', at: at(10, 5), detached: true, key: 'A:answer' },
  ]);
  // A가 아직 작업 중이면 A 질문 줄만 위에 남는다.
  assert.deepEqual(shape(app.chatRows([{ ...chats[0], status: 'running', answer: '', finishedAt: undefined }, chats[1]])), ['A:question', 'B:merged']);
});

test('줄 함수: 알림·실패·중단·시각 없는 답·같은 시각을 규칙대로 놓는다', () => {
  const { app } = fixture(async () => ({}));
  // 알림은 답 줄 하나(finishedAt, 없으면 createdAt)이고 떨어지지 않는다.
  assert.deepEqual(plainRows(app.chatRows([{ id: 'n', seq: 1, kind: 'alert', answer: '발주 마감', status: 'complete', createdAt: 50, finishedAt: 70 }])),
    [{ kind: 'answer', id: 'n', at: 70, detached: false, key: 'n:answer' }]);
  assert.equal(plainRows(app.chatRows([{ id: 'n', seq: 1, kind: 'alert', answer: '발주 마감', status: 'complete', createdAt: 50 }]))[0].at, 50);
  assert.deepEqual(shape(app.chatRows([{ id: 'n', seq: 1, kind: 'alert', status: 'running', createdAt: 50 }])), ['n:answer']);
  // 실패·중단은 답이 없어도 답 줄(실패 안내)이다. 끼어든 줄이 있으면 떨어진다.
  assert.deepEqual(shape(app.chatRows([
    { id: 'f', seq: 1, text: '실패할 질문', status: 'failed', createdAt: 100, finishedAt: 400 },
    { id: 'i', seq: 2, text: '중단될 질문', status: 'interrupted', createdAt: 200, finishedAt: 210 },
    { id: 'n', seq: 3, kind: 'alert', answer: '알림', status: 'complete', createdAt: 300, finishedAt: 300 },
  ])), ['f:question(떨어짐)', 'i:merged', 'n:answer', 'f:answer(떨어짐)']);
  // 답이 없는 complete는 질문 줄만이다(지금 화면과 같다).
  assert.deepEqual(shape(app.chatRows([{ id: 'e', seq: 1, text: '질문', status: 'complete', createdAt: 100, finishedAt: 120 }])), ['e:question']);
  // finishedAt이 없거나 0이면 createdAt: 질문에 붙는다(뒤 대화보다 앞).
  assert.deepEqual(shape(app.chatRows([
    { id: 'x', seq: 1, text: 'x', answer: 'x 답', status: 'complete', createdAt: 100, finishedAt: 0 },
    { id: 'y', seq: 2, text: 'y', answer: 'y 답', status: 'complete', createdAt: 150 },
  ])), ['x:merged', 'y:merged']);
  // 시각이 아예 없는 옛 대화는 0으로 끌어올리지 않고 자기 답 시각·앞 줄 시각을 써서 제자리에 붙는다.
  assert.deepEqual(shape(app.chatRows([
    { id: 'old', status: 'complete', answer: '옛 답', finishedAt: 10 },
    { id: 'bare', status: 'complete', answer: '시각 없음' },
    { id: 'last', status: 'complete', answer: '새 답', finishedAt: 20 },
  ])), ['old:merged', 'bare:merged', 'last:merged']);
});

test('줄 함수: 같은 시각은 질문이 자기 답보다 앞, 그다음 seq 순서로 늘 같게 놓는다', () => {
  const { app } = fixture(async () => ({}));
  const chats = [
    { id: 'p', seq: 7, text: 'p', answer: 'p 답', status: 'complete', createdAt: 100, finishedAt: 100 },
    { id: 'q', seq: 8, text: 'q', answer: 'q 답', status: 'complete', createdAt: 100, finishedAt: 100 },
    { id: 'r', seq: 9, text: 'r', answer: 'r 답', status: 'complete', createdAt: 100, finishedAt: 100 },
  ];
  assert.deepEqual(shape(app.chatRows(chats)), ['p:merged', 'q:merged', 'r:merged']);
  // 받은 순서가 달라도 seq가 있으면 seq 순서
  assert.deepEqual(shape(app.chatRows([chats[2], chats[0], chats[1]])), ['p:merged', 'q:merged', 'r:merged']);
  // seq가 없으면 받은 순서를 지킨다.
  const bare = chats.map(({ seq, ...chat }) => chat);
  assert.deepEqual(shape(app.chatRows([bare[1], bare[0], bare[2]])), ['q:merged', 'p:merged', 'r:merged']);
  // c0 답과 c1 질문이 같은 시각이어도 c0은 붙은 채로 남는다(이전 대화 fixture와 같은 모양).
  assert.deepEqual(shape(app.chatRows([
    { id: 'c0', seq: 0, text: '0', answer: '0', status: 'complete', createdAt: 1000, finishedAt: 1001 },
    { id: 'c1', seq: 1, text: '1', answer: '1', status: 'complete', createdAt: 1001, finishedAt: 1002 },
  ])), ['c0:merged', 'c1:merged']);
  // 답 시각이 질문보다 앞서 찍혀도 답이 질문 위로 가지 않는다.
  assert.deepEqual(shape(app.chatRows([{ id: 's', seq: 1, text: 's', answer: 's 답', status: 'complete', createdAt: 100, finishedAt: 90 }])), ['s:merged']);
});

test('줄 함수: 흐름으로 거른 뒤 다시 계산하면 떨어졌던 답이 그 흐름 안에서는 붙는다', () => {
  const { app } = fixture(async () => ({}));
  const chats = [
    { id: 'A', seq: 1, stream: 'flow-a', text: 'A', answer: 'A 답', status: 'complete', createdAt: 100, finishedAt: 500 },
    { id: 'B', seq: 2, stream: 'flow-b', text: 'B', answer: 'B 답', status: 'complete', createdAt: 200, finishedAt: 300 },
  ];
  assert.deepEqual(shape(app.chatRows(chats)), ['A:question(떨어짐)', 'B:merged', 'A:answer(떨어짐)']);
  assert.deepEqual(shape(app.chatRows(chats.filter(chat => chat.stream === 'flow-a'))), ['A:merged']);
});

function replyOrderChats(aDone) {
  const at = (h, m) => Date.UTC(2026, 10, 11, h - 9, m) / 1000;  // KST 오늘이 아니어도 시각 글자만 본다
  return [
    { id: 'A', seq: 1, text: 'A 질문: 재입고 일정 확인해 줘\n둘째 줄', stream: 'flow-a', streamTitle: '재입고',
      ...(aDone ? { status: 'complete', answer: 'A 답이에요.\n\n👉 일정 공유해 줘 | 다시 확인해 줘', finishedAt: at(20, 32) } : { status: 'running', progress: '재입고 일정 찾는 중' }),
      createdAt: at(20, 0) },
    { id: 'B', seq: 2, text: 'B 질문', stream: 'flow-b', streamTitle: '보고', status: 'complete', answer: 'B 답이에요.\n\n👉 B 추천', createdAt: at(20, 10), finishedAt: at(20, 12) },
  ];
}

test('늦게 끝난 A 답은 B 대화 아래 맨 아래에 인용을 달고, A 질문 자리에는 답으로 가는 링크만 남는다', async () => {
  let aDone = false;
  const call = async (url, options) => {
    if (url.includes('availability')) return { enabled: true };
    if (options?.method === 'POST') return {};
    return { chats: replyOrderChats(aDone), activity: {} };
  };
  const { nodes, app, tick } = fixture(call);
  await app.check(call); nodes['woni-open'].click(); await settle();
  const list = () => nodes['woni-messages'].children.filter(node => node.className?.startsWith('woni-message') || node.className === 'woni-suggestions');
  const label = node => node.className === 'woni-suggestions' ? '추천' : `${node.dataset.chatId || '-'}:${node.className.includes('user') ? '질문' : node.dataset.state || '답'}`;
  // A 작업 중: 진행 문장은 A 질문 바로 아래(지금 자리 그대로)
  assert.deepEqual(list().map(label), ['A:질문', '-:working', 'B:질문', 'B:답', '추천']);
  assert.equal(textOf(list()[1]).includes('재입고 일정 찾는 중'), true);
  aDone = true; await tick(); await settle();
  assert.deepEqual(list().map(label), ['A:질문', 'B:질문', 'B:답', 'A:답', '추천']);
  const [askA, askB, answerB, answerA] = list();
  // A 질문 아래 링크: 답한 시각과 ↓, 접근성 이름은 '답으로 이동'
  const link = byClass(askA, 'woni-answer-link')[0];
  assert.equal(link.textContent, '답은 아래에 · 오후 8:32 ↓');
  assert.equal(link['aria-label'], '답으로 이동'); assert.equal(link.type, 'button');
  // A 답 맨 위 인용: 질문 첫 줄, 접근성 이름은 '원래 질문으로 이동'
  const quote = answerA.children[0];
  assert.equal(quote.className, 'woni-message-quote is-ask');
  assert.equal(quote.textContent, '↩ 라이언 · A 질문: 재입고 일정 확인해 줘');
  assert.equal(quote['aria-label'], '원래 질문으로 이동');
  // 답한 시각·흐름 이름·메뉴는 지금 답 말풍선과 같다.
  assert.equal(byClass(answerA, 'woni-answer-time')[0].textContent, '오후 8:32');
  assert.equal(byClass(answerA, 'woni-stream-tag').length, 1);
  assert.equal(byClass(answerA, 'woni-message-actions').length, 1);
  // 붙은 B 대화에는 인용·링크가 없다.
  for (const node of [askB, answerB]) { assert.equal(byClass(node, 'woni-message-quote').length, 0); assert.equal(byClass(node, 'woni-answer-link').length, 0); }
  // 추천 칩은 맨 아래 답(A)에만
  const boxes = nodes['woni-messages'].children.filter(node => node.className === 'woni-suggestions');
  assert.equal(boxes.length, 1); assert.deepEqual(boxes[0].children.map(button => button.textContent), ['일정 공유해 줘', '다시 확인해 줘']);
  // 줄 키는 첫 말풍선에 남는다.
  assert.equal(askA.dataset.rowKey, 'A:question'); assert.equal(askB.dataset.rowKey, 'B:merged'); assert.equal(answerA.dataset.rowKey, 'A:answer');
  // 인용을 누르면 A 질문 줄만, 링크를 누르면 A 답 줄만 잠깐 강조한다.
  const found = () => list().filter(node => node.className.includes('is-found')).map(label);
  quote.listeners.click(); assert.deepEqual(found(), ['A:질문']);
  byClass(list()[0], 'woni-answer-link')[0].listeners.click(); assert.deepEqual(found(), ['A:답']);
  await tick(); assert.deepEqual(found(), []);  // 2.4초 뒤 강조를 지운다
});

test('추천 칩·답글 띠 추천은 맨 아래 답 줄 기준이고, 흐름으로 거르면 같은 규칙으로 다시 붙는다', async () => {
  const sent = [];
  const call = async (url, options) => {
    if (url.includes('availability')) return { enabled: true };
    if (url === '/api/woni' && options?.method === 'POST') { sent.push(JSON.parse(JSON.stringify(options.body))); return { id: options.body.requestId, status: 'queued' }; }
    if (options?.method === 'POST') return {};
    if (url.startsWith('/api/woni/chats?stream=flow-a')) return { chats: replyOrderChats(true).filter(chat => chat.stream === 'flow-a'), hasMore: false, total: 1, streamTitle: '재입고' };
    return { chats: replyOrderChats(true), activity: {} };
  };
  const { nodes, app } = fixture(call);
  await app.check(call); nodes['woni-open'].click(); await settle();
  const message = (id, role) => nodes['woni-messages'].children.find(node => node.dataset.chatId === id && node.className.includes(role));
  const reply = (id, role) => byClass(message(id, role), 'woni-message-actions')[0].children[0].click();
  const picks = nodes['woni-reply-suggestions'];
  // B는 seq상 마지막이지만 맨 아래 답이 아니라서 답글을 고르면 띠 아래에 추천이 뜬다.
  reply('B', 'assistant'); assert.equal(picks.hidden, false); assert.deepEqual(picks.children.map(button => button.textContent), ['B 추천']);
  // A가 맨 아래 답이라 말풍선 아래 칩이 이미 있어 띠 아래에는 두지 않는다.
  reply('A', 'assistant'); assert.equal(picks.hidden, true);
  const box = nodes['woni-messages'].children.find(node => node.className === 'woni-suggestions');
  await box.children[0].click();
  assert.deepEqual(sent, [{ text: '일정 공유해 줘', requestId: 'request-123456', replyTo: { id: 'A', part: 'answer' } }]);
  // A의 흐름만 보면 A 답은 자기 질문 바로 아래에 붙고 인용·링크가 없다.
  // (보낸 칩은 확인 전 말풍선으로 남으니 새 창에서 본다)
  const fresh = fixture(call);
  await fresh.app.check(call); fresh.nodes['woni-open'].click(); await settle();
  byClass(fresh.nodes['woni-messages'].children.find(node => node.dataset.chatId === 'A' && node.className.includes('assistant')), 'woni-stream-tag')[0].click(); await settle();
  assert.equal(fresh.nodes['woni-filter-label'].textContent, '흐름: 재입고');
  const shown = fresh.nodes['woni-messages'].children.filter(node => node.className?.startsWith('woni-message '));
  assert.deepEqual(shown.map(node => `${node.dataset.chatId}:${node.className.includes('user') ? '질문' : '답'}`), ['A:질문', 'A:답']);
  assert.equal(byClass(fresh.nodes['woni-messages'], 'woni-answer-link').length, 0);
  assert.equal(byClass(fresh.nodes['woni-messages'], 'is-ask').length, 0);
  assert.equal(shown[0].dataset.rowKey, 'A:merged');
});

test('떨어진 답이 다른 날 끝나면 그 앞에 날짜 줄이 서고, 질문 자리 링크도 그 날짜를 붙인다', async () => {
  const day1 = Date.UTC(2026, 10, 9, 14, 0) / 1000;  // KST 11/9 오후 11:00
  const chats = [
    { id: 'A', seq: 1, text: '밤 질문', answer: '다음 날 답', status: 'complete', createdAt: day1, finishedAt: day1 + 7200 },
    { id: 'B', seq: 2, text: '곧 끝난 질문', answer: '바로 답', status: 'complete', createdAt: day1 + 600, finishedAt: day1 + 700 },
  ];
  const call = async url => url.includes('availability') ? { enabled: true } : { chats, activity: {} };
  const { nodes, app } = fixture(call);
  await app.check(call); nodes['woni-open'].click(); await settle();
  const seen = nodes['woni-messages'].children.filter(node => node.className === 'woni-divider' || node.className?.startsWith('woni-message '))
    .map(node => node.className === 'woni-divider' ? '날짜' : `${node.dataset.chatId}:${node.className.includes('user') ? '질문' : '답'}`);
  assert.deepEqual(seen, ['날짜', 'A:질문', 'B:질문', 'B:답', '날짜', 'A:답']);
  // 다른 날 끝난 답의 링크는 앱처럼 날짜를 붙인다.
  assert.equal(byClass(nodes['woni-messages'], 'woni-answer-link')[0].textContent, '답은 아래에 · 11/10 오전 1:00 ↓');
});

test('만드는 중인 먼저 알림은 질문 줄이 없어도 진행 말풍선을 그대로 보인다', async () => {
  const chats = [{ id: 'n', seq: 1, kind: 'alert', status: 'running', progress: '알림 쓰는 중', createdAt: 100 }];
  const call = async url => url.includes('availability') ? { enabled: true } : { chats, activity: {} };
  const { nodes, app } = fixture(call);
  await app.check(call); nodes['woni-open'].click(); await settle();
  const shown = nodes['woni-messages'].children.filter(node => node.className?.startsWith('woni-message '));
  assert.equal(shown.length, 1); assert.equal(shown[0].dataset.state, 'working'); assert.equal(textOf(shown[0]).includes('알림 쓰는 중'), true);
  assert.equal(shown[0].dataset.rowKey, 'n:answer');
});

test('원이 입력창은 다른 입력칸처럼 맞춤법 검사·자동 보정이 꺼져 한글 입력 중 밑줄이 깜빡이지 않는다', () => {
  const html = fs.readFileSync(require('node:path').join(__dirname, '..', 'web', 'index.html'), 'utf8');
  const tag = html.match(/<textarea id="woni-input"[^>]*>/)?.[0];
  assert.ok(tag, '원이 입력창이 있어야 한다');
  for (const [key, val] of [['spellcheck', 'false'], ['autocorrect', 'off'], ['autocomplete', 'off'], ['autocapitalize', 'off']])
    assert.match(tag, new RegExp(`\\s${key}="${val}"`));
});

test('실패·중단 줄은 원이가 쓴 안내를 그대로 보이고, 다시 보내기는 retryOf로 한 번만 보낸다', async () => {
  // 라이언 2026-11-11: 재시작 뒤 하던일 원이에는 '완료하지 못했어요'만 떴다. 앱과 같은 안내와 다시 보내기를 둔다.
  const sent = [];
  const stopped = '9시 43분 원이 서버 재시작 때 이 작업도 멈췄어요. 그전까지 도구를 12번 썼어요. 다시 보내면 같은 세션에서 이어서 해요.';
  const chats = [
    { id: 'done', seq: 1, text: '끝난 질문', status: 'complete', answer: '끝난 답', createdAt: 10, finishedAt: 20 },
    { id: 'again', seq: 2, text: '이미 다시 보낸 질문', status: 'failed', answer: 'Claude 연결이 잠시 불안정했어요.', retriedBy: 'r1', createdAt: 30, finishedAt: 40 },
    { id: 'stop', seq: 3, text: '근본원인부터 해결해줘', status: 'interrupted', errorKind: 'interrupted', toolsRan: 12, answer: stopped,
      replyTo: { id: 'done', part: 'answer', who: '원이', excerpt: '끝난 답' }, attachments: [{ id: '20261111-abcdefabcdef', name: 'a.png' }], createdAt: 50, finishedAt: 60 },
  ];
  const call = async (url, options) => {
    if (url.includes('availability')) return { enabled: true };
    if (url === '/api/woni' && options?.method === 'POST') { sent.push(JSON.parse(JSON.stringify(options.body))); return { id: options.body.requestId, status: 'queued' }; }
    if (options?.method === 'POST') return {};
    return { chats, activity: {} };
  };
  const { nodes, app } = fixture(call);
  await app.check(call); nodes['woni-open'].click(); await settle();
  const failed = nodes['woni-messages'].children.filter(node => node.dataset?.state === 'failed');
  assert.deepEqual(failed.map(node => textOf(byClass(node, 'woni-message-body')[0])), ['Claude 연결이 잠시 불안정했어요.', stopped]);
  assert.equal(textOf(nodes['woni-messages']).includes('답변을 완료하지 못했어요'), false);  // 고정 문구 대신 원이 안내
  const boxes = nodes['woni-messages'].children.filter(node => node['aria-label'] === '다시 보내기' || node.attributes?.['aria-label'] === '다시 보내기' || (node.className === 'woni-suggestions' && textOf(node).includes('다시')));
  assert.deepEqual(boxes.map(textOf), ['다시 보냈어요. 아래 답을 확인해 주세요.', '다시 보내기']);
  await boxes[1].children[0].click(); await settle();
  assert.deepEqual(sent, [{ text: '근본원인부터 해결해줘', requestId: 'request-123456', retryOf: 'stop', attachments: ['20261111-abcdefabcdef'], replyTo: { id: 'done', part: 'answer' } }]);
});
