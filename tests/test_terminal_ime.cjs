"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs"), path = require("node:path"), vm = require("node:vm");
const ime = require("../web/terminal-ime.js");

// Exercise the actual pinned vendor implementation, without a browser, PTY or
// native session. Only its webpack entry is redirected to CompositionHelper.
const bundle = fs.readFileSync(path.join(__dirname, "../web/vendor/xterm.js"), "utf8");
const entry = "var s={};return(()=>{var e=s;";
assert.equal(bundle.split(entry).length, 2, "review this fixture when upgrading xterm");
function fixture(patched = true) {
  const timers = new Map(), chunks = []; let next = 0;
  const clock = { setTimeout(fn) { timers.set(++next, fn); return next; }, clearTimeout(id) { timers.delete(id); } };
  const box = { module: { exports: {} }, exports: {}, ...clock,
    navigator: { platform: "MacIntel", userAgent: "Chrome", language: "ko-KR" }, TextDecoder, TextEncoder };
  vm.runInNewContext(bundle.replace(entry, "var s={};return i(3618);(()=>{var e=s;"), box);
  const field = { value: "", selectionStart: 0, selectionEnd: 0, style: {} };
  const helper = new box.module.exports.CompositionHelper(field, { classList: { add() {}, remove() {} }, style: {} },
    { buffer: { isCursorInViewport: false } }, {}, { triggerDataEvent(text) { chunks.push(text); } }, {});
  const term = { textarea: field, _core: { _compositionHelper: helper }, reset() {}, dispose() {} };
  if (patched) assert.equal(ime.install(term, clock), true);
  const update = (value) => { field.value = value; field.selectionStart = field.selectionEnd = value.length; helper.compositionupdate({ data: value }); };
  const tick = () => { while (timers.size) { const [id, fn] = timers.entries().next().value; timers.delete(id); fn(); } };
  return { helper, term, field, chunks, update, tick, clock };
}

test("stock xterm reproduces dropped Hangul while the installed fix preserves commits once and before a control key", () => {
  for (const patched of [false, true]) {
    const f = fixture(patched);
    f.helper.compositionstart(); f.update("니"); f.helper.compositionend();
    f.helper.compositionstart(); f.update("니다"); f.helper.compositionend();
    f.helper.keydown({ keyCode: 32 }); f.tick();
    assert.equal(f.chunks.join(""), patched ? "니다" : "니");
  }
});

test("a vowel moving the previous final consonant uses the final textarea, not an outdated composition event", () => {
  const f = fixture();
  f.helper.compositionstart(); f.update("국"); f.helper.compositionend();
  // Before the deferred send runs, the browser moves ㄱ to the next syllable.
  f.field.value = "구"; f.field.selectionEnd = 1;
  f.helper.compositionstart(); f.update("구거"); f.tick();
  assert.deepEqual(f.chunks, ["구"]);
  f.helper.compositionend(); f.tick();
  assert.equal(f.chunks.join(""), "구거");
});

test("a composing vowel with a non-229 keyCode never finalizes the first consonant", () => {
  const f = fixture(); f.helper.compositionstart(); f.update("ㅎ"); f.tick();
  assert.equal(f.helper.keydown({ keyCode: 75, isComposing: true }), false);
  assert.equal(ime.composing(f.term), true); assert.deepEqual(f.chunks, []);
  f.update("하"); f.helper.compositionend(); f.tick(); assert.deepEqual(f.chunks, ["하"]);
});

test("the current caret preserves a composition interrupted before its update timer and late end does not duplicate", () => {
  const f = fixture(); f.helper.compositionstart(); f.update("가");
  f.helper.keydown({ keyCode: 13 }); assert.deepEqual(f.chunks, ["가"]);
  f.helper.compositionend(); f.tick(); assert.deepEqual(f.chunks, ["가"]);
});

test("a screen reset drains only committed text, disposal cancels delayed input, and reinstall is idempotent", () => {
  const f = fixture(); assert.equal(ime.install(f.term, f.clock), true);
  f.helper.compositionstart(); f.update("한"); f.helper.compositionend(); f.term.reset();
  assert.deepEqual(f.chunks, ["한"]); f.tick(); assert.deepEqual(f.chunks, ["한"]);
  f.field.value = ""; f.helper.compositionstart(); f.update("글"); f.helper.compositionend();
  f.term.dispose(); f.tick(); assert.deepEqual(f.chunks, ["한"]);
});

test("IME keys and held Shift stay in native composition; unsupported helpers are left alone", () => {
  const f = fixture(); f.helper.compositionstart(); f.update("ㅆ");
  for (const keyCode of [16, 17, 18, 20, 229]) assert.equal(f.helper.keydown({ keyCode }), false);
  assert.equal(ime.composing(f.term), true); assert.deepEqual(f.chunks, []);
  f.helper.compositionend(); f.tick(); assert.deepEqual(f.chunks, ["ㅆ"]);
  const unknown = { textarea: {}, _core: { _compositionHelper: {} } }; assert.equal(ime.install(unknown), false);
});

test("identical repeated syllables are retained and many queued commits remain in order", () => {
  const f = fixture();
  for (const value of ["가", "가가", "가가나", "가가나다"]) {
    f.helper.compositionstart(); f.update(value); f.helper.compositionend();
  }
  f.tick(); assert.equal(f.chunks.join(""), "가가나다");
});
