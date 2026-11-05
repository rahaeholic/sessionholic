(() => {
  const $ = (id) => document.getElementById(id);
  const dialog = $("woni-dialog"), open = $("woni-open"), input = $("woni-input");
  const messages = $("woni-messages"), status = $("woni-status"), form = $("woni-form");
  let api, timer, reading = false, sending = false, pending = null, epoch = 0;
  const pendingKey = "sessionholic:woni:pending";

  function savedPending() {
    try { return JSON.parse(sessionStorage.getItem(pendingKey) || "null"); } catch (_) { return null; }
  }
  function savePending(value) {
    pending = value;
    try { if (value) sessionStorage.setItem(pendingKey, JSON.stringify(value)); else sessionStorage.removeItem(pendingKey); } catch (_) {}
  }
  function row(role, text) {
    const article = document.createElement("article"), label = document.createElement("strong"), content = document.createElement("div");
    article.className = `woni-message ${role}`;
    label.textContent = role === "user" ? "라이언" : "원이";
    if (role === "assistant" && typeof MessageFormat !== "undefined") MessageFormat.render(content, text);
    else content.textContent = text;
    article.append(label, content);
    return article;
  }
  function show(data) {
    const chats = Array.isArray(data.chats) ? data.chats : [];
    if (pending && chats.some((chat) => chat.id === pending.requestId)) {
      if (input.value === pending.text) input.value = "";
      savePending(null);
    }
    const atEnd = messages.scrollHeight - messages.scrollTop - messages.clientHeight < 80;
    const nodes = [];
    for (const chat of chats) {
      if (chat.kind !== "alert" && chat.text) nodes.push(row("user", chat.text));
      if (chat.answer) nodes.push(row("assistant", chat.answer));
      else if (["queued", "running"].includes(chat.status)) nodes.push(row("assistant", chat.progress || "생각하고 있어요…"));
      else if (["failed", "interrupted"].includes(chat.status)) nodes.push(row("assistant", "답변을 완료하지 못했어요. 원이 앱에서 상태를 확인해 주세요."));
    }
    messages.replaceChildren(...nodes);
    if (atEnd || !messages.dataset.loaded) messages.scrollTop = messages.scrollHeight;
    messages.dataset.loaded = "1";
    const running = data.activity?.running || 0;
    status.textContent = pending ? "전송 여부를 확인하고 있어요. 같은 메시지를 다시 보내도 중복 실행되지 않아요." : running ? `원이 작업 중 ${running}건` : "기존 원이 대화";
  }
  async function refresh() {
    if (!dialog.open || reading || !api) return;
    reading = true; const current = epoch;
    try {
      const data = await api("/api/woni");
      if (current === epoch && dialog.open) show(data);
    } catch (_) {
      if (current === epoch && dialog.open) status.textContent = "원이에 연결하지 못했어요. 잠시 후 다시 확인해 주세요.";
    } finally {
      reading = false;
      if (dialog.open) timer = setTimeout(refresh, current === epoch ? 4000 : 0);
    }
  }
  open.addEventListener("click", () => { pending = savedPending(); if (pending) input.value = pending.text; dialog.showModal(); refresh(); });
  $("woni-close").addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => { clearTimeout(timer); epoch++; });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const text = input.value;
    if (sending || !text.trim()) return;
    if (pending && pending.text !== text) { status.textContent = "이전 메시지의 전송 여부를 먼저 확인해 주세요."; return; }
    if (!pending) savePending({ text, requestId: crypto.randomUUID() });
    sending = true; form.querySelector("button").disabled = true;
    try {
      await api("/api/woni", { method: "POST", body: pending, csrfRetry: false });
      status.textContent = "메시지를 보냈어요. 대화를 확인하고 있어요.";
    } catch (_) {
      status.textContent = "전송 여부를 확인하지 못했어요. 같은 메시지로 다시 보내거나 연결을 확인해 주세요.";
    } finally {
      sending = false; form.querySelector("button").disabled = false;
      clearTimeout(timer); refresh();
    }
  });
  window.SessionholicWoni = {
    async check(call) {
      api = call; const current = epoch;
      try { const data = await api("/api/woni?availability=1"); if (current === epoch) open.hidden = !data.enabled; } catch (_) { if (current === epoch) open.hidden = true; }
    },
    lock() { epoch++; clearTimeout(timer); if (dialog.open) dialog.close(); open.hidden = true; messages.replaceChildren(); delete messages.dataset.loaded; input.value = ""; }
  };
})();
