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
      append(...items) { this.children.push(...items); },
      replaceChildren(...items) { this.children = items; },
      querySelector() { return this.submit; },
      showModal() { this.open = true; },
      close() { this.open = false; this.listeners.close?.(); } };
  }
  for (const id of ['woni-dialog', 'woni-open', 'woni-input', 'woni-messages', 'woni-status', 'woni-form', 'woni-close']) nodes[id] = element();
  nodes['woni-form'].submit = element();
  const storage = new Map();
  const context = { document: { getElementById: id => nodes[id], createElement: element }, window: {},
    crypto: { randomUUID: () => 'request-123456' }, sessionStorage: {
      getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key) },
    setTimeout: () => 1, clearTimeout: () => {} };
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
