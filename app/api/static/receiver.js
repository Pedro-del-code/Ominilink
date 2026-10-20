(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const STORE_KEY = "omnilink.receiver.v1";
  const MAX_ATTEMPTS = 8;
  const IMAGE_MIMES = ["image/png", "image/jpeg", "image/gif", "image/webp"];
  const KIND_MIMES = { text: ["text/plain"], html: ["text/html"], image: IMAGE_MIMES };
  const KIND_LABEL = { text: "texto", image: "imagem", html: "HTML" };
  const HISTORY_LIMIT = 12;
  const CSP = "default-src 'none'; img-src data:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'";
  const streaming = typeof fetch === "function" && typeof ReadableStream !== "undefined" &&
    typeof TextDecoder !== "undefined" && typeof AbortController !== "undefined";

  const state = { token: null, deviceId: null, name: null, attempts: 0, stop: false, controller: null,
                  seen: new Map(), history: [], transport: streaming ? "sse" : "http-longpoll" };

  // ------------------------------------------------------------------ utilidades
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;   // sempre textContent: o conteúdo é não confiável
    return node;
  }
  function setStatus(kind, text) {
    $("dot").dataset.state = kind;
    $("status-text").textContent = text;
  }
  function pulse() {
    const dot = $("dot");
    dot.classList.remove("pulse"); void dot.offsetWidth; dot.classList.add("pulse");
  }
  function capabilities(name) {
    return {
      schema: 1, name, platform: "browser", roles: ["receiver"], protocol_versions: [1],
      transports: streaming ? ["sse", "http-longpoll"] : ["http-longpoll"], codecs: ["json-b64"],
      content: {
        text: { max_bytes: 80000, mimes: ["text/plain"], max_chars: 20000 },
        html: { max_bytes: 200000, mimes: ["text/html"] },
        image: { max_bytes: 2000000, mimes: IMAGE_MIMES, max_width: 4096, max_height: 4096 }
      },
      features: ["html-sandbox", "ack-displayed"]
    };
  }

  // ------------------------------------------------------------------ armazenamento da credencial
  function store(persist) { try { return persist ? window.localStorage : window.sessionStorage; } catch (_) { return null; } }
  function loadSaved() {
    for (const persist of [true, false]) {
      try {
        const raw = store(persist) && store(persist).getItem(STORE_KEY);
        if (raw) { const v = JSON.parse(raw); if (v && v.token && v.deviceId) return v; }
      } catch (_) { /* armazenamento indisponível: segue sem credencial */ }
    }
    return null;
  }
  function saveCreds(persist) {
    try { store(persist).setItem(STORE_KEY, JSON.stringify({ token: state.token, deviceId: state.deviceId, name: state.name })); } catch (_) {}
  }
  function clearCreds() {
    for (const persist of [true, false]) { try { store(persist).removeItem(STORE_KEY); } catch (_) {} }
  }

  // ------------------------------------------------------------------ API
  function apiError(resp, data) {
    const err = new Error((data && data.error && data.error.message) || ("HTTP " + resp.status));
    err.status = resp.status; err.code = data && data.error && data.error.code;
    return err;
  }
  async function api(path, opts = {}) {
    const headers = Object.assign({ "Accept": "application/json" }, opts.headers || {});
    if (state.token) headers["Authorization"] = "Bearer " + state.token;
    if (opts.body !== undefined) headers["Content-Type"] = "application/json";
    return fetch(path, { method: opts.method || "GET", headers, signal: opts.signal, cache: "no-store", credentials: "omit",
                         body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined });
  }
  async function apiJson(path, opts) {
    const resp = await api(path, opts);
    let data = null;
    try { data = await resp.json(); } catch (_) {}
    if (!resp.ok) throw apiError(resp, data);
    return data;
  }
  async function sendAck(id, status, reason) {
    try { await apiJson("/v1/inbox/ack", { method: "POST", body: { id, status, reason: reason || null } }); }
    catch (_) { /* sem confirmação, o coordenador reentrega e deduplicamos pelo id */ }
  }

  // ------------------------------------------------------------------ validação (o receptor não confia no relé)
  function b64ToBytes(b64) {
    const bin = atob(b64);
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  }
  function magicOk(mime, b) {
    if (mime === "image/png") return b.length > 8 && b[0] === 0x89 && b[1] === 0x50 && b[2] === 0x4e && b[3] === 0x47;
    if (mime === "image/jpeg") return b.length > 3 && b[0] === 0xff && b[1] === 0xd8 && b[2] === 0xff;
    if (mime === "image/gif") return b.length > 6 && b[0] === 0x47 && b[1] === 0x49 && b[2] === 0x46 && b[3] === 0x38;
    if (mime === "image/webp") return b.length > 12 && b[0] === 0x52 && b[1] === 0x49 && b[8] === 0x57 && b[9] === 0x45;
    return false;
  }
  async function sha256Hex(bytes) {
    const digest = await crypto.subtle.digest("SHA-256", bytes);
    return Array.from(new Uint8Array(digest), (x) => x.toString(16).padStart(2, "0")).join("");
  }
  async function validate(m) {
    const caps = capabilities(state.name).content;
    if (!m || m.v !== 1 || m.type !== "message") return "MALFORMED_MESSAGE";
    if (m.to !== state.deviceId) return "RECIPIENT_MISMATCH";
    if (!KIND_MIMES[m.kind] || !KIND_MIMES[m.kind].includes(m.mime)) return "CONTENT_UNSUPPORTED";
    if (typeof m.body !== "string" || typeof m.sha256 !== "string") return "MALFORMED_MESSAGE";
    let bytes;
    try {
      if (m.kind === "image") {
        if (m.encoding !== "base64" || !/^[A-Za-z0-9+/]+={0,2}$/.test(m.body)) return "MALFORMED_MESSAGE";
        bytes = b64ToBytes(m.body);
        if (!magicOk(m.mime, bytes)) return "INVALID_IMAGE";
      } else {
        if (m.encoding !== "utf-8") return "MALFORMED_MESSAGE";
        bytes = new TextEncoder().encode(m.body);
      }
    } catch (_) { return "MALFORMED_MESSAGE"; }
    if (bytes.length !== m.size) return "INTEGRITY_FAILED";
    if (bytes.length > caps[m.kind].max_bytes) return "SIZE_LIMIT_EXCEEDED";
    if (window.crypto && crypto.subtle && (await sha256Hex(bytes)) !== m.sha256) return "INTEGRITY_FAILED";
    return null;
  }

  // ------------------------------------------------------------------ apresentação
  function buildBody(m) {
    if (m.kind === "text") return el("p", "msg-text", m.body);
    if (m.kind === "image") {
      const img = el("img");
      img.alt = "Imagem recebida" + (m.filename ? ": " + m.filename : "");
      img.decoding = "async";
      img.src = "data:" + m.mime + ";base64," + m.body;   // mime e base64 já validados
      return img;
    }
    const frame = el("iframe", "html-frame");
    frame.setAttribute("sandbox", "");                    // sem scripts, formulários, popups ou navegação
    frame.setAttribute("referrerpolicy", "no-referrer");
    frame.title = "Conteúdo HTML recebido (isolado)";
    frame.srcdoc = "<!doctype html><meta charset=\"utf-8\"><meta http-equiv=\"Content-Security-Policy\" content=\"" + CSP + "\">" +
      "<style>body{font:16px/1.55 system-ui,sans-serif;margin:16px;color:#16212b}img{max-width:100%}</style>" + m.body;
    return frame;
  }
  function buildItem(m, animate) {
    const item = el("div", "item" + (animate ? " arrive" : ""));
    const meta = el("div", "meta");
    meta.append(el("span", "badge", KIND_LABEL[m.kind]), el("span", "mono", "de " + m.from),
                el("span", null, new Date((m.ts || Date.now() / 1000) * 1000).toLocaleTimeString()));
    if (m.adapted && m.adapted.length) meta.append(el("span", null, "adaptado: " + m.adapted.join(", ")));
    item.append(meta, buildBody(m));
    return item;
  }
  function showInStage(m, animate) {
    const stage = $("stage-item");
    stage.replaceChildren(buildItem(m, animate));
    stage.hidden = false;
    $("stage-empty").hidden = true;
  }
  function addHistory(m) {
    state.history.unshift(m);
    state.history.length = Math.min(state.history.length, HISTORY_LIMIT);
    const list = $("history");
    list.replaceChildren(...state.history.map((h) => {
      const li = el("li"), btn = el("button");
      btn.type = "button";
      btn.append(el("span", "badge", KIND_LABEL[h.kind]));
      if (h.kind === "image") { const t = el("img"); t.alt = ""; t.src = "data:" + h.mime + ";base64," + h.body; btn.append(t); }
      else btn.append(el("span", "preview", h.kind === "html" ? "Página HTML" : h.body.slice(0, 120)));
      btn.addEventListener("click", () => showInStage(h, false));
      li.append(btn);
      return li;
    }));
  }
  async function onMessage(m) {
    if (m && state.seen.has(m.id)) { await sendAck(m.id, state.seen.get(m.id)); return; }   // reentrega
    const problem = await validate(m);
    if (problem) { if (m && m.id) await sendAck(m.id, "rejected", problem); return; }
    showInStage(m, !window.matchMedia("(prefers-reduced-motion: reduce)").matches);
    addHistory(m);
    pulse();
    state.seen.set(m.id, "displayed");
    if (state.seen.size > 500) state.seen.delete(state.seen.keys().next().value);
    await sendAck(m.id, "displayed");
  }

  // ------------------------------------------------------------------ pedidos de autorização
  function onGrantRequest(p) {
    const box = $("grants");
    if (!p || !p.id || box.querySelector('[data-grant="' + CSS.escape(p.id) + '"]')) return;
    const card = el("div", "grant");
    card.dataset.grant = p.id;
    const kinds = (p.kinds || []).map((k) => KIND_LABEL[k] || k).join(", ");
    card.append(el("p", null, (p.from_name || "Um dispositivo") + " (" + p.from + ") quer enviar: " + kinds + "."));
    const row = el("div", "row");
    const decide = async (decision) => {
      try { await apiJson("/v1/grants/requests/" + encodeURIComponent(p.id), { method: "POST", body: { decision } }); card.remove(); }
      catch (err) { card.append(el("p", "error", "Falha: " + err.message)); }
    };
    const allow = el("button", "primary", "Permitir"), deny = el("button", "ghost", "Negar");
    allow.type = deny.type = "button";
    allow.addEventListener("click", () => decide("approve"));
    deny.addEventListener("click", () => decide("deny"));
    row.append(allow, deny);
    card.append(row);
    box.append(card);
  }

  // ------------------------------------------------------------------ conexão (SSE por fetch; fallback long-poll)
  function dispatch(event, payload) {
    if (event === "ready") { state.attempts = 0; setStatus("ok", "conectado · " + state.transport); }
    else if (event === "message") return onMessage(payload);
    else if (event === "grant.request") onGrantRequest(payload);
    else if (event === "revoked") { const e = new Error("dispositivo revogado"); e.status = 401; throw e; }
  }
  async function handleBlock(block) {
    let event = "message";
    const data = [];
    for (const line of block.split("\n")) {
      if (line.startsWith(":")) continue;
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) data.push(line.slice(5).replace(/^ /, ""));
    }
    if (!data.length) return;
    let payload;
    try { payload = JSON.parse(data.join("\n")); } catch (_) { return; }
    await dispatch(event, payload);
  }
  async function runStream(signal) {
    state.transport = "sse";
    const resp = await api("/v1/stream", { signal, headers: { "Accept": "text/event-stream" } });
    if (!resp.ok) { let d = null; try { d = await resp.json(); } catch (_) {} throw apiError(resp, d); }
    const reader = resp.body.getReader(), decoder = new TextDecoder("utf-8");
    let buf = "", last = Date.now();
    const watchdog = setInterval(() => { if (Date.now() - last > 45000) state.controller.abort(); }, 5000);
    try {
      for (;;) {
        const { value, done } = await reader.read();
        if (done) return;
        last = Date.now();
        buf += decoder.decode(value, { stream: true });
        if (buf.length > 8000000) throw new Error("fluxo excede o tamanho máximo");
        let idx;
        while ((idx = buf.indexOf("\n\n")) >= 0) { const block = buf.slice(0, idx); buf = buf.slice(idx + 2); await handleBlock(block); }
      }
    } finally { clearInterval(watchdog); }
  }
  async function runLongPoll(signal) {
    state.transport = "http-longpoll";
    let first = true;
    while (!state.stop) {
      // a primeira consulta é imediata (wait=0): confirma a conexão sem esperar; as seguintes aguardam até 20 s
      const data = await apiJson("/v1/inbox?wait=" + (first ? 0 : 20) + "&limit=10&codec=json-b64", { signal });
      if (first) { first = false; await dispatch("ready", {}); }
      for (const item of data.items || []) await dispatch(item.type === "grant.request" ? "grant.request" : "message", item);
    }
  }
  async function connectLoop() {
    state.stop = false;
    $("retry").hidden = true;
    while (!state.stop) {
      try {
        state.controller = new AbortController();
        await (streaming ? runStream(state.controller.signal) : runLongPoll(state.controller.signal));
        await sleep(200);                       // fim normal do fluxo: reconecta sem penalidade
      } catch (err) {
        if (state.stop) return;
        if (err && (err.status === 401 || err.status === 403)) {
          setStatus("bad", "credencial inválida ou revogada"); clearCreds(); state.stop = true; showSetup(); return;
        }
        state.attempts += 1;
        if (state.attempts > MAX_ATTEMPTS) { setStatus("bad", "sem conexão: limite de tentativas atingido"); $("retry").hidden = false; return; }
        setStatus("warn", "reconectando (" + state.attempts + "/" + MAX_ATTEMPTS + ")");
        await sleep(Math.min(15000, 500 * 2 ** (state.attempts - 1)) * (0.8 + Math.random() * 0.4));
      }
    }
  }

  // ------------------------------------------------------------------ telas
  function showSetup() { $("setup").hidden = false; $("inbox").hidden = true; setStatus("idle", "desconectado"); }
  function showInbox() {
    $("setup").hidden = true; $("inbox").hidden = false;
    $("device-id").textContent = state.deviceId + " · " + state.name;
    setStatus("warn", "conectando");
  }
  async function onSetup(ev) {
    ev.preventDefault();
    const err = $("setup-error"); err.textContent = "";
    const name = $("device-name").value.trim(), key = $("enroll-key").value;
    try {
      const resp = await fetch("/v1/devices", { method: "POST", credentials: "omit", cache: "no-store",
        headers: { "Content-Type": "application/json", "Accept": "application/json", "X-Enrollment-Key": key },
        body: JSON.stringify({ capabilities: capabilities(name) }) });
      let data = null; try { data = await resp.json(); } catch (_) {}
      if (!resp.ok) throw apiError(resp, data);
      state.token = data.token; state.deviceId = data.device_id; state.name = name;
      saveCreds($("remember").checked);
      $("enroll-key").value = "";
      showInbox(); connectLoop();
    } catch (e) { err.textContent = e.message; }
  }
  async function onForget() {
    if (!window.confirm("Revogar este dispositivo no coordenador e esquecer as credenciais?")) return;
    state.stop = true; if (state.controller) state.controller.abort();
    try { await apiJson("/v1/devices/me", { method: "DELETE" }); } catch (_) {}
    clearCreds(); state.token = null; showSetup();
  }
  function boot() {
    $("setup-form").addEventListener("submit", onSetup);
    $("forget").addEventListener("click", onForget);
    $("retry").addEventListener("click", () => { state.attempts = 0; connectLoop(); });
    $("device-name").value = "navegador-" + Math.random().toString(16).slice(2, 6);
    const saved = loadSaved();
    if (saved) { Object.assign(state, saved); showInbox(); connectLoop(); } else showSetup();
  }
  boot();
})();
