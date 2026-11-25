/* HealthOps first-owner setup and sign-in gate. Session token remains HttpOnly. */
(function () {
  const gate = document.getElementById("authGate");
  const app = document.getElementById("appShell");
  const productName = document.querySelector('meta[name="application-name"]')?.content || "HealthOps Intelligence";

  function showGate(content) {
    gate.innerHTML = content;
    gate.hidden = false;
    app.hidden = true;
    app.setAttribute("aria-hidden", "true");
  }

  function authMarkup(setup, message = "") {
    const fields = setup ? `
      <label>Hospital / organization name
        <input name="organization_name" required minlength="2" maxlength="200" autocomplete="organization" placeholder="e.g. City Care Hospital">
      </label>
      <label>Your name
        <input name="display_name" required minlength="2" maxlength="160" autocomplete="name" placeholder="Full name">
      </label>` : "";

    const heading = setup ? "Create your administrator account" : "Welcome back";
    const subtitle = setup
      ? "Set up the first owner account for this HealthOps workspace."
      : "Sign in to access your healthcare analytics workspace.";
    const passwordLabel = setup ? "Create password" : "Password";
    const passwordAutocomplete = setup ? "new-password" : "current-password";
    const confirm = setup ? `
      <label>Confirm password
        <input name="confirm_password" type="password" required minlength="12" maxlength="256" autocomplete="new-password">
      </label>` : "";

    return `
      <div class="authBackdrop">
        <section class="authCard" aria-labelledby="authTitle">
          <div class="authBrand">
            <span class="authBrandMark">HO</span>
            <div><strong>${productName.replace(/[&<>"']/g, "")}</strong><small>Healthcare data intelligence</small></div>
          </div>
          <div class="authEyebrow">SECURE WORKSPACE ACCESS</div>
          <h1 id="authTitle">${heading}</h1>
          <p class="authIntro">${subtitle}</p>
          <form id="authForm" novalidate>
            ${fields}
            <label>Email address
              <input name="email" type="email" required maxlength="320" autocomplete="email" placeholder="name@hospital.com">
            </label>
            <label>${passwordLabel}
              <input name="password" type="password" required minlength="${setup ? "12" : "1"}" maxlength="256" autocomplete="${passwordAutocomplete}" placeholder="${setup ? "At least 12 characters" : "Enter your password"}">
            </label>
            ${confirm}
            <button class="authSubmit" type="submit">${setup ? "Create workspace and continue" : "Sign in"}</button>
          </form>
          <p id="authMessage" class="authMessage" role="status">${message}</p>
          <p class="authPrivacy">Use synthetic or properly de-identified healthcare data only in this local development build.</p>
        </section>
      </div>`;
  }

  async function submitAuth(event, setup) {
    event.preventDefault();
    const form = event.currentTarget;
    const button = form.querySelector("button[type=submit]");
    const message = document.getElementById("authMessage");
    const data = Object.fromEntries(new FormData(form).entries());
    if (setup && data.password !== data.confirm_password) {
      message.textContent = "The passwords do not match.";
      return;
    }
    button.disabled = true;
    button.textContent = setup ? "Creating workspace???" : "Signing in???";
    message.textContent = "";
    try {
      const response = await fetch(setup ? "/api/auth/setup" : "/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify(data)
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "Authentication failed.");
      window.location.reload();
    } catch (error) {
      message.textContent = error.message || "Unable to sign in.";
      button.disabled = false;
      button.textContent = setup ? "Create workspace and continue" : "Sign in";
    }
  }

  function addAccountControls(identity) {
    const target = document.querySelector(".topActions");
    if (!target || document.getElementById("accountControls")) return;
    const wrap = document.createElement("div");
    wrap.id = "accountControls";
    wrap.className = "accountControls";
    const name = document.createElement("span");
    name.className = "accountName";
    name.textContent = `${identity.display_name || identity.email || "Signed in"} ?? ${identity.organization_name || "Workspace"}`;
    const logout = document.createElement("button");
    logout.className = "logoutBtn";
    logout.type = "button";
    logout.textContent = "Sign out";
    logout.addEventListener("click", async () => {
      logout.disabled = true;
      try {
        await fetch("/api/auth/logout", { method: "POST", credentials: "same-origin" });
      } finally {
        window.location.reload();
      }
    });
    wrap.append(name, logout);
    target.appendChild(wrap);
  }

  window.healthopsAuthReady = (async function () {
    try {
      const response = await fetch("/api/auth/state", { cache: "no-store", credentials: "same-origin" });
      const state = await response.json();
      if (!response.ok) throw new Error(state.error || "Authentication service is unavailable.");

      if (state.authenticated) {
        gate.hidden = true;
        app.hidden = false;
        app.setAttribute("aria-hidden", "false");
        if (!state.test_mode) addAccountControls(state.identity || {});
        return true;
      }

      showGate(authMarkup(Boolean(state.setup_required)));
      const form = document.getElementById("authForm");
      form.addEventListener("submit", event => submitAuth(event, Boolean(state.setup_required)));
      return false;
    } catch (error) {
      showGate(`
        <div class="authBackdrop"><section class="authCard authError">
          <div class="authBrand"><span class="authBrandMark">HO</span><div><strong>${productName}</strong><small>Healthcare data intelligence</small></div></div>
          <h1>Cannot reach the authentication service</h1>
          <p class="authIntro">${String(error.message || "Check PostgreSQL and the application server.").replace(/[&<>"']/g, "")}</p>
          <button class="authSubmit" id="authRetry" type="button">Try again</button>
        </section></div>`);
      document.getElementById("authRetry")?.addEventListener("click", () => window.location.reload());
      return false;
    }
  })();
})();
