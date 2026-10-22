const notice = document.querySelector("#notice");

function show(text) {
  if (notice) notice.textContent = text;
}

let createAuthClient;
try {
  ({ createAuthClient } = await import("./auth-client.js"));
} catch {
  show("Sign-in did not load. Try again.");
}

if (createAuthClient) {
  try {
    const response = await fetch("/api/sign-in");
    if (!response.ok) throw new Error("missing");
    const config = await response.json();
    const client = createAuthClient({
      baseURL: config.authUrl,
      fetchOptions: { credentials: "include" },
    });

    async function handOff() {
      const root = String(config.authUrl || "").replace(/\/$/, "");
      const issued = await fetch(root + "/token", { credentials: "include" });
      const issuedBody = await issued.json().catch(() => ({}));
      const token = issued.ok && issuedBody ? issuedBody.token : "";
      if (!token) {
        show("Sign-in did not complete. Try again.");
        return;
      }
      const posted = await fetch("/api/session", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token }),
      });
      if (!posted.ok) {
        const body = await posted.json().catch(() => ({}));
        show(body.error || "Sign-in did not complete. Try again.");
        return;
      }
      window.location.assign("/");
    }

    if (location.pathname === "/sign-in/callback") {
      await handOff();
    }

    const social = document.querySelector("#social");
    if (social) {
      social.addEventListener("click", () => {
        client.signIn.social({
          provider: "google",
          callbackURL: config.callbackURL,
        });
      });
    }

    const form = document.querySelector("#email-form");
    if (form) {
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        const result = await client.signIn.email({
          email: document.querySelector("#email").value,
          password: document.querySelector("#password").value,
          callbackURL: config.callbackURL,
        });
        if (result && result.error) {
          show("Sign-in did not complete. Try again.");
          return;
        }
        await handOff();
      });
    }

    const signup = document.querySelector("#signup");
    if (signup) {
      signup.addEventListener("click", async () => {
        const result = await client.signUp.email({
          name: document.querySelector("#name").value || "Caller",
          email: document.querySelector("#email").value,
          password: document.querySelector("#password").value,
          callbackURL: config.callbackURL,
        });
        if (result && result.error) {
          show("The account was not created. Try again.");
          return;
        }
        await handOff();
      });
    }
  } catch {
    show("Sign-in did not load. Try again.");
  }
}
