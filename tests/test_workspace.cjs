"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const W = require("../web/workspace.js");
const source = fs.readFileSync(require.resolve("../web/workspace.js"), "utf8");
const plain = (value) => JSON.parse(JSON.stringify(value));
const combo = (key, extra = {}) => ({ key, code: "", ctrlKey: true, metaKey: true, altKey: false, shiftKey: false,
  preventDefault() { this.prevented = true; }, stopImmediatePropagation() { this.stopped = true; }, ...extra });
function four() {
  return W.split(W.split(W.split(W.leaf("p1"), "p1", "p2", "right"), "p1", "p3", "bottom"), "p2", "p4", "bottom");
}

test("four pane layout covers the area once and all drag arrangements preserve every pane", () => {
  const tree = four(), all = W.ids(tree).sort();
  assert.equal(W.validTree(tree), true);
  const rects = Object.values(W.geometry(tree).panes);
  assert.equal(rects.reduce((sum, box) => sum + box.w * box.h, 0), 1);
  for (let i = 0; i < rects.length; i++) for (let j = i + 1; j < rects.length; j++) {
    const a = rects[i], b = rects[j];
    assert.ok(Math.min(a.x + a.w, b.x + b.w) <= Math.max(a.x, b.x) || Math.min(a.y + a.h, b.y + b.h) <= Math.max(a.y, b.y));
  }
  for (const from of all) for (const to of all) for (const edge of ["left", "right", "top", "bottom", "center"]) {
    const moved = W.move(tree, from, to, edge);
    assert.equal(W.validTree(moved), true);
    assert.deepEqual(W.ids(moved).sort(), all);
    assert.equal(Object.values(W.geometry(moved).panes).reduce((sum, box) => sum + box.w * box.h, 0), 1);
  }
  assert.deepEqual(W.split(tree, "p1", "p5"), tree);
  assert.deepEqual(W.split(tree, "p1", "p2"), tree);
  assert.deepEqual(W.move(tree, "p1", "p2", "invalid"), tree);
});

test("closing collapses the affected split, ratios are bounded, and geometric navigation follows neighbors", () => {
  const tree = four();
  assert.equal(W.neighbor(tree, "p1", "ArrowRight"), "p2");
  assert.equal(W.neighbor(tree, "p1", "ArrowDown"), "p3");
  assert.equal(W.neighbor(tree, "p4", "ArrowUp"), "p2");
  assert.equal(W.neighbor(tree, "p4", "ArrowLeft"), "p3");
  assert.equal(W.neighbor(tree, "p1", "ArrowLeft"), null);
  const three = W.remove(tree, "p1");
  assert.equal(W.validTree(three), true);
  assert.deepEqual(W.ids(three).sort(), ["p2", "p3", "p4"]);
  assert.equal(W.ratioAt(tree, "", -100).ratio, 0.15);
  assert.equal(W.ratioAt(tree, "", 100).ratio, 0.85);
  assert.equal(W.ratioAt(tree, "0", 0.65).children[0].ratio, 0.65);
});

test("saved layout accepts only four unique pane references, never saved draft/auth/commands", () => {
  const value = { version: 1, tree: four(), active: "foreign", panes: {
    p1: { key: "session-a", mode: "chat", draft: "private", token: "secret", command: "run" },
    p2: { terminalId: "a".repeat(32), mode: "terminal", title: "private title" },
  }, transcript: "private transcript" };
  const restored = W.restoreLayout(value);
  assert.equal(restored.active, "p1");
  assert.deepEqual(restored.panes.p1, { key: "session-a", mode: "chat" });
  assert.doesNotMatch(JSON.stringify(restored), /private|secret|command|transcript|token/);
  assert.equal(W.restoreLayout({ ...value, panes: { p1: { key: "same" }, p2: { key: "same" } } }), null);
  assert.equal(W.restoreLayout({ ...value, tree: { axis: "x", ratio: 0.5, children: [W.leaf("p1"), W.leaf("p1")] } }), null);
  assert.equal(W.restoreLayout({ ...value, tree: { axis: "x", ratio: 0, children: [W.leaf("p1"), W.leaf("p2")] } }), null);
  assert.equal(W.restoreLayout({ ...value, tree: W.leaf("__proto__") }), null);
  assert.equal(W.validRef({ key: "bad\nkey", terminalId: "invalid" }), null);
});

test("Mac and iPad use the same Control+Command keys without taking IME, send, or Option-left", () => {
  for (const key of ["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"]) assert.deepEqual(W.shortcut(combo(key)), { direction: key });
  assert.deepEqual(W.shortcut(combo("Enter")), { maximize: true });
  assert.deepEqual(W.shortcut(combo("ㅂ", { code: "Digit3" })), { number: 3 });
  for (const change of [{ ctrlKey: false }, { metaKey: false }, { altKey: true }, { shiftKey: true }, { isComposing: true }, { keyCode: 229 }])
    assert.equal(W.shortcut(combo("ArrowLeft", change)), null);
  assert.equal(W.shortcut(combo("ArrowLeft", { ctrlKey: false, metaKey: false, altKey: true })), null);
  assert.deepEqual(W.shortcut(combo("ArrowLeft", { altKey: true }), "ctrl-alt-meta"), { direction: "ArrowLeft" });
  assert.equal(W.shortcut(combo("ArrowLeft"), "ctrl-alt-meta"), null);
});

// A small DOM fixture tests lifetime and cross-document boundaries. It does not
// pretend to test Safari hardware keys or microphone capture.
function fixture(saved = null) {
  const nodes = new Map(), calls = [], preferences = new Map(saved ? [["workspace", saved]] : []), frames = [];
  class Node {
    constructor(tag = "div") {
      this.tagName = tag.toUpperCase(); this.children = []; this.style = {}; this.dataset = {}; this.events = new Map();
      this.attrs = {}; this.hidden = false; this.className = ""; this.parentNode = null; this.focusCount = 0; this.appendCount = 0;
      this.classList = { contains: (key) => this.className.split(" ").includes(key),
        add: (...keys) => { this.className = [...new Set([...this.className.split(" ").filter(Boolean), ...keys])].join(" "); },
        remove: (...keys) => { this.className = this.className.split(" ").filter((key) => !keys.includes(key)).join(" "); },
        toggle: (key, force) => { const add = force ?? !this.classList.contains(key); this.classList[add ? "add" : "remove"](key); return add; } };
      if (tag === "iframe") { this.contentWindow = { focus: () => this.focusCount++ }; frames.push(this); }
    }
    append(...children) { for (const child of children) { child.remove(); child.parentNode = this; child.appendCount++; this.children.push(child); } }
    prepend(child) { child.remove(); child.parentNode = this; child.appendCount++; this.children.unshift(child); }
    replaceChildren(...children) { for (const child of this.children) child.parentNode = null; this.children = []; this.append(...children); }
    remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter((node) => node !== this); this.parentNode = null; }
    setAttribute(key, value) { this.attrs[key] = String(value); }
    addEventListener(key, fn) { if (!this.events.has(key)) this.events.set(key, []); this.events.get(key).push(fn); }
    removeEventListener(key, fn) { this.events.set(key, (this.events.get(key) || []).filter((item) => item !== fn)); }
    fire(key, event) { for (const fn of this.events.get(key) || []) fn(event); }
    focus() { this.focusCount++; }
    getBoundingClientRect() {
      const left = 240 + parseFloat(this.style.left || "0") * 10, top = 52 + parseFloat(this.style.top || "0") * 7;
      const width = parseFloat(this.style.width || "100") * 10, height = parseFloat(this.style.height || "100") * 7;
      return { left, top, width, height, right: left + width, bottom: top + height };
    }
  }
  for (const id of ["session-workspace", "workspace-primary", "workspace-dividers", "workspace-drop-cue", "workspace-switcher", "workspace-arrange", "workspace-split-open", "groups", "home", "workspace-keys"])
    nodes.set(id, new Node());
  nodes.get("workspace-primary").dataset.paneId = "p1";
  nodes.get("session-workspace").append(nodes.get("workspace-primary"), nodes.get("workspace-dividers"), nodes.get("workspace-drop-cue"));
  const document = new Node(); document.body = new Node(); document.createElement = (tag) => new Node(tag);
  document.getElementById = (id) => nodes.get(id); document.querySelector = () => null;
  const window = { document, location: { pathname: "/", search: "", origin: "https://board.example" }, innerWidth: 1440,
    addEventListener: (key, fn) => document.addEventListener("window:" + key, fn), requestAnimationFrame: () => {},
    setTimeout: () => 1 };
  const prefs = { get: (key, fallback) => preferences.get(key) ?? fallback, set: (key, value) => preferences.set(key, plain(value)) };
  const infos = new Map();
  function app(id, ref = null) {
    const editor = { text: "초안 " + id, caret: 3, recognition: {}, terminal: {} };
    const info = { authenticated: true, snapshot: { version: 1 }, ref, composing: false, title: id, dictation: false, editor, canToggleView: true };
    infos.set(id, info);
    return { prefs, describe: () => info, api: async (path) => { calls.push(["api", path]); return { path }; },
      toast: (message) => calls.push(["toast", message]), select: (key) => { info.ref = { key, mode: "chat" }; calls.push(["select", id, key]); },
      sessionExists: (key) => ["session-a", "session-b", "session-c"].includes(key),
      restore: (value) => { info.ref = plain(value); calls.push(["restore", id, value]); return true; },
      focus: () => calls.push(["focus", id]), resize: () => calls.push(["resize", id]),
      canClose: () => !info.busy, prepareClose: () => calls.push(["close-view", id]),
      visibility: (value) => calls.push(["visibility", id, value]), refresh: (kind) => calls.push(["refresh", id, kind]),
      locked: () => { info.authenticated = false; calls.push(["locked", id]); }, newSession: () => calls.push(["new", id]), unsaved: () => !!info.unsaved,
      browseSessions: () => calls.push(["browse", id]),
      toggleView: () => calls.push(["view", id]),
      sessionKeyForTerminal: (terminal) => terminal.key || null, openTerminal: (terminal) => calls.push(["open-existing-terminal", id, terminal.id]) };
  }
  vm.runInNewContext(source, { window, URLSearchParams, console, Set, Map, Date, Promise });
  const workspace = window.SessionholicWorkspace, primary = app("p1", { key: "session-a", mode: "chat" });
  workspace.init(primary); workspace.changed();
  function attach(id, ref = null) {
    const row = nodes.get("session-workspace").children.find((node) => node.dataset.paneId === id);
    const frame = row.children.find((node) => node.tagName === "IFRAME");
    const child = app(id, ref);
    assert.equal(workspace.attach(id, frame.contentWindow, child), true);
    return { row, frame, child, info: infos.get(id) };
  }
  return { workspace, nodes, calls, preferences, frames, document, window, app, primary, infos, attach };
}

test("split, focus, maximize and resize retain editor/recognition/frame identity and do not invoke execution APIs", () => {
  const f = fixture(), editor = f.infos.get("p1").editor;
  assert.equal(f.nodes.get("workspace-primary").children[0].hidden, true, "single-pane chrome remains unchanged");
  f.workspace.add({ key: "session-b", mode: "chat" });
  const second = f.attach("p2"), frameAppends = second.frame.appendCount, editor2 = second.info.editor;
  assert.equal(second.frame.src, "/pane?pane=p2");
  for (const key of ["ArrowLeft", "ArrowRight", "Enter", "Enter", "1", "2"]) {
    const event = combo(key); assert.equal(f.workspace.handleKey(event), true); assert.ok(event.prevented && event.stopped);
  }
  f.workspace.layout(); f.workspace.layout();
  assert.equal(second.frame.appendCount, frameAppends);
  assert.equal(f.infos.get("p1").editor, editor);
  assert.equal(second.info.editor, editor2);
  assert.equal(editor.text, "초안 p1"); assert.equal(editor.caret, 3);
  assert.equal(f.calls.filter(([name]) => name === "restore").length, 1);
  assert.equal(f.calls.filter(([name]) => name === "api").length, 0);
  assert.equal(f.frames.length, 1);
});

test("closing a pane only closes its view, guards pending sends, and closing primary retains the shared board", () => {
  const f = fixture(); f.workspace.add({ key: "session-b" }); const second = f.attach("p2");
  second.info.busy = true; f.workspace.closePane("p2"); assert.ok(second.row.parentNode);
  second.info.busy = false; f.workspace.closePane("p2"); assert.equal(second.row.parentNode, null);
  assert.ok(f.calls.some(([name, id]) => name === "close-view" && id === "p2"));
  assert.equal(f.workspace.visible("p1"), true);
  assert.equal(f.calls.filter(([name]) => name === "api").length, 0);
  f.workspace.add({ key: "session-c" }); f.attach("p2");
  f.workspace.closePane("p1");
  assert.equal(f.workspace.visible("p1"), false);
  assert.equal(f.workspace.visible("p2"), true);
  assert.equal(f.nodes.get("workspace-primary").parentNode, f.nodes.get("session-workspace"));
});

test("selection and new sessions target the active pane, duplicate sessions and terminals focus their existing pane", () => {
  const f = fixture(); f.workspace.add({ key: "session-b" }); const second = f.attach("p2");
  assert.equal(f.workspace.routeNew(), true); assert.ok(f.calls.some(([name, id]) => name === "new" && id === "p2"));
  assert.equal(f.workspace.routeSession("session-c"), true);
  assert.deepEqual(second.info.ref, { key: "session-c", mode: "chat" });
  assert.equal(f.workspace.add({ key: "session-c" }), true); assert.equal(f.frames.length, 1);
  second.info.terminalId = "a".repeat(32); second.info.terminalVisible = true;
  assert.equal(f.workspace.claimTerminal({ id: "a".repeat(32) }), false);
  assert.equal(f.calls.at(-2)?.[0] === "focus" || f.calls.some(([name, id]) => name === "focus" && id === "p2"), true);
  assert.equal(f.workspace.claim("p2", {}, "a".repeat(32)), false);
});

test("empty panes restore without starting sessions and browsing keeps the target pane with strict frame ownership", () => {
  const f = fixture();
  f.workspace.add(null);
  assert.equal(f.workspace.routeNew(), true);
  assert.equal(f.calls.filter(([name]) => name === "new").length, 0, "a loading pane never starts in the parent");
  const saved = f.preferences.get("workspace");
  assert.equal(saved.panes.p2, null);
  const restored = fixture(saved), second = restored.attach("p2"), editor = restored.infos.get("p1").editor;
  second.info.title = ""; restored.workspace.changed();
  assert.equal(second.row.children[0].children[0].children[0].textContent, "2 · 빈 창");
  assert.equal(restored.calls.some(([name, id]) => name === "new" || name === "api" || name === "restore" && id === "p2"), false);
  restored.workspace.focus("p1", false);
  assert.equal(restored.workspace.browseSessions("p2", {}), false, "foreign windows cannot choose a target");
  assert.equal(restored.workspace.browseSessions("p2", second.frame.contentWindow), true);
  assert.equal(restored.preferences.get("workspace").active, "p2");
  assert.equal(restored.document.body.classList.contains("workspace-list-open"), true);
  assert.ok(restored.calls.some(([name, id]) => name === "browse" && id === "p1"));
  assert.equal(restored.workspace.routeSession("session-c"), true);
  assert.deepEqual(second.info.ref, { key: "session-c", mode: "chat" });
  assert.equal(second.frame.appendCount, 1); assert.equal(restored.infos.get("p1").editor, editor);
  restored.infos.get("p1").authenticated = false;
  assert.equal(restored.workspace.browseSessions("p2", second.frame.contentWindow), false);
  assert.equal(restored.calls.some(([name]) => name === "new" || name === "api"), false);
});

test("IME blocks pane changes and ongoing voice remains bound to its original pane", () => {
  const f = fixture(); f.workspace.add({ key: "session-b" }); const second = f.attach("p2");
  second.info.composing = true;
  assert.equal(f.workspace.focus("p1"), false);
  assert.equal(f.preferences.get("workspace").active, "p2");
  second.info.composing = false; second.info.dictation = true;
  assert.equal(f.workspace.focus("p1"), true);
  assert.equal(second.info.dictation, true);
  assert.equal(f.workspace.claimDictation(), false);
  assert.equal(f.calls.filter(([name]) => name === "close-view").length, 0);
  second.info.dictation = false; assert.equal(f.workspace.claimDictation(), true);
});

test("each pane's expand/restore button preserves every frame/editor, including after reload", () => {
  const f = fixture(); f.workspace.add({ key: "session-b" }); const second = f.attach("p2");
  const toggle = second.row.children[0].children[1], editor = second.info.editor;
  assert.equal(toggle.textContent, "이 창 확대");
  toggle.fire("click", {});
  assert.equal(toggle.textContent, "분할로 돌아가기");
  assert.equal(f.nodes.get("workspace-primary").classList.contains("workspace-pane-covered"), true);
  assert.equal(second.row.classList.contains("workspace-pane-covered"), false);
  assert.equal(second.frame.appendCount, 1);
  assert.equal(second.info.editor, editor);
  assert.equal(f.preferences.get("workspace").single, true);
  const restored = fixture(f.preferences.get("workspace"));
  assert.equal(restored.attach("p2").row.children[0].children[1].textContent, "분할로 돌아가기");
  toggle.fire("click", {});
  assert.equal(toggle.textContent, "이 창 확대");
  assert.equal(f.nodes.get("workspace-primary").classList.contains("workspace-pane-covered"), false);
  assert.equal(f.calls.filter(([name]) => name === "api" || name === "close-view").length, 0);
});

test("pane focus waits for Samsung IME blur without replacing the original editor or sending", async () => {
  const f = fixture(); f.workspace.add({ key: "session-b" }); const second = f.attach("p2");
  second.info.composing = true;
  let commit; second.child.commitComposition = () => new Promise((done) => { commit = done; });
  assert.equal(f.workspace.focus("p1"), false);
  assert.equal(f.preferences.get("workspace").active, "p2");
  second.info.composing = false; commit(true); await Promise.resolve();
  assert.equal(f.preferences.get("workspace").active, "p1");
  assert.equal(second.info.editor.text, "초안 p2");
  assert.equal(f.calls.filter(([name]) => name === "api" || name === "close-view").length, 0);
});

test("pane headers show their own title and route, and expanding an inactive pane targets that document", () => {
  const f = fixture(); f.workspace.add({ key: "session-b" }); const second = f.attach("p2");
  second.info.title = "입력창 개선"; second.info.route = "Mac mini · Codex"; f.workspace.changed();
  const header = second.row.children[0], title = header.children[0], expand = header.children[1];
  assert.equal(title.children[0].textContent, "2 · 입력창 개선");
  assert.equal(title.children[1].textContent, "Mac mini · Codex");
  assert.match(title.attrs["aria-label"], /Option\+2/);
  assert.equal(header.children.length, 3, "title, pane view and view close have distinct roles");
  f.workspace.focus("p1", false); const editor = second.info.editor;
  expand.fire("click", {});
  assert.equal(f.preferences.get("workspace").active, "p2");
  assert.equal(f.preferences.get("workspace").single, true);
  assert.equal(second.row.classList.contains("workspace-pane-covered"), false);
  assert.equal(expand.attrs["aria-label"], "분할로 돌아가기");
  expand.fire("click", {});
  assert.equal(f.nodes.get("workspace-primary").classList.contains("workspace-pane-covered"), false);
  assert.equal(second.frame.appendCount, 1); assert.equal(second.info.editor, editor);
  assert.equal(f.calls.filter(([name]) => name === "api" || name === "close-view").length, 0);
});

test("snapshot reads reuse parent data and concurrent common reads share one request with strict frame ownership", async () => {
  const f = fixture(); f.workspace.add({ key: "session-b" }); const second = f.attach("p2");
  const win = second.frame.contentWindow;
  assert.deepEqual(await f.workspace.readShared("p2", win, "/api/snapshot?refresh=1"), { version: 1 });
  assert.equal(f.calls.filter(([name]) => name === "api").length, 0);
  await Promise.all([f.workspace.readShared("p2", win, "/api/terminals"), f.workspace.readShared("p2", win, "/api/terminals")]);
  assert.equal(f.calls.filter(([name, path]) => name === "api" && path === "/api/terminals").length, 1);
  await assert.rejects(f.workspace.readShared("p2", {}, "/api/snapshot"));
  await assert.rejects(f.workspace.readShared("p2", win, "/api/terminal/secret/input"));
  assert.equal(f.workspace.attach("p2", {}, second.child), false);
  f.workspace.dataChanged("terminals", { terminals: [{ id: "a".repeat(32) }] });
  assert.equal((await f.workspace.readShared("p2", win, "/api/terminals")).terminals.length, 1);
  assert.ok(f.calls.some(([name, id, kind]) => name === "refresh" && id === "p2" && kind === "terminals"));
});

test("restoring layout never creates executions and authentication lock clears every frame", () => {
  const saved = { version: 1, tree: W.split(W.leaf("p1"), "p1", "p2"), active: "p2",
    panes: { p1: { key: "session-a" }, p2: { key: "session-b", mode: "terminal", terminalId: "a".repeat(32) } } };
  const f = fixture(saved); f.attach("p2");
  assert.equal(f.frames.length, 1); assert.equal(f.calls.filter(([name]) => name === "api").length, 0);
  assert.doesNotMatch(JSON.stringify(f.preferences.get("workspace")), /초안|editor|recognition/);
  f.workspace.locked();
  assert.equal(f.nodes.get("session-workspace").children.some((node) => node.dataset.paneId === "p2"), false);
  assert.ok(f.calls.some(([name, id]) => name === "locked" && id === "p2"));
  assert.equal(f.workspace.managed(), false);
});

test("configured alternate modifiers are respected in all panes and shortcuts do not fire through a dialog", () => {
  const f = fixture(); f.workspace.add(null); f.attach("p2");
  f.preferences.set("workspaceKeys", "ctrl-alt-meta");
  assert.equal(f.workspace.handleKey(combo("ArrowLeft")), false);
  assert.equal(f.workspace.handleKey(combo("ArrowLeft", { altKey: true })), true);
  f.infos.get("p1").dialog = true;
  assert.equal(f.workspace.handleKey(combo("Enter", { altKey: true }), "p1"), false);
});

test("dragging pane headers reorders existing documents and resizing only changes coordinates", () => {
  const f = fixture(); f.workspace.add({ key: "session-b" }); const second = f.attach("p2");
  const appends = second.frame.appendCount, editor = second.info.editor;
  const data = new Map(), transfer = { setData: (key, value) => data.set(key, value), getData: (key) => data.get(key) };
  second.row.children[0].children[0].fire("dragstart", { dataTransfer: transfer });
  f.document.fire("drop", { clientX: 250, clientY: 400, dataTransfer: transfer, preventDefault() {} });
  assert.equal(f.preferences.get("workspace").tree.children[0].pane, "p2");
  assert.equal(second.frame.appendCount, appends);
  const divider = f.nodes.get("workspace-dividers").children[0];
  divider.fire("pointerdown", { button: 0, pointerId: 1, preventDefault() {} });
  divider.fire("pointermove", { clientX: 940, clientY: 400 });
  divider.fire("pointerup", {});
  assert.equal(f.preferences.get("workspace").tree.ratio, 0.7);
  assert.equal(second.frame.appendCount, appends);
  assert.equal(second.info.editor, editor);
  assert.equal(f.calls.filter(([name]) => name === "restore").length, 1);
});

test("list drops create a new view only for known sessions and duplicate drops move the existing view", () => {
  const f = fixture();
  const transfer = { setData() {}, getData() {}, types: [] };
  const card = { dataset: { sessionKey: "session-b" } };
  const drag = () => f.nodes.get("groups").fire("dragstart", { target: { closest: () => card }, dataTransfer: transfer });
  drag(); f.document.fire("drop", { clientX: 1230, clientY: 400, dataTransfer: transfer, preventDefault() {} });
  const second = f.attach("p2"), appends = second.frame.appendCount;
  drag(); f.document.fire("drop", { clientX: 250, clientY: 400, dataTransfer: transfer, preventDefault() {} });
  assert.equal(f.frames.length, 1); assert.equal(second.frame.appendCount, appends);
  card.dataset.sessionKey = "foreign-session";
  drag(); assert.equal(f.frames.length, 1);
});

test("dropping another session at an occupied pane center previews and creates a split without replacing the conversation", () => {
  const f = fixture();
  const transfer = { setData() {}, getData() {}, types: [] };
  const card = { dataset: { sessionKey: "session-b" } };
  f.nodes.get("groups").fire("dragstart", { target: { closest: () => card }, dataTransfer: transfer });
  f.document.fire("dragover", { clientX: 740, clientY: 400, dataTransfer: transfer, preventDefault() {} });
  assert.equal(f.nodes.get("workspace-drop-cue").textContent, "여기에 분할");
  f.document.fire("drop", { clientX: 740, clientY: 400, dataTransfer: transfer, preventDefault() {} });
  assert.equal(f.frames.length, 1);
  assert.deepEqual(f.infos.get("p1").ref, { key: "session-a", mode: "chat" });
  const second = f.attach("p2");
  assert.deepEqual(second.info.ref, { key: "session-b", mode: "terminal" });
  assert.equal(f.nodes.get("workspace-primary").style.width, "50%");
  assert.equal(second.row.style.width, "50%");
});

test("a center drop into a loading empty pane is retained until its document is ready", () => {
  const f = fixture(); f.workspace.add(null);
  const transfer = { setData() {}, types: [] };
  const card = { dataset: { sessionKey: "session-b" } };
  f.nodes.get("groups").fire("dragstart", { target: { closest: () => card }, dataTransfer: transfer });
  f.document.fire("dragover", { clientX: 990, clientY: 400, dataTransfer: transfer, preventDefault() {} });
  assert.equal(f.nodes.get("workspace-drop-cue").textContent, "빈 창에서 열기");
  f.document.fire("drop", { clientX: 990, clientY: 400, dataTransfer: transfer, preventDefault() {} });
  assert.equal(f.frames.length, 1, "the empty pane is filled rather than split again");
  assert.deepEqual(f.preferences.get("workspace").panes.p2, { key: "session-b", mode: "terminal" });
  assert.deepEqual(f.attach("p2").info.ref, { key: "session-b", mode: "terminal" });
  assert.equal(f.calls.filter(([name]) => name === "api").length, 0);
});

test("dropping a session onto its own pane explains the unchanged layout", () => {
  const f = fixture(), transfer = { setData() {}, types: [] };
  const card = { dataset: { sessionKey: "session-a" } };
  f.nodes.get("groups").fire("dragstart", { target: { closest: () => card }, dataTransfer: transfer });
  f.document.fire("dragover", { clientX: 1230, clientY: 400, dataTransfer: transfer, preventDefault() {} });
  assert.match(f.nodes.get("workspace-drop-cue").textContent, /이미 이 창에/);
  f.document.fire("drop", { clientX: 1230, clientY: 400, dataTransfer: transfer, preventDefault() {} });
  assert.equal(f.frames.length, 0);
  assert.ok(f.calls.some(([name, message]) => name === "toast" && message.includes("이미 이 창에")));
  assert.deepEqual(f.infos.get("p1").ref, { key: "session-a", mode: "chat" });
});

test("a center drop of an already open session swaps existing panes and a full workspace explains its limit", () => {
  const f = fixture(); f.workspace.add({ key: "session-b" }); const second = f.attach("p2");
  const transfer = { setData() {}, types: [] }, card = { dataset: { sessionKey: "session-b" } };
  const start = () => f.nodes.get("groups").fire("dragstart", { target: { closest: () => card }, dataTransfer: transfer });
  const event = { clientX: 490, clientY: 400, dataTransfer: transfer, preventDefault() {} };
  start(); f.document.fire("dragover", event);
  assert.equal(f.nodes.get("workspace-drop-cue").textContent, "창 위치 교환");
  f.document.fire("drop", event);
  assert.equal(f.frames.length, 1); assert.equal(second.frame.appendCount, 1);
  assert.equal(f.preferences.get("workspace").tree.children[0].pane, "p2");
  f.workspace.add(null, "p1", "bottom"); f.workspace.add(null, "p2", "bottom");
  card.dataset.sessionKey = "session-c";
  const full = { ...event, clientX: 990, clientY: 227 };
  start(); f.document.fire("dragover", full);
  assert.match(f.nodes.get("workspace-drop-cue").textContent, /최대 4개/);
  f.document.fire("drop", full);
  assert.equal(f.frames.length, 3);
  assert.ok(f.calls.some(([name, message]) => name === "toast" && message.includes("최대 4개")));
  assert.deepEqual(f.infos.get("p1").ref, { key: "session-a", mode: "chat" });
});

test("terminal list entries target the focused pane, and unlinked terminals cannot cover the whole workspace", () => {
  const f = fixture(); f.workspace.add({ key: "session-b" }); f.attach("p2");
  assert.equal(f.workspace.routeTerminal({ id: "a".repeat(32), key: "session-b" }), true);
  assert.ok(f.calls.some(([name, id]) => name === "open-existing-terminal" && id === "p2"));
  f.workspace.focus("p1");
  assert.equal(f.workspace.routeTerminal({ id: "b".repeat(32), key: "session-a" }), false, "primary opens an exact match inline");
  assert.equal(f.workspace.routeTerminal({ id: "c".repeat(32) }), true);
  assert.equal(f.frames.length, 2, "standalone terminal stays in a new child document");
  assert.equal(f.preferences.get("workspace").panes.p3.terminalId, "c".repeat(32));
});

test("compact and maximized views preserve documents, passive panes remain unread, and reload checks all drafts", () => {
  const f = fixture(); f.workspace.add({ key: "session-b" }); const second = f.attach("p2");
  assert.equal(f.workspace.readingActive(), false);
  assert.equal(f.workspace.isActive("p2", second.frame.contentWindow), true);
  assert.equal(f.nodes.get("workspace-switcher").hidden, true, "desktop panes already show their numbers");
  f.window.innerWidth = 650; f.workspace.layout();
  assert.equal(f.nodes.get("workspace-switcher").hidden, false, "covered panes stay reachable on a narrow screen");
  assert.equal(f.nodes.get("workspace-primary").classList.contains("workspace-pane-covered"), true);
  f.workspace.handleKey(combo("1"));
  assert.equal(f.workspace.readingActive(), true);
  assert.equal(second.row.classList.contains("workspace-pane-covered"), true);
  assert.equal(second.frame.appendCount, 1);
  second.info.unsaved = true;
  const event = combo("unused"); f.document.fire("window:beforeunload", event);
  assert.equal(event.prevented, true);
  assert.equal(event.returnValue, "");
});

test('four repeated right splits retile into four visible panes before considering compact mode', () => {
  const f = fixture(), editors = [f.infos.get('p1').editor], children = [];
  for (const [id, key] of [['p2', 'session-b'], ['p3', 'session-c'], ['p4', null]]) {
    f.workspace.add(key ? { key } : null);
    const child = f.attach(id); children.push(child); editors.push(child.info.editor);
  }
  assert.equal(f.document.body.classList.contains('workspace-compact'), false);
  const saved = f.preferences.get('workspace'), geometry = f.workspace.geometry(saved.tree).panes;
  assert.equal(Object.keys(geometry).length, 4);
  for (const box of Object.values(geometry)) { assert.equal(box.w, .5); assert.equal(box.h, .5); }
  for (const child of children) {
    assert.equal(child.row.classList.contains('workspace-pane-covered'), false);
    assert.equal(child.frame.appendCount, 1);
  }
  assert.equal(f.nodes.get('workspace-arrange').hidden, false);
  f.workspace.handleKey(combo('Enter'));
  assert.equal(f.nodes.get('workspace-arrange').textContent, '분할로 돌아가기');
  f.nodes.get('workspace-arrange').fire('click', {});
  assert.equal(f.nodes.get('workspace-arrange').textContent, '분할 정렬');
  for (const [index, child] of children.entries()) {
    assert.equal(child.row.classList.contains('workspace-pane-covered'), false);
    assert.equal(child.info.editor, editors[index + 1]);
  }
  assert.equal(f.calls.some(([name]) => name === 'api' || name === 'close-view'), false);
});

test('restored narrow desktop splits recover while genuine small screens keep reachable tabs', () => {
  let tree = W.leaf('p1');
  for (let number = 2; number <= 4; number++) tree = W.split(tree, 'p' + (number - 1), 'p' + number, 'right');
  const f = fixture({ version: 1, tree, active: 'p4', panes: {} });
  assert.equal(f.document.body.classList.contains('workspace-compact'), false);
  f.window.innerWidth = 650; f.workspace.layout();
  assert.equal(f.document.body.classList.contains('workspace-compact'), true);
  assert.equal(f.nodes.get('workspace-switcher').hidden, false);
  assert.equal(f.nodes.get('workspace-arrange').hidden, true);
  f.window.innerWidth = 1440; f.workspace.layout();
  assert.equal(f.document.body.classList.contains('workspace-compact'), false);
});

test('passive terminal claims never move focus or notify when another pane owns the terminal', () => {
  const f = fixture(); f.workspace.add({ terminalId: 'a'.repeat(32), mode: 'terminal' });
  const second = f.attach('p2', { terminalId: 'a'.repeat(32), mode: 'terminal' });
  second.info.terminalId = 'a'.repeat(32); second.info.terminalVisible = true;
  f.workspace.focus('p1'); const before = f.calls.length;
  for (let index = 0; index < 3; index++) assert.equal(f.workspace.claimTerminal({ id: 'a'.repeat(32) }, { passive: true }), false);
  assert.equal(f.workspace.readingActive(), true); assert.equal(f.calls.length, before);
  assert.equal(f.workspace.claimTerminal({ id: 'a'.repeat(32) }), false);
  assert.equal(f.workspace.readingActive(), false); assert.ok(f.calls.some((call) => call[0] === 'toast'));
});

test('answer intent reaches an already mounted record pane without creating a new execution', () => {
  const f = fixture(); f.workspace.add({ key: 'session-b', mode: 'chat' });
  const second = f.attach('p2', { key: 'session-b', mode: 'chat' }), answers = [];
  second.child.answerSession = (key) => answers.push(key);
  f.workspace.focus('p1'); assert.equal(f.workspace.routeSession('session-b', { answer: true }), true);
  assert.deepEqual(answers, ['session-b']); assert.equal(f.workspace.readingActive(), false);
  assert.equal(f.calls.some((call) => call[0] === 'api' && /launch|new-session/.test(call[1])), false);
});

test('a loading pane retains answer intent until restore completes and opens in work mode', () => {
  const f = fixture(); f.workspace.add(null);
  assert.equal(f.workspace.routeSession('session-b', { answer: true }), true);
  assert.deepEqual(f.preferences.get('workspace').panes.p2, { key: 'session-b', mode: 'terminal' });
  const row = f.nodes.get('session-workspace').children.find((node) => node.dataset.paneId === 'p2');
  const frame = row.children.find((node) => node.tagName === 'IFRAME'), child = f.app('p2'), answers = [];
  child.answerSession = (key) => answers.push(key);
  assert.equal(f.workspace.attach('p2', frame.contentWindow, child), true);
  assert.deepEqual(answers, ['session-b']); assert.equal(f.infos.get('p2').ref.mode, 'terminal');
  f.workspace.changed(); assert.equal(answers.length, 1, 'answer intent is consumed only once');
});

test('browser-reserved Command+Option directions remain with the browser in every saved setting', () => {
  for (const saved of [undefined, 'ctrl-meta', 'ctrl-alt-meta']) {
    for (const [key, direction] of [['ArrowLeft', 'ArrowLeft'], ['ArrowRight', 'ArrowRight'], ['ArrowUp', 'ArrowUp'], ['ArrowDown', 'ArrowDown'],
      ['UIKeyInputLeftArrow', 'ArrowLeft'], ['UIKeyInputRightArrow', 'ArrowRight'], ['UIKeyInputUpArrow', 'ArrowUp'], ['UIKeyInputDownArrow', 'ArrowDown']]) {
      const event = combo(key, { ctrlKey: false, altKey: true });
      assert.equal(W.shortcut(event, saved), null);
    }
  }
  for (const extra of [{ isComposing: true }, { keyCode: 229 }, { shiftKey: true }, { metaKey: false }])
    assert.equal(W.shortcut(combo('ArrowLeft', { ctrlKey: false, altKey: true, ...extra })), null);
  for (const key of ['Enter', '1', 'a']) assert.equal(W.shortcut(combo(key, { ctrlKey: false, altKey: true })), null);
});

test('pane movement starts from the document that received the key even before its focus event arrives', () => {
  const f = fixture(); f.workspace.add({ key: 'session-b' }); f.attach('p2');
  f.workspace.focus('p1', false); f.calls.length = 0;
  assert.equal(f.workspace.handleKey(combo('ArrowLeft'), 'p2'), true);
  assert.ok(f.calls.some(([name, id]) => name === 'focus' && id === 'p1'));
  assert.equal(f.workspace.handleKey(combo('ArrowRight'), 'removed-pane'), false);
});

test('embedded textarea and xterm Option number keys are captured at the window before CLI input', () => {
  const f = fixture(); f.workspace.add({ key: 'session-b' }); const second = f.attach('p2');
  const listeners = new Map(), childDocument = { body: { classList: { add() {} } },
    addEventListener(name, fn, capture) { listeners.set(name, { fn, capture }); } };
  const childWindow = second.frame.contentWindow;
  Object.assign(childWindow, { addEventListener(name, fn, capture) { listeners.set(name, { fn, capture }); }, document: childDocument, location: { pathname: '/pane', search: '?pane=p2', origin: f.window.location.origin }, parent: f.window });
  vm.runInNewContext(source, { window: childWindow, URLSearchParams, console, Set, Map, Date, Promise });
  childWindow.SessionholicWorkspace.init(second.child);
  assert.equal(listeners.get('keydown').capture, true);
  for (const target of [{ tagName: 'TEXTAREA', id: 'chat-input' }, { className: 'xterm-helper-textarea', id: 'xterm' }]) {
    f.workspace.focus('p1', false); f.calls.length = 0;
    const event = combo('1', { ctrlKey: false, metaKey: false, altKey: true, code: 'Digit1', target });
    listeners.get('keydown').fn(event);
    assert.ok(event.prevented && event.stopped);
    assert.ok(f.calls.some(([name, id]) => name === 'focus' && id === 'p1'));
    assert.equal(f.calls.some(([name]) => name === 'api'), false);
  }
  second.info.dialog = true;
  const event = combo('1', { ctrlKey: false, metaKey: false, altKey: true, code: 'Digit1' }); listeners.get('keydown').fn(event);
  assert.equal(event.prevented, undefined);
  const single = fixture(), singleKey = combo('1', { ctrlKey: false, metaKey: false, altKey: true, code: 'Digit1' });
  assert.equal(single.workspace.handleKey(singleKey), false); assert.equal(singleKey.prevented, undefined);
});

test('an embedded session menu delegates new views to its own pane and rejects foreign callers without executing sessions', () => {
  const f = fixture(); f.workspace.add({ key: 'session-b' }); const second = f.attach('p2');
  const childWindow = second.frame.contentWindow;
  Object.assign(childWindow, { addEventListener() {}, document: { body: { classList: { add() {} } }, addEventListener() {} },
    location: { pathname: '/pane', search: '?pane=p2', origin: f.window.location.origin }, parent: f.window });
  vm.runInNewContext(source, { window: childWindow, URLSearchParams, console, Set, Map, Date, Promise });
  childWindow.SessionholicWorkspace.init(second.child);
  assert.equal(f.workspace.addFromPane('p1', undefined, { key: 'session-c' }), false);
  assert.equal(f.workspace.addFromPane('p2', {}, { key: 'session-c' }), false);
  assert.equal(f.workspace.addFromPane('p2', childWindow, { key: 'session-c' }, 'foreign'), false);
  assert.equal(childWindow.SessionholicWorkspace.add({ key: 'session-c', mode: 'chat' }), true);
  const saved = f.preferences.get('workspace');
  assert.deepEqual(saved.tree.children[1].children, [{ pane: 'p2' }, { pane: 'p3' }]);
  assert.deepEqual(saved.panes.p3, { key: 'session-c', mode: 'chat' });
  assert.equal(f.frames.length, 2); assert.equal(second.frame.appendCount, 1);
  childWindow.SessionholicWorkspace.add({ key: 'session-c', mode: 'chat' });
  assert.equal(f.frames.length, 2, 'existing session views are focused without duplicating them');
  f.infos.get('p1').authenticated = false;
  assert.equal(childWindow.SessionholicWorkspace.add({ key: 'session-a' }), false);
  assert.equal(f.calls.some(([name]) => name === 'api' || name === 'new'), false);
});

test('Option zero switches only the eligible current pane in single and split layouts, even with an IME event', () => {
  for (const extra of [{ key: '0' }, { key: 'º', code: 'Digit0' }, { key: 'Process', code: 'Digit0', isComposing: true, keyCode: 229 }]) {
    const event = combo('0', { ctrlKey: false, metaKey: false, altKey: true, ...extra });
    assert.deepEqual(W.shortcut(event), { view: true });
  }
  const f = fixture(), event = () => combo('º', { ctrlKey: false, metaKey: false, altKey: true, code: 'Digit0' });
  assert.equal(f.workspace.handleKey(event()), true); assert.ok(f.calls.some(([name, id]) => name === 'view' && id === 'p1'));
  f.workspace.add({ key: 'session-b' }); const second = f.attach('p2'); f.calls.length = 0;
  f.document.fire('window:keydown', event());
  assert.deepEqual(f.calls.filter(([name]) => name === 'view'), [['view', 'p2']]);
  f.workspace.handleKey({ ...event(), repeat: true });
  assert.equal(f.calls.filter(([name]) => name === 'view').length, 1, 'holding the key does not cycle views');
  second.info.canToggleView = false; assert.equal(f.workspace.handleKey(event(), 'p2'), false);
  second.info.canToggleView = true; second.info.dialog = true; assert.equal(f.workspace.handleKey(event(), 'p2'), false);
  assert.equal(W.shortcut(combo('0', { ctrlKey: false, metaKey: false, altKey: true, keyCode: 229 })), null);
  assert.equal(f.calls.some(([name]) => name === 'api' || name === 'new'), false);
});

test('Option plus physical pane numbers selects the matching visible pane in any saved configuration', () => {
  for (const saved of [undefined, 'ctrl-meta', 'ctrl-alt-meta']) {
    for (let number = 1; number <= 4; number++) {
      const event = combo('¡', { ctrlKey: false, metaKey: false, altKey: true, code: 'Digit' + number });
      assert.deepEqual(W.shortcut(event, saved), { number });
    }
  }
  assert.deepEqual(W.shortcut(combo('2', { ctrlKey: false, metaKey: false, altKey: true })), { number: 2 });
  for (const extra of [{ altKey: false }, { shiftKey: true }, { code: 'Digit5' }, { code: '' }, { code: '', key: 'Process', isComposing: true, keyCode: 229 }])
    assert.equal(W.shortcut(combo('¡', { ctrlKey: false, metaKey: false, altKey: true, code: 'Digit1', ...extra })), null);
});

test('a physical Option pane number commits Korean in the original pane before moving, with no message sent', async () => {
  const f = fixture(); f.workspace.add({ key: 'session-b' }); const second = f.attach('p2');
  second.info.composing = true;
  let done; second.child.commitComposition = () => new Promise(resolve => { done = resolve; });
  const originalEditor = second.info.editor;
  const event = combo('Process', { ctrlKey: false, metaKey: false, altKey: true, code: 'Digit1', isComposing: true, keyCode: 229 });
  assert.equal(f.workspace.handleKey(event, 'p2'), true);
  assert.ok(event.prevented && event.stopped); assert.equal(f.workspace.isActive('p2', second.frame.contentWindow), true);
  assert.equal(typeof done, 'function');
  second.info.composing = false; done(true); await Promise.resolve(); await Promise.resolve();
  assert.equal(f.workspace.readingActive(), true); assert.equal(second.info.editor, originalEditor);
  assert.equal(originalEditor.text, '초안 p2'); assert.equal(originalEditor.caret, 3);
  assert.equal(f.calls.some(([name]) => name === 'api'), false);
});

test('the root window capture and visible Option number buttons select the same mounted editor', () => {
  const f = fixture(); f.workspace.add({ key: 'session-b' }); const second = f.attach('p2');
  const tabs = f.nodes.get('workspace-switcher').children;
  assert.deepEqual(tabs.map(tab => tab.textContent), ['창 1', '창 2']);
  assert.deepEqual(tabs.map(tab => tab.attrs['aria-keyshortcuts']), ['Alt+1', 'Alt+2']);
  const editor = second.info.editor;
  f.workspace.focus('p1', false); f.calls.length = 0;
  const event = combo('™', { code: 'Digit2', ctrlKey: false, metaKey: false, altKey: true, target: { tagName: 'TEXTAREA', id: 'chat-input' } });
  f.document.fire('window:keydown', event);
  assert.ok(event.prevented && event.stopped); assert.equal(f.workspace.isActive('p2', second.frame.contentWindow), true);
  assert.equal(second.info.editor, editor); assert.equal(editor.text, '초안 p2');
  f.nodes.get('workspace-switcher').children[0].fire('click', {});
  assert.equal(f.workspace.readingActive(), true); assert.equal(second.frame.appendCount, 1);
  assert.equal(f.calls.some(([name]) => name === 'api'), false);
});
