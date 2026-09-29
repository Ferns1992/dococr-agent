(function () {
  const $ = (id) => document.getElementById(id);
  let me = null;
  let users = [];
  let files = [];

  function toast(msg, bad) {
    const t = $("toast");
    t.textContent = msg;
    t.className = "toast show" + (bad ? " bad" : " good");
    clearTimeout(toast._t);
    toast._t = setTimeout(() => (t.className = "toast"), 2600);
  }

  async function api(path, opts) {
    const res = await fetch(path, Object.assign({ credentials: "same-origin" }, opts));
    if (res.status === 401) {
      window.location.href = "/login";
      return null;
    }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      toast(data.detail || "Request failed", true);
      return null;
    }
    return data;
  }

  function when(ts) {
    return new Date(ts * 1000).toLocaleString();
  }

  function size(n) {
    if (n < 1024) return n + " B";
    if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
    return (n / 1048576).toFixed(1) + " MB";
  }

  // ------------------------------------------------------------------ tabs
  document.querySelectorAll(".tab").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((b) => b.classList.remove("on"));
      btn.classList.add("on");
      ["users", "files", "account"].forEach((k) => {
        $("tab-" + k).hidden = k !== btn.dataset.tab;
      });
      if (btn.dataset.tab === "files") loadFiles();
    });
  });

  // ------------------------------------------------------------------ users
  async function loadUsers() {
    const data = await api("/api/users");
    if (!data) return;
    users = data.users;
    const tb = $("users");
    tb.innerHTML = "";
    $("users-empty").hidden = users.length > 0;

    users.forEach((u) => {
      const tr = document.createElement("tr");
      const isMe = me && u.id === me.id;
      tr.innerHTML =
        "<td><strong>" + u.username + "</strong>" + (isMe ? " (you)" : "") + "</td>" +
        '<td><span class="pill ' + u.role + '">' + u.role + "</span></td>" +
        '<td><span class="pill ' + (u.is_active ? "on" : "off") + '">' +
          (u.is_active ? "active" : "disabled") + "</span></td>" +
        '<td class="mono">' + when(u.created_at) + "</td>" +
        '<td><div class="row-actions">' +
          '<button class="btn ghost" data-act="reset" data-id="' + u.id + '">Reset password</button>' +
          '<button class="btn ghost" data-act="toggle" data-id="' + u.id + '"' +
            (isMe ? " disabled" : "") + ">" + (u.is_active ? "Disable" : "Enable") + "</button>" +
          '<button class="btn danger" data-act="del" data-id="' + u.id + '"' +
            (isMe ? " disabled" : "") + ">Delete</button>" +
        "</div></td>";
      tb.appendChild(tr);
    });
  }

  $('users').addEventListener("click", async (e) => {
    const btn = e.target.closest("button[data-act]");
    if (!btn) return;
    const id = Number(btn.dataset.id);
    const u = users.find((x) => x.id === id);
    if (!u) return;

    if (btn.dataset.act === "reset") {
      const pw = prompt("New password for " + u.username + " (min 8 characters):");
      if (!pw) return;
      const r = await api("/api/users/" + id + "/password", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ new_password: pw }),
      });
      if (r) toast("Password updated for " + u.username);
    }

    if (btn.dataset.act === "toggle") {
      const r = await api("/api/users/" + id + "/active", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ is_active: !u.is_active }),
      });
      if (r) {
        toast(u.username + (r.user.is_active ? " enabled" : " disabled"));
        loadUsers();
      }
    }

    if (btn.dataset.act === "del") {
      if (!confirm("Delete " + u.username + "? Their files and chat history are removed too.")) return;
      const r = await api("/api/users/" + id, { method: "DELETE" });
      if (r) {
        toast(u.username + " deleted");
        loadUsers();
        loadOwnerFilter();
      }
    }
  });

  $("create").addEventListener("click", async () => {
    const username = $("nu").value.trim();
    const password = $("np").value;
    if (!username || !password) return toast("Username and password are required", true);
    const r = await api("/api/users", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password, role: $("nr").value }),
    });
    if (r) {
      toast("Created " + r.user.username);
      $("nu").value = "";
      $("np").value = "";
      loadUsers();
      loadOwnerFilter();
    }
  });

  // ------------------------------------------------------------------ files
  async function loadOwnerFilter() {
    const data = await api("/api/users");
    if (!data) return;
    users = data.users;
    const sel = $("fo");
    const cur = sel.value;
    sel.innerHTML = '<option value="">Everyone</option>';
    users.forEach((u) => {
      const o = document.createElement("option");
      o.value = u.id;
      o.textContent = u.username;
      sel.appendChild(o);
    });
    sel.value = cur;
  }

  async function loadFiles() {
    const owner = $("fo").value;
    const url = owner ? "/api/sources/" + owner : "/api/sources/all";
    const data = await api(url);
    if (!data) return;
    files = data.sources;
    const tb = $("files");
    tb.innerHTML = "";
    $("files-empty").hidden = files.length > 0;
    const total = files.reduce((a, f) => a + (f.size_bytes || 0), 0);
    $("fstat").textContent = files.length + " file" + (files.length === 1 ? "" : "s") +
      " · " + size(total);

    files.forEach((f) => {
      const tr = document.createElement("tr");
      tr.innerHTML =
        "<td><strong>" + f.source_name + "</strong><br><span class='mono'>" +
          (f.origin === "url" ? "from link" : "upload") + "</span></td>" +
        "<td>" + (f.owner || "—") + "</td>" +
        '<td><span class="pill">' + (f.kind || "text") + "</span></td>" +
        '<td class="mono">' + size(f.size_bytes || 0) + "</td>" +
        '<td class="mono">' + (f.chunks || 0) + " text / " + (f.images || 0) + " img</td>" +
        '<td class="mono">' + when(f.created_at) + "</td>" +
        '<td><div class="row-actions">' +
          (f.stored_path
            ? '<a class="btn ghost" href="/api/sources/' + f.source_id + '/file">Download</a>'
            : "") +
          '<button class="btn danger" data-del="' + f.source_id + '">Delete</button>' +
        "</div></td>";
      tb.appendChild(tr);
    });
  }

  $("fo").addEventListener("change", loadFiles);

  $("files").addEventListener("click", async (e) => {
    const btn = e.target.closest("button[data-del]");
    if (!btn) return;
    const f = files.find((x) => x.source_id === btn.dataset.del);
    if (!confirm("Delete " + (f ? f.source_name : "this file") + " and its vectors?")) return;
    const r = await api("/api/sources/" + btn.dataset.del, { method: "DELETE" });
    if (r) {
      toast("File deleted");
      loadFiles();
    }
  });

  // ------------------------------------------------------------------ account
  $("chpw").addEventListener("click", async () => {
    const cur = $("cur").value;
    const nw = $("nw").value;
    if (!cur || !nw) return toast("Fill in both password fields", true);
    if (nw !== $("nw2").value) return toast("New passwords do not match", true);
    const r = await api("/api/password", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ current_password: cur, new_password: nw }),
    });
    if (r) {
      toast("Password updated");
      $("cur").value = $("nw").value = $("nw2").value = "";
    }
  });

  $("back").addEventListener("click", () => (window.location.href = "/"));
  $("logout").addEventListener("click", async () => {
    await api("/api/logout", { method: "POST" });
    window.location.href = "/login";
  });

  // ------------------------------------------------------------------ boot
  (async function () {
    const data = await api("/api/me");
    if (!data) return;
    me = data.user;
    $("who").textContent = "Signed in as " + me.username + " · " + me.role;
    $("acct-user").textContent = me.username;
    await loadUsers();
    await loadOwnerFilter();
    await loadFiles();
  })();
})();
