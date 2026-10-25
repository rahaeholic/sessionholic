"use strict";

// xterm 6.0.0 compatibility: preserve queued composition commits under load.
// Based on the FIFO/range approach discussed in xtermjs/xterm.js #6089/#6090.
// xterm.js is Copyright (c) 2014-2026 The xterm.js authors, MIT licensed;
// its bundled license remains in vendor/xterm-LICENSE. Native IME events and
// candidate rendering stay with xterm; finalization and native composing-key
// handling are protected without inferring characters from physical key codes.
(function (root) {
  const installed = new WeakMap();
  function install(term, clock = { setTimeout: (fn, ms) => setTimeout(fn, ms), clearTimeout: (id) => clearTimeout(id) }) {
    if (installed.has(term)) return true;
    const helper = term?._core?._compositionHelper, field = term?.textarea;
    if (!helper || helper._textarea !== field || typeof helper._isSendingComposition !== "boolean" ||
        typeof helper.compositionstart !== "function" || typeof helper.keydown !== "function" || typeof helper._finalizeComposition !== "function" ||
        !helper._compositionPosition || typeof helper._coreService?.triggerDataEvent !== "function") return false;
    const start = helper.compositionstart, keydown = helper.keydown, pending = [];
    let sentUpTo = 0, disposed = false;
    const markPending = () => { helper._isSendingComposition = pending.length > 0; };
    function emit(from, end) {
      if (disposed) return;
      const value = field.value, lo = Math.min(value.length, Math.max(0, from, sentUpTo));
      const hi = Math.min(value.length, Math.max(lo, end));
      const text = value.slice(lo, hi);
      sentUpTo = Math.max(sentUpTo, hi);
      if (text) helper._coreService.triggerDataEvent(text, true);
    }
    function send(commit) {
      const index = pending.indexOf(commit);
      if (index < 0 || disposed) return;
      pending.splice(index, 1); clock.clearTimeout(commit.timer); markPending();
      // Once another composition begins, its start is this commit's end. Do
      // not read through into that newer preedit while draining synchronously.
      const end = commit.end ?? field.value.length;
      emit(commit.start + commit.alreadySent, end);
    }
    const flush = () => { for (const commit of [...pending]) send(commit); };
    helper.keydown = function (event) {
      // Native IME state is authoritative even when a browser supplies a real
      // letter keyCode rather than 229 for a vowel inside the same syllable.
      if (disposed || event.isComposing) return false;
      return keydown.call(this, event);
    };
    helper.compositionstart = function (...args) {
      if (disposed) return;
      start.apply(this, args);
      const position = this._compositionPosition;
      if (pending.length) pending[pending.length - 1].end = position.start;
      sentUpTo = Math.min(sentUpTo, position.start);
      position.end = position.start;
    };
    helper._finalizeComposition = function (wait) {
      if (disposed) return;
      this._compositionView.classList.remove("active"); this._isComposing = false;
      const position = this._compositionPosition;
      if (!wait) {
        // Enter/control keys must follow every already committed syllable.
        // selectionEnd is current even if compositionupdate's timer is late.
        const end = Math.max(position.end, field.selectionEnd ?? field.value.length);
        flush(); emit(position.start, end); markPending();
        return;
      }
      const commit = { start: position.start, end: null, alreadySent: (this._dataAlreadySent || "").length, timer: null };
      pending.push(commit); markPending();
      commit.timer = clock.setTimeout(() => send(commit), 0);
    };
    const dispose = () => {
      disposed = true;
      for (const commit of pending.splice(0)) clock.clearTimeout(commit.timer);
      markPending();
    };
    // Reset changes only the displayed terminal, not the native session. Flush
    // committed input first, keeping an active IME and its textarea intact.
    if (typeof term.reset === "function") {
      const reset = term.reset;
      term.reset = function (...args) { flush(); return reset.apply(this, args); };
    }
    if (typeof term.dispose === "function") {
      const originalDispose = term.dispose;
      term.dispose = function (...args) { dispose(); return originalDispose.apply(this, args); };
    }
    installed.set(term, { flush, dispose });
    return true;
  }
  const api = {
    install,
    flush: (term) => installed.get(term)?.flush(),
    composing: (term) => term?._core?._compositionHelper?.isComposing === true,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else if (root) root.SessionholicTerminalIme = api;
})(typeof window !== "undefined" ? window : null);
