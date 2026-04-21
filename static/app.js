// Tiny chat client: reads the Mealie JWT, posts to /chat/stream, renders
// SSE events.
//
// Mealie v3 is stateless JWT in localStorage (Nuxt @auth module stores
// `Bearer <jwt>` under `auth._token.local`) — no cookies. localStorage
// is origin-scoped, so recipes.epetersons.com
// and mealie-agent.epetersons.com CANNOT share it directly. Handoff is
// via URL fragment: a Mealie-side link (or bookmarklet) sends the user
// to `https://mealie-agent.epetersons.com/#token=<jwt>`, we stash the
// token in our own localStorage under `mealieAgentToken`, strip the
// fragment, and use it on every request.

const logEl = document.getElementById("log");
const formEl = document.getElementById("form");
const inputEl = document.getElementById("input");
const btnEl = formEl.querySelector("button");
const userEl = document.getElementById("user");

const api = (path) => path; // same origin

// Strip the "Bearer " prefix Nuxt @auth stores with the JWT.
function _clean(t) {
    if (!t) return t;
    return t.startsWith("Bearer ") ? t.slice(7) : t;
}

function getToken() {
    // 1. URL fragment: mealie-agent.epetersons.com/#token=...
    const hash = new URLSearchParams(window.location.hash.slice(1));
    if (hash.get("token")) {
        const t = _clean(hash.get("token"));
        localStorage.setItem("mealieAgentToken", t);
        history.replaceState(null, "", window.location.pathname);
        return t;
    }
    // 2. Previously stored.
    const stored = localStorage.getItem("mealieAgentToken");
    if (stored) return _clean(stored);
    // 3. Mealie's own localStorage key — only visible when same-origin.
    //    Mealie v3's Nuxt @auth module stores `Bearer <jwt>` under
    //    `auth._token.local`. Useful for a future same-origin sidebar.
    const mealie = localStorage.getItem("auth._token.local");
    if (mealie) return _clean(mealie);
    return null;
}

function append(kind, text) {
    const div = document.createElement("div");
    div.className = `msg ${kind}`;
    div.textContent = text;
    logEl.appendChild(div);
    logEl.scrollTop = logEl.scrollHeight;
    return div;
}

async function showWhoami(token) {
    try {
        const r = await fetch(api("/api/health"));
        const h = await r.json();
        userEl.textContent = `build ${h.commit ?? "?"}`;
    } catch {
        userEl.textContent = "offline";
    }
}

async function send(message, token) {
    btnEl.disabled = true;
    append("user", message);
    const agentMsg = append("agent", "");

    // sse-starlette over fetch + ReadableStream — EventSource doesn't allow
    // custom headers, and we need Authorization.
    const resp = await fetch(api("/chat/stream"), {
        method: "POST",
        headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({ message }),
    });
    if (!resp.ok) {
        append("err", `${resp.status}: ${await resp.text()}`);
        btnEl.disabled = false;
        return;
    }

    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = "";

    while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        // Strip CRs so both \n\n and \r\n\r\n delimiters work. sse-starlette
        // emits CRLF line endings; we treat either form as a frame separator.
        buf += dec.decode(value, { stream: true }).replace(/\r/g, "");
        let idx;
        while ((idx = buf.indexOf("\n\n")) !== -1) {
            const frame = buf.slice(0, idx);
            buf = buf.slice(idx + 2);
            const lines = frame.split("\n");
            let event = "message";
            let data = "";
            for (const line of lines) {
                if (line.startsWith("event: ")) event = line.slice(7);
                else if (line.startsWith("data: ")) data += line.slice(6);
            }
            if (event === "text") {
                agentMsg.textContent += data;
                logEl.scrollTop = logEl.scrollHeight;
            } else if (event === "thinking") {
                append("think", data);
            } else if (event === "tool_use") {
                append("tool", `🔧 ${data}`);
            } else if (event === "error") {
                append("err", data);
            } else if (event === "done") {
                // agentMsg already finalized.
            }
        }
    }
    btnEl.disabled = false;
    inputEl.focus();
}

async function doSend() {
    const msg = inputEl.value.trim();
    if (!msg) return;
    const token = getToken();
    if (!token) {
        append(
            "err",
            "No token. Get one from Mealie: log in to recipes.epetersons.com, " +
            "run `localStorage.mealie.access_token` in the browser console, " +
            "then open this page as `#token=<that-jwt>`."
        );
        return;
    }
    inputEl.value = "";
    await send(msg, token);
}

// Enter submits; Shift+Enter inserts a newline. Bypass HTML5 form
// validation by calling doSend directly — the textarea's `required`
// attribute would otherwise short-circuit requestSubmit() silently.
inputEl.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
        e.preventDefault();
        doSend();
    }
});

formEl.addEventListener("submit", async (e) => {
    e.preventDefault();
    await doSend();
});

const t = getToken();
if (!t) {
    append(
        "err",
        "No Mealie token found. Log in to Mealie first, or open this page via a #token=... link."
    );
} else {
    showWhoami(t);
    inputEl.focus();
}
