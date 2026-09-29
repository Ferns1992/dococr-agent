
/* ---------------------------------------------------------------- session UI */
(function () {
  const $ = (id) => document.getElementById(id);
  let me = null;

  async function api(path, opts) {
    const res = await fetch(path, Object.assign({ credentials: "same-origin" }, opts));
    if (res.status === 401) {
      window.location.href = "/login";
      return null;
    }
    return res.json().catch(function () { return {}; });
  }

  // A session that expired mid-session should send the user to the login page
  // instead of leaving a chat UI that silently fails every request.
  (async function () {
    const data = await api("/api/me");
    if (!data || !data.user) {
      window.location.href = "/login";
      return;
    }
    me = data.user;

    const initial = (me.username || "?").charAt(0);
    $("avatar").textContent = initial;
    $("uname").textContent = me.username;
    $("urole").textContent = me.role;

    if (me.role !== "admin") {
      const link = $("adminlink");
      if (link) link.remove();
    }
  })();

  // ---------------------------------------------------------------- dropdown
  const btn = $("userbtn");
  const drop = $("userdrop");

  function closeDrop() {
    drop.hidden = true;
    btn.setAttribute("aria-expanded", "false");
  }

  btn.addEventListener("click", function (e) {
    e.stopPropagation();
    drop.hidden = !drop.hidden;
    btn.setAttribute("aria-expanded", String(!drop.hidden));
  });
  document.addEventListener("click", function (e) {
    if (!drop.hidden && !drop.contains(e.target)) closeDrop();
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") closeDrop();
  });

  $("logout").addEventListener("click", async function () {
    await api("/api/logout", { method: "POST" });
    window.location.href = "/login";
  });

  // ---------------------------------------------------------------- modal
  const modal = $("pwmodal");
  const err = $("pw-err");

  function openModal() {
    closeDrop();
    modal.hidden = false;
    $("pw-cur").value = $("pw-new").value = $("pw-new2").value = "";
    err.hidden = true;
    $("pw-cur").focus();
  }
  function closeModal() {
    modal.hidden = true;
  }

  $("changepw").addEventListener("click", openModal);
  $("pw-cancel").addEventListener("click", closeModal);
  modal.addEventListener("click", function (e) {
    if (e.target === modal) closeModal();
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && !modal.hidden) closeModal();
  });

  $("pw-save").addEventListener("click", async function () {
    const cur = $("pw-cur").value;
    const nw = $("pw-new").value;
    const nw2 = $("pw-new2").value;

    if (!cur || !nw) {
      err.textContent = "Fill in both password fields.";
      err.hidden = false;
      return;
    }
    if (nw !== nw2) {
      err.textContent = "New passwords do not match.";
      err.hidden = false;
      return;
    }

    const save = $("pw-save");
    save.disabled = true;
    const res = await api("/api/password", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ current_password: cur, new_password: nw }),
    });
    save.disabled = false;

    if (res && res.ok) {
      closeModal();
      const done = document.createElement("div");
      done.className = "toast";
      done.style.cssText =
        "position:fixed;bottom:22px;left:50%;transform:translateX(-50%);padding:11px 18px;" +
        "font-size:13.5px;border-radius:10px;background:#161d2b;color:#c9f7dd;" +
        "border:1px solid rgba(60,220,140,.4);z-index:120";
      done.textContent = "Password updated";
      document.body.appendChild(done);
      setTimeout(function () { done.remove(); }, 2600);
    } else {
      err.textContent = (res && res.detail) || "Could not update the password.";
      err.hidden = false;
    }
  });
})();

  // ---------------- theme (dark / light) ----------------
  const Theme = {
    key: "dococr.theme",
    get() {
      try { return localStorage.getItem(this.key) || "dark"; }
      catch (e) { return "dark"; }
    },
    set(v) {
      try { localStorage.setItem(this.key, v); } catch (e) {}
      document.documentElement.setAttribute("data-theme", v);
      this.sync();
    },
    toggle() { this.set(this.get() === "light" ? "dark" : "light"); },
    init() {
      document.documentElement.setAttribute("data-theme", this.get());
      this.sync();
    },
    sync() {
      const light = this.get() === "light";
      const btn = $("theme-toggle");
      if (btn) {
        btn.setAttribute("aria-pressed", light ? "true" : "false");
        const label = btn.querySelector("span");
        if (label) label.textContent = light ? "Dark mode" : "Light mode";
      }
    },
  };

  Theme.init();
  const themeBtn = $("theme-toggle");
  if (themeBtn) {
    themeBtn.addEventListener("click", function (e) {
      e.stopPropagation();
      Theme.toggle();
      closeDrop();
    });
  }
