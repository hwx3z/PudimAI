/* PudimAI Web — cliente da plataforma (apenas REST + WebSocket).
   O navegador é só mais uma interface assinando o mesmo EventBus do Core.
   Inclui: autenticação local, menu de contexto (renomear/excluir),
   streaming do agente e modal de permissões. */
"use strict";

const $ = (sel) => document.querySelector(sel);
const TOKEN_KEY = "pudimai_token";

const state = {
  chatId: null,
  runningTaskId: null,
  ws: null,
  editingChatId: null,
};

/* ---------------- token / api ---------------- */
function getToken() { return localStorage.getItem(TOKEN_KEY) || ""; }
function setToken(t) { localStorage.setItem(TOKEN_KEY, t); }
function clearToken() { localStorage.removeItem(TOKEN_KEY); }

async function api(path, options = {}) {
  const headers = { "Content-Type": "application/json",
                    ...(options.headers || {}) };
  if (getToken()) headers.Authorization = `Bearer ${getToken()}`;
  const response = await fetch(path, { headers, ...options });
  if (!response.ok) {
    if (response.status === 401 && !path.startsWith("/api/auth")) {
      clearToken();
      showAuth();
    }
    let detail = await response.text();
    try { detail = JSON.parse(detail).detail || detail; } catch {}
    throw new Error(`${response.status}: ${detail}`);
  }
  return response.json();
}

/* ---------------- utilitários ---------------- */
function escapeHtml(text) {
  return text.replace(/&/g, "&amp;").replace(/</g, "&lt;")
             .replace(/>/g, "&gt;");
}

function renderMarkdown(raw) {
  let html = escapeHtml(raw);
  const blocks = [];
  html = html.replace(/```(\w*)\n([\s\S]*?)```/g, (_, lang, code) => {
    blocks.push(`<pre><code>${code}</code></pre>`);
    return `\u0000BLOCK${blocks.length - 1}\u0000`;
  });
  html = html.replace(/`([^`\n]+)`/g, "<code>$1</code>");
  html = html.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
  html = html.replace(/^###?\s+(.+)$/gm, "<h4>$1</h4>");
  html = html.replace(/^[-*]\s+(.+)$/gm, "<li>$1</li>");
  html = html.replace(/(<li>[\s\S]*?<\/li>)(?!\s*<li>)/g, "<ul>$1</ul>");
  html = html.split(/\n{2,}/).map(p => /^<(ul|h4|pre)/.test(p.trim())
    ? p : `<p>${p.replace(/\n/g, "<br>")}</p>`).join("");
  html = html.replace(/\u0000BLOCK(\d+)\u0000/g, (_, i) => blocks[i]);
  return html;
}

function clip(text, limit = 220) {
  return text.length <= limit ? text : text.slice(0, limit) + "…";
}

let toastTimer = null;
function toast(message, kind = "info") {
  const el = $("#toast");
  el.textContent = message;
  el.className = `toast toast-${kind}`;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 3200);
}

/* ---------------- autenticação ---------------- */
let authMode = "login";

function showAuth() {
  $("#auth-screen").hidden = false;
  $("#app").style.visibility = "hidden";
  $("#auth-error").textContent = "";
  setTimeout(() => $("#auth-user").focus(), 50);
}

function hideAuth() {
  $("#auth-screen").hidden = true;
  $("#app").style.visibility = "visible";
}

function setAuthMode(mode) {
  authMode = mode;
  $("#tab-login").classList.toggle("active", mode === "login");
  $("#tab-register").classList.toggle("active", mode === "register");
  $("#auth-submit").textContent =
    mode === "login" ? "Entrar" : "Criar conta";
  $("#auth-pass").autocomplete =
    mode === "login" ? "current-password" : "new-password";
  $("#auth-error").textContent = "";
}

$("#tab-login").onclick = () => setAuthMode("login");
$("#tab-register").onclick = () => setAuthMode("register");

$("#auth-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const username = $("#auth-user").value.trim();
  const password = $("#auth-pass").value;
  const errorEl = $("#auth-error");
  errorEl.textContent = "";
  try {
    const result = await api(`/api/auth/${authMode}`, {
      method: "POST",
      body: JSON.stringify({ username, password }),
    });
    setToken(result.token);
    hideAuth();
    await bootApp();
    toast(`Bem-vindo(a), ${result.username}!`, "ok");
  } catch (error) {
    errorEl.textContent = String(error.message).replace(/^\d+: /, "");
  }
});

$("#logout-btn").onclick = async () => {
  await api("/api/auth/logout", { method: "POST" }).catch(() => {});
  clearToken();
  state.chatId = null;
  if (state.ws) { try { state.ws.close(); } catch {} }
  if (termState.ws) { try { termState.ws.close(); } catch {} }
  showAuth();
};

/* ---------------- sidebar / status ---------------- */
async function refreshStatus() {
  const data = await api("/api/status");
  $("#ws-path").textContent = data.workspace.root;
  $("#ws-stack").innerHTML =
    data.workspace.stack.slice(0, 4).map(s =>
      `<span class="chip">${escapeHtml(s)}</span>`).join("");
  $("#security-badge").textContent = data.security_mode;
  if (data.user) $("#whoami").textContent = data.user;
  $("#status-dot").classList.toggle("ok", !!data.ollama_ok);
  const select = $("#model-select");
  select.innerHTML = "";
  for (const model of [...new Set(
      [...(data.models || []), data.model].filter(Boolean))].sort()) {
    const option = document.createElement("option");
    option.value = option.textContent = model;
    if (model === data.model) option.selected = true;
    select.appendChild(option);
  }
}

async function refreshChats() {
  const chats = await api("/api/chats");
  const list = $("#chat-list");
  list.innerHTML = "";
  for (const chat of chats) {
    const button = document.createElement("button");
    button.className = "chat-item" +
      (chat.id === state.chatId ? " active" : "");
    button.title = `${chat.title}\n${chat.messages} mensagens\n` +
      `(clique direito: renomear/excluir)`;

    if (chat.id === state.editingChatId) {
      const input = document.createElement("input");
      input.className = "rename-input";
      input.value = chat.title;
      input.maxLength = 120;

      const commit = async () => {
        const title = input.value.trim();
        state.editingChatId = null;
        if (title && title !== chat.title) {
          try {
            await api(`/api/chats/${chat.id}`, {
              method: "PATCH",
              body: JSON.stringify({ title }),
            });
            if (state.chatId === chat.id)
              $("#chat-title").textContent = title;
          } catch (error) {
            toast(String(error.message).replace(/^\d+: /, ""), "err");
          }
        }
        refreshChats().catch(() => {});
      };
      const cancel = () => {
        state.editingChatId = null;
        refreshChats().catch(() => {});
      };

      input.addEventListener("keydown", (e) => {
        e.stopPropagation();
        if (e.key === "Enter") commit();
        if (e.key === "Escape") cancel();
      });
      input.addEventListener("blur", cancel);
      input.addEventListener("click", e => e.stopPropagation());
      input.addEventListener("contextmenu", e => e.stopPropagation());

      button.appendChild(input);
      button.classList.add("editing");
      setTimeout(() => { input.focus(); input.select(); }, 20);
    } else {
      button.textContent = chat.title || chat.id;
      button.onclick = () => openChat(chat.id);
      button.oncontextmenu = (e) => {
        e.preventDefault();
        openCtxMenu(e.clientX, e.clientY, chat);
      };
    }
    list.appendChild(button);
  }
}

$("#new-chat").onclick = async () => {
  const chat = await api("/api/chats", {
    method: "POST", body: JSON.stringify({ title: "Nova conversa" }) });
  await openChat(chat.id);
};

$("#model-select").onchange = async (event) => {
  event.target.selectedIndex = 0; // troca de modelo virá em rota própria
};

/* ---------------- menu de contexto ---------------- */
const ctxMenu = $("#ctx-menu");
let ctxTargetChat = null;

function openCtxMenu(x, y, chat) {
  ctxTargetChat = chat;
  ctxMenu.hidden = false;
  const rect = ctxMenu.getBoundingClientRect();
  ctxMenu.style.left =
    Math.min(x, window.innerWidth - rect.width - 8) + "px";
  ctxMenu.style.top =
    Math.min(y, window.innerHeight - rect.height - 8) + "px";
}

window.addEventListener("click", () => { ctxMenu.hidden = true; });
ctxMenu.addEventListener("click", (e) => e.stopPropagation());
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") ctxMenu.hidden = true;
});

ctxMenu.addEventListener("click", async (event) => {
  const action = event.target.dataset?.action;
  if (!action || !ctxTargetChat) return;
  const chat = ctxTargetChat;
  ctxMenu.hidden = true;

  if (action === "rename") {
    state.editingChatId = chat.id;
    refreshChats().catch(() => {});
  }

  if (action === "delete") {
    const ok = await confirmDialog(
      "Excluir conversa",
      `Excluir "${chat.title}"? Esta ação não pode ser desfeita.`);
    if (!ok) return;
    try {
      await api(`/api/chats/${chat.id}`, { method: "DELETE" });
      toast("Conversa excluída.", "ok");
      if (state.chatId === chat.id) {
        state.chatId = null;
        $("#messages").innerHTML = "";
        showEmptyState();
        $("#chat-title").textContent = "PudimAI";
        history.replaceState(null, "", "#");
      }
      refreshChats().catch(() => {});
    } catch (error) {
      toast(String(error.message).replace(/^\d+: /, ""), "err");
    }
  }
});

/* ---------------- confirmação genérica ---------------- */
function confirmDialog(title, message) {
  return new Promise((resolve) => {
    const modal = $("#confirm-modal");
    $("#confirm-title").textContent = title;
    $("#confirm-msg").textContent = message;
    modal.hidden = false;
    const done = (answer) => {
      modal.hidden = true;
      $("#confirm-ok").onclick = null;
      $("#confirm-cancel").onclick = null;
      resolve(answer);
    };
    $("#confirm-ok").onclick = () => done(true);
    $("#confirm-cancel").onclick = () => done(false);
  });
}

/* ---------------- chat ---------------- */
function addMessage(role, content) {
  $(".empty-state")?.remove();
  const wrap = document.createElement("div");
  wrap.className = `msg ${role}`;
  wrap.innerHTML = `<span class="who">${role === "user" ? "Você"
    : "PudimAI"}</span>
    <div class="bubble">${role === "user"
      ? escapeHtml(content) : renderMarkdown(content)}</div>`;
  const anchor = document.createElement("div");
  $("#messages").appendChild(anchor);
  anchor.replaceWith(wrap);
  scrollBottom();
  return wrap;
}

function showEmptyState() {
  if ($("#messages").children.length) return;
  $("#messages").innerHTML = `
    <div class="empty-state">
      <h1>PudimAI</h1>
      <p>Agente autônomo de engenharia de software — IA local.</p>
      <p>
        <code>analise meu projeto</code>
        <code>crie uma API FastAPI</code><br>
        <code>corrija os testes</code>
        <code>procure vulnerabilidades</code>
      </p>
    </div>`;
}

function scrollBottom() {
  const box = $("#messages");
  box.scrollTop = box.scrollHeight;
}

async function openChat(chatId) {
  state.chatId = chatId;
  history.replaceState(null, "", `#${chatId}`);
  connectWs();
  const chat = await api(`/api/chats/${chatId}`);
  $("#messages").innerHTML = "";
  for (const message of chat.messages) addMessage(message.role,
                                                   message.content);
  if (!chat.messages.length) showEmptyState();
  $("#chat-title").textContent = chat.title;
  setRunning(chat.running ? chat.task_id : null);
  await refreshChats();
  if (chat.running && chat.task_id) attachToTask(chat.task_id);
}

/* ---------------- atividade do agente ---------------- */
function setRunning(taskId) {
  state.runningTaskId = taskId;
  $("#activity").hidden = !taskId;
  $("#send-btn").disabled = !!taskId;
  const badge = $("#agent-state");
  badge.textContent = taskId ? "trabalhando…" : "idle";
  badge.classList.toggle("busy", !!taskId);
  if (taskId) $("#activity-list").innerHTML = "";
}

function logActivity(kind, text, detail = "") {
  const li = document.createElement("li");
  li.className = kind;
  if (detail) {
    const details = document.createElement("details");
    const summary = document.createElement("summary");
    summary.textContent = text;
    const pre = document.createElement("pre");
    pre.textContent = detail;
    details.appendChild(summary);
    details.appendChild(pre);
    li.appendChild(details);
  } else {
    li.textContent = text;
  }
  const list = $("#activity-list");
  list.appendChild(li);
  while (list.children.length > 200) list.firstChild.remove();
  list.scrollTop = list.scrollHeight;
}

const ACTIVITY_MAP = {
  "agent.started":     () => ["run", "iniciando tarefa"],
  "agent.planning":    () => ["run", "elaborando plano…"],
  "agent.plan_created": (p) => ["ok",
    "plano: " + (p.plan || []).slice(0, 6).join(" · ")],
  "agent.iteration":   (p) => ["run", `iteração ${p.iteration}`],
  "tool.started":      (p) => ["run",
    `${p.name}(${clip(JSON.stringify(p.arguments ?? {}), 120)})`],
  "tool.completed":    (p) => ["ok",
    `${p.name} concluída`, clip(p.summary || "", 1500)],
  "tool.failed":       (p) => ["fail",
    `${p.name} falhou`, clip(p.summary || "", 1500)],
  "tool.denied":       (p) => ["fail",
    `bloqueado: ${clip(p.command || "", 80)}`],
  "permission.required": (p) => p.request_id ? ["fail",
    `permissão pedida: ${clip(p.command || "", 90)}`] : null,
  "file.changed":      (p) => ["ok", `arquivo alterado: ${p.path}`],
  "terminal.completed": (p) => ["ok",
    `terminal saiu com código ${p.exit_code}`],
  "agent.error":       (p) => ["fail", `erro: ${clip(p.error || "", 200)}`],
  "task.cancelled":    () => ["fail", "tarefa cancelada"],
  "agent.completed":   () => ["ok", "tarefa finalizada"],
};

/* ---------------- websocket ---------------- */
function connectWs() {
  if (state.ws) { try { state.ws.close(); } catch {} }
  if (!state.chatId || !getToken()) return;
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(
    `${proto}://${location.host}/ws/${state.chatId}?token=` +
    encodeURIComponent(getToken()));
  state.ws = ws;

  ws.onmessage = (event) => {
    let envelope;
    try { envelope = JSON.parse(event.data); } catch { return; }
    handleEnvelope(envelope);
  };
  ws.onclose = (event) => {
    if (event.code === 4401) { clearToken(); showAuth(); return; }
    if (state.chatId && getToken()) setTimeout(connectWs, 2500);
  };
}

function handleEnvelope(envelope) {
  const type = envelope.type;
  const payload = envelope.payload || {};

  if (type === "llm.chunk") { streamAssistantChunk(payload.text || ""); return; }
  finishAssistantStream();

  if (type === "permission.required" && payload.request_id) {
    showPermissionModal(payload);
  }

  if (state.runningTaskId) {
    const renderer = ACTIVITY_MAP[type];
    const rendered = renderer ? renderer(payload) : null;
    if (rendered) logActivity(rendered[0], rendered[1], rendered[2] || "");
  }
  if (type === "agent.completed" || type === "task.cancelled") {
    setRunning(null);
    refreshChats().catch(() => {});
  }
}

let streamingEl = null;
function streamAssistantChunk(text) {
  if (!text) return;
  if (!streamingEl) streamingEl = addMessage("assistant", "");
  const bubble = streamingEl.querySelector(".bubble");
  bubble.dataset.raw = (bubble.dataset.raw || "") + text;
  bubble.innerHTML = renderMarkdown(bubble.dataset.raw);
  scrollBottom();
}
function finishAssistantStream() { streamingEl = null; }

function attachToTask(taskId) { setRunning(taskId); }

/* ---------------- permissões remotas ---------------- */
function showPermissionModal(payload) {
  if (!payload || !payload.request_id) return;
  $("#perm-command").textContent = payload.command || "?";
  $("#perm-reason").textContent =
    `Motivo: ${payload.reason || "—"} · modo ${payload.mode || "—"}`;
  const modal = $("#perm-modal");
  const allow = $("#perm-allow");
  const deny = $("#perm-deny");
  modal.hidden = false;

  const submit = async (approved) => {
    allow.disabled = deny.disabled = true;
    try {
      await api(`/api/permissions/${payload.request_id}`, {
        method: "POST", body: JSON.stringify({ approved }),
      });
      modal.hidden = true;
    } catch (error) {
      $("#perm-reason").textContent =
        `Falha ao responder (${error.message}). O pedido pode ter ` +
        `expirado — peça a ação de novo.`;
    } finally {
      allow.disabled = deny.disabled = false;
    }
  };
  allow.onclick = () => submit(true);
  deny.onclick = () => submit(false);
}

/* ---------------- envio ---------------- */
async function send() {
  const input = $("#input");
  const task = input.value.trim();
  if (!task || state.runningTaskId) return;
  input.value = "";
  input.style.height = "auto";

  let chatId = state.chatId;
  if (!chatId) {
    const chat = await api("/api/chats", {
      method: "POST", body: JSON.stringify({ title: task.slice(0, 48) }) });
    chatId = state.chatId = chat.id;
    connectWs();
    $("#chat-title").textContent = chat.title;
  }
  addMessage("user", task);
  try {
    const response = await api(`/api/chats/${chatId}/tasks`, {
      method: "POST", body: JSON.stringify({ task }),
    });
    attachToTask(response.task_id);
  } catch (error) {
    addMessage("assistant",
      `**Falha ao iniciar tarefa**\n\n\`${error.message}\``);
  }
}

$("#send-btn").onclick = send;
$("#input").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    send();
  }
});
$("#input").addEventListener("input", (event) => {
  event.target.style.height = "auto";
  event.target.style.height = Math.min(event.target.scrollHeight, 180) + "px";
});

$("#stop-btn").onclick = async () => {
  if (!state.runningTaskId) return;
  await api(`/api/tasks/${state.runningTaskId}/cancel`,
            { method: "POST" }).catch(() => {});
};

document.addEventListener("keydown", (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "j") {
    event.preventDefault();
    $("#new-chat").click();
  }
});

/* ---------------- abas (Conversa | Computador da IA) ---------------- */
let activeView = "chat";

document.querySelectorAll("#view-tabs button").forEach((button) => {
  button.onclick = () => showView(button.dataset.view);
});

function showView(which) {
  activeView = which;
  $("#view-chat").hidden = which !== "chat";
  $("#view-computer").hidden = which !== "computer";
  document.querySelectorAll("#view-tabs button").forEach((b) =>
    b.classList.toggle("active", b.dataset.view === which));
  if (which === "computer") {
    loadTree();
    termConnect();
  }
}

/* ---------------- explorador de arquivos ---------------- */
const expandedDirs = new Set(["."]);
let currentFile = null;
let isDirty = false;

async function loadTree() {
  const root = $("#tree");
  root.innerHTML = "";
  await renderDirInto(root, ".", 0);
}

async function renderDirInto(container, path, depth) {
  let data;
  try {
    data = await api(`/api/fs/tree?path=${encodeURIComponent(path)}`);
  } catch (error) {
    container.innerHTML =
      `<div class="tree-empty">${escapeHtml(String(error.message))}</div>`;
    return;
  }
  if (!data.entries.length) {
    container.innerHTML = `<div class="tree-empty">(vazio)</div>`;
    return;
  }
  for (const entry of data.entries) {
    const childPath = path === "." ? entry.name : `${path}/${entry.name}`;
    const row = document.createElement("div");
    row.className = "tree-row";
    row.style.paddingLeft = (depth * 14 + 8) + "px";
    row.title = childPath;

    if (entry.type === "dir") {
      const open = expandedDirs.has(childPath);
      row.innerHTML =
        `<span class="tw ${open ? "open" : ""}">▶</span>` +
        `<span>${escapeHtml(entry.name)}</span>`;
      row.onclick = () => {
        if (expandedDirs.has(childPath)) expandedDirs.delete(childPath);
        else expandedDirs.add(childPath);
        row.querySelector(".tw").classList.toggle("open");
        const sub = row.nextSibling;
        const exists = sub && sub.classList?.contains("tree-sub");
        if (expandedDirs.has(childPath) && !exists) {
          const wrap = document.createElement("div");
          wrap.className = "tree-sub";
          row.after(wrap);
          renderDirInto(wrap, childPath, depth + 1);
        } else if (exists) sub.remove();
      };
      container.appendChild(row);
      if (open) {
        const wrap = document.createElement("div");
        wrap.className = "tree-sub";
        container.appendChild(wrap);
        await renderDirInto(wrap, childPath, depth + 1);
      }
    } else {
      row.innerHTML =
        `<span class="tw">·</span>` +
        `<span>${escapeHtml(entry.name)}</span>` +
        `<span class="fsize">${fmtSize(entry.size)}</span>`;
      if (currentFile === childPath)
        row.classList.add("active-file");
      row.onclick = () => openFile(childPath);
      container.appendChild(row);
    }
  }
}

function fmtSize(bytes) {
  if (bytes < 0) return "?";
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " kB";
  return (bytes / (1024 * 1024)).toFixed(1) + " MB";
}

$("#tree-refresh").onclick = () => loadTree().catch(() => {});

/* ---------------- editor ---------------- */
const editorEl = $("#editor");

async function openFile(path) {
  try {
    const data = await api(
      `/api/file/raw?path=${encodeURIComponent(path)}`);
    currentFile = path;
    editorEl.value = data.content;
    editorEl.disabled = false;
    setDirty(false);
    $("#editor-path").textContent = path;
    refreshChatsHighlight(path);
    document.querySelectorAll(".tree-row").forEach((row) => {
      row.classList.toggle("active-file", row.title === path);
    });
  } catch (error) {
    toast(String(error.message).replace(/^\d+: /, ""), "err");
  }
}

function setDirty(dirty) {
  isDirty = dirty;
  $("#dirty-mark").hidden = !dirty;
  $("#save-btn").disabled = !dirty || !currentFile;
}

editorEl.addEventListener("input", () => setDirty(true));
editorEl.addEventListener("keydown", (e) => {
  if (e.key === "Tab") { // tab insere indentação em vez de sair do campo
    e.preventDefault();
    const start = editorEl.selectionStart;
    editorEl.setRangeText("    ", start, editorEl.selectionEnd, "end");
    setDirty(true);
  }
});

async function saveFile() {
  if (!currentFile || !isDirty) return;
  try {
    await api("/api/file/raw", {
      method: "PUT",
      body: JSON.stringify({ path: currentFile,
                             content: editorEl.value }),
    });
    setDirty(false);
    toast("Arquivo salvo.", "ok");
  } catch (error) {
    toast(String(error.message).replace(/^\d+: /, ""), "err");
  }
}

$("#save-btn").onclick = saveFile;

document.addEventListener("keydown", (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
    if (activeView === "computer" && currentFile) {
      event.preventDefault();
      saveFile();
    }
  }
});

function refreshChatsHighlight(_path) { /* hook futuro */ }

/* ---------------- terminal interativo ---------------- */
const termState = { ws: null, history: [], historyIndex: -1 };

function termConnect() {
  if (termState.ws &&
      (termState.ws.readyState === WebSocket.OPEN ||
       termState.ws.readyState === WebSocket.CONNECTING)) return;
  if (!getToken()) return;
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(
    `${proto}://${location.host}/ws/term?token=` +
    encodeURIComponent(getToken()));
  termState.ws = ws;

  ws.onmessage = (event) => {
    let message;
    try { message = JSON.parse(event.data); } catch { return; }
    if (message.type === "output") appendTerm(message.data);
    else if (message.type === "ws.ready")
      appendTerm(`-- shell conectado (${message.payload.shell}) — ` +
                 `cwd: workspace --\n`);
    else if (message.type === "ws.error")
      appendTerm(`-- ${message.payload.reason} --\n`);
    else if (message.type === "ws.closed")
      appendTerm("\n-- sessão encerrada; reabra a aba --\n");
    else if (message.type === "ws.unauthorized") {
      clearToken(); showAuth();
    }
  };
}

const ANSI_RE = /\x1B\[[0-9;?]*[A-Za-z]|\x1B\][^\x07]*\x07|\x1B[=>]|\x1Bc/g;

function appendTerm(rawText) {
  const out = $("#term-out");
  const clean = rawText
    .replace(/\r\n/g, "\n")
    .replace(/\r/g, "\n")
    .replace(ANSI_RE, "");
  out.textContent += clean;
  while (out.textContent.length > 400_000)
    out.textContent = out.textContent.slice(-300_000);
  out.scrollTop = out.scrollHeight;
}

async function sendTermLine() {
  const inputEl = $("#term-in");
  const line = inputEl.value;
  inputEl.value = "";
  if (!line.trim()) {
    termState.ws?.send(JSON.stringify({ type: "input", data: "\n" }));
    return;
  }
  termState.history.push(line);
  termState.historyIndex = termState.history.length;
  termState.ws?.send(JSON.stringify({ type: "input",
                                      data: line + "\n" }));
}

$("#term-in").addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    sendTermLine();
  } else if (event.key === "ArrowUp") {
    event.preventDefault();
    if (termState.historyIndex > 0) {
      termState.historyIndex -= 1;
      event.target.value = termState.history[termState.historyIndex];
    }
  } else if (event.key === "ArrowDown") {
    event.preventDefault();
    if (termState.historyIndex < termState.history.length - 1) {
      termState.historyIndex += 1;
      event.target.value = termState.history[termState.historyIndex];
    } else {
      termState.historyIndex = termState.history.length;
      event.target.value = "";
    }
  }
});

$("#term-clear").onclick = () => { $("#term-out").textContent = ""; };

/* ---------------- boot ---------------- */
async function bootApp() {
  await Promise.all([refreshStatus(), refreshChats()]);
  const fromHash = location.hash.slice(1);
  if (fromHash) await openChat(fromHash).catch(() => showEmptyState());
  else showEmptyState();
}

(async function boot() {
  setAuthMode("login");
  if (!getToken()) { showAuth(); return; }
  try {
    await bootApp();
  } catch {
    clearToken();
    showAuth();
  }
})();
