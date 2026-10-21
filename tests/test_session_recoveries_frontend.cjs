"use strict";
// Production frontend with the existing inert VM fixture. No browser, SSH or native CLI.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs"), path = require("node:path"), vm = require("node:vm");
const fixturePath = path.join(__dirname, "test_frontend.cjs");
const fixture = fs.readFileSync(fixturePath, "utf8");
const boundary = fixture.indexOf('test("routing uses runtime identity;');
assert.ok(boundary > 0);
const sandbox = { require: require("node:module").createRequire(fixturePath), __dirname,
  console, Buffer, URL, URLSearchParams, Uint8Array, TextEncoder, AbortController, queueMicrotask, setTimeout, clearTimeout };
vm.createContext(sandbox);
vm.runInContext(fixture.slice(0, boundary) + "\nglobalThis.fixtureClient = client;", sandbox);
const NOW = 2000000;
const ref = (extra = {}) => ({ host: "fixture-pc", agent: "codex", home: ".codex", id: "same-thread",
  cwd: "C:/Projects/fixture", title: "이전 작업", hostLabel: "작업 PC", lastSeenAt: NOW - 60, closedReason: "released", ...extra });
const row = (extra = {}) => ({ ...ref(), phase: "unknown", runtimeKnown: false, source: "cli", updatedAt: NOW - 86400, ...extra });
const response = (data) => ({ ok: true, status: 200, json: async () => data });
function client(fetchImpl = async () => { throw new Error("Unexpected request"); }) {
  const calls = [];
  const c = sandbox.fixtureClient((url, options) => { calls.push({ url, method: options.method || "GET", body: options.body }); return fetchImpl(url, options); },
    { setTimeout: () => 1, clearTimeout() {} });
  Object.assign(c, vm.runInContext("({renderRecoveries,openRecovery,recentRecoveries,recoveryIdentity,sameRecoverySource,recoveryForTerminal,sessionRecentAt})", c.context));
  c.state.authenticated = true;
  c.state.snapshot = { serverTime: NOW, hosts: [{ name: "fixture-pc", label: "작업 PC", ok: true, data: { codex: [], claude: [] } }], recoverySessions: [ref()] };
  // Keep the actual select/prepareSessionListFor logic, with presentation and reads inert.
  c.context.render = () => {};
  c.context.setDetailShown = () => {};
  return { ...c, calls };
}
test("reload 뒤 snapshot 참조만으로 최근 연결을 보이며 모든 종료 터미널 목록을 펼치지 않는다", () => {
  const c = client();
  c.state.terminals = [{ id: "a".repeat(32), alive: false, closed: true, closedReason: "released", nativeSource: ref() }];
  c.renderTerminals(); c.renderRecoveries();
  assert.equal(c.get("running-terminals").hidden, true);
  assert.equal(c.get("session-recoveries").hidden, false);
  assert.equal(c.get("session-recovery-list").children.length, 1);
  assert.match(c.get("session-recovery-list").children[0].recoveryMeta.textContent, /작업 PC.*Codex.*터미널 연결 정리/);
  assert.equal(c.calls.length, 0);
});
test("이미 목록에 있거나 정확한 터미널이 살아 있는 작업은 복귀 안내에 중복 표시하지 않는다", () => {
  const c = client();
  c.state.snapshot.hosts[0].data.codex = [row({ phase: 'working' })];
  c.renderRecoveries();
  assert.equal(c.get('session-recoveries').hidden, true);
  c.state.snapshot.hosts[0].data.codex = [];
  c.state.terminals = [{ alive: true, nativeSource: ref(), linkConfidence: 'exact' }];
  c.renderRecoveries(); assert.equal(c.get('session-recoveries').hidden, true);
  c.state.terminals[0].alive = false;
  c.renderRecoveries(); assert.equal(c.get('session-recoveries').hidden, false);
  assert.equal(c.get('session-recovery-list').children.length, 1);
  assert.equal(c.get('session-recovery-list').children[0].children.at(-1).textContent, '대화 열기 ›');
  assert.equal(c.recentRecoveries().length, 1, '표시를 숨겨도 원래 복귀 참조는 보존한다');
  assert.equal(c.calls.length, 0);
});
test("최근 참조는 최대 20개이며 명시 종료·제거·중복·계정 홈 누락을 노출하지 않는다", () => {
  const c = client();
  c.state.snapshot.recoverySessions = [ref({ closedReason: "user" }), ref({ removedAt: NOW }),
    ref({ closedByBoard: true }), ref({ home: "" }), ...Array.from({ length: 25 }, (_, i) => ref({ id: `session-${i}` })), ref({ id: "session-0" })];
  c.renderRecoveries();
  assert.equal(c.recentRecoveries().length, 20);
  assert.equal(c.get("session-recovery-list").children.length, 20);
  assert.equal(c.recentRecoveries()[0].id, "session-0");
});
test("목록에 있는 같은 세션만 선택하며 다른 계정·기기로 연결하지 않고 입력·실행을 만들지 않는다", async () => {
  const c = client(async () => response({}));
  c.state.snapshot.hosts[0].data.codex = [row(), row({ id: "other", home: ".codex-other" })];
  c.state.search = "다른 검색"; c.state.filters.host = "other-host"; c.state.phaseFilter = "working";
  await c.openRecovery(ref());
  assert.equal(c.state.selected, "fixture-pc|codex|.codex|same-thread");
  assert.equal(c.state.filters.host, ""); assert.equal(c.state.search, ""); assert.equal(c.state.phaseFilter, "");
  assert.ok(c.calls.every((call) => call.url === "/api/session/opened"));
  assert.equal(c.state.nativeSessions.size, 0);
});
test("목록 밖 복귀는 exact GET 하나 뒤 같은 native ID를 열고 새 세션·launch·send를 호출하지 않는다", async () => {
  const c = client(async (url) => response(url.startsWith("/api/session/recover?") ? { session: row() } : {}));
  await c.openRecovery(ref());
  const recover = c.calls.filter((call) => call.url.startsWith("/api/session/recover?"));
  assert.equal(recover.length, 1); assert.equal(recover[0].method, "GET");
  const query = new URL(recover[0].url, "https://fixture.invalid").searchParams;
  assert.equal(query.get("host"), "fixture-pc"); assert.equal(query.get("home"), ".codex"); assert.equal(query.get("id"), "same-thread");
  assert.equal(c.state.selected, "fixture-pc|codex|.codex|same-thread");
  const recovered = c.allSessions()[0]; assert.equal(recovered.recoverySource, true); assert.equal(recovered.nativeSource, false);
  assert.ok(c.calls.every((call) => call.url.startsWith("/api/session/recover?") || call.url === "/api/session/opened"));
});
test("resolver가 다른 home·host·id 또는 Claude 대화 ID를 반환하면 선택하지 않는다", async () => {
  for (const changed of [{ home: ".codex-other" }, { host: "other-pc" }, { id: "other-thread" }]) {
    const c = client(async () => response({ session: row(changed) }));
    await c.openRecovery(ref()); assert.equal(c.state.selected, null); assert.equal(c.state.nativeSessions.size, 0);
    assert.match(c.state.recoveryError, /식별자/);
  }
  const saved = ref({ agent: "claude", home: undefined, sessionId: "original-conversation" });
  const c = client(async () => response({ session: row({ ...saved, sessionId: "different-conversation" }) }));
  c.state.snapshot.recoverySessions = [saved]; await c.openRecovery(saved);
  assert.equal(c.state.selected, null); assert.equal(c.state.nativeSessions.size, 0);
});
test("Claude 기본 home 호환은 기존 listed 행의 실제 source를 유지하며 다른 home은 거부한다", async () => {
  const c = client(async () => response({}));
  const saved = ref({ agent: "claude", home: ".claude", sessionId: "conversation-one" });
  c.state.snapshot.recoverySessions = [saved];
  c.state.snapshot.hosts[0].data.claude = [row({ ...saved, home: undefined })];
  await c.openRecovery(saved);
  assert.equal(c.state.selected, "fixture-pc|claude||same-thread");
  assert.equal(c.calls.some((call) => call.url.startsWith("/api/session/recover?")), false);
  assert.equal(c.recoveryIdentity({ ...saved, home: ".claude-other" }), null);
});
test("선택 전환·인증 세대 변경·참조 제거 뒤 늦은 resolver 응답은 대화를 바꾸지 않는다", async () => {
  for (const steer of [
    (c) => c.select("another-session"),
    (c) => { c.state.selectedBotRequest = "another-bot-conversation"; },
    (c) => c.beginAuthEpoch(),
    (c) => { c.state.snapshot.recoverySessions = []; },
  ]) {
    let finish;
    const c = client(() => new Promise((resolve) => { finish = resolve; }));
    const request = c.openRecovery(ref()); steer(c); finish(response({ session: row() })); await request;
    assert.equal(c.state.nativeSessions.size, 0); assert.notEqual(c.state.selected, "fixture-pc|codex|.codex|same-thread");
  }
});
test("중복된 listed identity는 첫 행으로 추측 선택하거나 resolver로 우회하지 않는다", async () => {
  const c = client(); c.state.snapshot.hosts[0].data.codex = [row(), row()];
  await c.openRecovery(ref());
  assert.equal(c.state.selected, null); assert.equal(c.calls.length, 0);
  assert.match(c.state.recoveryError, /여러 개/);
});
test("종료 화면도 저장된 참조로 복귀할 수 있지만 명시 user 종료는 자동 노출하지 않는다", () => {
  const c = client();
  c.state.terminal = { id: "a".repeat(32), alive: false, closed: true, closedReason: "released", nativeSource: ref() };
  assert.equal(c.sessionForTerminal(c.state.terminal), null);
  c.showTerminalEnded(); assert.equal(c.get("terminal-reopen").hidden, false);
  c.state.terminal.closedReason = "user";
  c.showTerminalEnded(); assert.equal(c.get("terminal-reopen").hidden, true);
});
test("방금 열어 본 오래된 대화는 age 필터에 남고 명시 tidy·remove의 의미는 보존한다", () => {
  const c = client();
  c.state.snapshot.hosts[0].data.codex = [row({ updatedAt: NOW - 4 * 86400, openedAt: NOW })];
  let s = c.allSessions()[0]; assert.equal(c.matches(s, NOW), true);
  c.state.snapshot.hosts[0].data.codex[0].tidiedAt = NOW;
  s = c.allSessions()[0]; assert.equal(c.matches(s, NOW), false);
  c.state.showOld = true; assert.equal(c.matches(s, NOW), true);
  c.state.snapshot.hosts[0].data.codex[0].removedAt = NOW;
  s = c.allSessions()[0]; assert.equal(c.matches(s, NOW), false);
  c.state.showRemoved = true; assert.equal(c.matches(s, NOW), true);
});
