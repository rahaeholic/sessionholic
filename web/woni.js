(() => {
  const $ = (id) => document.getElementById(id);
  const dialog = $("woni-dialog"), open = $("woni-open"), input = $("woni-input");
  const messages = $("woni-messages"), status = $("woni-status"), form = $("woni-form");
  const fileInput = $("woni-files"), attach = $("woni-attach"), filesBox = $("woni-attachments");
  const send = $("woni-send"), mic = $("woni-mic");
  const chatTab = $("woni-chat-tab"), workTab = $("woni-work-tab"), work = $("woni-work");
  const filterBox = $("woni-filter"), replyBox = $("woni-reply"), original = $("woni-original");
  const replyPicks = $("woni-reply-suggestions");
  let api, services = {}, timer, reading = false, sending = false, pending = null, epoch = 0;
  let attachments = [], uploading = false, uploadAbort = null, composing = false, compositionEndedAt = 0, authEpoch = 0;
  let replyTarget = null, filter = null, view = "chat", latestData = null, workData = null, workError = "";
  let pendingState = "uncertain";
  let enabled = false, focusId = null;
  // The message whose menu (time · reply · copy · stream) is open. The list is rebuilt on every refresh, so it is kept by key.
  let menuKey = null;
  // Ryan One 1.40.0 ("여러개를 병렬로 지시하니까 이전 대화 찾기가 어렵네"): /api/woni holds Woni's latest 60 chats
  // (latestData, which the read mark and badges use). Earlier pages and an opened stream are kept apart and merged
  // by seq, Woni's position in its whole list, only to draw and to find a chat.
  const findBox = $("woni-find"), findOpen = $("woni-find-open");
  const chatIdPattern = /^[A-Za-z0-9_-]{1,100}$/, streamPattern = /^[a-z0-9-]{1,40}$/;
  let olderChats = [], streamChats = [], streamChatsFor = null, streamHasMore = false;
  let olderLoading = false, olderError = "", olderRetry = null, olderGen = 0, streamBackTo = null;
  // highlight: the row a jump marked for a moment ({ id, part }); shownRows: the rows drawn last, to find a row's place.
  let highlight = null, highlightTimer = 0, shownRows = [];
  const pendingKey = "sessionholic:woni:pending";
  const seenKey = "sessionholic:woni:seen";
  let seen = null, unread = new Set();
  // Woni's shared read mark: answers finished at or before it were seen in the Woni app or a board.
  let readThrough = null, serverRead = 0, readPosting = 0;
  const maxFile = 100 * 1024 * 1024, maxFiles = 10;
  const answerTimeFormat = new Intl.DateTimeFormat("ko-KR", { timeZone: "Asia/Seoul", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
  // Ryan 2026-11-10 (ChatGPT dots): Woni is the only speaker, so its messages carry no name.
  // Ryan 2026-11-10 17:25 ("원이 답변 시간도 표시", reversing the 00:26 no-time choice): Woni's answer shows its
  // finish time small at the bubble's end, as Ryan's sent time sits beside his. A centered line marks a new day
  // or a message Woni started (that line already has its time, so the alert bubble has none).
  const clockFormat = new Intl.DateTimeFormat("ko-KR", { timeZone: "Asia/Seoul", hour: "numeric", minute: "2-digit", hour12: true });
  const dayKeyFormat = new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Seoul", year: "numeric", month: "2-digit", day: "2-digit" });
  const weekdayFormat = new Intl.DateTimeFormat("ko-KR", { timeZone: "Asia/Seoul", weekday: "long" });
  const shortWeekdayFormat = new Intl.DateTimeFormat("ko-KR", { timeZone: "Asia/Seoul", weekday: "short" });

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
  function finishedAt(chat) {
    const at = Number(chat.finishedAt) > 0 ? Number(chat.finishedAt) : Number(chat.createdAt);
    return Number.isFinite(at) ? at : 0;
  }
  function observeReplies(data) {
    const chats = Array.isArray(data.chats) ? data.chats : [];
    const first = seen === null, next = Object.create(null), visible = dialog.open && view === "chat" && !document.hidden;
    // With Woni's shared mark an answer read in the Woni app is not new here; older Woni servers keep the per-tab baseline.
    const shared = typeof data.readThrough === "number" && Number.isFinite(data.readThrough);
    if (shared) { serverRead = Math.max(serverRead, data.readThrough); readThrough = Math.max(readThrough ?? 0, data.readThrough); }
    if (shared) unread = new Set();
    let arrived = false;
    for (const chat of chats) {
      if (typeof chat.id !== "string" || !/^[A-Za-z0-9_-]{1,100}$/.test(chat.id)) continue;
      const complete = chat.status === "complete" && !!chat.answer && chat.kind !== "alert";
      const shown = visible && (!filter || chat.stream === filter.id);
      next[chat.id] = complete;
      if (shared) {
        if (complete && !shown && finishedAt(chat) > readThrough) {
          unread.add(chat.id);
          if (!first && seen[chat.id] !== true) arrived = true;
        }
        continue;
      }
      if (!first && complete && seen[chat.id] !== true) {
        if (!shown) { unread.add(chat.id); arrived = true; }
      }
      if (shown) unread.delete(chat.id);
    }
    for (const id of unread) if (!Object.prototype.hasOwnProperty.call(next, id)) unread.delete(id);
    seen = next; badge(); saveSeen();
    if (shared && visible) markVisibleRead();
    if (arrived && !document.hidden) services.toast?.("원이의 새 답변이 도착했어요. 원이 버튼에서 확인해 주세요.");
  }
  function markVisibleRead() {
    if (!latestData || document.hidden || view !== "chat") return;
    for (const chat of latestData.chats || []) if (!filter || chat.stream === filter.id) unread.delete(chat.id);
    badge(); saveSeen();
    if (readThrough === null) return;
    // Advance through answers in finish order; stop before an unread one hidden by a stream filter.
    let through = readThrough;
    const finished = (latestData.chats || []).filter((chat) => !["queued", "running"].includes(chat.status))
      .map((chat) => [finishedAt(chat), !filter || chat.stream === filter.id]).sort((a, b) => a[0] - b[0]);
    for (const [at, shown] of finished) { if (at <= through) continue; if (!shown) break; through = at; }
    readThrough = through;
    if (through > serverRead && through > readPosting) postRead(through);
  }
  async function postRead(through) {
    if (!api) return;
    const current = authEpoch; readPosting = through;
    try {
      const result = await api("/api/woni/read", { method: "POST", body: { through } });
      if (current === authEpoch && typeof result?.readThrough === "number") serverRead = Math.max(serverRead, result.readThrough);
    } catch (_) { /* the next check while the chat is open records it again */ }
    finally { if (readPosting === through) readPosting = 0; }
  }
  // Woni's 👉 line holds tap-to-send replies; the 🔊 line is for listening only. Neither is shown as answer text.
  function suggestedReplies(answer) {
    const line = String(answer || "").split("\n").find((item) => /^\s*👉/u.test(item));
    return line ? line.replace(/^\s*👉\uFE0F?/u, "").split("|").map((item) => item.trim()).filter(Boolean).slice(0, 3) : [];
  }
  function visibleAnswer(answer) {
    return String(answer || "").replace(/^[ \t]*(?:👉|🔊).*$/gmu, "").replace(/\n{3,}/g, "\n\n").trim();
  }
  // target: the Woni answer being replied to. A suggestion tapped while replying to that answer goes as a reply to it.
  async function sendSuggestion(text, target = null) {
    if (pending || sending || uploading) { status.textContent = "이전 메시지의 전송 여부를 먼저 확인해 주세요."; return; }
    const reply = target?.part === "answer" ? { id: target.id, part: "answer" } : null;
    savePending({ text, requestId: crypto.randomUUID(), ...(reply ? { replyTo: reply } : {}) });
    // The reply bar closes only when it was on this answer; a bar on another message stays for the composer.
    if (reply && replyTarget?.id === reply.id && replyTarget.part === "answer") replyBox.hidden = true;
    leaveStream();
    const result = sendPending(); messages.scrollTop = messages.scrollHeight;
    await result;
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
  function validSeconds(seconds) { return typeof seconds === "number" && Number.isFinite(seconds) && seconds > 0; }
  function clockLabel(seconds) { return validSeconds(seconds) ? clockFormat.format(new Date(seconds * 1000)) : ""; }
  function dayNumber(seconds) {
    const [y, m, d] = dayKeyFormat.format(new Date(seconds * 1000)).split("-").map(Number);
    return Math.round(Date.UTC(y, m - 1, d) / 86400000);
  }
  function dayLabel(seconds, now = Date.now() / 1000) {
    const date = new Date(seconds * 1000), days = dayNumber(now) - dayNumber(seconds);
    if (days <= 0) return "오늘";
    if (days === 1) return "어제";
    if (days < 7) return weekdayFormat.format(date);
    const [y, m, d] = dayKeyFormat.format(date).split("-").map(Number);
    const sameYear = String(y) === dayKeyFormat.format(new Date(now * 1000)).slice(0, 4);
    return `${sameYear ? "" : `${y}년 `}${m}월 ${d}일 (${shortWeekdayFormat.format(date)})`;
  }
  // Ryan 2026-11-11 08:10 ("답이 가장 최신으로 왔으면 … 위에 온 답변을 내가 놓치게 되잖아"): Woni works on several
  // asks at once, so the list is drawn in rows, not chats. Ryan's ask is a row at createdAt, Woni's finished
  // answer (or failure) a row at finishedAt, a Woni alert only an answer row. An answer right after its own ask
  // is drawn with it as now ('merged'); one with other rows between is 'detached': it sits at its own time
  // (usually the bottom) quoting the ask, and the ask gets a link down to it. A working chat has only its ask row.
  // chats: oldest first (Woni's list order). Rows: { kind: question|answer|merged, id, at, detached, key }.
  function chatRows(chats) {
    const list = (Array.isArray(chats) ? chats : []).filter((chat) => typeof chat?.id === "string" && chat.id);
    const bySeq = list.every((chat) => Number.isInteger(chat.seq));
    const rows = []; let last = 0;
    list.forEach((chat, index) => {
      const order = bySeq ? chat.seq : index, alert = chat.kind === "alert";
      const finished = !["queued", "running"].includes(chat.status) && (!!chat.answer || ["failed", "interrupted"].includes(chat.status));
      const created = Number(chat.createdAt), done = Number(chat.finishedAt);
      // A missing time is not 0 (that would lift the row to the top): the ask without one takes its answer's time,
      // else the row before it; an answer is never before its ask.
      const asked = validSeconds(created) ? created : finished && validSeconds(done) ? done : last;
      if (!alert) rows.push({ kind: "question", id: chat.id, at: asked, order, part: 0 });
      if (alert || finished) rows.push({ kind: "answer", id: chat.id, at: Math.max(validSeconds(done) ? done : asked, asked), order, part: 1 });
      last = Math.max(last, asked);
    });
    // Time, then Woni's order, then a chat's own ask before its answer: the same list always gives the same rows.
    rows.sort((a, b) => a.at - b.at || a.order - b.order || a.part - b.part);
    const out = [];
    for (const row of rows) {
      const before = out.at(-1), own = row.kind === "answer" && before?.id === row.id && before.kind === "question";
      if (own) { before.kind = "merged"; before.key = `${row.id}:merged`; continue; }
      out.push({ kind: row.kind, id: row.id, at: row.at, detached: false, key: `${row.id}:${row.kind}` });
    }
    // Both halves of an ask whose answer went below other rows are marked: the ask shows the link, the answer the quote.
    const asks = new Map(out.filter((row) => row.kind === "question").map((row) => [row.id, row]));
    for (const row of out) if (row.kind === "answer" && asks.has(row.id)) { row.detached = true; asks.get(row.id).detached = true; }
    return out;
  }
  // The row a jump lands on: the ask (an alert has only its answer), or the answer when asked for and apart.
  function rowFor(rows, id, part = "question") {
    const own = rows.filter((row) => row.id === id);
    return own.find((row) => row.kind === part || row.kind === "merged") || own[0] || null;
  }
  // The bottom answer row (the answer that finished last) of the rows on screen carries the 👉 suggestions
  // (a stream shows its own bottom answer's, as in the Ryan One app).
  function latestRowId(rows) {
    return rows.filter((row) => row.kind !== "question").at(-1)?.id || null;
  }
  function latestAnswerId(chats) { return latestRowId(chatRows(chats)); }
  // Row key -> centered line: the first row of a day shows the day, a message Woni started shows its time.
  function timeDividers(rows, chats, now = Date.now() / 1000) {
    const out = new Map(); let lastDay = null;
    for (const row of rows) {
      const at = row.at;
      if (!validSeconds(at)) continue;
      const day = dayNumber(at), newDay = day !== lastDay, alert = chats.get(row.id)?.kind === "alert";
      lastDay = day;
      if (alert) out.set(row.key, `${newDay ? dayLabel(at, now) + " " : ""}${clockLabel(at)} · 먼저 알림`);
      else if (newDay) out.set(row.key, dayLabel(at, now));
    }
    return out;
  }
  // Woni's answer time: the clock only (a day line above already names the day). An answer finished on another
  // day than its question (the day line follows the question) also says that day, as in the app: '11/9 오후 9:31'.
  function answerClock(chat) {
    const at = Number(chat?.finishedAt);
    if (!chat || chat.kind === "alert" || !chat.answer || !validSeconds(at)) return "";
    const asked = Number(chat.createdAt), clock = clockLabel(at);
    if (!validSeconds(asked) || dayNumber(asked) === dayNumber(at)) return clock;
    const [, m, d] = dayKeyFormat.format(new Date(at * 1000)).split("-").map(Number);
    return `${m}/${d} ${clock}`;
  }
  // Ryan 2026-11-10 ("토스트가 채팅창을 가려서 거슬림 … 더 빨리"): 2.5 seconds, 3.5 for more than 40 characters,
  // 5 when the notice offers an undo (none does in this window yet; the app's do).
  function toastMs(text, undo = false) {
    if (undo) return 5000;
    return [...String(text || "")].length > 40 ? 3500 : 2500;
  }
  // The board's toast sits under the modal window, so while it is open the same words also show inside it,
  // just above the composer (it moves up as the composer grows) instead of over the chat's last lines.
  let noticeTimer = 0;
  function notify(text) {
    services.toast?.(text);
    if (!dialog.open) return;
    const node = $("woni-notice"); node.textContent = text; node.hidden = false;
    clearTimeout(noticeTimer); noticeTimer = setTimeout(() => { node.hidden = true; }, toastMs(text));
  }
  async function copyText(text) {
    try { await navigator.clipboard.writeText(text); notify("복사했어요."); }
    catch (_) { notify("복사하지 못했어요. 글자를 길게 눌러 선택해 주세요."); }
  }
  function setView(value) {
    view = value; const isChat = value === "chat";
    messages.hidden = !isChat; form.hidden = !isChat; work.hidden = value !== "work";
    tasksBox.hidden = value !== "tasks"; notesBox.hidden = value !== "notes"; findBox.hidden = value !== "find";
    filterBox.hidden = !isChat || !filter;
    for (const [tab, name] of [[chatTab, "chat"], [tasksTab, "tasks"], [notesTab, "notes"], [workTab, "work"], [findOpen, "find"]]) tab.setAttribute("aria-pressed", String(value === name));
    original.hidden = true;
    if (isChat && dialog.open) markVisibleRead();
    if (value === "work") { renderWork(); clearTimeout(timer); refresh(); }
    if (value === "tasks") { renderTasks(); loadTasks(); }
    if (value === "notes") { renderNotes(); if (!notes && !notesLoading) loadNotes(); }
    if (value === "find") { renderFind(); loadStreams(); }
    renderSide();
  }
  function setReply(chat, part) {
    if (pending) { status.textContent = "이전 메시지의 전송 여부를 먼저 확인해 주세요."; return; }
    // The reply bar quotes the answer as shown: the 👉 and 🔊 lines are left out (the 👉 ones come back as buttons below it).
    const answer = part === "answer";
    replyTarget = { id: chat.id, part, excerpt: (answer ? visibleAnswer(chat.answer) || chat.answer : chat.text) || "",
      suggestions: answer ? suggestedReplies(chat.answer) : [] };
    replyBox.hidden = false;
    $("woni-reply-label").textContent = `${answer ? "원이 답변" : "라이언 메시지"}에 답글 · ${replyTarget.excerpt}`;
    renderReplyPicks();
    setView("chat"); input.focus?.();
  }
  // Ryan 2026-11-10: "이전 답변에 답글 남길 때도 추천답변 뜨게 해줘" (same as Ryan One app 1.38.1).
  // Replying to an earlier Woni answer shows its 👉 suggestions under the reply bar; a tap sends one as a reply to it.
  // The latest answer already has them under its bubble, so they are not repeated here.
  function renderReplyPicks() {
    const target = replyTarget, latest = shownRows.length ? latestRowId(shownRows) : latestAnswerId(latestData?.chats);
    const picks = target?.part === "answer" && !pending && !replyBox.hidden && target.id !== latest ? target.suggestions || [] : [];
    replyPicks.replaceChildren(...picks.map((text) => action(text, () => sendSuggestion(text, target))));
    replyPicks.hidden = !picks.length;
  }
  async function showOriginal(id) {
    const current = epoch;
    original.hidden = false; original.replaceChildren(el("p", "", "원문을 불러오고 있어요."));
    try {
      const chat = loadedChats().find((item) => item.id === id) || await api(`/api/woni/chat?${new URLSearchParams({ id })}`);
      if (current !== epoch || !dialog.open) return;
      const block = el("div", "", [chat.text, chat.answer].filter(Boolean).join("\n\n"));
      original.replaceChildren(el("strong", "", "원문"), block, action("답글 달기", () => setReply(chat, chat.answer ? "answer" : "question")), action("닫기", () => { original.hidden = true; }));
    } catch (_) { if (current === epoch && dialog.open) original.replaceChildren(el("p", "", "원문을 불러오지 못했어요."), action("닫기", () => { original.hidden = true; })); }
  }
  // The ask's first line for a detached answer's quote (the line is cut to one row by style.css).
  function firstLine(chat) {
    const line = String(chat?.text || "").split("\n").map((item) => item.trim()).find(Boolean);
    return line || (chat?.attachments?.length ? "첨부 파일" : "질문");
  }
  // Under an ask whose answer is further down: '답은 아래에 · 오후 8:32 ↓', a tap goes to that answer.
  function answerLink(chat) {
    const at = answerClock(chat) || clockLabel(Number(chat.finishedAt));
    const link = el("button", "woni-answer-link", `답은 아래에${at ? ` · ${at}` : ""} ↓`);
    link.type = "button"; link.setAttribute("aria-label", "답으로 이동");
    link.addEventListener("click", () => jumpTo(chat.id, "answer", true));
    return link;
  }
  // tagFor: the chat whose stream name goes under this Woni bubble (also under a working or failed one, which has no menu).
  // askOf: a detached answer's chat, quoted above the bubble. link: the line under an ask whose answer went below.
  function row(role, text, files = [], chat = null, tagFor = null, { askOf = null, link = null } = {}) {
    // The speaker name stays for screen readers only; the bubble side and colour show who is talking.
    const article = el("article", `woni-message ${role}`), label = el("span", "sr-only", role === "user" ? "라이언" : "원이");
    const content = el("div", "woni-message-body");
    if (role === "user" && chat?.replyTo) {
      const quote = el("button", "woni-message-quote", `↩ ${chat.replyTo.who || "원문"} · ${chat.replyTo.excerpt || "원문 보기"}`);
      quote.type = "button"; quote.addEventListener("click", () => showOriginal(chat.replyTo.id));
      article.append(quote);
    }
    if (askOf) {
      // The same quote as a reply's, one line: it goes back up to the ask this answer belongs to.
      const quote = el("button", "woni-message-quote is-ask", `↩ 라이언 · ${firstLine(askOf)}`);
      quote.type = "button"; quote.setAttribute("aria-label", "원래 질문으로 이동");
      quote.addEventListener("click", () => jumpTo(askOf.id, "question", true));
      article.append(quote);
    }
    if (role === "assistant" && typeof MessageFormat !== "undefined") MessageFormat.render(content, text);
    else content.textContent = text;
    article.append(label);
    const sent = role === "user" ? clockLabel(Number(chat?.createdAt)) : "";
    if (sent) {
      const line = el("div", "woni-message-line"), time = el("time", "woni-sent-time", sent);
      time.dateTime = new Date(Number(chat.createdAt) * 1000).toISOString(); time.setAttribute("aria-label", `보낸 시각 ${sent}`);
      line.append(time, content); article.append(line);
    } else article.append(content);
    if (link) article.append(link);
    const answered = role === "assistant" ? answerClock(chat) : "";
    if (answered) {
      const time = el("time", "woni-answer-time", answered);
      time.dateTime = new Date(Number(chat.finishedAt) * 1000).toISOString(); time.setAttribute("aria-label", `답한 시각 ${answered}`);
      article.append(time);
    }
    const tag = tagFor && streamTag(tagFor);
    if (tag) article.append(tag);
    if (files.length) {
      const list = el("div", "woni-message-files");
      for (const file of files) list.append(el("span", "woni-message-file", file.name || "첨부 파일"));
      article.append(list);
    }
    if (chat?.id) {
      // Reply, copy, stream and the answer time are one tap away instead of under every message.
      const key = `${chat.id}:${role}`, part = role === "user" ? "question" : "answer";
      const actions = el("div", "woni-message-actions");
      actions.append(action("답글", () => { menuKey = null; setReply(chat, part); }));
      actions.append(action("복사", () => { copyText(role === "user" ? chat.text || "" : visibleAnswer(chat.answer) || chat.answer || ""); }));
      if (chat.stream && chat.streamTitle) actions.append(action(`이 흐름만 보기 · ${chat.streamTitle}`, () => openStream(chat.stream, chat.streamTitle)));
      if (role === "assistant" && chat.answer) {
        const time = answerTime(chat.finishedAt);
        if (time) actions.append(time);
      }
      article.append(actions);
      if (menuKey === key) article.className += " is-open";
      content.setAttribute("aria-expanded", String(menuKey === key));
      article.addEventListener("click", (event) => {
        if (event?.target?.closest?.("a, button")) return;
        if (globalThis.getSelection?.()?.toString()) return;  // dragging to select text is not a tap
        menuKey = menuKey === key ? null : key;
        if (latestData) show(latestData);
      });
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
        if (item.id) card.append(action("대화에서 이어보기 →", () => { leaveStream(); jumpTo(item.id); }));
        nodes.push(card);
      }
    }
    work.replaceChildren(...nodes);
  }

  // Ryan 2026-11-10: "할일, 시킨일 등. 기록 노트 원문 정도는 볼 필요도 있고" — the Ryan One app's to-do and record
  // screens inside this window. Rules: docs/plan/2026-11-10-woni-panels.md. Inputs and switches stay in place
  // across refreshes; only rows that changed are drawn again.
  const tasksBox = $("woni-tasks"), notesBox = $("woni-notes"), tasksTab = $("woni-tasks-tab"), notesTab = $("woni-notes-tab");
  const tasksBadge = $("woni-tasks-badge");
  const taskRequestsKey = "sessionholic:woni:task-requests";
  const taskKinds = [["mine", "내가 할 일"], ["delegated", "내가 맡긴 일"], ["unassigned", "담당 확인"]];
  const emptyTaskHint = { mine: "라이언이 직접 실행하는 일이 모여요.", delegated: "다른 사람이 실행하고 라이언이 확인하는 일이 모여요.", unassigned: "담당자를 확정할 근거가 부족한 일이 모여요." };
  const uncertainTask = "저장 여부를 확인하지 못했어요. 다시 누르면 같은 요청으로 확인해요.";
  let tasks = null, tasksError = "", tasksAt = 0, tasksGen = 0, taskKind = "mine", taskFilter = "open", taskId = null;
  let taskListScroll = 0, taskRowsKey = "", taskDetailKey = "";
  const taskSaving = new Map();
  let notes = null, notesQuery = "", notesError = "", notesLoading = false, notesGen = 0, notesTimer = 0, notesScroll = 0;
  let note = null, noteBodyText = null, passagesTimer = 0;

  function serverMessage(error, fallback) {
    // A 4xx carries the server's own reason (state changed elsewhere, a Slack record has no transcript);
    // no status means the connection failed and nothing is known.
    return error?.status >= 400 && error.status < 500 && error.message ? error.message : fallback;
  }
  function button(label, className, fn) { const node = action(label, fn); node.className = className; return node; }
  function searchField(label, onInput) {
    const box = el("label", "woni-search"), input = el("input");
    input.type = "search"; input.placeholder = label; input.autocomplete = "off"; input.maxLength = 300;
    input.setAttribute("aria-label", label); input.setAttribute("enterkeyhint", "search");
    input.addEventListener("input", onInput);
    box.append(input); return { box, input };
  }
  function toggle(label, pressed, fn, count) {
    const node = action("", fn); node.setAttribute("aria-pressed", String(pressed));
    node.append(el("span", "", label));
    if (count != null) node.append(el("span", "woni-count", String(count)));
    return node;
  }
  function clock(seconds) {
    if (typeof seconds !== "number" || !Number.isFinite(seconds) || seconds < 0) return "";
    const s = Math.floor(seconds), h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), pad = (n) => String(n).padStart(2, "0");
    return h ? `${h}:${pad(m)}:${pad(s % 60)}` : `${m}:${pad(s % 60)}`;
  }

  const taskKindBox = el("div", "woni-segment woni-kinds"), taskViewBox = el("div", "woni-chips");
  taskKindBox.setAttribute("role", "group"); taskKindBox.setAttribute("aria-label", "할 일 종류");
  taskViewBox.setAttribute("role", "group"); taskViewBox.setAttribute("aria-label", "할 일 보기");
  const taskSearch = searchField("할 일·담당자·프로젝트 찾기", () => renderTasks());
  taskSearch.input.placeholder = "할 일 찾기";  // the full hint stays as its label; a phone row has room for a short one
  const taskTools = el("div", "woni-tools"), taskHint = el("p", "woni-note-hint"), taskRows = el("div", "woni-task-list");
  const taskList = el("div", "woni-pane"), taskDetail = el("div", "woni-pane woni-detail");
  // The switches are made once and only change state, so a keyboard user keeps focus on the one pressed.
  const kindButtons = taskKinds.map(([kind, label]) => toggle(label, kind === taskKind, () => { taskKind = kind; renderTasks(); }, 0));
  const viewButtons = [["open", "남은 일"], ["done", "완료"], ["archived", "보관"]].map(([value, label]) => {
    const node = toggle(label, value === taskFilter, () => { taskFilter = value; renderTasks(); }); node.dataset.view = value; return node;
  });
  taskKindBox.append(...kindButtons); taskViewBox.append(...viewButtons);
  taskTools.append(taskViewBox, taskSearch.box);
  taskList.append(taskKindBox, taskTools, taskHint, taskRows);
  taskDetail.hidden = true;
  tasksBox.append(taskList, taskDetail);

  function allTasks() { return [...(tasks?.actions || []), ...(tasks?.completedActions || []), ...(tasks?.archivedActions || [])]; }
  function findTask(id) { return allTasks().find((item) => item.id === id) || null; }
  function kindOf(task) { return ["delegated", "unassigned"].includes(task.kind) ? task.kind : "mine"; }
  function isDone(task) { return (taskSaving.get(task.id) || task.taskStatus) === "done"; }
  function reopenStatus(task) { return kindOf(task) === "delegated" && task.previousStatus === "reported" ? "reported" : "open"; }
  function attentionClass(text) { return String(text || "").includes("확인 필요") ? " is-attention" : ""; }
  function taskBadge() {
    const counts = tasks?.counts || {}, count = (Number(counts.due) || 0) + (Number(counts.followup) || 0);
    tasksBadge.hidden = !(count > 0); tasksBadge.textContent = count > 99 ? "99+" : String(count);
    tasksBadge.setAttribute("aria-hidden", "true");
    tasksTab.setAttribute("aria-label", count > 0 ? `할 일, 확인 필요 ${count}개` : "할 일");
  }
  function shownTasks() {
    const list = (taskFilter === "done" ? tasks?.completedActions : taskFilter === "archived" ? tasks?.archivedActions : tasks?.actions) || [];
    const query = taskSearch.input.value.trim().toLowerCase();
    // The archive mixes every kind (as in the app); the other views follow the kind switch.
    return list.filter((task) => taskFilter === "archived" || kindOf(task) === taskKind)
      .filter((task) => !query || ["title", "project", "recordTitle", "assignee"].some((key) => String(task[key] || "").toLowerCase().includes(query)));
  }
  function taskLines(task) {
    const kind = kindOf(task), saving = taskSaving.has(task.id), attention = String(task.attention || ""), lines = [];
    if (kind === "delegated") lines.push([`woni-task-line${attentionClass(attention)}`, `담당: ${task.assignee || "담당 미정"}${attention ? ` · ${attention}` : ""}`]);
    else if (kind === "unassigned" && task.guessBasis) lines.push(["woni-task-line is-guess", attention.replace(/^담당 확인 · /, "")]);
    if (isDone(task) && !saving && task.autoBasis?.quote) lines.push(["woni-task-auto", `원이 확인 · “${task.autoBasis.quote}”`]);
    lines.push(saving ? ["woni-task-meta is-saving", "저장 중…"] : ["woni-task-meta", [task.project, task.due].filter(Boolean).join(" · ")]);
    return lines.filter(([, text]) => text);
  }
  function taskRow(task) {
    const kind = kindOf(task), done = isDone(task), saving = taskSaving.has(task.id), title = task.title || "제목 없는 할 일";
    const row = el("div", `woni-task${done ? " is-done" : ""}`); row.dataset.taskId = task.id;
    const check = action("", () => setTaskStatus(task, done ? reopenStatus(task) : "done"));
    // Ryan One 1.40.1: a to-do whose owner is unknown can be checked too (finished, or not needed).
    check.className = "woni-check"; check.disabled = saving;
    check.setAttribute("aria-pressed", String(done));
    check.setAttribute("aria-label", done ? `완료 취소: ${title}` : kind === "delegated" ? `결과 확인 완료: ${title}` : `완료 처리: ${title}`);
    const open = button("", "woni-task-open", () => openTask(task.id));
    open.append(el("span", "woni-task-title", title));
    for (const [className, text] of taskLines(task)) open.append(el("span", className, text));
    row.append(check, open); return row;
  }
  function renderTasks() {
    const counts = tasks?.counts || {};
    taskBadge();
    kindButtons.forEach((node, index) => {
      const kind = taskKinds[index][0];
      node.setAttribute("aria-pressed", String(taskKind === kind)); node.children[1].textContent = String(Number(counts[kind]) || 0);
    });
    for (const node of viewButtons) node.setAttribute("aria-pressed", String(taskFilter === node.dataset.view));
    const archive = viewButtons[2];
    archive.hidden = !(Number(counts.archived) > 0 || taskFilter === "archived"); archive.children[0].textContent = `보관 ${Number(counts.archived) || 0}`;
    const rows = shownTasks();
    taskHint.textContent = taskFilter === "archived" ? "녹음한 지 14일이 넘은 일, 담당 확인에서 며칠 지난 일이라 목록과 알림에서 뺐어요(마감이 남은 일은 그대로 둬요). 아직 필요한 일이면 앱에서 열어 '담당 구분 수정'으로 꺼내 주세요."
      : taskFilter === "open" && taskKind === "unassigned" && rows.length ? "누가 할지 모르는 일이에요. 라이언 일로 보이는 것부터 보여줘요. 끝났거나 필요 없으면 체크하세요. 그냥 두면 모르는 일은 3일, 라이언 일로 보이는 일은 7일 뒤 보관으로 옮겨요."
      : taskFilter === "open" && taskKind === "delegated" ? `확인 필요 ${Number(counts.followup) || 0}개 · 완료 보고와 마감이 된 일을 먼저 보여줘요. 홀릭이·뷰링이 같은 에이전트에게 시킨 일은 넣지 않아요.`
      : taskFilter === "done" && Number(counts.autoDone) > 0 ? "'원이 확인' 표시는 끝낸 근거(Slack 글·보낸 메일·원이가 한 일)를 원이가 확인해 체크한 일이에요. 틀리면 열어서 되돌려 주세요." : "";
    taskHint.hidden = !tasks || !taskHint.textContent;
    // Redraw rows only when what they show changed, so a 30-second refresh keeps focus and scroll.
    const key = JSON.stringify([!!tasks, tasksError, tasks?.error || "", taskKind, taskFilter, taskSearch.input.value, [...taskSaving], rows]);
    if (key !== taskRowsKey) {
      taskRowsKey = key;
      const nodes = [];
      if (!tasks) nodes.push(el("p", tasksError ? "woni-note-hint is-error" : "woni-note-hint", tasksError || "할 일을 불러오고 있어요."));
      else {
        if (tasksError) nodes.push(el("p", "woni-note-hint is-error", "연결을 확인하지 못해 이전 할 일을 보여 줍니다."));
        if (tasks.error) nodes.push(el("p", "woni-note-hint is-error", tasks.error));
        if (!rows.length) {
          const empty = el("div", "woni-empty");
          empty.append(el("strong", "", taskSearch.input.value.trim() ? "찾는 할 일이 없어요" : "표시할 일이 없어요"),
            el("p", "", taskFilter === "archived" ? "보관한 일이 없어요." : emptyTaskHint[taskKind]));
          nodes.push(empty);
        }
        nodes.push(...rows.map(taskRow));
      }
      const focus = focusedIn(taskRows, "[data-task-id]");
      taskRows.replaceChildren(...nodes);
      if (focus) taskRows.querySelector?.(`[data-task-id="${CSS.escape(focus.key)}"] .${focus.className}`)?.focus?.({ preventScroll: true });
    }
    if (taskId) renderTaskDetail();
  }
  // Which row control had focus before a redraw: the same control of the same row gets it back.
  function focusedIn(box, rowSelector) {
    const active = globalThis.document?.activeElement, row = active?.closest?.(rowSelector);
    if (!row || !box.contains?.(row)) return null;
    return { key: row.dataset.taskId, className: String(active.className).split(" ")[0] };
  }
  function openTask(id) {
    taskListScroll = tasksBox.scrollTop || 0; taskId = id; taskDetailKey = "";
    taskList.hidden = true; taskDetail.hidden = false;
    renderTaskDetail(); tasksBox.scrollTop = 0;
    taskDetail.children[0]?.focus?.({ preventScroll: true });
  }
  function closeTask() {
    const id = taskId; taskId = null; taskDetailKey = "";
    taskDetail.hidden = true; taskDetail.replaceChildren(); taskList.hidden = false;
    renderTasks(); tasksBox.scrollTop = taskListScroll;
    tasksBox.querySelector?.(`[data-task-id="${CSS.escape(id || "")}"] .woni-task-open`)?.focus?.({ preventScroll: true });
  }
  function renderTaskDetail() {
    const task = findTask(taskId), saving = task ? taskSaving.has(task.id) : false;
    const key = JSON.stringify([task, saving, taskSaving.get(taskId) || ""]);
    if (key === taskDetailKey) return;
    taskDetailKey = key;
    const nodes = [button("할 일", "woni-back", closeTask)];
    if (!task) {
      nodes.push(el("p", "woni-note-hint", tasks ? "목록이 바뀌어 이 할 일을 찾지 못했어요. 뒤로 가서 다시 열어 주세요." : "할 일을 불러오고 있어요."));
      taskDetail.replaceChildren(...nodes); return;
    }
    const kind = kindOf(task), done = isDone(task), attention = String(task.attention || "");
    nodes.push(el("h3", "woni-detail-title", task.title || "제목 없는 할 일"),
      el("p", "woni-detail-meta", [task.project, task.due, done ? "완료" : "남은 일"].filter(Boolean).join(" · ")));
    if (done && task.autoBasis?.quote) {
      const box = el("section", "woni-auto");
      box.append(el("strong", "", "원이가 끝낸 근거를 확인해 완료로 체크했어요"), el("p", "woni-quote", `“${task.autoBasis.quote}”`));
      if (task.autoBasis.source) box.append(el("p", "woni-note-hint", task.autoBasis.source));
      box.append(el("p", "woni-note-hint", "틀렸으면 아래 '완료 되돌리기'를 눌러 주세요. 되돌린 일은 다시 자동으로 체크하지 않아요."));
      nodes.push(box);
    }
    const actions = el("div", "woni-detail-actions");
    if (kind === "delegated") {
      const group = el("section", "woni-group");
      group.append(el("p", "woni-group-title", `담당: ${task.assignee || "담당 미정"}`));
      if (attention) group.append(el("p", `woni-note-hint${attentionClass(attention)}`, attention));
      if (task.reportQuote) group.append(el("p", "", `완료 보고 근거: ${task.reportQuote}`));
      nodes.push(group);
    }
    if (kind === "unassigned" && task.guessBasis) {
      const group = el("section", "woni-group");
      group.append(el("p", "woni-group-title", attention.replace(/^담당 확인 · /, "") || "담당 확인"),
        el("p", "woni-note-hint", `추정 근거: ${task.guessBasis}`), el("p", "woni-note-hint", "담당 구분은 Ryan One 앱에서 확정할 수 있어요."));
      nodes.push(group);
    }
    const main = button(saving ? "저장 중…" : done ? "완료 되돌리기" : kind === "delegated" ? "결과 확인 완료" : "완료로 표시",
      done ? "secondary" : "primary", () => setTaskStatus(task, done ? reopenStatus(task) : "done"));
    main.disabled = saving; actions.append(main);
    if (kind === "delegated" && !done && task.taskStatus === "open") {
      const reported = button("완료 보고 받음", "secondary", () => setTaskStatus(task, "reported")); reported.disabled = saving; actions.append(reported);
    }
    if (task.taskStatus === "reported") {
      const wait = button("다시 기다리는 중으로", "quiet", () => setTaskStatus(task, "open")); wait.disabled = saving; actions.append(wait);
    }
    if (actions.children.length) nodes.push(actions);
    const basis = el("section", "woni-group");
    basis.append(el("h4", "woni-group-heading", "근거"));
    if (task.quote) basis.append(el("p", "woni-quote", `“${task.quote}”`));
    if (task.requestQuote) basis.append(el("p", "", `요청 근거: ${task.requestQuote}`));
    const source = [task.recordTitle, task.recordedAt, task.counterpart].filter(Boolean).join(" · ");
    if (source) basis.append(el("p", "woni-note-hint", source));
    if (task.sourceId) basis.append(button("노트 읽기", "secondary woni-wide", () => openNote(task.sourceId, "tasks")));
    if (basis.children.length > 1) nodes.push(basis);
    const controls = () => Array.from(taskDetail.querySelectorAll?.("button") || []);
    const active = globalThis.document?.activeElement, at = taskDetail.contains?.(active) ? controls().indexOf(active) : -1;
    taskDetail.replaceChildren(...nodes);
    if (at >= 0) (controls()[at] || controls()[0])?.focus?.({ preventScroll: true });
  }
  function taskRequests() {
    try { const value = JSON.parse(sessionStorage.getItem(taskRequestsKey) || "{}"); return value && typeof value === "object" && !Array.isArray(value) ? value : {}; }
    catch (_) { return {}; }
  }
  function saveTaskRequests(value) { try { sessionStorage.setItem(taskRequestsKey, JSON.stringify(value)); } catch (_) {} }
  // The app's withTaskStatus: move the item between lists at once; the next read decides the rest.
  function withTaskStatus(data, id, status, event) {
    if (!data || !["open", "reported", "done"].includes(status)) return data;
    const open = data.actions || [], done = data.completedActions || [], archived = data.archivedActions || [];
    const stored = archived.find((item) => item.id === id);
    if (stored && status !== "done") return data;
    const target = [...open, ...done].find((item) => item.id === id) || stored;
    if (!target) return data;
    const updated = { ...target, taskStatus: status, stateEvent: event, previousStatus: target.taskStatus || "open" };
    const restOpen = open.filter((item) => item.id !== id), restDone = done.filter((item) => item.id !== id);
    const counts = { ...(data.counts || {}) }, kind = kindOf(target), isOpen = status !== "done";
    counts[kind] = (Number(counts[kind]) || 0) - (restOpen.length !== open.length ? 1 : 0) + (isOpen ? 1 : 0);
    counts.completed = (Number(counts.completed) || 0) - (restDone.length !== done.length ? 1 : 0) + (isOpen ? 0 : 1);
    if (stored) counts.archived = Math.max(0, (Number(counts.archived) || 0) - 1);
    return { ...data, counts, actions: isOpen ? [updated, ...restOpen] : restOpen, completedActions: isOpen ? restDone : [updated, ...restDone],
      archivedActions: archived.filter((item) => item.id !== id) };
  }
  function statusNotice(task, status) {
    const title = `‘${String(task.title || "").slice(0, 24)}’`;
    if (status === "done") return kindOf(task) === "delegated" ? `${title} 결과 확인 완료` : `${title} 완료`;
    return status === "reported" ? `${title} 완료 보고 · 결과 확인 필요` : `${title} 남은 일로 되돌렸어요`;
  }
  async function setTaskStatus(task, status) {
    if (!api || taskSaving.has(task.id)) return;
    const body = { actionId: task.id, status, expectedEvent: typeof task.stateEvent === "string" ? task.stateEvent : "" };
    // The same change pressed again (after an unknown result) reuses its request ID, so Woni returns the first outcome.
    const payload = JSON.stringify(body), requests = taskRequests(), saved = requests[task.id];
    const requestId = saved?.payload === payload && typeof saved.id === "string" ? saved.id : crypto.randomUUID();
    requests[task.id] = { payload, id: requestId }; saveTaskRequests(requests);
    const current = authEpoch; tasksGen++;  // a list read that left before this change must not undo it
    taskSaving.set(task.id, status); renderTasks();
    let notice = "";
    try {
      const result = await api("/api/woni/task-status", { method: "POST", body: { ...body, requestId } });
      if (current !== authEpoch) return;
      if (!["saved", "unconfirmed"].includes(result?.result)) throw new Error("unknown result");
      const left = taskRequests(); delete left[task.id]; saveTaskRequests(left);
      if (result.result === "unconfirmed") notice = "이전 변경은 저장을 확인하지 못했어요. 현재 상태를 불러왔으니 필요하면 다시 눌러 주세요.";
      else {
        tasks = withTaskStatus(tasks, task.id, result.taskStatus, typeof result.stateEvent === "string" ? result.stateEvent : "");
        notice = statusNotice(task, result.taskStatus);
      }
    } catch (error) {
      if (current !== authEpoch) return;
      // 409 is the board's "outcome unknown" (Woni unreachable or no readable answer): never a verdict on the save.
      notice = error?.status === 409 ? uncertainTask : serverMessage(error, uncertainTask);
    } finally {
      if (current === authEpoch) { taskSaving.delete(task.id); renderTasks(); loadTasks(); }
    }
    if (notice) notify(notice);
  }
  async function loadTasks() {
    if (!api || !enabled) return;
    const current = authEpoch, generation = ++tasksGen; tasksAt = Date.now();
    try {
      const data = await api("/api/woni/tasks");
      if (current !== authEpoch || generation !== tasksGen) return;
      if (!Array.isArray(data?.actions)) throw new Error("not a task list");
      tasks = data; tasksError = "";
    } catch (_) {
      if (current !== authEpoch || generation !== tasksGen) return;
      tasksError = "할 일을 불러오지 못했어요. 잠시 후 다시 확인해 주세요.";
    }
    renderTasks(); renderSide();
  }

  const noteSearch = searchField("이름·원문·요약으로 찾기", () => { clearTimeout(notesTimer); notesTimer = setTimeout(() => loadNotes(), 250); });
  const noteCount = el("p", "woni-note-hint"), noteCards = el("div", "woni-note-list"), noteMore = button("노트 더 보기", "secondary woni-wide", () => loadNotes(true));
  const noteList = el("div", "woni-pane"), noteView = el("div", "woni-pane woni-detail");
  noteList.append(noteSearch.box, noteCount, noteCards, noteMore);
  const noteBack = button("기록", "woni-back", closeNote), noteHead = el("header", "woni-note-head"), noteBody = el("div", "woni-note-body");
  const noteStatus = el("div", "woni-note-status"), noteContinue = button("이어 읽기", "secondary woni-wide", () => readNote(true));
  const transcriptOpen = button("녹취 원문 보기", "secondary woni-wide", () => { if (note) { note.transcript = true; readPassages(); } });
  const passagesBox = el("section", "woni-passages"), passageCaption = el("h4", "woni-group-heading");
  const passageSearch = searchField("이 기록의 원문 찾기", () => { clearTimeout(passagesTimer); passagesTimer = setTimeout(() => readPassages(), 250); });
  const passageList = el("div", "woni-passage-list"), passageStatus = el("div", "woni-note-status");
  const passageMore = button("원문 더 보기", "secondary woni-wide", () => readPassages(true));
  passagesBox.append(passageCaption, passageSearch.box, passageList, passageStatus, passageMore);
  noteView.append(noteBack, noteHead, noteBody, noteStatus, noteContinue, transcriptOpen, passagesBox);
  noteView.hidden = true;
  notesBox.append(noteList, noteView);

  function statusBlock(box, text, error, retry) {
    const nodes = text ? [el("p", error ? "woni-note-hint is-error" : "woni-note-hint", text)] : [];
    if (text && retry) nodes.push(button("다시 시도", "quiet", retry));
    box.replaceChildren(...nodes); box.hidden = !nodes.length;
    box.setAttribute("role", error ? "alert" : "status");
  }
  function renderNotes() {
    noteList.hidden = !!note; noteView.hidden = !note;
    if (note) { renderNote(); return; }
    const items = notes?.items || [];
    noteCount.textContent = items.length ? `정리 노트 ${Number.isFinite(Number(notes.total)) ? Number(notes.total) : items.length}개` : "";
    const nodes = [];
    if (notesError) {
      nodes.push(el("p", "woni-note-hint is-error", notesError), button("다시 시도", "quiet", () => loadNotes()));
    } else if (!notes) nodes.push(el("p", "woni-note-hint", "노트를 불러오고 있어요."));
    else {
      if (notes.error) nodes.push(el("p", "woni-note-hint is-error", notes.error));
      if (!items.length && !notesLoading) {
        const empty = el("div", "woni-empty");
        empty.append(el("strong", "", "찾는 노트가 없어요"), el("p", "", "녹음을 정리하면 이곳에서 바로 읽을 수 있어요."));
        nodes.push(empty);
      }
    }
    for (const item of items) {
      const card = button("", "woni-note-card", () => openNote(item.id, "notes"));
      if (item.recordedAt) card.append(el("span", "woni-note-date", String(item.recordedAt)));
      card.append(el("span", "woni-note-title", item.title || "제목 없는 기록"));
      if (item.preview) card.append(el("span", "woni-note-preview", item.preview));
      nodes.push(card);
    }
    noteCards.replaceChildren(...nodes);
    noteMore.hidden = notes?.nextOffset == null;
    noteMore.disabled = notesLoading; noteMore.textContent = notesLoading ? "불러오는 중…" : "노트 더 보기";
  }
  async function loadNotes(more = false) {
    if (!api) return;
    if (more && (notesLoading || notes?.nextOffset == null)) return;
    const current = authEpoch, generation = ++notesGen, previous = notes;
    if (!more) notesQuery = noteSearch.input.value.trim().slice(0, 300);
    notesLoading = true; notesError = ""; renderNotes();
    try {
      const query = new URLSearchParams({ q: notesQuery, offset: String(more ? previous.nextOffset : 0) });
      const data = await api(`/api/woni/notes?${query}`);
      if (current !== authEpoch || generation !== notesGen) return;
      if (!Array.isArray(data?.items)) throw new Error("not a note list");
      if (more) { const ids = new Set(previous.items.map((item) => item.id)); data.items = [...previous.items, ...data.items.filter((item) => !ids.has(item.id))]; }
      notes = data;
    } catch (error) {
      if (current !== authEpoch || generation !== notesGen) return;
      notesError = serverMessage(error, "노트를 불러오지 못했어요. 연결을 확인하고 다시 시도해 주세요.");
    }
    notesLoading = false; renderNotes();
  }
  function openNote(id, back) {
    if (back === "notes") notesScroll = notesBox.scrollTop || 0;
    note = { id, back, data: null, error: "", loading: false, gen: 0, transcript: false, passages: null, passagesError: "", passagesLoading: false, passagesGen: 0, query: "" };
    noteBodyText = null; passageSearch.input.value = ""; clearTimeout(passagesTimer);
    if (view !== "notes") setView("notes"); else renderNotes();
    notesBox.scrollTop = 0; noteBack.focus?.({ preventScroll: true });
    readNote();
  }
  function closeNote() {
    const back = note?.back; note = null; noteBodyText = null; clearTimeout(passagesTimer);
    if (back === "tasks") { setView("tasks"); return; }
    renderNotes(); notesBox.scrollTop = notesScroll;
    if (!notes && !notesLoading) loadNotes();
  }
  function renderNote() {
    const data = note.data;
    noteBack.textContent = note.back === "tasks" ? "할 일" : "기록";
    const head = [];
    if (data) {
      head.push(el("h3", "woni-detail-title", data.title || "제목 없는 기록"));
      if (data.recordedAt) head.push(el("p", "woni-detail-meta", String(data.recordedAt)));
      if (typeof data.url === "string" && /^https:\/\//.test(data.url)) {
        const link = el("a", "woni-link", "Drive에서 열기"); link.href = data.url; link.target = "_blank"; link.rel = "noopener noreferrer";
        head.push(link);
      }
    }
    noteHead.replaceChildren(...head);
    const text = data ? String(data.text || "") : null;
    if (text !== noteBodyText) {
      noteBodyText = text;
      if (text == null) noteBody.replaceChildren();
      else if (typeof MessageFormat !== "undefined") MessageFormat.render(noteBody, text);  // the same renderer as Woni's answers
      else noteBody.textContent = text;
    }
    noteBody.hidden = !data || !text;
    statusBlock(noteStatus, note.loading ? (data ? "이어서 불러오고 있어요." : "노트를 불러오고 있어요.") : note.error || (data && !text ? "노트 본문이 비어 있어요." : ""),
      !!note.error && !note.loading, note.error ? () => readNote() : null);
    noteContinue.hidden = !data || data.nextOffset == null || !!note.error; noteContinue.disabled = note.loading;
    transcriptOpen.hidden = !data || note.transcript;
    passagesBox.hidden = !note.transcript;
    if (!note.transcript) return;
    const result = note.passages, rows = result?.passages || [];
    const total = Number(result?.total);
    passageCaption.textContent = `${result?.label || "녹취 원문"}${Number.isFinite(total) && result ? ` · ${note.query ? "찾은 구간" : "구간"} ${total}개` : ""}`;
    passageList.replaceChildren(...rows.map((passage) => {
      const row = el("article", "woni-passage"), meta = el("p", "woni-passage-meta", [passage.speaker || "화자 미상", clock(passage.start)].filter(Boolean).join(" · "));
      if (passage.transcriptDoubt) meta.append(el("span", "woni-doubt", "전사 불확실"));
      row.append(meta, el("p", "woni-passage-text", passage.text || ""));
      return row;
    }));
    const empty = result && !rows.length ? (note.query ? "해당 원문이 없어요. 검색어를 바꿔 보세요." : "이 기록에는 볼 수 있는 녹취 원문이 없어요.") : "";
    statusBlock(passageStatus, note.passagesLoading ? "원문을 불러오고 있어요." : note.passagesError || empty, !!note.passagesError && !note.passagesLoading);
    passageMore.hidden = !result || result.nextOffset == null || !!note.passagesError; passageMore.disabled = note.passagesLoading;
    // A record with no transcript at all (a Slack record) has nothing to search in.
    passageSearch.box.hidden = !result && !!note.passagesError && !note.passagesLoading;
  }
  async function readNote(more = false) {
    const target = note;
    if (!target || !api || (more && (target.loading || target.data?.nextOffset == null))) return;
    const current = authEpoch, generation = ++target.gen, previous = target.data;
    target.loading = true; target.error = ""; renderNotes();
    try {
      const query = new URLSearchParams({ id: target.id, offset: String(more ? previous.nextOffset : 0) });
      const data = await api(`/api/woni/note?${query}`);
      if (current !== authEpoch || note !== target || generation !== target.gen) return;
      if (more && data?.revision !== previous.revision) target.error = "읽는 사이 노트가 변경됐어요. 새로고침해 주세요.";
      else target.data = more ? { ...data, text: String(previous.text || "") + String(data?.text || "") } : data;
    } catch (error) {
      if (current !== authEpoch || note !== target || generation !== target.gen) return;
      target.error = serverMessage(error, "노트를 읽지 못했어요. 연결을 확인하고 다시 시도해 주세요.");
    }
    target.loading = false; renderNotes();
  }
  async function readPassages(more = false) {
    const target = note, previous = target?.passages;
    if (!target || !api || (more && (target.passagesLoading || previous?.nextOffset == null))) return;
    if (!more) target.query = passageSearch.input.value.trim().slice(0, 300);
    const current = authEpoch, generation = ++target.passagesGen;
    target.passagesLoading = true; target.passagesError = ""; renderNotes();
    try {
      const query = new URLSearchParams({ id: target.id, format: "passages", q: target.query, offset: String(more ? previous.nextOffset : 0) });
      const data = await api(`/api/woni/transcript?${query}`);
      if (current !== authEpoch || note !== target || generation !== target.passagesGen) return;
      if (!Array.isArray(data?.passages)) throw new Error("no passages");
      if (more && data.revision !== previous.revision) target.passagesError = "읽는 사이 원문이 변경됐어요. 다시 불러와 주세요.";
      else {
        if (more) { const ids = new Set(previous.passages.map((item) => item.id)); data.passages = [...previous.passages, ...data.passages.filter((item) => !ids.has(item.id))]; }
        target.passages = data;
      }
    } catch (error) {
      if (current !== authEpoch || note !== target || generation !== target.passagesGen) return;
      // A record without a transcript (Slack) answers 409 with its own message; it is shown as is.
      target.passagesError = serverMessage(error, "원문을 불러오지 못했어요. 연결 후 다시 시도해 주세요.");
    }
    target.passagesLoading = false; renderNotes();
  }
  function resetPanels() {
    tasks = null; tasksError = ""; tasksAt = 0; tasksGen++; taskKind = "mine"; taskFilter = "open"; taskId = null; taskSaving.clear();
    taskRowsKey = ""; taskDetailKey = ""; taskSearch.input.value = ""; taskDetail.hidden = true; taskDetail.replaceChildren(); taskList.hidden = false;
    notes = null; notesQuery = ""; notesError = ""; notesLoading = false; notesGen++; clearTimeout(notesTimer); clearTimeout(passagesTimer);
    note = null; noteBodyText = null; noteSearch.input.value = ""; passageSearch.input.value = ""; noteBody.replaceChildren();
    renderTasks(); renderNotes();
  }
  renderTasks(); renderNotes();

  // Earlier chats, streams and chat search (Ryan One 1.40.0, assistant/ChatHistory.kt and WoniModel.kt).
  // Parts fetched apart become one list: the same chat from a later part wins, and with seq on every chat they
  // follow Woni's list order (an older Woni server without seq keeps the order they arrived in).
  function mergeChats(...parts) {
    const byId = new Map();
    for (const part of parts) for (const chat of part || []) if (typeof chat?.id === "string" && chat.id) byId.set(chat.id, chat);
    const list = [...byId.values()];
    return list.every((chat) => Number.isInteger(chat.seq)) ? list.sort((a, b) => a.seq - b.seq) : list;
  }
  // Finished chats the new 60-chat window no longer holds (a working one comes back in its new state with a page).
  function slidOut(previous, fetched) {
    const first = fetched[0]?.seq;
    if (!Number.isInteger(first)) return [];
    const ids = new Set(fetched.map((chat) => chat.id));
    return previous.filter((chat) => Number.isInteger(chat.seq) && chat.seq < first && !ids.has(chat.id) && !["queued", "running"].includes(chat.status));
  }
  function mergedChats(windowChats) {
    return mergeChats(olderChats, filter && streamChatsFor === filter.id ? streamChats : [], windowChats);
  }
  function loadedChats() { return mergedChats(Array.isArray(latestData?.chats) ? latestData.chats : []); }
  // Whether Woni holds chats above the first one shown. An older Woni server (no chatsHasMore or seq) has none to give.
  function canLoadOlder() {
    if (filter) return streamChatsFor === filter.id && streamHasMore;
    const first = loadedChats()[0];
    return latestData?.chatsHasMore === true && Number.isInteger(first?.seq) && first.seq > 0;
  }
  // The line at the top of the list: loading, a failure with its retry, more to fetch, or the start of a stream.
  function olderRow(shown) {
    if (olderLoading) { const line = el("p", "woni-older", "이전 대화를 불러오고 있어요."); line.setAttribute("role", "status"); return [line]; }
    if (olderError) {
      const box = el("div", "woni-older is-error"), text = el("span", "", olderError);
      text.setAttribute("role", "alert");
      box.append(text, button("다시 시도", "quiet", () => { const retry = olderRetry || loadOlder; olderError = ""; olderRetry = null; retry(); }));
      return [box];
    }
    if (canLoadOlder()) return [button("이전 대화 더 보기", "quiet woni-older-more", () => loadOlder())];
    if (filter && streamChatsFor === filter.id && shown) return [el("p", "woni-divider", `이 흐름의 첫 대화 · 모두 ${shown}개`)];
    return [];
  }
  function redraw(keepPlace = false) {
    if (dialog.open && (latestData || pending)) show(latestData || { chats: [], activity: {} }, keepPlace);
  }
  async function loadOlder() {
    if (!api || olderLoading) return;
    const scope = filter, oldest = loadedChats().filter((chat) => !scope || chat.stream === scope.id)[0]?.id;
    if (!oldest) return;
    const query = new URLSearchParams(scope ? { stream: scope.id, before: oldest, limit: "100" } : { before: oldest, limit: "40" });
    const current = authEpoch, generation = ++olderGen;
    olderLoading = true; olderError = ""; olderRetry = null; redraw(true);
    try {
      const page = await api(`/api/woni/chats?${query}`);
      if (current !== authEpoch || generation !== olderGen) return;
      if (!Array.isArray(page?.chats)) throw new Error("not a chat page");
      if (!scope) olderChats = mergeChats(page.chats, olderChats);
      else if (streamChatsFor === scope.id) { streamChats = mergeChats(page.chats, streamChats); streamHasMore = page.hasMore === true; }
      olderLoading = false;
    } catch (error) {
      if (current !== authEpoch || generation !== olderGen) return;
      olderLoading = false; olderError = serverMessage(error, "이전 대화를 불러오지 못했어요.");
    }
    redraw(true);
  }
  // '# 흐름 이름' under Woni's answer (working and failed ones too); not on an alert, a chat with no stream, or inside a stream.
  function streamTag(chat) {
    if (filter || chat.kind === "alert" || !chat.stream || !chat.streamTitle) return null;
    const tag = action("", () => openStream(chat.stream, chat.streamTitle));
    tag.append(el("span", "", `# ${chat.streamTitle}`)); tag.className = "woni-stream-tag"; tag.setAttribute("aria-label", `흐름 ${chat.streamTitle}만 보기`);
    return tag;
  }
  // One stream from its first chat (the stream list, a stream name, '이 흐름만 보기'). from: where its back goes.
  // then: the chat to show once it has arrived. A stream name Woni would refuse is only filtered on this screen.
  async function openStream(slug, title, from = null, then = null) {
    if (typeof slug !== "string" || !slug) return;
    menuKey = null; original.hidden = true;
    filter = { id: slug, title: title || "작업 흐름" }; streamBackTo = from;
    const known = streamPattern.test(slug);
    if (streamChatsFor !== slug) { streamChats = []; streamHasMore = false; streamChatsFor = known ? slug : null; }
    const generation = ++olderGen, current = authEpoch;
    olderLoading = known && !!api; olderError = ""; olderRetry = null;
    setView("chat"); redraw(); messages.scrollTop = messages.scrollHeight;
    // The row or name pressed is gone from view: focus moves to the stream bar, so Esc still reaches this window.
    $("woni-filter-clear").focus?.({ preventScroll: true });
    if (!olderLoading) { if (then) jumpTo(then); return; }
    try {
      const page = await api(`/api/woni/chats?${new URLSearchParams({ stream: slug, limit: "200" })}`);
      if (current !== authEpoch || generation !== olderGen || streamChatsFor !== slug) return;
      if (!Array.isArray(page?.chats)) throw new Error("not a chat page");
      streamChats = page.chats; streamHasMore = page.hasMore === true;
      if (typeof page.streamTitle === "string" && page.streamTitle && filter?.id === slug) filter = { id: slug, title: page.streamTitle };
      olderLoading = false; redraw(true);
      if (then) jumpTo(then);
    } catch (error) {
      if (current !== authEpoch || generation !== olderGen) return;
      olderLoading = false; olderError = serverMessage(error, "이 흐름의 지난 대화를 불러오지 못했어요.");
      olderRetry = () => openStream(slug, filter?.title || title, streamBackTo, then);
      redraw();
    }
  }
  // Leaves a stream without moving (sending, a notification); closeStream also goes back to the stream list it came from.
  function leaveStream() {
    streamBackTo = null;
    if (!filter) return;
    filter = null; olderGen++; olderLoading = false; olderError = ""; olderRetry = null;
  }
  function closeStream() {
    const back = streamBackTo, slug = filter?.id || ""; leaveStream();
    if (back !== "find") { redraw(); input.focus?.({ preventScroll: true }); return; }
    setView("find");  // back on the list, the stream that was opened keeps the focus
    (findList.querySelector?.(`[data-stream="${CSS.escape(slug)}"]`) || findSearch.input).focus?.({ preventScroll: true });
  }
  // Esc or the phone's back: close the stream, or leave the find screen. False when there is nothing to step out of.
  function stepBack() {
    if (!dialog.open) return false;
    if (drawerOpen) { closeDrawer(); return true; }
    if (view === "chat" && filter) { closeStream(); return true; }
    if (view === "find") { setView("chat"); input.focus?.({ preventScroll: true }); return true; }
    return false;
  }
  // Shows a chat already received in place (leaving a stream that hides it) and marks its row for a moment.
  // part: the ask's row (an alert's only row) or, from the link under an ask, its answer's row.
  // paired: pressed from that link or a detached answer's quote; the focus goes to the other one, to come back.
  function jumpTo(id, part = "question", paired = false) {
    const chat = loadedChats().find((item) => item.id === id);
    if (!chat) { setView("chat"); showOriginal(id); return; }
    if (filter && chat.stream !== filter.id) leaveStream();
    if (view !== "chat") setView("chat");
    highlight = { id, part }; clearTimeout(highlightTimer);
    highlightTimer = setTimeout(() => { highlight = null; redraw(); }, 2400);
    redraw();
    const target = rowNode(id, part);
    target?.scrollIntoView?.({ block: "center" });
    const other = paired ? target?.querySelector?.(part === "answer" ? ".woni-message-quote.is-ask" : ".woni-answer-link") : null;
    if (other) other.focus?.({ preventScroll: true });
    // What was pressed (a search result, a work card) is out of view now; the composer takes the focus.
    else if (!messages.contains?.(globalThis.document?.activeElement)) input.focus?.({ preventScroll: true });
  }
  // The first node drawn for a chat's row (its day line is left above it).
  function rowNode(id, part = "question") {
    const key = rowFor(shownRows, id, part)?.key;
    return key ? messages.querySelector?.(`[data-row-key="${CSS.escape(key)}"]`) : null;
  }

  // The find screen: with no words, the streams (latest first); with words, matching streams and chats.
  let streamsData = null, streamsError = "", streamsGen = 0, streamsAt = 0;
  let hits = null, hitsFor = "", hitsError = "", hitsLoading = false, hitsGen = 0, findTimer = 0;
  const findSearch = searchField("대화·흐름 이름으로 찾기", () => { clearTimeout(findTimer); findTimer = setTimeout(() => searchChats(), 300); });
  findSearch.input.maxLength = 100;
  const findList = el("div", "woni-find-list"), findMore = button("더 보기", "secondary woni-wide", () => searchChats(true));
  const findPane = el("div", "woni-pane");
  findPane.append(findSearch.box, findList, findMore);
  findBox.append(findPane);

  function listTime(seconds) {
    if (!validSeconds(seconds)) return "";
    const date = new Date(seconds * 1000), days = dayNumber(Date.now() / 1000) - dayNumber(seconds);
    if (days <= 0) return clockLabel(seconds);
    if (days === 1) return "어제";
    if (days < 7) return weekdayFormat.format(date);
    const [y, m, d] = dayKeyFormat.format(date).split("-").map(Number);
    return String(y) === dayKeyFormat.format(new Date()).slice(0, 4) ? `${m}월 ${d}일` : `${y}. ${m}. ${d}.`;
  }
  // Where the search words sit in a text (any case, overlaps joined), to show them in bold.
  function matchRanges(text, query) {
    const words = String(query || "").trim().toLowerCase().split(/\s+/).filter(Boolean), lower = String(text || "").toLowerCase();
    if (!words.length || !lower) return [];
    const found = [];
    for (const word of words) for (let at = lower.indexOf(word); at >= 0; at = lower.indexOf(word, at + word.length)) found.push([at, at + word.length]);
    const merged = [];
    for (const range of found.sort((a, b) => a[0] - b[0])) {
      const last = merged.at(-1);
      if (last && range[0] <= last[1]) last[1] = Math.max(last[1], range[1]); else merged.push([...range]);
    }
    return merged;
  }
  function highlighted(className, text, query) {
    const node = el("span", className), value = String(text || "");
    let at = 0;
    for (const [start, end] of matchRanges(value, query)) {
      if (start > at) node.append(el("span", "", value.slice(at, start)));
      node.append(el("strong", "woni-match", value.slice(start, end))); at = end;
    }
    if (at < value.length || !value) node.append(el("span", "", value.slice(at)));
    return node;
  }
  // A stream row's preview: what was asked while it works, otherwise the first line of the last answer.
  function streamPreview(row) {
    if (Number(row.running) > 0) return row.lastText || "";
    if (["failed", "interrupted"].includes(row.lastStatus)) return `답을 받지 못했어요 · ${row.lastText || ""}`;
    return row.lastAnswer || row.lastText || "";
  }
  function streamRow(row, query) {
    const card = button("", "woni-find-row", () => openStream(row.id, row.title, "find"));
    const top = el("span", "woni-find-top");
    top.append(highlighted("woni-find-title", row.title || row.id || "작업 흐름", query), el("span", "woni-find-time", listTime(Number(row.lastAt))));
    const bottom = el("span", "woni-find-bottom");
    bottom.append(el("span", "woni-find-preview", streamPreview(row) || `대화 ${Number(row.count) || 0}개`));
    if (Number(row.running) > 0) bottom.append(el("span", "woni-find-running", "진행 중"));
    if (Number(row.unread) > 0) {
      const unreadCount = el("span", "woni-find-unread");
      unreadCount.append(el("span", "sr-only", "새 답 "), el("span", "", String(row.unread)), el("span", "sr-only", "개"));
      bottom.append(unreadCount);
    }
    card.append(top, bottom); card.dataset.stream = String(row.id || "");
    return card;
  }
  function hitRow(hit, query) {
    const card = button("", "woni-find-row", () => openSearchResult(hit)), top = el("span", "woni-find-top");
    top.append(el("span", "woni-find-stream", hit.streamTitle || (hit.kind === "alert" ? "먼저 알림" : "흐름 없음")),
      el("span", "woni-find-time", listTime(Number(hit.createdAt))));
    card.append(top);
    if (hit.question) card.append(highlighted("woni-find-question", `나: ${hit.question}`, query));
    if (hit.snippet) card.append(highlighted("woni-find-snippet", hit.snippet, query));
    card.dataset.chatId = String(hit.id || "");
    return card;
  }
  function findStatus(text, error = false, retry = null) {
    const line = el("p", error ? "woni-note-hint is-error" : "woni-note-hint", text);
    line.setAttribute("role", error ? "alert" : "status");
    return retry ? [line, button("다시 시도", "quiet", retry)] : [line];
  }
  function renderFind() {
    const query = findSearch.input.value.trim(), nodes = [];
    const data = query && hitsFor === query ? hits : null;
    if (!query) {
      const rows = Array.isArray(streamsData?.items) ? streamsData.items : [];
      nodes.push(el("h3", "woni-find-heading", "흐름"));
      if (streamsError) nodes.push(...findStatus(streamsError, true, () => { streamsError = ""; renderFind(); loadStreams(); }));
      else if (!streamsData) nodes.push(...findStatus("흐름 목록을 불러오고 있어요."));
      else nodes.push(el("p", "woni-note-hint", rows.length ? `최근 순 · ${rows.length}개 · 누르면 그 흐름을 처음부터 봐요` : "원이가 일을 흐름별로 나눠 기억해요."));
      nodes.push(...rows.filter((row) => typeof row?.id === "string").map((row) => streamRow(row, "")));
    } else {
      if (hitsLoading) nodes.push(...findStatus("대화를 찾고 있어요."));
      if (hitsError && !hitsLoading) nodes.push(...findStatus(hitsError, true, () => searchChats()));
      if (data) {
        const streams = (Array.isArray(data.streams) ? data.streams : []).filter((row) => typeof row?.id === "string");
        if (streams.length) nodes.push(el("h3", "woni-find-heading", `흐름 ${streams.length}`), ...streams.map((row) => streamRow(row, query)));
        const items = Array.isArray(data.items) ? data.items : [];
        nodes.push(el("h3", "woni-find-heading", `대화 ${Number(data.total) || items.length}건`));
        if (!items.length) {
          const empty = el("div", "woni-empty");
          empty.append(el("strong", "", "찾는 대화가 없어요"), el("p", "", "다른 낱말로 찾아 보세요. 흐름 이름과 원이 답도 함께 찾아요."));
          nodes.push(empty);
        }
        nodes.push(...items.filter((hit) => typeof hit?.id === "string").map((hit) => hitRow(hit, query)));
      }
    }
    findList.replaceChildren(...nodes);
    findMore.hidden = !(data && data.nextOffset != null);
    findMore.disabled = hitsLoading; findMore.textContent = hitsLoading ? "불러오는 중…" : "더 보기";
  }
  async function loadStreams() {
    if (!api) return;
    const current = authEpoch, generation = ++streamsGen;
    try {
      const data = await api("/api/woni/streams");
      if (current !== authEpoch || generation !== streamsGen) return;
      if (!Array.isArray(data?.items)) throw new Error("not a stream list");
      streamsData = data; streamsError = "";
    } catch (error) {
      if (current !== authEpoch || generation !== streamsGen) return;
      streamsError = serverMessage(error, "흐름 목록을 불러오지 못했어요.");
    }
    streamsAt = Date.now();
    if (view === "find") renderFind();
    renderSide();
  }
  async function searchChats(more = false) {
    if (!api) return;
    const query = findSearch.input.value.trim().slice(0, 100);
    if (more && (hitsLoading || hitsFor !== query || hits?.nextOffset == null)) return;
    const current = authEpoch, generation = ++hitsGen, previous = hits;
    clearTimeout(findTimer);
    if (!query) { hits = null; hitsFor = ""; hitsError = ""; hitsLoading = false; renderFind(); return; }
    hitsLoading = true; hitsError = ""; renderFind();
    try {
      const offset = more ? previous.nextOffset : 0;
      const data = await api(`/api/woni/chat-search?${new URLSearchParams({ q: query, offset: String(offset), limit: "20" })}`);
      if (current !== authEpoch || generation !== hitsGen) return;
      if (!Array.isArray(data?.items)) throw new Error("not a search result");
      if (more) { const ids = new Set(previous.items.map((item) => item.id)); data.items = [...previous.items, ...data.items.filter((item) => !ids.has(item.id))]; data.streams = previous.streams; }
      hits = data; hitsFor = query;
    } catch (error) {
      if (current !== authEpoch || generation !== hitsGen) return;
      hitsError = serverMessage(error, "대화를 찾지 못했어요. 연결을 확인해 주세요.");
      if (!more) { hits = null; hitsFor = ""; }
    }
    hitsLoading = false; renderFind();
  }
  // A found chat: in place if it is already here, else fetched up to it; too far back, its stream from the start.
  async function openSearchResult(hit) {
    const id = hit?.id;
    if (typeof id !== "string" || !chatIdPattern.test(id)) return;
    leaveStream();
    const loaded = mergeChats(olderChats, Array.isArray(latestData?.chats) ? latestData.chats : []);
    if (loaded.some((chat) => chat.id === id)) { jumpTo(id); return; }
    const oldest = loaded[0]?.id, current = authEpoch, generation = ++olderGen;
    olderLoading = true; olderError = ""; olderRetry = null;
    setView("chat"); redraw();
    try {
      const page = await api(`/api/woni/chats?${new URLSearchParams({ through: id, ...(oldest ? { before: oldest } : {}) })}`);
      if (current !== authEpoch || generation !== olderGen) return;
      olderLoading = false;
      if (page?.tooFar === true) {
        if (typeof hit.stream === "string" && streamPattern.test(hit.stream)) openStream(hit.stream, hit.streamTitle || "작업 흐름", null, id);
        else { redraw(); showOriginal(id); }
        return;
      }
      if (!Array.isArray(page?.chats)) throw new Error("not a chat page");
      olderChats = mergeChats(olderChats, page.chats);
      jumpTo(id);
    } catch (_) {
      if (current !== authEpoch || generation !== olderGen) return;
      olderLoading = false; redraw(); showOriginal(id);  // the chat itself still opens in the original window
    }
  }
  function resetHistory() {
    olderChats = []; streamChats = []; streamChatsFor = null; streamHasMore = false; olderGen++;
    olderLoading = false; olderError = ""; olderRetry = null; streamBackTo = null; highlight = null; clearTimeout(highlightTimer);
    streamsData = null; streamsError = ""; streamsGen++; hits = null; hitsFor = ""; hitsError = ""; hitsLoading = false; hitsGen++;
    clearTimeout(findTimer); findSearch.input.value = ""; findList.replaceChildren(); findMore.hidden = true; findBox.hidden = true;
  }
  renderFind();

  // Ryan 2026-11-10 ("햄버거 버튼 누르면 옆으로 화면이 밀리면서 사이드 메뉴가 좀 더 큼지막하게"), as Ryan One 1.42.0:
  // one menu with the screens, the five latest streams, '오늘 정리 부탁', refresh and the board settings.
  // From 900px wide it stays open on the left; narrower, ≡ pushes the window right and slides it in (style.css).
  const side = $("woni-side"), main = $("woni-main"), menuButton = $("woni-menu"), scrim = $("woni-scrim");
  const todayAsk = "지금 하던일에서 진행 중인 작업과 내가 챙길 일을 정리해 줘.";  // the app's '오늘 정리 부탁'
  const sideScreens = [["chat", "대화"], ["tasks", "할 일"], ["notes", "기록"], ["work", "작업 현황"], ["find", "대화 찾기"]];
  let drawerOpen = false, sideKey = "";
  function sideItem(key, label, fn, extra = null) {
    const node = button("", "woni-side-item", fn); node.dataset.side = key;
    node.append(el("span", "woni-side-label", label));
    if (extra) node.append(extra);
    return node;
  }
  function sideCount(text, label) {
    const count = el("span", "woni-side-count"); count.append(el("span", "sr-only", label), el("span", "", text));
    return count;
  }
  function renderSide(force = false) {
    const counts = tasks?.counts || {}, due = (Number(counts.due) || 0) + (Number(counts.followup) || 0);
    const streams = (Array.isArray(streamsData?.items) ? streamsData.items : []).filter((row) => typeof row?.id === "string" && row.id).slice(0, 5);
    const busy = !!pending || sending || uploading;
    // Redrawn only when what it shows changed, so the 4-second refresh keeps focus and hover.
    const key = JSON.stringify([view, filter?.id || "", due, busy, streamsError, !!streamsData, streams.map((row) => [row.id, row.title, Number(row.running) || 0, Number(row.unread) || 0])]);
    if (key === sideKey && !force) return;
    sideKey = key;
    const nodes = [el("p", "woni-side-heading", "화면")];
    for (const [name, label] of sideScreens) {
      const item = sideItem(`view:${name}`, label, () => goSide(name), name === "tasks" && due > 0 ? sideCount(due > 99 ? "99+" : String(due), "확인 필요 ") : null);
      if (view === name && !(name === "chat" && filter)) item.setAttribute("aria-current", "page");
      nodes.push(item);
    }
    nodes.push(el("p", "woni-side-heading", "최근 작업 흐름"));
    if (!streamsData) nodes.push(el("p", "woni-side-note", streamsError || "흐름을 불러오고 있어요."));
    else if (!streams.length) nodes.push(el("p", "woni-side-note", "아직 흐름이 없어요."));
    for (const row of streams) {
      const extra = Number(row.running) > 0 ? el("span", "woni-side-running", "진행 중")
        : Number(row.unread) > 0 ? sideCount(String(row.unread), "새 답 ") : null;
      const item = sideItem(`stream:${row.id}`, row.title || "작업 흐름", () => { closeDrawer(false); openStream(row.id, row.title); }, extra);
      item.className += " is-stream";
      if (view === "chat" && filter?.id === row.id) item.setAttribute("aria-current", "true");
      nodes.push(item);
    }
    if (streamsData && Number(streamsData.total) > streams.length) nodes.push(sideItem("streams:all", "모든 흐름 보기", () => goSide("find")));
    nodes.push(el("p", "woni-side-heading", "바로 하기"));
    const today = sideItem("do:today", "오늘 정리 부탁", () => { closeDrawer(false); setView("chat"); input.focus?.({ preventScroll: true }); sendSuggestion(todayAsk); });
    today.disabled = busy;
    nodes.push(today, sideItem("do:refresh", "새로고침", reloadAll),
      sideItem("do:settings", "하던일 설정", () => { dialog.close(); globalThis.document?.getElementById?.("settings")?.click?.(); }));
    const active = globalThis.document?.activeElement, at = side.contains?.(active) ? active.dataset?.side : null;
    side.replaceChildren(...nodes);
    if (at) nodes.find((node) => node.dataset?.side === at)?.focus?.({ preventScroll: true });
  }
  // The item pressed in a narrow drawer slides out of reach, so the focus goes to the composer, the find field,
  // or (for a panel, which has no field of its own) back to ≡.
  function goSide(name) {
    closeDrawer(name !== "chat" && name !== "find");
    if (name === "chat") { leaveStream(); setView("chat"); redraw(); input.focus?.({ preventScroll: true }); return; }
    setView(name);
    if (name === "find") findSearch.input.focus?.({ preventScroll: true });
  }
  function reloadAll() {
    closeDrawer();
    clearTimeout(timer); refresh(); loadStreams();
    if (view === "tasks") loadTasks();
    if (view === "notes" && !note) loadNotes();
    notify("새로 불러오고 있어요.");
  }
  // The drawer of a narrow window: the window's own content is pushed right and cannot be reached until it closes.
  function openDrawer() {
    drawerOpen = true; dialog.dataset.drawer = "open"; menuButton.setAttribute("aria-expanded", "true");
    main.inert = true; renderSide(true); loadStreams();
    // ≡ itself is now out of reach: the current screen or stream (else the first item) takes the focus.
    (side.querySelector?.("[aria-current]") || side.querySelector?.(".woni-side-item"))?.focus?.({ preventScroll: true });
  }
  function closeDrawer(returnFocus = true) {
    if (!drawerOpen) return;
    drawerOpen = false; dialog.dataset.drawer = "closed"; menuButton.setAttribute("aria-expanded", "false");
    main.inert = false;
    if (returnFocus) menuButton.focus?.({ preventScroll: true });
  }
  menuButton.addEventListener("click", () => { if (drawerOpen) closeDrawer(); else openDrawer(); });
  scrim.addEventListener("click", () => closeDrawer());
  // A window widened past 900px shows the menu beside the chat; a drawer left open would keep the chat inert.
  globalThis.matchMedia?.("(min-width: 900px)")?.addEventListener?.("change", (event) => { if (event.matches) closeDrawer(false); });
  function touchDevice() { return !!globalThis.matchMedia?.("(pointer: coarse)")?.matches; }

  // keepPlace: chats were added above the ones being read, so the view stays on them instead of jumping.
  function show(data, keepPlace = false) {
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
    const oldHeight = messages.scrollHeight, oldTop = messages.scrollTop;
    const nodes = [], visibleChats = mergedChats(chats).filter((item) => !filter || item.stream === filter.id);
    nodes.push(...olderRow(visibleChats.length));
    // Rows, not chats (chatRows): a stream shows its own chats by the same rule, so an answer apart in the whole
    // list may sit right under its ask there. The 👉 suggestions go under the bottom answer of the rows shown.
    const rows = chatRows(visibleChats), byId = new Map(visibleChats.map((chat) => [chat.id, chat]));
    const dividers = timeDividers(rows, byId), latestId = latestRowId(rows);
    const found = highlight && rowFor(rows, highlight.id, highlight.part)?.key;
    shownRows = rows;
    for (const item of rows) {
      const chat = byId.get(item.id);
      if (dividers.has(item.key)) nodes.push(el("p", "woni-divider", dividers.get(item.key)));
      const first = nodes.length;
      if (item.kind !== "answer") askRow(nodes, chat, item);
      if (item.kind !== "question") answerRow(nodes, chat, item, latestId);
      if (nodes[first]) nodes[first].dataset.rowKey = item.key;
      if (found === item.key) for (const node of nodes.slice(first)) if (node.className.startsWith("woni-message ")) node.className += " is-found";
    }
    // An empty chat says what Woni is for and offers the same first ask as the menu.
    if (!visibleChats.length && !pending && !filter && !olderLoading) {
      const welcome = el("div", "woni-welcome");
      const ask = action("오늘 정리 부탁", () => sendSuggestion(todayAsk));
      welcome.append(el("strong", "", "무엇을 도와드릴까요?"), el("p", "", "하던 일 정리, 할 일 확인, 기록 찾기를 원이에게 맡겨 보세요."), ask);
      nodes.push(welcome);
    }
    if (pending) {
      const sentFiles = (pending.attachments || []).map((id) => attachments.find((item) => item.id === id) || { name: "첨부 파일" });
      const outgoing = row("user", pending.text, sentFiles);
      if (pending.replyTo) {
        const own = replyTarget?.id === pending.replyTo.id ? replyTarget.excerpt : "";
        const quoted = loadedChats().find((chat) => chat.id === pending.replyTo.id);
        const excerpt = own || (!quoted ? "" : pending.replyTo.part === "answer" ? visibleAnswer(quoted.answer || "") || quoted.answer : quoted.text);
        outgoing.append(el("div", "woni-send-status", `↩ 답글 · ${excerpt || "이전 메시지"}`));
      }
      const message = pendingState === "sending" ? "전송 중…" : pendingState === "checking" ? "전달됨 · 대화 확인 중…" : "전송 여부를 확인하지 못했어요.";
      const indicator = el("div", "woni-send-status", message);
      indicator.setAttribute("role", "status");
      if (pendingState === "uncertain" && !sending) indicator.append(action("다시 보내기", sendPending));
      outgoing.append(indicator); nodes.push(outgoing);
    }
    messages.replaceChildren(...nodes);
    if (atEnd || !messages.dataset.loaded) messages.scrollTop = messages.scrollHeight;
    else if (keepPlace) messages.scrollTop = oldTop + messages.scrollHeight - oldHeight;
    messages.dataset.loaded = "1";
    filterBox.hidden = view !== "chat" || !filter;
    $("woni-filter-label").textContent = filter ? `흐름: ${filter.title}` : "";
    renderReplyPicks();
    const running = data.activity?.running || 0;
    status.textContent = pending ? (pendingState === "uncertain" ? "전송 확인이 필요해요." : "메시지를 전달하고 있어요.") : running ? `작업 중 ${running}건` : "나의 개인 비서";
    renderSide();
  }
  // A working answer shows quiet dots, a failed one the failure colour (style.css); the class stays as before.
  function stateRow(state, node) { node.dataset.state = state; return node; }
  // Ryan's ask. While Woni works on it, what it is doing stays right under it (as before); once its answer has
  // gone below other rows, a link down to it takes that place.
  function askRow(nodes, chat, item) {
    if (chat.text || chat.attachments?.length) nodes.push(row("user", chat.text || "", chat.attachments || [], chat, null, { link: item.detached ? answerLink(chat) : null }));
    if (item.kind !== "question" || item.detached) return;
    if (chat.answer) nodes.push(row("assistant", visibleAnswer(chat.answer) || chat.answer, [], chat, chat));
    if (["queued", "running"].includes(chat.status)) nodes.push(stateRow("working", row("assistant", chat.progress || "생각하고 있어요…", [], null, chat.answer ? null : chat)));
  }
  // Woni's answer, an alert or a failure; a detached one quotes the ask above its bubble.
  function answerRow(nodes, chat, item, latestId) {
    const askOf = item.detached ? chat : null;
    if (chat.answer) nodes.push(row("assistant", visibleAnswer(chat.answer) || chat.answer, [], chat, chat, { askOf }));
    const replies = chat.answer && chat.id === latestId && !pending ? suggestedReplies(chat.answer) : [];
    if (replies.length) {
      const box = el("div", "woni-suggestions"); box.setAttribute("aria-label", "추천 답장");
      // A suggestion always goes as a reply to this answer (Ryan One app 1.44.0): the answer that finished last can be
      // many asks back, so the reply tells Woni which work it follows instead of a guess from the last few chats.
      for (const reply of replies) box.append(action(reply, () => sendSuggestion(reply, { id: chat.id, part: "answer" })));
      nodes.push(box);
    }
    // An alert has no ask row, so one still being made shows its working line here (as before).
    else if (["queued", "running"].includes(chat.status)) nodes.push(stateRow("working", row("assistant", chat.progress || "생각하고 있어요…", [], null, chat.answer ? null : chat)));
    else if (["failed", "interrupted"].includes(chat.status)) nodes.push(stateRow("failed", row("assistant", "답변을 완료하지 못했어요. 원이 앱에서 상태를 확인해 주세요.", [], null, chat.answer ? null : chat, { askOf: chat.answer ? null : askOf })));
  }
  async function refresh() {
    if (!enabled || reading || !api) return;
    reading = true; const current = authEpoch;
    try {
      const data = await api("/api/woni");
      if (current !== authEpoch || !enabled) return;
      // A finished chat pushed out of the 60-chat window stays as an earlier chat, so the list keeps no gap.
      const left = slidOut(latestData?.chats || [], Array.isArray(data?.chats) ? data.chats : []);
      if (left.length) olderChats = mergeChats(olderChats, left);
      latestData = data; observeReplies(data);
      if (dialog.open) {
        show(data);
        // The to-do list is read when its tab opens and every 30 seconds while it stays open.
        if (view === "tasks" && Date.now() - tasksAt >= 30000) loadTasks();
        // The menu's latest streams are read on opening and once a minute while the window stays open.
        if (Date.now() - streamsAt >= 60000) loadStreams();
        if (focusId) {
          const id = focusId; focusId = null;
          if (loadedChats().some((chat) => chat.id === id)) rowNode(id)?.scrollIntoView?.({ block: "center" });
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
    if (!input.value && !attachments.length && !uploading && !pending && !replyTarget && !sending) {
      try {
        if (window.RyanOneHost?.version === 1 && window.RyanOneHost.openWoni(focusId || "") === true) {
          focusId = null;
          return;
        }
      } catch (_) { /* The normal web composer remains available if the host cannot navigate. */ }
    }
    if (pending) {
      if (!attachments.length) attachments = (pending.attachments || []).map((id) => ({ id, name: "첨부 파일", state: "ready" }));
      if (pending.replyTo && !replyTarget) {
        replyTarget = { ...pending.replyTo, excerpt: "대상 메시지" };
      }
      replyBox.hidden = true;
    }
    renderAttachments(); dialog.showModal(); setView("chat");
    loadTasks();  // once on opening, for the count on the to-do tab
    loadStreams();  // the menu's latest streams
    if (latestData || pending) show(latestData || { chats: [], activity: {} });
    markVisibleRead();
    input.focus({ preventScroll: true });
    refresh();
  });
  $("woni-close").addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => { closeDrawer(false); clearTimeout(timer); services.voice?.stop(input); epoch++; if (enabled) refresh(); });
  chatTab.addEventListener("click", () => setView("chat"));
  workTab.addEventListener("click", () => setView("work"));
  tasksTab.addEventListener("click", () => setView("tasks"));
  notesTab.addEventListener("click", () => setView("notes"));
  // '전체 대화' shows every chat here; only Esc or back returns to the stream list a stream was opened from.
  $("woni-filter-clear").addEventListener("click", () => { leaveStream(); redraw(); });
  findOpen.addEventListener("click", () => {
    if (view === "find") { setView("chat"); return; }
    setView("find"); findSearch.input.focus?.({ preventScroll: true });
  });
  // Esc and the phone's back (app.js sends a cancelable "cancel") step back out of the menu drawer, a stream or the
  // find screen first, in that order; with nothing left, they close the window.
  // Esc is taken at keydown: Chrome stops honouring a second cancel in a row without a click in between.
  dialog.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !event.isComposing && stepBack()) event.preventDefault();
  });
  dialog.addEventListener("cancel", (event) => { if (stepBack()) event.preventDefault(); });
  // Reaching the top of the list fetches the chats before it. A failure waits for '다시 시도' (no automatic repeat).
  messages.addEventListener("scroll", () => {
    if (view !== "chat" || !messages.dataset.loaded || olderLoading || olderError || messages.scrollTop > 120) return;
    if (canLoadOlder()) loadOlder();
  });
  $("woni-reply-clear").addEventListener("click", () => {
    if (pending) { status.textContent = "이전 메시지의 전송 여부를 먼저 확인해 주세요."; return; }
    replyTarget = null; replyBox.hidden = true; renderReplyPicks();
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
  // The board's rule (app.js terminal draft): on a computer Enter sends and Shift+Enter keeps a newline; a phone's
  // Enter is a newline (its send button sends). Cmd/Ctrl+Enter sends everywhere. The Enter that commits Korean
  // composition (isComposing or 229) neither sends nor is held back; macOS Chrome then repeats it once composition
  // has ended, and that one sends. Only the shortcut keeps its old 80ms guard after composition.
  if (touchDevice() && $("woni-hint")) $("woni-hint").textContent = "보내기 버튼으로 보내요";
  input.addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    const shortcut = event.metaKey || event.ctrlKey;
    if (!shortcut && (event.shiftKey || event.altKey || touchDevice())) return;
    if (event.isComposing || composing || event.keyCode === 229) { if (shortcut) event.preventDefault(); return; }
    event.preventDefault();
    if (shortcut && Date.now() - compositionEndedAt < 80) return;
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
    input.value = ""; replyBox.hidden = true; leaveStream(); renderReplyPicks();
    services.voice?.stop(input);
    const result = sendPending(); messages.scrollTop = messages.scrollHeight;
    await result;
  });
  window.SessionholicWoni = {
    hasDraft() { return !!input.value || !!attachments.length || uploading || !!pending || !!replyTarget; },
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
      focusId = id; leaveStream();
      if (enabled) { open.click(); clearTimeout(timer); refresh(); }
      return true;
    },
    lock() {
      enabled = false; epoch++; authEpoch++; clearTimeout(timer); uploadAbort?.abort(); services.voice?.stop(input);
      closeDrawer(false);
      if (dialog.open) dialog.close(); open.hidden = true; messages.replaceChildren(); delete messages.dataset.loaded;
      input.value = ""; attachments = []; replyTarget = null; replyBox.hidden = true; replyPicks.replaceChildren(); replyPicks.hidden = true;
      filter = null; latestData = null; workData = null; resetHistory();
      pending = null; pendingState = "uncertain"; sending = false; seen = null; unread.clear(); badge();
      readThrough = null; serverRead = 0; readPosting = 0;
      original.replaceChildren(); original.hidden = true; work.replaceChildren(); resetPanels(); renderAttachments();
      clearTimeout(noticeTimer); $("woni-notice").hidden = true; $("woni-notice").textContent = "";
      streamsAt = 0; renderSide(true);
    },
    toastMs,
    chatRows,
  };
  renderSide();
})();
