(() => {
  const $ = (id) => document.getElementById(id);
  const dialog = $("woni-dialog"), open = $("woni-open"), input = $("woni-input");
  const messages = $("woni-messages"), status = $("woni-status"), form = $("woni-form");
  const fileInput = $("woni-files"), attach = $("woni-attach"), filesBox = $("woni-attachments");
  const send = $("woni-send"), mic = $("woni-mic");
  const chatTab = $("woni-chat-tab"), workTab = $("woni-work-tab"), work = $("woni-work");
  const filterBox = $("woni-filter"), replyBox = $("woni-reply"), original = $("woni-original");
  let api, services = {}, timer, reading = false, sending = false, pending = null, epoch = 0;
  let attachments = [], uploading = false, uploadAbort = null, composing = false, compositionEndedAt = 0, authEpoch = 0;
  let replyTarget = null, filter = null, view = "chat", latestData = null, workData = null, workError = "";
  let pendingState = "uncertain";
  let enabled = false, focusId = null;
  const pendingKey = "sessionholic:woni:pending";
  const seenKey = "sessionholic:woni:seen";
  let seen = null, unread = new Set();
  const maxFile = 100 * 1024 * 1024, maxFiles = 10;
  const answerTimeFormat = new Intl.DateTimeFormat("ko-KR", { timeZone: "Asia/Seoul", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" });

  function loadSeen() {
    try {
      const value = JSON.parse(sessionStorage.getItem(seenKey) || "null");
      seen = value && value.seen && typeof value.seen === "object" && !Array.isArray(value.seen) ? value.seen : null;
      unread = new Set(Array.isArray(value?.unread) ? value.unread.filter((id) => typeof id === "string") : []);
    } catch (_) { seen = null; unread = new Set(); }
    badge();
  }
  function saveSeen() {
    try { sessionStorage.setItem(seenKey, JSON.stringify({ seen, unread: [...unread] })); } catch (_) {}
  }
  function badge() {
    const count = unread.size, node = $("woni-unread");
    node.hidden = !count; node.textContent = count > 99 ? "99+" : String(count);
    open.setAttribute("aria-label", count ? `원이 열기, 새 답변 ${count}개` : "원이 열기");
  }
  function observeReplies(data) {
    const chats = Array.isArray(data.chats) ? data.chats : [];
    const first = seen === null, next = Object.create(null), visible = dialog.open && view === "chat" && !document.hidden;
    let arrived = false;
    for (const chat of chats) {
      if (typeof chat.id !== "string" || !/^[A-Za-z0-9_-]{1,100}$/.test(chat.id)) continue;
      const complete = chat.status === "complete" && !!chat.answer && chat.kind !== "alert";
      next[chat.id] = complete;
      if (!first && complete && seen[chat.id] !== true) {
        if (!visible || (filter && chat.stream !== filter.id)) { unread.add(chat.id); arrived = true; }
      }
      if (visible && (!filter || chat.stream === filter.id)) unread.delete(chat.id);
    }
    for (const id of unread) if (!Object.prototype.hasOwnProperty.call(next, id)) unread.delete(id);
    seen = next; badge(); saveSeen();
    if (arrived && !document.hidden) services.toast?.("원이의 새 답변이 도착했어요. 원이 버튼에서 확인해 주세요.");
  }
  function markVisibleRead() {
    if (!latestData || document.hidden || view !== "chat") return;
    for (const chat of latestData.chats || []) if (!filter || chat.stream === filter.id) unread.delete(chat.id);
    badge(); saveSeen();
  }

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
  function action(label, fn) {
    const button = el("button", "", label); button.type = "button"; button.addEventListener("click", fn); return button;
  }
  function answerTime(seconds) {
    if (typeof seconds !== "number" || !Number.isFinite(seconds) || seconds <= 0) return null;
    const date = new Date(seconds * 1000);
    if (!Number.isFinite(date.getTime())) return null;
    const text = answerTimeFormat.format(date);
    const node = el("time", "woni-message-time", text);
    node.dateTime = date.toISOString(); node.setAttribute("aria-label", `답변 시각 ${text}`);
    return node;
  }
  function setView(value) {
    view = value; const isChat = value === "chat";
    messages.hidden = !isChat; form.hidden = !isChat; work.hidden = isChat;
    filterBox.hidden = !isChat || !filter;
    chatTab.setAttribute("aria-pressed", String(isChat)); workTab.setAttribute("aria-pressed", String(!isChat));
    original.hidden = true;
    if (isChat && dialog.open) markVisibleRead();
    if (!isChat) { renderWork(); clearTimeout(timer); refresh(); }
  }
  function setReply(chat, part) {
    if (pending) { status.textContent = "이전 메시지의 전송 여부를 먼저 확인해 주세요."; return; }
    replyTarget = { id: chat.id, part, excerpt: (part === "question" ? chat.text : chat.answer) || "" };
    replyBox.hidden = false;
    $("woni-reply-label").textContent = `${part === "question" ? "라이언 메시지" : "원이 답변"}에 답글 · ${replyTarget.excerpt}`;
    setView("chat"); input.focus?.();
  }
  async function showOriginal(id) {
    const current = epoch;
    original.hidden = false; original.replaceChildren(el("p", "", "원문을 불러오고 있어요."));
    try {
      const chat = latestData?.chats?.find((item) => item.id === id) || await api(`/api/woni/chat?${new URLSearchParams({ id })}`);
      if (current !== epoch || !dialog.open) return;
      const block = el("div", "", [chat.text, chat.answer].filter(Boolean).join("\n\n"));
      original.replaceChildren(el("strong", "", "원문"), block, action("답글 달기", () => setReply(chat, chat.answer ? "answer" : "question")), action("닫기", () => { original.hidden = true; }));
    } catch (_) { if (current === epoch && dialog.open) original.replaceChildren(el("p", "", "원문을 불러오지 못했어요."), action("닫기", () => { original.hidden = true; })); }
  }
  function row(role, text, files = [], chat = null) {
    const article = el("article", `woni-message ${role}`), label = el("strong", "", role === "user" ? "라이언" : "원이");
    const content = el("div", "woni-message-body");
    if (role === "user" && chat?.replyTo) {
      const quote = el("button", "woni-message-quote", `↩ ${chat.replyTo.who || "원문"} · ${chat.replyTo.excerpt || "원문 보기"}`);
      quote.type = "button"; quote.addEventListener("click", () => showOriginal(chat.replyTo.id));
      article.append(quote);
    }
    if (role === "assistant" && typeof MessageFormat !== "undefined") MessageFormat.render(content, text);
    else content.textContent = text;
    article.append(label, content);
    if (files.length) {
      const list = el("div", "woni-message-files");
      for (const file of files) list.append(el("span", "woni-message-file", file.name || "첨부 파일"));
      article.append(list);
    }
    if (role === "assistant" && chat?.answer) {
      const time = answerTime(chat.finishedAt);
      if (time) article.append(time);
    }
    if (chat?.id) {
      const actions = el("div", "woni-message-actions");
      actions.append(action("답글", () => setReply(chat, role === "user" ? "question" : "answer")));
      if (chat.stream && chat.streamTitle) actions.append(action(chat.streamTitle, () => {
        filter = { id: chat.stream, title: chat.streamTitle }; setView("chat"); if (latestData) show(latestData);
      }));
      article.append(actions);
    }
    if (chat?.id) article.dataset.chatId = chat.id;
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
    filesBox.hidden = !!pending || !nodes.length;
    attach.disabled = !!pending || attachments.length >= maxFiles;
    send.disabled = sending || uploading || !!pending;
    send.textContent = sending ? "전송 중…" : pending ? (pendingState === "uncertain" ? "전송 확인 필요" : "확인 중…") : "보내기";
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
  async function showWorkerResult(batch, task) {
    const current = epoch;
    original.hidden = false; original.replaceChildren(el("p", "", "작업 결과를 불러오고 있어요."));
    try {
      const query = new URLSearchParams({ batch: String(batch.id), n: String(task.n) });
      const result = await api(`/api/woni/worker-result?${query}`);
      if (current !== epoch || !dialog.open) return;
      original.replaceChildren(el("strong", "", task.title || "작업 결과"), el("div", "", result.text || "결과가 없어요."),
        ...(result.truncated ? [el("p", "", "결과의 앞부분만 표시합니다.")] : []), action("닫기", () => { original.hidden = true; }));
    } catch (_) { if (current === epoch && dialog.open) original.replaceChildren(el("p", "", "작업 결과를 불러오지 못했어요."), action("닫기", () => { original.hidden = true; })); }
  }
  function renderWork() {
    const nodes = [];
    if (workError) nodes.push(el("p", "woni-status", workData ? "연결을 확인하지 못해 이전 작업 현황을 보여 줍니다." : workError));
    if (!workData) { if (!workError) nodes.push(el("p", "woni-status", "작업 현황을 불러오고 있어요.")); work.replaceChildren(...nodes); return; }
    const running = [...(workData.running || []), ...(workData.loose || [])];
    const groups = [["지금", running], ["최근 24시간", workData.recent || []]];
    for (const [title, rows] of groups) {
      if (!rows.length && title !== "지금") continue;
      nodes.push(el("h3", "", title));
      if (!rows.length) nodes.push(el("p", "woni-status", "지금 진행 중인 원이 작업이 없어요."));
      for (const item of rows) {
        const card = el("article", "woni-work-card");
        card.append(el("h4", "", item.streamTitle || item.text || "원이 작업"));
        if (item.streamTitle && item.text) card.append(el("p", "", item.text));
        const state = item.status === "queued" ? "순서를 기다리고 있어요" : item.status === "running" ? (item.progress || item.steps?.at(-1)?.tool || "작업하고 있어요")
          : item.status === "complete" ? "답변 완료" : "확인이 필요해요";
        card.append(el("p", "", state));
        for (const batch of item.batches || []) for (const task of batch.tasks || []) {
          const label = `${task.title || "하위 작업"} · ${{ queued: "대기", waiting: "자리 대기", running: "진행 중", ok: "완료", error: "오류", timeout: "시간 초과", stopped: "중단됨", busy: "시작 못 함" }[task.status] || "상태 확인 필요"}`;
          if (task.status === "ok") card.append(action(label + " · 결과 보기", () => showWorkerResult(batch, task)));
          else card.append(el("p", "", label));
        }
        if (item.id) card.append(action("대화에서 이어보기 →", () => {
          filter = null; setView("chat"); if (latestData) show(latestData);
          const found = latestData?.chats?.find((chat) => chat.id === item.id);
          if (found) messages.querySelector?.(`[data-chat-id="${CSS.escape(item.id)}"]`)?.scrollIntoView?.({ block: "center" });
          else showOriginal(item.id);
        }));
        nodes.push(card);
      }
    }
    work.replaceChildren(...nodes);
  }
  function show(data) {
    latestData = data;
    const chats = Array.isArray(data.chats) ? data.chats : [];
    if (pending && chats.some((chat) => chat.id === pending.requestId)) {
      const ids = new Set(pending.attachments || []);
      attachments = attachments.filter((item) => !ids.has(item.id));
      if (JSON.stringify(pending.replyTo || null) === JSON.stringify(replyTarget && { id: replyTarget.id, part: replyTarget.part })) {
        replyTarget = null; replyBox.hidden = true;
      }
      savePending(null); renderAttachments();
    }
    const atEnd = messages.scrollHeight - messages.scrollTop - messages.clientHeight < 80;
    const nodes = [];
    for (const chat of chats.filter((item) => !filter || item.stream === filter.id)) {
      if (chat.kind !== "alert" && (chat.text || chat.attachments?.length)) nodes.push(row("user", chat.text || "", chat.attachments || [], chat));
      if (chat.answer) nodes.push(row("assistant", chat.answer, [], chat));
      else if (["queued", "running"].includes(chat.status)) nodes.push(row("assistant", chat.progress || "생각하고 있어요…"));
      else if (["failed", "interrupted"].includes(chat.status)) nodes.push(row("assistant", "답변을 완료하지 못했어요. 원이 앱에서 상태를 확인해 주세요."));
    }
    if (pending) {
      const sentFiles = (pending.attachments || []).map((id) => attachments.find((item) => item.id === id) || { name: "첨부 파일" });
      const outgoing = row("user", pending.text, sentFiles);
      if (pending.replyTo) outgoing.append(el("div", "woni-send-status", `↩ 답글 · ${replyTarget?.excerpt || "이전 메시지"}`));
      const message = pendingState === "sending" ? "전송 중…" : pendingState === "checking" ? "전달됨 · 대화 확인 중…" : "전송 여부를 확인하지 못했어요.";
      const indicator = el("div", "woni-send-status", message);
      indicator.setAttribute("role", "status");
      if (pendingState === "uncertain" && !sending) indicator.append(action("다시 보내기", sendPending));
      outgoing.append(indicator); nodes.push(outgoing);
    }
    messages.replaceChildren(...nodes);
    if (atEnd || !messages.dataset.loaded) messages.scrollTop = messages.scrollHeight;
    messages.dataset.loaded = "1";
    filterBox.hidden = view !== "chat" || !filter;
    $("woni-filter-label").textContent = filter ? `흐름: ${filter.title}` : "";
    const running = data.activity?.running || 0;
    status.textContent = pending ? (pendingState === "uncertain" ? "전송 확인이 필요해요." : "메시지를 전달하고 있어요.") : running ? `작업 중 ${running}건` : "나의 개인 비서";
  }
  async function refresh() {
    if (!enabled || reading || !api) return;
    reading = true; const current = authEpoch;
    try {
      const data = await api("/api/woni");
      if (current !== authEpoch || !enabled) return;
      latestData = data; observeReplies(data);
      if (dialog.open) {
        show(data);
        if (focusId) {
          const id = focusId; focusId = null;
          if (data.chats?.some((chat) => chat.id === id)) messages.querySelector?.(`[data-chat-id="${CSS.escape(id)}"]`)?.scrollIntoView?.({ block: "center" });
          else showOriginal(id);
        }
      }
      if (current === authEpoch && dialog.open && view === "work") {
        try {
          const result = await api("/api/woni/activity");
          if (current === authEpoch && dialog.open) { workData = result; workError = ""; }
        } catch (_) { if (current === authEpoch && dialog.open) workError = "작업 현황을 확인하지 못했어요."; }
        if (current === authEpoch && dialog.open && view === "work") renderWork();
      }
    } catch (_) {
      if (current === authEpoch && dialog.open) {
        status.textContent = "원이에 연결하지 못했어요. 잠시 후 다시 확인해 주세요.";
        if (view === "work") { workError = "작업 현황을 확인하지 못했어요."; renderWork(); }
      }
    } finally {
      reading = false;
      if (enabled) timer = setTimeout(refresh, current === authEpoch ? (dialog.open ? 4000 : 6000) : 0);
    }
  }
  open.addEventListener("click", () => {
    if (dialog.open) { setView("chat"); markVisibleRead(); return; }
    pending = savedPending();
    if (pending) {
      if (!attachments.length) attachments = (pending.attachments || []).map((id) => ({ id, name: "첨부 파일", state: "ready" }));
      if (pending.replyTo && !replyTarget) {
        replyTarget = { ...pending.replyTo, excerpt: "대상 메시지" };
      }
      replyBox.hidden = true;
    }
    renderAttachments(); dialog.showModal(); setView("chat");
    if (latestData || pending) show(latestData || { chats: [], activity: {} });
    markVisibleRead();
    input.focus({ preventScroll: true });
    refresh();
  });
  $("woni-close").addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => { clearTimeout(timer); services.voice?.stop(input); epoch++; if (enabled) refresh(); });
  chatTab.addEventListener("click", () => setView("chat"));
  workTab.addEventListener("click", () => setView("work"));
  $("woni-filter-clear").addEventListener("click", () => { filter = null; if (latestData) show(latestData); });
  $("woni-reply-clear").addEventListener("click", () => {
    if (pending) { status.textContent = "이전 메시지의 전송 여부를 먼저 확인해 주세요."; return; }
    replyTarget = null; replyBox.hidden = true;
  });
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
  async function sendPending() {
    if (!pending || sending) return;
    const request = pending, current = authEpoch;
    sending = true; pendingState = "sending"; renderAttachments();
    show(latestData || { chats: [], activity: {} });
    try {
      await api("/api/woni", { method: "POST", body: request, csrfRetry: false });
      if (current === authEpoch && pending?.requestId === request.requestId) pendingState = "checking";
    } catch (_) {
      if (current === authEpoch && pending?.requestId === request.requestId) pendingState = "uncertain";
    } finally {
      if (current === authEpoch) {
        sending = false; renderAttachments();
        if (dialog.open) show(latestData || { chats: [], activity: {} });
        clearTimeout(timer); refresh();
      }
    }
  }
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (pending || sending || uploading || composing) return;
    if (attachments.some((item) => item.state !== "ready")) { status.textContent = "첨부를 다시 올리거나 제거해 주세요."; return; }
    const text = input.value.trim() ? input.value : attachments.length ? "첨부한 파일을 확인해 줘" : "";
    if (!text) return;
    const ids = attachments.map((item) => item.id);
    const reply = replyTarget ? { id: replyTarget.id, part: replyTarget.part } : null;
    savePending({ text, requestId: crypto.randomUUID(), ...(ids.length ? { attachments: ids } : {}), ...(reply ? { replyTo: reply } : {}) });
    input.value = ""; replyBox.hidden = true; filter = null;
    services.voice?.stop(input);
    const result = sendPending(); messages.scrollTop = messages.scrollHeight;
    await result;
  });
  window.SessionholicWoni = {
    hasDraft() { return !!input.value || !!attachments.length || uploading || !!pending; },
    async check(call, helpers = {}) {
      api = call; services = helpers; mic.hidden = !services.voice?.supported(); const current = authEpoch;
      try {
        const data = await api("/api/woni?availability=1");
        if (current === authEpoch) {
          enabled = !!data.enabled; open.hidden = !enabled;
          if (enabled) { loadSeen(); if (focusId) open.click(); refresh(); }
        }
      } catch (_) { if (current === authEpoch) { enabled = false; open.hidden = true; } }
    },
    openFromNotification(id) {
      if (typeof id !== "string" || !/^[A-Za-z0-9_-]{1,100}$/.test(id)) return false;
      focusId = id; filter = null;
      if (enabled) { open.click(); clearTimeout(timer); refresh(); }
      return true;
    },
    lock() {
      enabled = false; epoch++; authEpoch++; clearTimeout(timer); uploadAbort?.abort(); services.voice?.stop(input);
      if (dialog.open) dialog.close(); open.hidden = true; messages.replaceChildren(); delete messages.dataset.loaded;
      input.value = ""; attachments = []; replyTarget = null; replyBox.hidden = true; filter = null; latestData = null; workData = null;
      pending = null; pendingState = "uncertain"; sending = false; seen = null; unread.clear(); badge();
      original.replaceChildren(); original.hidden = true; work.replaceChildren(); renderAttachments();
    }
  };
})();
