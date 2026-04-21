// Tiny chat client: reads the Mealie JWT from localStorage (populated by
// the host page / bookmarklet), posts to /chat/stream, renders SSE events.
//
// Mealie stores the session JWT in localStorage under `mealie.auth.token`
// when the user logs in via its Nuxt frontend. If this page is served on
// the same registerable domain (e.g. recipes.epetersons.com → chat-mealie),
// the cookie path makes it visible; if not, the user pastes the token or
// we fall back to a URL-fragment handoff (#token=...).

const logEl = document.getElementById("log");
const formEl = document.getElementById("form");
const inputEl = document.getElementById("input");
const btnEl = formEl.querySelector("button");
const userEl = document.getElementById("user");

const api = (path) => path; // same origin

function getToken() {
    // 1. URL fragment: chat-mealie.epetersons.com/#token=...
    const hash = new URLSearchParams(window.location.hash.slice(1));
    if (hash.get("token")) {
        const t = hash.get("token");
        localStorage.setItem("mealieAgentToken", t);
        history.replaceState(null, "", window.location.pathname);
        return t;
    }
    // 2. Previously stored.
    const stored = localStorage.getItem("mealieAgentToken");
    if (stored) return stored;
    // 3. Mealie's own localStorage key (when same-origin).
    const mealie = localStorage.getItem("mealie.auth.token");
    if (mealie) return mealie;
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
        buf += dec.decode(value, { stream: true });
        // SSE frames are delimited by blank lines.
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

formEl.addEventListener("submit", async (e) => {
    e.preventDefault();
    const msg = inputEl.value.trim();
    if (!msg) return;
    const token = getToken();
    if (!token) {
        append(
            "err",
            "No Mealie token found. Log in to Mealie first, or open this page via a #token=... link."
        );
        return;
    }
    inputEl.value = "";
    await send(msg, token);
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
