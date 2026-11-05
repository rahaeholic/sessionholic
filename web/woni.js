(() => {
  const $ = (id) => document.getElementById(id);
  const dialog = $("woni-dialog"), open = $("woni-open"), input = $("woni-input");
  const messages = $("woni-messages"), status = $("woni-status"), form = $("woni-form");
  const fileInput = $("woni-files"), attach = $("woni-attach"), filesBox = $("woni-attachments");
  const send = $("woni-send"), mic = $("woni-mic");
  let api, services = {}, timer, reading = false, sending = false, pending = null, epoch = 0;
  let attachments = [], uploading = false, uploadAbort = null, composing = false, compositionEndedAt = 0, authEpoch = 0;
  const pendingKey = "sessionholic:woni:pending";
  const maxFile = 100 * 1024 * 1024, maxFiles = 10;

  function savedPending() {
    try { return JSON.parse(sessionStorage.getItem(pendingKey) || "null"); } catch (_) { return null; }
  }
  function savePending(value) {
    pending = value;
    try { if (value) sessionStorage.setItem(pendingKey, JSON.stringify(value)); else sessionStorage.removeItem(pendingKey); } catch (_) {}
  }
  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }
  function row(role, text, files = []) {
    const article = el("article", `woni-message ${role}`), label = el("strong", "", role === "user" ? "라이언" : "원이");
    const content = el("div", "woni-message-body");
    if (role === "assistant" && typeof MessageFormat !== "undefined") MessageFormat.render(content, text);
    else content.textContent = text;
    article.append(label, content);
    if (files.length) {
      const list = el("div", "woni-message-files");
      for (const file of files) list.append(el("span", "woni-message-file", file.name || "첨부 파일"));
      article.append(list);
    }
    return article;
  }
  function renderAttachments() {
    const nodes = attachments.map((item) => {
      const chip = el("span", "woni-file");
      let detail = item.state === "uploading" ? ` · 올리는 중 ${Math.round((item.progress || 0) * 100)}%`
        : item.state === "waiting" ? " · 대기 중" : item.state === "failed" ? " · 실패" : "";
      chip.append(el("span", "woni-file-name", item.name + detail));
      if (item.state === "failed") {
        const retry = el("button", "woni-file-remove", "다시 올리기");
        retry.type = "button"; retry.addEventListener("click", () => { item.state = "waiting"; uploadFiles(); });
        chip.append(retry);
      }
      const remove = el("button", "woni-file-remove", "×");
      remove.type = "button"; remove.disabled = !!pending || item.state === "uploading";
      remove.setAttribute("aria-label", `${item.name} 첨부 제거`);
      remove.addEventListener("click", () => { attachments = attachments.filter((one) => one !== item); renderAttachments(); });
      chip.append(remove); return chip;
    });
    filesBox.replaceChildren(...nodes);
    filesBox.hidden = !nodes.length;
    attach.disabled = !!pending || attachments.length >= maxFiles;
    send.disabled = sending || uploading;
  }
  async function uploadFiles() {
    if (uploading || !services.upload) return;
    uploading = true; uploadAbort = new AbortController();
    const current = authEpoch;
    try {
      while (attachments.some((item) => item.state === "waiting")) {
        const item = attachments.find((one) => one.state === "waiting");
        item.state = "uploading"; item.progress = 0; renderAttachments();
        try {
          const result = await services.upload(item.file, (ratio) => { item.progress = ratio; renderAttachments(); }, uploadAbort.signal);
          if (current !== authEpoch) return;
          Object.assign(item, result, { state: "ready", file: null });
        } catch (error) {
          if (current !== authEpoch) return;
          item.state = "failed";
          status.textContent = error.message || "파일을 올리지 못했어요. 다시 올리거나 제거해 주세요.";
        }
        renderAttachments();
      }
    } finally { uploading = false; uploadAbort = null; renderAttachments(); }
  }
  function addFiles(files) {
    if (pending || sending) { status.textContent = "먼저 이전 메시지의 전송 여부를 확인해 주세요."; return; }
    const selected = Array.from(files || []);
    if (attachments.length + selected.length > maxFiles) { status.textContent = `첨부는 한 번에 ${maxFiles}개까지예요.`; return; }
    for (const file of selected) {
      if (!file.size || file.size > maxFile) { status.textContent = `${file.name || "파일"}: 100MB 이하의 파일을 선택해 주세요.`; continue; }
      attachments.push({ name: file.name || "첨부 파일", file, state: "waiting" });
    }
    renderAttachments(); uploadFiles();
  }
  function show(data) {
    const chats = Array.isArray(data.chats) ? data.chats : [];
    if (pending && chats.some((chat) => chat.id === pending.requestId)) {
      if (input.value === pending.text || (!input.value.trim() && pending.text === "첨부한 파일을 확인해 줘")) input.value = "";
      const ids = new Set(pending.attachments || []);
      attachments = attachments.filter((item) => !ids.has(item.id));
      savePending(null); renderAttachments();
    }
    const atEnd = messages.scrollHeight - messages.scrollTop - messages.clientHeight < 80;
    const nodes = [];
    for (const chat of chats) {
      if (chat.kind !== "alert" && (chat.text || chat.attachments?.length)) nodes.push(row("user", chat.text || "", chat.attachments || []));
      if (chat.answer) nodes.push(row("assistant", chat.answer));
      else if (["queued", "running"].includes(chat.status)) nodes.push(row("assistant", chat.progress || "생각하고 있어요…"));
      else if (["failed", "interrupted"].includes(chat.status)) nodes.push(row("assistant", "답변을 완료하지 못했어요. 원이 앱에서 상태를 확인해 주세요."));
    }
    messages.replaceChildren(...nodes);
    if (atEnd || !messages.dataset.loaded) messages.scrollTop = messages.scrollHeight;
    messages.dataset.loaded = "1";
    const running = data.activity?.running || 0;
    status.textContent = pending ? "전송 여부를 확인하고 있어요." : running ? `작업 중 ${running}건` : "나의 개인 비서";
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
  open.addEventListener("click", () => {
    pending = savedPending();
    if (pending) {
      if (!input.value) input.value = pending.text;
      if (!attachments.length) attachments = (pending.attachments || []).map((id) => ({ id, name: "첨부 파일", state: "ready" }));
    }
    renderAttachments(); dialog.showModal(); refresh();
  });
  $("woni-close").addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => { clearTimeout(timer); services.voice?.stop(input); epoch++; });
  attach.addEventListener("click", () => fileInput.click());
  fileInput.addEventListener("change", () => { addFiles(fileInput.files); fileInput.value = ""; });
  input.addEventListener("paste", (event) => {
    const files = Array.from(event.clipboardData?.files || []);
    if (files.length) { event.preventDefault(); addFiles(files); }
  });
  input.addEventListener("input", (event) => services.voice?.typed(input, event));
  input.addEventListener("compositionstart", () => { composing = true; });
  input.addEventListener("compositionend", () => { composing = false; compositionEndedAt = Date.now(); });
  input.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" || !(event.metaKey || event.ctrlKey)) return;
    event.preventDefault();
    if (event.isComposing || composing || event.keyCode === 229 || Date.now() - compositionEndedAt < 80) return;
    form.requestSubmit();
  });
  mic.addEventListener("click", () => services.voice?.toggle(input, (text) => { if (text) status.textContent = text; }));
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (sending || uploading || composing) return;
    if (attachments.some((item) => item.state !== "ready")) { status.textContent = "첨부를 다시 올리거나 제거해 주세요."; return; }
    const text = input.value.trim() ? input.value : attachments.length ? "첨부한 파일을 확인해 줘" : "";
    if (!text) return;
    const ids = attachments.map((item) => item.id);
    if (pending && (pending.text !== text || JSON.stringify(pending.attachments || []) !== JSON.stringify(ids))) {
      status.textContent = "이전 메시지의 전송 여부를 먼저 확인해 주세요."; return;
    }
    if (!pending) savePending({ text, requestId: crypto.randomUUID(), ...(ids.length ? { attachments: ids } : {}) });
    services.voice?.stop(input);
    sending = true; renderAttachments();
    try {
      await api("/api/woni", { method: "POST", body: pending, csrfRetry: false });
      status.textContent = "메시지를 보냈어요. 대화를 확인하고 있어요.";
    } catch (_) {
      status.textContent = "전송 여부를 확인하지 못했어요. 같은 메시지로 다시 보낼 수 있어요.";
    } finally {
      sending = false; renderAttachments();
      clearTimeout(timer); refresh();
    }
  });
  window.SessionholicWoni = {
    async check(call, helpers = {}) {
      api = call; services = helpers; mic.hidden = !services.voice?.supported(); const current = epoch;
      try { const data = await api("/api/woni?availability=1"); if (current === epoch) open.hidden = !data.enabled; } catch (_) { if (current === epoch) open.hidden = true; }
    },
    lock() {
      epoch++; authEpoch++; clearTimeout(timer); uploadAbort?.abort(); services.voice?.stop(input);
      if (dialog.open) dialog.close(); open.hidden = true; messages.replaceChildren(); delete messages.dataset.loaded;
      input.value = ""; attachments = []; renderAttachments();
    }
  };
})();
