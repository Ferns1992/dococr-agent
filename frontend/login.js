(function () {
  const form = document.getElementById("form");
  const username = document.getElementById("username");
  const password = document.getElementById("password");
  const error = document.getElementById("error");
  const submit = document.getElementById("submit");
  const reveal = document.getElementById("reveal");

  reveal.addEventListener("click", function () {
    const showing = password.type === "text";
    password.type = showing ? "password" : "text";
    reveal.textContent = showing ? "Show" : "Hide";
    password.focus();
  });

  function fail(msg) {
    error.textContent = msg;
    error.hidden = false;
    submit.classList.remove("loading");
    submit.disabled = false;
    password.value = "";
    password.focus();
  }

  form.addEventListener("submit", async function (e) {
    e.preventDefault();
    error.hidden = true;

    if (!username.value.trim() || !password.value) {
      fail("Enter both your username and password.");
      return;
    }

    submit.classList.add("loading");
    submit.disabled = true;

    try {
      const res = await fetch("/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({
          username: username.value.trim(),
          password: password.value,
        }),
      });

      if (res.ok) {
        window.location.href = "/";
        return;
      }
      const data = await res.json().catch(function () { return {}; });
      fail(data.detail || "Sign in failed. Please try again.");
    } catch (err) {
      fail("Could not reach the server. Check your connection.");
    }
  });
})();
