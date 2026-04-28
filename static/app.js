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

import { marked } from "https://esm.sh/marked@12";

// When loaded inside the shim's drawer iframe, drop the standalone-page
// chrome (centering, card border). CSS keys off `body.iframe-mode`.
if (window.self !== window.top) document.body.classList.add("iframe-mode");

marked.setOptions({ breaks: true, gfm: true });

// All rendered links open in a new tab with safe rel attributes. Keeps
// the user's chat open when they click through to a Mealie recipe.
marked.use({
    renderer: {
        // marked v12 uses the legacy positional signature:
        // link(href, title, text) — text is already rendered HTML.
        link(href, title, text) {
            const t = title ? ` title="${title}"` : "";
            return `<a href="${href}"${t} target="_blank" rel="noopener noreferrer">${text}</a>`;
        },
    },
});

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

// Safari's ITP throws SecurityError on storage access in cross-origin
// iframes (which is exactly how the shim's drawer loads us). Treat any
// failure as "no stored token" — we still get a fresh one from the URL
// fragment on each iframe construction.
function _lsGet(k) { try { return localStorage.getItem(k); } catch { return null; } }
function _lsSet(k, v) { try { localStorage.setItem(k, v); } catch { /* ITP */ } }

function getToken() {
    // 1. URL fragment: mealie-agent.epetersons.com/#token=...
    const hash = new URLSearchParams(window.location.hash.slice(1));
    if (hash.get("token")) {
        const t = _clean(hash.get("token"));
        _lsSet("mealieAgentToken", t);
        history.replaceState(null, "", window.location.pathname);
        return t;
    }
    // 2. Previously stored.
    const stored = _lsGet("mealieAgentToken");
    if (stored) return _clean(stored);
    // 3. Mealie's own localStorage key — only visible when same-origin.
    //    Mealie v3's Nuxt @auth module stores `Bearer <jwt>` under
    //    `auth._token.local`. Useful for a future same-origin sidebar.
    const mealie = _lsGet("auth._token.local");
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
    // Agent message: accumulate plain-text chunks in markdownBuf, re-render
    // the whole buffer as HTML on each update. Streaming a partial markdown
    // document through marked is resilient — unclosed tokens render as
    // plain text until the closer arrives.
    //
    // Both are reassigned after each tool call so the next text chunk lives
    // in a fresh bubble (see tool_use branch below).
    let agentMsg = append("agent markdown", "");
    let markdownBuf = "";

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
            const dataParts = [];
            for (const line of lines) {
                if (line.startsWith("event: ")) event = line.slice(7);
                // Per SSE spec, multiple `data:` lines in a frame are joined
                // with \n — sse-starlette uses this to encode newlines in a
                // single event. Concatenating without \n flattens lists and
                // headers back into one paragraph.
                else if (line.startsWith("data: ")) dataParts.push(line.slice(6));
                else if (line.startsWith("data:")) dataParts.push(line.slice(5));
            }
            const data = dataParts.join("\n");
            if (event === "text") {
                // After a tool call, start a fresh agent bubble + buffer so
                // post-tool text doesn't fuse with pre-tool text visually.
                if (!agentMsg) {
                    agentMsg = append("agent markdown", "");
                    markdownBuf = "";
                }
                markdownBuf += data;
                let html;
                try {
                    html = marked.parse(markdownBuf);
                } catch (err) {
                    console.warn("[chat] marked threw on partial buffer", err);
                    // Preserve newlines as <br> so the fallback stays
                    // readable (CSS sets white-space: normal on markdown
                    // bubbles, which otherwise collapses them to spaces).
                    const esc = markdownBuf
                        .replace(/&/g, "&amp;")
                        .replace(/</g, "&lt;")
                        .replace(/>/g, "&gt;");
                    html = esc.replace(/\n/g, "<br>");
                }
                agentMsg.innerHTML = html;
                logEl.scrollTop = logEl.scrollHeight;
            } else if (event === "thinking") {
                append("think", data);
            } else if (event === "tool_use") {
                append("tool", `🔧 ${data}`);
                // Any following text starts a new bubble.
                agentMsg = null;
                markdownBuf = "";
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
