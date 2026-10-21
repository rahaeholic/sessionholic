"use strict";

// Layout is independent of the conversation DOM. Frames stay mounted in their
// original slots: dragging/resizing changes coordinates, never their documents.
// Only opaque session/terminal references and layout ratios are preferences.
(function (root) {
  const MAX_PANES = 4;
  const MIME = "application/x-sessionholic-pane";
  const leaf = (id) => ({ pane: id });
  const ids = (tree) => !tree ? [] : tree.pane ? [tree.pane] : tree.children.flatMap(ids);
  const copy = (value) => value == null ? value : JSON.parse(JSON.stringify(value));
  function validTree(tree, seen = new Set(), depth = 0) {
    if (!tree || typeof tree !== "object" || Array.isArray(tree) || depth > MAX_PANES) return false;
    if (tree.pane) {
      if (!/^p[1-4]$/.test(tree.pane) || seen.has(tree.pane) || seen.size >= MAX_PANES) return false;
      seen.add(tree.pane);
      return Object.keys(tree).length === 1;
    }
    return ["x", "y"].includes(tree.axis) && Number.isFinite(tree.ratio) && tree.ratio >= 0.15 && tree.ratio <= 0.85 &&
      Array.isArray(tree.children) && tree.children.length === 2 && tree.children.every((child) => validTree(child, seen, depth + 1));
  }
  function remove(tree, id) {
    if (!tree || tree.pane) return tree?.pane === id ? null : tree;
    const children = tree.children.map((child) => remove(child, id)).filter(Boolean);
    return children.length === 1 ? children[0] : { ...tree, children };
  }
  function split(tree, target, id, edge = "right") {
    if (!tree || ids(tree).includes(id) || ids(tree).length >= MAX_PANES || !ids(tree).includes(target) ||
        !["left", "right", "top", "bottom"].includes(edge)) return tree;
    const visit = (node) => {
      if (node.pane !== target) return node.pane ? node : { ...node, children: node.children.map(visit) };
      const children = [node, leaf(id)];
      if (["left", "top"].includes(edge)) children.reverse();
      return { axis: ["left", "right"].includes(edge) ? "x" : "y", ratio: 0.5, children };
    };
    return visit(tree);
  }
  function move(tree, from, target, edge) {
    if (from === target || !ids(tree).includes(from) || !ids(tree).includes(target)) return tree;
    if (edge === "center") {
      const visit = (node) => node.pane ? leaf(node.pane === from ? target : node.pane === target ? from : node.pane)
        : { ...node, children: node.children.map(visit) };
      return visit(tree);
    }
    if (!["left", "right", "top", "bottom"].includes(edge)) return tree;
    return split(remove(tree, from), target, from, edge);
  }
  function geometry(tree, box = { x: 0, y: 0, w: 1, h: 1 }, path = "", out = { panes: {}, dividers: [] }) {
    if (tree.pane) out.panes[tree.pane] = box;
    else {
      const horizontal = tree.axis === "x", size = horizontal ? "w" : "h", pos = horizontal ? "x" : "y";
      const first = { ...box, [size]: box[size] * tree.ratio };
      const second = { ...box, [pos]: box[pos] + first[size], [size]: box[size] - first[size] };
      out.dividers.push({ path, axis: tree.axis, box, at: second[pos], ratio: tree.ratio });
      geometry(tree.children[0], first, path + "0", out);
      geometry(tree.children[1], second, path + "1", out);
    }
    return out;
  }
  function ratioAt(tree, path, ratio) {
    if (tree.pane || !/^[01]*$/.test(path)) return tree;
    if (!path) return { ...tree, ratio: Math.max(0.15, Math.min(0.85, ratio)) };
    const index = Number(path[0]);
    return { ...tree, children: tree.children.map((child, i) => i === index ? ratioAt(child, path.slice(1), ratio) : child) };
  }
  function neighbor(tree, current, direction) {
    const panes = geometry(tree).panes, here = panes[current];
    if (!here) return null;
    const vertical = ["ArrowUp", "ArrowDown"].includes(direction), forward = ["ArrowRight", "ArrowDown"].includes(direction);
    const pos = vertical ? "y" : "x", size = vertical ? "h" : "w", cross = vertical ? "x" : "y", span = vertical ? "w" : "h";
    const center = (box, p, s) => box[p] + box[s] / 2;
    return Object.entries(panes).filter(([id, box]) => id !== current &&
      (center(box, pos, size) - center(here, pos, size)) * (forward ? 1 : -1) > 0.001)
      .map(([id, box]) => {
        const overlap = Math.min(here[cross] + here[span], box[cross] + box[span]) - Math.max(here[cross], box[cross]);
        return { id, score: (overlap > 0 ? 0 : 10) + Math.abs(center(box, pos, size) - center(here, pos, size)) +
          Math.abs(center(box, cross, span) - center(here, cross, span)) * 2 };
      }).sort((a, b) => a.score - b.score)[0]?.id || null;
  }
  function shortcut(event, modifiers = "ctrl-meta") {
    if (!event.ctrlKey || !event.metaKey || !!event.altKey !== (modifiers === "ctrl-alt-meta") || event.shiftKey || event.isComposing || event.keyCode === 229) return null;
    if (["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key)) return { direction: event.key };
    if (event.key === "Enter") return { maximize: true };
    const digit = /^Digit([1-4])$/.exec(event.code || "")?.[1] || (/^[1-4]$/.test(event.key) ? event.key : null);
    return digit ? { number: Number(digit) } : null;
  }
  function validRef(value) {
    if (!value || typeof value !== "object" || Array.isArray(value)) return null;
    const ref = {};
    if (typeof value.key === "string" && value.key.length > 0 && value.key.length <= 1500 && !/[\x00-\x1f\x7f]/.test(value.key)) ref.key = value.key;
    if (typeof value.terminalId === "string" && /^[a-f0-9]{32}$/.test(value.terminalId)) ref.terminalId = value.terminalId;
    if (!ref.key && !ref.terminalId) return null;
    ref.mode = value.mode === "terminal" ? "terminal" : "chat";
    return ref;
  }
  function restoreLayout(value) {
    if (!value || value.version !== 1 || !validTree(value.tree)) return null;
    const panes = {}, keys = new Set(), terminals = new Set();
    for (const id of ids(value.tree)) {
      const ref = validRef(value.panes?.[id]);
      if (ref?.key && keys.has(ref.key) || ref?.terminalId && terminals.has(ref.terminalId)) return null;
      if (ref?.key) keys.add(ref.key);
      if (ref?.terminalId) terminals.add(ref.terminalId);
      panes[id] = ref;
    }
    return { version: 1, tree: copy(value.tree), active: ids(value.tree).includes(value.active) ? value.active : ids(value.tree)[0], panes };
  }
  const model = { MAX_PANES, ids, leaf, validTree, remove, split, move, geometry, ratioAt, neighbor, shortcut, validRef, restoreLayout };
  if (typeof module !== "undefined" && module.exports) { module.exports = model; return; }
  if (!root?.document) return;

  const doc = root.document;
  const $ = (id) => doc.getElementById(id);
  const embedded = root.location.pathname === "/pane";
  const ownId = embedded ? new URLSearchParams(root.location.search).get("pane") : "p1";
  let hooks, parent, tree = leaf("p1"), active = "p1", maximized = false, restored = false, enabled = false, internal = 0;
  let frameReady = false, restoring = false, drag = null, pointer = null, refreshTimer = null;
  const panes = new Map(), shared = new Map();
  const make = (tag, className, text) => {
    const node = doc.createElement(tag);
    if (className) node.className = className;
    if (text) node.textContent = text;
    return node;
  };
  function button(label, text, run) {
    const node = make("button", "workspace-button", text);
    node.type = "button"; node.title = label; node.setAttribute("aria-label", label);
    node.addEventListener("click", run);
    return node;
  }
  function paneBar(id, container) {
    const bar = make("header", "workspace-pane-bar"), title = make("button", "workspace-pane-title", "빈 창");
    bar.hidden = true;
    title.type = "button"; title.draggable = true; title.title = "창 제목을 끌어 배치 변경";
    title.addEventListener("click", () => focus(id));
    title.addEventListener("dragstart", (event) => {
      if (event.ctrlKey || event.metaKey) return;
      drag = { pane: id }; event.dataTransfer.setData(MIME, JSON.stringify(drag)); event.dataTransfer.effectAllowed = "move";
      doc.body.classList.add("workspace-dragging");
    });
    title.addEventListener("pointerdown", (event) => {
      if (event.button !== 0 || event.pointerType === "mouse") return;
      pointer = { id, pointerId: event.pointerId, x: event.clientX, y: event.clientY, title, moved: false };
      title.setPointerCapture?.(event.pointerId);
    });
    title.addEventListener("pointermove", (event) => {
      if (!pointer || pointer.pointerId !== event.pointerId) return;
      if (Math.hypot(event.clientX - pointer.x, event.clientY - pointer.y) < 8 && !pointer.moved) return;
      pointer.moved = true; drag = { pane: id };
      doc.body.classList.add("workspace-dragging"); event.preventDefault(); cue(event.clientX, event.clientY);
    });
    title.addEventListener("pointerup", (event) => {
      if (!pointer || pointer.pointerId !== event.pointerId) return;
      if (pointer.moved) { event.preventDefault(); drop(event.clientX, event.clientY, drag); }
      pointer = null; clearDrag();
    });
    title.addEventListener("pointercancel", () => { pointer = null; clearDrag(); });
    bar.append(title, button("오른쪽으로 분할", "◫", () => add(null, id, "right")),
      button("아래로 분할", "⬒", () => add(null, id, "bottom")),
      button("창 확대·복원 · Control+Command+Enter", "⤢", () => { focus(id, false); maximize(); }),
      button("이 창만 닫기 · 세션 실행은 유지", "×", () => closePane(id)));
    bar.addEventListener("pointerdown", () => focus(id, false));
    container.prepend(bar);
    return { bar, title };
  }
  function record(id, ref = null) {
    if (panes.has(id)) return panes.get(id);
    const node = id === "p1" ? $("workspace-primary") : make("section", "workspace-pane");
    node.dataset.paneId = id; node.setAttribute("aria-label", `세션 창 ${id.slice(1)}`);
    const row = { id, node, ref: validRef(ref), hooks: id === "p1" ? hooks : null, visible: true };
    Object.assign(row, paneBar(id, node));
    panes.set(id, row);
    if (id !== "p1") {
      const frame = make("iframe", "workspace-frame");
      frame.title = `세션 창 ${id.slice(1)}`;
      frame.setAttribute("allow", "microphone 'self'");
      frame.src = `/pane?pane=${id}`; row.frame = frame;
      node.append(frame); $("session-workspace").append(node);
    }
    return row;
  }
  function local(fn) {
    internal++;
    try { return fn(); } finally { internal--; }
  }
  function notify(message) { hooks?.toast(message); }
  function visible(id = ownId) { return embedded ? frameReady : !enabled || ids(tree).includes(id); }
  function managed() { return embedded || enabled; }
  function persist() {
    if (restoring || !hooks?.describe().authenticated) return;
    const refs = {};
    for (const id of ids(tree)) refs[id] = validRef(panes.get(id)?.ref || panes.get(id)?.hooks?.describe().ref);
    hooks.prefs.set("workspace", { version: 1, tree: copy(tree), active, panes: refs });
  }
  function fit(row) { row.hooks?.resize(); }
  function layout({ save = true } = {}) {
    if (embedded || !hooks) return;
    const list = ids(tree), rootNode = $("session-workspace");
    enabled = list.length > 1 || list[0] !== "p1";
    doc.body.classList.toggle("workspace-split", enabled);
    doc.body.classList.toggle("workspace-has-layout", enabled);
    const rects = geometry(tree).panes;
    const bounds = rootNode.getBoundingClientRect();
    const compact = root.innerWidth <= 700 || list.length > 1 && Object.values(rects).some((r) => r.w * bounds.width < 280 || r.h * bounds.height < 170);
    doc.body.classList.toggle("workspace-compact", compact && enabled);
    for (const [id, row] of panes) {
      const included = list.includes(id), shown = included && (!(maximized || compact) || active === id);
      row.node.hidden = !included;
      row.node.classList.toggle("workspace-pane-active", id === active);
      row.node.classList.toggle("workspace-pane-covered", included && !shown);
      row.node.setAttribute("aria-hidden", String(!shown));
      row.node.inert = !shown;
      row.bar.hidden = !enabled;
      if (included && enabled) {
        const r = maximized || compact ? { x: 0, y: 0, w: 1, h: 1 } : rects[id];
        Object.assign(row.node.style, { left: `${r.x * 100}%`, top: `${r.y * 100}%`, width: `${r.w * 100}%`, height: `${r.h * 100}%` });
      } else for (const prop of ["left", "top", "width", "height"]) row.node.style[prop] = "";
      if (row.visible !== included) { row.visible = included; row.hooks?.visibility(included); }
      // No iframe reinsertion, select(), openTerminal() or editor replacement here.
    }
    $("workspace-dividers").replaceChildren();
    if (enabled && !maximized && !compact) for (const divider of geometry(tree).dividers) {
      const node = make("div", `workspace-divider workspace-divider-${divider.axis}`);
      node.tabIndex = 0; node.setAttribute("role", "separator");
      node.setAttribute("aria-label", "창 크기 조절"); node.setAttribute("aria-orientation", divider.axis === "x" ? "vertical" : "horizontal");
      node.setAttribute("aria-valuemin", "15"); node.setAttribute("aria-valuemax", "85");
      node.setAttribute("aria-valuenow", String(Math.round(divider.ratio * 100)));
      const b = divider.box;
      Object.assign(node.style, divider.axis === "x" ? { left: `${divider.at * 100}%`, top: `${b.y * 100}%`, height: `${b.h * 100}%` }
        : { top: `${divider.at * 100}%`, left: `${b.x * 100}%`, width: `${b.w * 100}%` });
      node.addEventListener("pointerdown", (event) => {
        if (event.button !== 0) return;
        event.preventDefault(); node.setPointerCapture?.(event.pointerId);
        doc.body.classList.add("workspace-resizing");
        const box = rootNode.getBoundingClientRect();
        const adjust = (e) => {
          const point = divider.axis === "x" ? (e.clientX - box.left) / box.width : (e.clientY - box.top) / box.height;
          const value = (point - b[divider.axis]) / b[divider.axis === "x" ? "w" : "h"];
          tree = ratioAt(tree, divider.path, value);
          // Keep the captured separator mounted for the entire gesture.
          const next = geometry(tree).panes;
          for (const [id, r] of Object.entries(next)) Object.assign(panes.get(id).node.style,
            { left: `${r.x * 100}%`, top: `${r.y * 100}%`, width: `${r.w * 100}%`, height: `${r.h * 100}%` });
        };
        const end = () => { node.removeEventListener("pointermove", adjust); doc.body.classList.remove("workspace-resizing"); layout(); };
        node.addEventListener("pointermove", adjust);
        node.addEventListener("pointerup", end, { once: true }); node.addEventListener("pointercancel", end, { once: true });
      });
      node.addEventListener("keydown", (event) => {
        const previous = divider.axis === "x" ? "ArrowLeft" : "ArrowUp", next = divider.axis === "x" ? "ArrowRight" : "ArrowDown";
        if (![previous, next, "Home"].includes(event.key) || event.metaKey || event.ctrlKey || event.altKey) return;
        event.preventDefault(); tree = ratioAt(tree, divider.path, event.key === "Home" ? 0.5 : divider.ratio + (event.key === next ? 0.05 : -0.05));
        layout(); $("workspace-dividers").children[geometry(tree).dividers.findIndex((d) => d.path === divider.path)]?.focus();
      });
      $("workspace-dividers").append(node);
    }
    const switcher = $("workspace-switcher"); switcher.hidden = !enabled;
    switcher.replaceChildren();
    list.forEach((id, index) => {
      const tab = button(`창 ${index + 1}로 이동 · Control+Command+${index + 1}`, `${index + 1}`, () => focus(id));
      tab.setAttribute("aria-pressed", String(id === active)); switcher.append(tab);
    });
    hooks.listChanged?.();
    if (save) persist();
    root.requestAnimationFrame?.(() => { for (const id of list) fit(panes.get(id)); });
  }
  function focus(id, editor = true) {
    if (!ids(tree).includes(id)) return false;
    if (active !== id && panes.get(active)?.hooks?.describe().composing) {
      notify("한글 조합을 마친 뒤 창을 이동해 주세요."); return false;
    }
    const changed = active !== id;
    active = id;
    if (changed) layout();
    if (editor) {
      const row = panes.get(id); row.frame?.contentWindow?.focus(); row.hooks?.focus();
    }
    return true;
  }
  function maximize() { maximized = !maximized; layout({ save: false }); }
  function handleKey(event, id = active) {
    const action = shortcut(event, hooks?.prefs.get("workspaceKeys", "ctrl-meta"));
    if (!action || !hooks?.describe().authenticated || doc.querySelector("dialog[open]") || panes.get(id)?.hooks?.describe().dialog) return false;
    if (event.repeat && action.maximize) { event.preventDefault(); event.stopImmediatePropagation(); return true; }
    event.preventDefault(); event.stopImmediatePropagation();
    if (action.maximize) { if (ids(tree).length > 1) maximize(); }
    else {
      const target = action.number ? ids(tree)[action.number - 1] : neighbor(tree, active, action.direction);
      if (target) focus(target);
    }
    return true;
  }
  function findRef(ref) {
    if (!ref) return null;
    for (const id of ids(tree)) {
      const actual = panes.get(id)?.ref || panes.get(id)?.hooks?.describe().ref;
      if (ref.key && actual?.key === ref.key || ref.terminalId && actual?.terminalId === ref.terminalId) return id;
    }
    return null;
  }
  function add(ref, target = active, edge = "right") {
    if (!hooks?.describe().authenticated) return false;
    ref = validRef(ref);
    const existing = findRef(ref);
    if (existing) { focus(existing); return true; }
    if (ids(tree).length >= MAX_PANES) { notify("창은 최대 4개까지 열 수 있습니다."); return false; }
    if (!enabled && hooks.prepareSplit?.() === false) return false;
    if (!ids(tree).includes(target)) target = ids(tree)[0];
    const id = ["p1", "p2", "p3", "p4"].find((name) => !ids(tree).includes(name));
    const row = record(id, ref); row.ref = ref;
    tree = split(tree, target, id, edge); maximized = false;
    active = id; layout();
    if (row.hooks) applyRef(row);
    return true;
  }
  function closePane(id) {
    if (ids(tree).length < 2) { notify("마지막 창은 유지합니다."); return; }
    const row = panes.get(id);
    if (row.hooks && !row.hooks.canClose()) return;
    row.hooks?.prepareClose({ discard: id !== "p1" });
    tree = remove(tree, id);
    if (id !== "p1") { row.node.remove(); panes.delete(id); }
    else row.ref = null;
    if (active === id) active = ids(tree)[0];
    maximized = false; layout(); focus(active);
  }
  function applyRef(row) {
    if (!row.hooks || !row.ref || row.restoring) return;
    row.restoring = true;
    try { if (local(() => row.hooks.restore(copy(row.ref)))) row.ref = null; }
    finally { row.restoring = false; }
  }
  function changed() {
    if (!hooks) return;
    if (embedded) { if (frameReady) parent.report(ownId, root); return; }
    const info = hooks.describe();
    $("workspace-split-open").hidden = !info.authenticated;
    if (!info.authenticated) return;
    if (!restored) {
      restored = true;
      const saved = restoreLayout(hooks.prefs.get("workspace", null));
      if (saved) {
        restoring = true; tree = saved.tree; active = saved.active;
        for (const id of ids(tree)) record(id, saved.panes[id]);
        panes.get("p1").ref = saved.panes.p1 || null;
        layout({ save: false });
        for (const id of ids(tree)) applyRef(panes.get(id));
        restoring = false;
      }
    }
    if (restoring) return;
    for (const [id, row] of panes) {
      if (row.ref) applyRef(row);
      const value = row.hooks?.describe();
      const index = ids(tree).indexOf(id);
      row.title.textContent = `${index >= 0 ? index + 1 : ""} · ${value?.title || (row.ref ? "저장한 세션 확인 중" : "목록에서 세션을 선택하세요")}`;
      row.title.title = value?.route ? `${value.title}\n${value.route}\n끌어서 배치 변경` : "창 제목을 끌어 배치 변경";
      row.title.setAttribute("aria-label", row.title.title);
    }
    persist();
  }
  function attach(id, win, childHooks) {
    const row = panes.get(id);
    if (!row?.frame || row.frame.contentWindow !== win || !ids(tree).includes(id)) return false;
    row.hooks = childHooks;
    applyRef(row); changed();
    return true;
  }
  function report(id, win) {
    if (panes.get(id)?.frame?.contentWindow !== win) return;
    changed();
  }
  function routeSession(key) {
    if (embedded || internal || !enabled) return false;
    doc.body.classList.remove("workspace-list-open");
    const existing = findRef({ key });
    if (existing) { focus(existing); return true; }
    if (active === "p1") return false;
    const row = panes.get(active);
    if (!row?.hooks) { row.ref = { key, mode: "chat" }; return true; }
    row.ref = null; row.hooks.select(key); row.hooks.focus(); changed();
    return true;
  }
  function routeNew() {
    if (embedded || internal || !enabled || active === "p1") return false;
    panes.get(active)?.hooks?.newSession(); return true;
  }
  function routeTerminal(terminal) {
    if (embedded || internal || !enabled) return false;
    const key = hooks.sessionKeyForTerminal?.(terminal);
    const existing = findRef({ ...(key ? { key } : {}), terminalId: terminal.id });
    const target = existing || active;
    if (target !== "p1") {
      if (!focus(target, false)) return true;
      const row = panes.get(target);
      if (row.hooks) row.hooks.openTerminal(terminal);
      else row.ref = { terminalId: terminal.id, mode: "terminal" };
      return true;
    }
    if (existing) focus(existing, false);
    // An unlinked terminal has no trustworthy chat to pair with. Keep its
    // standalone screen inside a child document rather than covering all panes.
    if (!key) { add({ terminalId: terminal.id, mode: "terminal" }); return true; }
    return false;
  }
  function routeSessionTerminal(session) {
    if (embedded || internal || !enabled) return false;
    const target = findRef({ key: session.key }) || active;
    if (target === "p1") return false;
    if (!focus(target, false)) return true;
    const row = panes.get(target);
    if (row.hooks) row.hooks.openSessionTerminal(session);
    else row.ref = { key: session.key, mode: "terminal" };
    return true;
  }
  function activeSessionKey() {
    const row = panes.get(active);
    return (row?.ref || row?.hooks?.describe().ref)?.key || null;
  }
  function readingActive() { return embedded ? frameReady && parent.isActive(ownId, root) : !enabled || active === "p1"; }
  function isActive(id, win) { return active === id && panes.get(id)?.frame?.contentWindow === win; }
  function claimTerminal(terminal) {
    if (embedded) return frameReady && parent.claim(ownId, root, terminal.id);
    return claim("p1", root, terminal.id);
  }
  function claim(id, win, terminalId) {
    if (id !== "p1" && panes.get(id)?.frame?.contentWindow !== win) return false;
    for (const other of ids(tree)) {
      if (other === id) continue;
      const descriptor = panes.get(other)?.hooks?.describe();
      if (descriptor?.terminalId === terminalId && descriptor.terminalVisible) {
        focus(other); notify("이 터미널이 열린 창으로 이동했습니다."); return false;
      }
    }
    return true;
  }
  function claimDictation() {
    if (embedded) return frameReady && parent.claimMic(ownId, root);
    return claimMic("p1", root);
  }
  function claimMic(id, win) {
    if (id !== "p1" && panes.get(id)?.frame?.contentWindow !== win) return false;
    for (const other of ids(tree)) if (other !== id && panes.get(other)?.hooks?.describe().dictation) {
      notify("다른 창에서 음성 입력 중입니다. 그 창의 마이크를 끈 뒤 시작해 주세요."); return false;
    }
    return true;
  }
  const SHARED_PATHS = new Set(["/api/snapshot", "/api/capabilities", "/api/terminals", "/api/usage", "/api/hardware", "/api/bot-requests"]);
  function sharedRead(path) {
    if (!embedded || !frameReady || !SHARED_PATHS.has(String(path).split("?")[0])) return null;
    return parent.readShared(ownId, root, path).then(copy);
  }
  async function readShared(id, win, path) {
    if (panes.get(id)?.frame?.contentWindow !== win || !SHARED_PATHS.has(String(path).split("?")[0])) throw new Error("연결된 창을 확인하지 못했습니다.");
    const info = hooks.describe();
    if (!info.authenticated) { const error = new Error("로그인이 필요합니다."); error.unauthorized = true; throw error; }
    const name = path.split("?")[0];
    if (name === "/api/snapshot" && info.snapshot) return info.snapshot;
    const cached = shared.get(name);
    if (cached && (cached.busy || name !== "/api/capabilities" && Date.now() - cached.at < 1000)) return cached.run;
    const entry = { at: Date.now(), busy: true };
    entry.run = hooks.api(name).then((value) => { entry.busy = false; return value; }, (error) => { shared.delete(name); throw error; });
    shared.set(name, entry);
    return entry.run;
  }
  function dataChanged(kind = "snapshot", value = null) {
    if (embedded || !hooks) return;
    if (kind === "terminals" && value) shared.set("/api/terminals", { at: Date.now(), busy: false, run: Promise.resolve(value) });
    changed();
    for (const [id, row] of panes) if (id !== "p1" && ids(tree).includes(id)) row.hooks?.refresh(kind);
  }
  function mutated() {
    if (embedded) { if (frameReady) parent.mutation(ownId, root); return; }
    shared.clear();
  }
  function mutation(id, win) {
    if (panes.get(id)?.frame?.contentWindow !== win) return;
    shared.clear();
    if (!refreshTimer) refreshTimer = root.setTimeout(() => { refreshTimer = null; hooks.refresh("mutation"); }, 250);
  }
  function locked() {
    if (embedded) { if (frameReady) parent.childLocked(ownId, root); return; }
    restoring = true;
    for (const [id, row] of panes) if (id !== "p1") { row.hooks?.locked(); row.node.remove(); panes.delete(id); }
    shared.clear(); tree = leaf("p1"); active = "p1"; maximized = false; restored = false;
    panes.get("p1").ref = null;
    layout({ save: false }); restoring = false;
  }
  function childLocked(id, win) {
    if (!restoring && panes.get(id)?.frame?.contentWindow === win) hooks.locked();
  }
  function targetAt(x, y) {
    for (const id of ids(tree)) {
      const row = panes.get(id);
      if (row.node.classList.contains("workspace-pane-covered")) continue;
      const box = row.node.getBoundingClientRect();
      if (x < box.left || x > box.right || y < box.top || y > box.bottom) continue;
      const rx = (x - box.left) / box.width, ry = (y - box.top) / box.height;
      const distances = { left: rx, right: 1 - rx, top: ry, bottom: 1 - ry };
      const edge = Object.entries(distances).sort((a, b) => a[1] - b[1])[0];
      return { id, edge: edge[1] < 0.27 ? edge[0] : "center", box };
    }
    return null;
  }
  function dropTarget(x, y, data) {
    const target = targetAt(x, y);
    if (!target || !data) return null;
    const from = data.pane && ids(tree).includes(data.pane) ? data.pane
      : typeof data.key === "string" && hooks.sessionExists(data.key) ? findRef({ key: data.key }) : null;
    if (!from && (typeof data.key !== "string" || !hooks.sessionExists(data.key))) return null;
    if (from === target.id) return { ...target, blocked: "이 세션은 이미 이 창에 열려 있습니다. 다른 세션을 끌어 분할해 주세요." };
    if (from) return { ...target, from };
    const row = panes.get(target.id), occupied = validRef(row.ref || row.hooks?.describe().ref);
    // A list drop onto a conversation adds a view. Replacing that conversation
    // made a successful center drop look like the split had done nothing.
    const edge = target.edge === "center" && occupied ? "right" : target.edge;
    return { ...target, edge, ...(edge !== "center" && ids(tree).length >= MAX_PANES
      ? { blocked: "창은 최대 4개까지 열 수 있습니다." } : {}) };
  }
  function cue(x, y) {
    const target = dropTarget(x, y, drag), node = $("workspace-drop-cue");
    if (!target) { node.hidden = true; return; }
    const outer = $("session-workspace").getBoundingClientRect(), box = target.box;
    let left = box.left - outer.left, top = box.top - outer.top, width = box.width, height = box.height;
    if (["left", "right"].includes(target.edge)) { width /= 2; if (target.edge === "right") left += width; }
    if (["top", "bottom"].includes(target.edge)) { height /= 2; if (target.edge === "bottom") top += height; }
    Object.assign(node.style, { left: `${left}px`, top: `${top}px`, width: `${width}px`, height: `${height}px` });
    node.textContent = target.blocked || (target.edge === "center" ? target.from ? "창 위치 교환" : "빈 창에서 열기" : "여기에 분할");
    node.hidden = false;
    return target;
  }
  function clearDrag() { drag = null; $("workspace-drop-cue").hidden = true; doc.body.classList.remove("workspace-dragging"); }
  function drop(x, y, data) {
    const target = dropTarget(x, y, data);
    if (!hooks?.describe().authenticated || !data) return;
    if (!target) { notify("세션을 대화 영역 안에 놓아 주세요."); return; }
    if ([...panes.values()].some((row) => row.hooks?.describe().composing)) {
      notify("한글 조합을 마친 뒤 분할해 주세요."); return;
    }
    if (target.blocked) { notify(target.blocked); return; }
    doc.body.classList.remove("workspace-list-open");
    if (target.from) {
      tree = move(tree, target.from, target.id, target.edge); active = target.from; maximized = false; layout(); focus(active);
    } else if (target.edge === "center") {
      const row = panes.get(target.id);
      focus(target.id, false); row.ref = { key: data.key, mode: "chat" }; applyRef(row); changed();
    } else add({ key: data.key, mode: "chat" }, target.id, target.edge);
  }
  function init(appHooks) {
    hooks = appHooks;
    if (embedded) {
      doc.body.classList.add("workspace-embedded");
      try {
        parent = root.parent !== root && root.parent.location.origin === root.location.origin && root.parent.SessionholicWorkspace;
        frameReady = !!parent?.attach(ownId, root, hooks);
      } catch { frameReady = false; }
      if (!frameReady) { root.location.replace("/"); return; }
      doc.addEventListener("keydown", (event) => parent.handleKey(event, ownId), true);
      doc.addEventListener("pointerdown", () => parent.focus(ownId, false), true);
      doc.addEventListener("focusin", () => parent.focus(ownId, false));
      return;
    }
    record("p1");
    const keys = $("workspace-keys");
    keys.value = hooks.prefs.get("workspaceKeys", "ctrl-meta");
    keys.addEventListener("change", () => hooks.prefs.set("workspaceKeys", keys.value === "ctrl-alt-meta" ? keys.value : "ctrl-meta"));
    $("workspace-split-open").addEventListener("click", () => add(null));
    $("home").addEventListener("click", (event) => {
      if (!enabled) return;
      event.preventDefault(); event.stopImmediatePropagation();
      doc.body.classList.toggle("workspace-list-open");
    }, true);
    doc.addEventListener("keydown", (event) => handleKey(event), true);
    $("workspace-primary").addEventListener("pointerdown", () => { if (enabled) focus("p1", false); }, true);
    $("workspace-primary").addEventListener("focusin", () => { if (enabled) focus("p1", false); });
    $("groups").addEventListener("dragstart", (event) => {
      const key = event.target.closest?.(".card")?.dataset.sessionKey;
      if (!key || !hooks.sessionExists(key)) return;
      drag = { key }; event.dataTransfer.setData(MIME, JSON.stringify(drag)); event.dataTransfer.effectAllowed = "copyMove";
      // The first drag also offers the original detail as a drop target.
      $("session-workspace").classList.add("workspace-drop-ready");
      doc.body.classList.add("workspace-dragging");
    });
    doc.addEventListener("dragenter", (event) => {
      if (drag || Array.from(event.dataTransfer?.types || []).includes(MIME)) event.preventDefault();
    }, true);
    doc.addEventListener("dragover", (event) => {
      if (!drag && !Array.from(event.dataTransfer?.types || []).includes(MIME)) return;
      event.preventDefault();
      const target = cue(event.clientX, event.clientY);
      if (target && event.dataTransfer) event.dataTransfer.dropEffect = target.from ? "move" : "copy";
    }, true);
    doc.addEventListener("drop", (event) => {
      if (!drag && !Array.from(event.dataTransfer?.types || []).includes(MIME)) return;
      event.preventDefault();
      let data = drag;
      try { data ||= JSON.parse(event.dataTransfer.getData(MIME)); } catch { /* untrusted or foreign drag */ }
      drop(event.clientX, event.clientY, data); clearDrag(); $("session-workspace").classList.remove("workspace-drop-ready");
    }, true);
    doc.addEventListener("dragend", () => { clearDrag(); $("session-workspace").classList.remove("workspace-drop-ready"); });
    root.addEventListener("resize", () => layout({ save: false }));
    root.addEventListener("beforeunload", (event) => {
      if ([...panes].some(([id, row]) => id !== "p1" && row.hooks?.unsaved())) {
        event.preventDefault(); event.returnValue = "";
      }
    });
  }
  root.SessionholicWorkspace = { ...model, embedded, init, changed, managed, visible, routeSession, routeNew, routeTerminal, routeSessionTerminal, claimTerminal, claimDictation, claimMic,
    sharedRead, dataChanged, mutated, locked, attach, report, readShared, mutation, childLocked, claim, focus, handleKey,
    add, closePane, layout, activeSessionKey, readingActive, isActive };
})(typeof window !== "undefined" ? window : null);
