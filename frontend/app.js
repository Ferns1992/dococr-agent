const $ = (id) => document.getElementById(id);

const el = {
  status: $("status"), drop: $("drop"), file: $("file"), browse: $("browse"),
  url: $("url"), urlGo: $("url-go"), sources: $("sources"), count: $("count"),
  scope: $("scope"), thread: $("thread"), empty: $("empty"), composer: $("composer"),
  question: $("question"), send: $("send"), toast: $("toast"), jobs: $("jobs"),
  chats: $("chats"), newchat: $("newchat"), srcmodes: document.querySelectorAll(".srcmode"),
};

let currentChat = null;
let srcMode = localStorage.getItem("docchat.srcmode") || "strict";

let mode = "text";
let busy = false;
let cache = {};

function toast(message, isError) {
  el.toast.textContent = message;
  el.toast.className = "toast show" + (isError ? " err" : "");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (el.toast.className = "toast"), isError ? 6500 : 3200);
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function renderMarkdown(raw) {
  const blocks = String(raw).split(/\n{2,}/);
  return blocks.map((block) => {
    const code = block.match(/^```([\s\S]*?)```$/);
    if (code) return `<pre><code>${escapeHtml(code[1].trim())}</code></pre>`;
    let html = escapeHtml(block);
    html = html.replace(/`([^`\n]+)`/g, "<code>$1</code>");
    html = html.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
    html = html.replace(/(^|[\s(])\[(\d{1,2})\]/g,
      '$1<span class="cite" data-cite="$2">$2</span>');
    return `<p>${html.replace(/\n/g, "<br>")}</p>`;
  }).join("");
}

function citationMap() {
  return new Map(Object.entries(cache).map(([id, source]) => [id, source]));
}

function addMessage(role, html) {
  const wrap = document.createElement("div");
  wrap.className = `msg ${role}`;
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  bubble.innerHTML = html;
  wrap.appendChild(bubble);
  el.thread.appendChild(wrap);
  el.empty.style.display = "none";
  el.thread.scrollTop = el.thread.scrollHeight;
  return bubble;
}

function sourceLabel(hit) {
  const parts = [hit.source_name || "unknown"];
  if (hit.page) parts.push(`p.${hit.page}`);
  if (hit.caption && !hit.page) parts.push(hit.caption);
  return parts.join(" · ");
}

function renderSources(hits) {
  if (!hits || !hits.length) return "";
  const items = hits.map((hit) => {
    const preview = escapeHtml((hit.text || "").slice(0, 220));
    const img = hit.media_path
      ? `<img src="/media/${encodeURIComponent(hit.media_path)}" alt="${sourceLabel(hit)}" loading="lazy">`
      : "";
    return `<div class="hit">
      <button class="hit-open" data-preview="${escapeHtml(hit.source_id || "")}"
              title="Open a preview of this file">
        <span class="loc">${escapeHtml(sourceLabel(hit))} · ${hit.score.toFixed(3)}</span>
        <span class="zoom-hint">preview &#8599;</span>
      </button>
      ${preview ? `<div class="txt">${preview}…</div>` : ""}
      ${img}
    </div>`;
  }).join("");
  return `<details class="sources-panel"><summary>${hits.length} source${hits.length > 1 ? "s" : ""}</summary>${items}</details>`;
}

async function checkHealth() {
  try {
    const res = await fetch("/api/health");
    const data = await res.json();
    if (!data.configured) {
      const missing = [];
      if (!data.collections) missing.push("qdrant");
      el.status.className = "status bad";
      el.status.textContent = data.error
        ? "qdrant not configured"
        : `missing config: ${missing.join(", ") || "nvidia key"}`;
      return;
    }
    el.status.className = "status ok";
    el.status.textContent = `${data.collections.text ? "ready" : "starting"} · ${data.models.chat.split("/").pop()}`;
  } catch (err) {
    el.status.className = "status bad";
    el.status.textContent = "backend unreachable";
  }
}

async function loadSources() {
  try {
    const res = await fetch("/api/sources");
    const data = await res.json();
    const sources = data.sources || [];
    el.count.textContent = sources.length;
    cache = Object.fromEntries(sources.map((s) => [s.source_id, s.source_name]));

    el.sources.innerHTML = sources.length
      ? sources.map((s) => {
          const bits = [];
          if (s.chunks) bits.push(`${s.chunks} chunks`);
          if (s.images) bits.push(`${s.images} images`);
          return `<div class="source">
            <div class="meta">
              <div class="name" title="${escapeHtml(s.source_name)}">${escapeHtml(s.source_name)}</div>
              <div class="sub">${bits.join(" · ") || "empty"}</div>
            </div>
            <button class="del" data-del="${s.source_id}" title="Remove">&times;</button>
          </div>`;
        }).join("")
      : `<p class="none">Nothing indexed yet.</p>`;

    const current = el.scope.value;
    el.scope.innerHTML = `<option value="">All documents</option>` + sources.map((s) =>
      `<option value="${s.source_id}">${escapeHtml(s.source_name)}</option>`).join("");
    el.scope.value = current;
  } catch (err) {
    el.sources.innerHTML = `<p class="none">Could not load library.</p>`;
  }
}

const webCache = {};

function answerBadge(mode) {
  const label = mode === "research"
    ? "Researched — documents [n] and web [Wn]"
    : "Answered partly from general knowledge";
  return `<div class="ansbadge">${label}</div>`;
}

function webPanel(sources) {
  if (!sources.length) return "";
  const items = sources.map((s) =>
    `<li><a href="${escapeHtml(s.url)}" target="_blank" rel="noopener">${escapeHtml(s.title)}</a></li>`
  ).join("");
  return `<details class="sources-panel web"><summary>${sources.length} web source${
    sources.length > 1 ? "s" : ""} searched</summary><ul>${items}</ul></details>`;
}

document.querySelectorAll(".srcmode").forEach((b) => {
  b.addEventListener("click", () => {
    document.querySelectorAll(".srcmode").forEach((o) => {
      o.classList.remove("active");
      o.setAttribute("aria-checked", "false");
    });
    b.classList.add("active");
    b.setAttribute("aria-checked", "true");
    srcMode = b.dataset.src;
    localStorage.setItem("docchat.srcmode", srcMode);
    el.question.placeholder = mode === "visual"
      ? "Describe an image to find it…"
      : srcMode === "research"
        ? "Ask anything — your documents and the web are both searched…"
        : srcMode === "blend"
          ? "Ask about your documents, or anything else…"
          : "Ask a question about your documents…";
  });
});

// restore the saved choice
document.querySelectorAll(".srcmode").forEach((b) => {
  if (b.dataset.src === srcMode) b.click();
});

/* ---------------------------------------------------------------- chat history */
function ago(epoch) {
  const secs = Math.max(0, Date.now() / 1000 - epoch);
  if (secs < 60) return "just now";
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
  if (secs < 604800) return `${Math.floor(secs / 86400)}d ago`;
  return new Date(epoch * 1000).toLocaleDateString();
}

function clearThread() {
  el.thread.querySelectorAll(".msg").forEach((node) => node.remove());
  el.empty.style.display = "";
  el.thread.scrollTop = 0;
}

async function loadChats() {
  try {
    const res = await fetch("/api/conversations");
    const data = await res.json();
    const chats = data.conversations || [];

    el.chats.innerHTML = chats.length
      ? chats.map((c) => `<div class="chat${c.id === currentChat ? " active" : ""}" data-chat="${c.id}">
          <div class="meta">
            <div class="name" title="${escapeHtml(c.title)}">${escapeHtml(c.title)}</div>
            <div class="sub">${ago(c.updated_at)}</div>
          </div>
          <button class="del" data-delchat="${c.id}" title="Delete chat">&times;</button>
        </div>`).join("")
      : `<p class="none">No chats yet.</p>`;
  } catch (err) {
    el.chats.innerHTML = `<p class="none">Could not load chats.</p>`;
  }
}

async function openChat(id) {
  try {
    const res = await fetch(`/api/conversations/${id}/messages`);
    if (!res.ok) throw new Error("not found");
    const data = await res.json();
    const messages = data.messages || [];

    currentChat = id;
    sessionStorage.setItem("docchat.chat", id);
    clearThread();

    for (const m of messages) {
      if (m.role === "user") {
        addMessage("user", renderMarkdown(m.content));
        continue;
      }
      let bubble;
      if (m.content) {
        bubble = addMessage("assistant", renderMarkdown(m.content));
      } else {
        // an answer that never streamed, kept for the record
        bubble = addMessage("assistant",
          `<span class="sub" style="color:var(--muted)">no response recorded</span>`);
      }
      if (m.sources && m.sources.length) {
        for (const hit of m.sources) {
          if (hit.source_id) cache[hit.source_id] = hit.source_name;
        }
        bubble.insertAdjacentHTML("beforeend", renderSources(m.sources));
      }
    }

    if (!messages.length) el.empty.style.display = "";
    el.thread.scrollTop = el.thread.scrollHeight;
    loadChats();
  } catch (err) {
    toast("could not open that chat", true);
  }
}

async function newChat() {
  if (busy) return;
  try {
    const res = await fetch("/api/conversations", { method: "POST" });
    const data = await res.json();
    if (!data.conversation) throw new Error("failed");
    currentChat = data.conversation.id;
    sessionStorage.setItem("docchat.chat", currentChat);
    clearThread();
    el.question.focus();
    loadChats();
  } catch (err) {
    toast("could not start a new chat", true);
  }
}

el.chats.addEventListener("click", async (e) => {
  const remove = e.target.dataset.delchat;
  if (remove) {
    e.stopPropagation();
    await fetch(`/api/conversations/${remove}`, { method: "DELETE" });
    if (remove === currentChat) {
      currentChat = null;
      sessionStorage.removeItem("docchat.chat");
      clearThread();
    }
    loadChats();
    return;
  }
  const row = e.target.closest(".chat");
  if (row && row.dataset.chat) openChat(row.dataset.chat);
});

el.newchat.addEventListener("click", newChat);

window.addEventListener("hashchange", () => {
  const id = location.hash.replace(/^#/, "");
  if (id && id !== currentChat) openChat(id);
});


const jobNodes = new Map();

function addJobCard(jobId, name) {
  const box = document.createElement("div");
  box.className = "job";
  box.innerHTML = `<div class="job-top">
      <span class="job-name">${escapeHtml(name)}</span>
      <span class="job-pct">0%</span>
    </div>
    <div class="job-step"><span class="spin"></span>queued</div>
    <div class="bar"><i style="width:0%"></i></div>`;
  el.jobs.prepend(box);
  jobNodes.set(jobId, box);
  return box;
}

function paintJob(box, job) {
  const pct = Math.round((job.progress || 0) * 100);
  box.querySelector(".job-pct").textContent = `${pct}%`;
  box.querySelector(".bar i").style.width = `${pct}%`;
  const step = box.querySelector(".job-step");
  if (job.status === "done") {
    step.innerHTML = "ready — click to ask";
    box.classList.add("done");
  } else if (job.status === "error") {
    step.textContent = job.error || "failed";
    box.classList.add("err");
  } else {
    step.innerHTML = `<span class="spin"></span>${escapeHtml(job.detail || job.step)}`;
  }
}

async function trackJob(jobId, name) {
  const box = addJobCard(jobId, name);
  while (true) {
    await new Promise((r) => setTimeout(r, 900));
    let job;
    try {
      const res = await fetch(`/api/jobs/${jobId}`);
      job = await res.json();
    } catch {
      continue;
    }
    paintJob(box, job);
    if (job.status === "done" || job.status === "error") {
      if (job.status === "done") {
        const r = job.result || {};
        const bits = [`${r.chunks || 0} passages`];
        if (r.ocr_chunks) bits.push(`${r.ocr_chunks} OCR`);
        if (r.images) bits.push(`${r.images} images`);
        toast(`${r.source_name || name} — ${bits.join(", ")}`);
        await loadSources();
      } else {
        toast(`${name}: ${job.error}`, true);
      }
      setTimeout(() => box.remove(), 12000);
      return;
    }
  }
}

async function sendFiles(files) {
  for (const file of files) {
    const body = new FormData();
    body.append("file", file);
    try {
      const res = await fetch("/api/ingest", { method: "POST", body });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "upload failed");
      trackJob(data.job_id, file.name);
    } catch (err) {
      toast(`${file.name}: ${err.message}`, true);
    }
  }
}

async function sendUrl() {
  const url = el.url.value.trim();
  if (!url) return;
  el.url.value = "";
  try {
    const res = await fetch("/api/ingest/url", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "failed");
    trackJob(data.job_id, url);
  } catch (err) {
    toast(err.message, true);
  }
}

function parseSse(buffer, onEvent) {
  const parts = buffer.split("\n\n");
  const tail = parts.pop();
  for (const part of parts) {
    let event = "message";
    const dataLines = [];
    for (const line of part.split("\n")) {
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
    }
    if (!dataLines.length) continue;
    try { onEvent(event, JSON.parse(dataLines.join("\n"))); }
    catch (err) { /* ignore malformed frame */ }
  }
  return tail;
}

async function ask(question) {
  const answer = addMessage("assistant", `<span class="cursor"></span>`);
  const body = {
    question,
    source_id: el.scope.value || null,
    visual: mode === "visual",
    conversation_id: currentChat,
    mode: srcMode,
  };

  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.detail || "chat failed");
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let text = "";
    let thinking = "";
    let pendingSources = "";
    let elapsed = null;
    let badge = null;

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer = parseSse(buffer + decoder.decode(value, { stream: true }), (event, data) => {
        if (event === "delta") {
          text += data.text;
          thinking = "";
          answer.innerHTML = renderMarkdown(text) + `<span class="cursor"></span>`;
        } else if (event === "thinking") {
          thinking += data.text;
          answer.innerHTML = `<span class="sub" style="color:var(--muted)">${
            escapeHtml(thinking.slice(-160))}</span><br><span class="cursor"></span>`;
        } else if (event === "sources") {
          pendingSources = renderSources(data.hits);
        } else if (event === "web") {
          if (data.count) {
            pendingSources += webPanel(data.sources || []);
            for (const s of data.sources || []) webCache[s.url] = s.title;
          }
        } else if (event === "mode") {
          badge = data.mode;
        } else if (event === "done") {
          elapsed = data.elapsed;
          if (data.conversation_id && data.conversation_id !== currentChat) {
            currentChat = data.conversation_id;
            sessionStorage.setItem("docchat.chat", currentChat);
          }
        } else if (event === "error") {
          throw new Error(data.message);
        }
      });
      el.thread.scrollTop = el.thread.scrollHeight;
    }

    answer.innerHTML = (text ? renderMarkdown(text) : "_no response_") + pendingSources;
    if (badge && badge !== "strict") {
      answer.insertAdjacentHTML("afterbegin", answerBadge(badge));
    }
    if (elapsed != null) {
      const foot = document.createElement("div");
      foot.className = "sub";
      foot.style.cssText = "font-size:11px;color:var(--muted);margin-top:6px";
      foot.textContent = `answered in ${elapsed}s`;
      answer.appendChild(foot);
    }
  } catch (err) {
    answer.innerHTML = `<p style="color:var(--danger)">${escapeHtml(err.message)}</p>`;
  }
  loadChats();
}

el.drop.addEventListener("click", () => el.file.click());
el.browse.addEventListener("click", (e) => { e.stopPropagation(); el.file.click(); });
el.file.addEventListener("change", () => { sendFiles(el.file.files); el.file.value = ""; });
el.urlGo.addEventListener("click", sendUrl);
el.url.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); sendUrl(); } });

["dragenter", "dragover"].forEach((type) =>
  el.drop.addEventListener(type, (e) => { e.preventDefault(); el.drop.classList.add("over"); }));
["dragleave", "drop"].forEach((type) =>
  el.drop.addEventListener(type, (e) => { e.preventDefault(); el.drop.classList.remove("over"); }));
el.drop.addEventListener("drop", (e) => sendFiles(e.dataTransfer.files));

el.sources.addEventListener("click", async (e) => {
  const id = e.target.dataset.del;
  if (!id) return;
  try {
    await fetch(`/api/sources/${id}`, { method: "DELETE" });
    toast("removed from library");
  } catch (err) {
    toast("could not remove", true);
  }
  loadSources();
});

document.querySelectorAll(".mode").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll(".mode").forEach((b) => b.classList.remove("active"));
    button.classList.add("active");
    mode = button.dataset.mode;
    el.question.placeholder = mode === "visual"
      ? "Describe an image to find it, e.g. “architecture diagram with arrows”…"
      : "Ask a question about your documents…";
  });
});

const Q_MIN = 68;
const Q_MAX = 320;

function autosizeQuestion() {
  el.question.style.height = "auto";
  const next = Math.min(Math.max(el.question.scrollHeight, Q_MIN), Q_MAX);
  el.question.style.height = next + "px";
  el.question.style.overflowY = el.question.scrollHeight > Q_MAX ? "auto" : "hidden";
}

el.question.addEventListener("input", autosizeQuestion);
autosizeQuestion();
el.question.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); el.composer.requestSubmit(); }
});

el.composer.addEventListener("submit", async (e) => {
  e.preventDefault();
  const question = el.question.value.trim();
  if (!question || busy) return;
  busy = true;
  el.send.disabled = true;
  el.question.value = "";
  autosizeQuestion();
  addMessage("user", renderMarkdown(question));
  await ask(question);
  busy = false;
  el.send.disabled = false;
  el.question.focus();
});

checkHealth();
loadSources();

// Reopen the conversation that was on screen, so a refresh does not look
// like an empty app. Falls back to the most recent one the user has.
(async () => {
  const wanted = location.hash.replace(/^#/, "") || sessionStorage.getItem("docchat.chat");
  try {
    const res = await fetch("/api/conversations");
    const data = await res.json();
    const chats = data.conversations || [];
    if (!chats.length) return;
    const match = wanted && chats.find((c) => c.id === wanted);
    const target = match || chats[0];
    if (target) {
      currentChat = target.id;
      openChat(target.id);
    }
  } catch (err) {
    /* history is a convenience; a fresh chat still works */
  }
})();


// ---------------------------------------------------------------- source preview
const pv = {
  scale: 1,
  min: 0.25,
  max: 6,
  type: null,
  sourceId: null,

  el() { return document.getElementById("preview-modal"); },

  async open(sourceId) {
    if (!sourceId) return;
    this.sourceId = sourceId;
    this.scale = 1;
    const modal = this.el();
    if (!modal) return;
    modal.classList.add("open");
    document.body.classList.add("modal-open");
    const body = document.getElementById("preview-body");
    body.innerHTML = '<p class="pv-loading">Loading preview…</p>';
    document.getElementById("pv-title").textContent = "Loading…";
    try {
      const res = await fetch(`/api/sources/${encodeURIComponent(sourceId)}/preview`);
      if (!res.ok) {
        const e = new Error(`Preview failed (${res.status})`);
        e.status = res.status;
        throw e;
      }
      const d = await res.json();
      this.type = d.preview_type;
      document.getElementById("pv-title").textContent =
        d.source_name || d.filename || "Preview";
      document.getElementById("pv-sub").textContent =
        [d.kind, d.size_bytes ? `${(d.size_bytes / 1024).toFixed(0)} KB` : null]
          .filter(Boolean).join(" · ");
      const dl = document.getElementById("pv-download");
      dl.href = d.download_url || "";
      dl.style.display = d.download_url ? "" : "none";
      this.render(d);
    } catch (err) {
      const status = err && err.status;
      const btn = document.querySelector(
        `[data-preview="${(window.CSS && CSS.escape) ? CSS.escape(this.sourceId || "") : (this.sourceId || "")}"]`);
      if (status === 410) {
        body.innerHTML =
          `<p class="pv-note">This document was deleted, so there is no longer a
           preview for it.</p>`;
        if (btn) {
          const loc = btn.querySelector(".loc");
          if (loc) loc.style.opacity = "0.35";
          btn.disabled = true;
        }
      } else {
        body.innerHTML =
          `<p class="pv-error">Could not open this preview.<br><code>${escapeHtml(String(err.message || err))}</code></p>`;
      }
    }
  },

  render(d) {
    const body = document.getElementById("preview-body");
    const wrap = document.getElementById("pv-canvas");
    if (d.preview_type === "image" && d.url) {
      body.className = "pv-body image";
      body.innerHTML =
        `<div class="pv-scroll"><img id="pv-img" src="${escapeHtml(d.url)}" alt="${escapeHtml(d.source_name || "")}"></div>`;
      const img = document.getElementById("pv-img");
      img.onload = () => { this.fit(img); };
      if (img.complete) this.fit(img);
    } else if (d.preview_type === "pdf" && d.page_image) {
      body.className = "pv-body image";
      body.innerHTML =
        `<div class="pv-scroll"><img id="pv-img" src="/media/${encodeURIComponent(d.page_image)}" alt="${escapeHtml(d.source_name || "")}"></div>`;
      const img = document.getElementById("pv-img");
      img.onload = () => { this.fit(img); };
      if (img.complete) this.fit(img);
    } else if (d.preview_type === "pdf" && !d.page_image) {
      body.className = "pv-body text";
      body.innerHTML =
        `<iframe src="${escapeHtml(d.url || d.download_url)}#toolbar=1" title="PDF preview"></iframe>`;
    } else if (d.preview_type === "text") {
      body.className = "pv-body text";
      body.innerHTML = `<pre id="pv-text">${escapeHtml(d.text || "")}</pre>`;
      document.getElementById("pv-text").style.fontSize = `${this.scale}em`;
    } else if (d.preview_type === "download") {
      body.className = "pv-body text";
      body.innerHTML =
        `<p class="pv-note">This file type cannot be shown inline.
         Use <strong>Download</strong> to open it.</p>`;
    } else {
      body.className = "pv-body text";
      body.innerHTML =
        `<p class="pv-note">${escapeHtml(d.reason || "No preview available.")}</p>`;
    }
    this.zoomLabel();
    wrap.scrollTop = 0;
  },

  // Zoom to fit the viewport width on open, so a wide diagram is readable
  // without manual panning, then allow free zoom from there.
  fit(img) {
    const scroll = img.closest(".pv-scroll");
    if (!scroll) return;
    const avail = scroll.clientWidth - 24;
    if (avail > 0 && img.naturalWidth) {
      this.scale = Math.max(this.min, Math.min(this.max, avail / img.naturalWidth));
      img.style.width = `${this.scale * 100}%`;
      img.style.maxWidth = "none";
    }
    this.zoomLabel();
  },

  setZoom(next) {
    this.scale = Math.max(this.min, Math.min(this.max, next));
    const img = document.getElementById("pv-img");
    if (img) {
      img.style.width = `${this.scale * 100}%`;
      img.style.maxWidth = "none";
    }
    const txt = document.getElementById("pv-text");
    if (txt) txt.style.fontSize = `${this.scale}em`;
    this.zoomLabel();
  },

  zoomLabel() {
    const l = document.getElementById("pv-zoom-level");
    if (l) l.textContent = `${Math.round(this.scale * 100)}%`;
  },

  close() {
    const modal = this.el();
    if (modal) modal.classList.remove("open");
    document.body.classList.remove("modal-open");
    const body = document.getElementById("preview-body");
    if (body) body.innerHTML = "";
  },
};


// click a citation (or a library row) to preview it
document.addEventListener("click", (e) => {
  const open = e.target.closest("[data-preview]");
  if (open) {
    e.preventDefault();
    pv.open(open.getAttribute("data-preview"));
    return;
  }
  if (e.target.closest("#pv-close") || e.target.closest("#pv-backdrop")) {
    pv.close();
    return;
  }
  if (e.target.closest("[data-zoom]")) {
    const step = e.target.closest("[data-zoom]").getAttribute("data-zoom");
    if (step === "in") pv.setZoom(pv.scale * 1.25);
    else if (step === "out") pv.setZoom(pv.scale / 1.25);
    else pv.setZoom(1);
    return;
  }
});

document.addEventListener("keydown", (e) => {
  if (!document.getElementById("preview-modal")?.classList.contains("open")) return;
  if (e.key === "Escape") pv.close();
  else if (e.key === "+" || e.key === "=") pv.setZoom(pv.scale * 1.25);
  else if (e.key === "-") pv.setZoom(pv.scale / 1.25);
  else if (e.key === "0") pv.setZoom(1);
});
