// meal-agent shim — loaded into Mealie's HTML by NPM's sub_filter rule.
//
// Injects a floating "Meal Assistant" button on every Mealie page. On
// click, reads the Nuxt-stored JWT from Mealie's localStorage and opens
// mealie-agent in a new tab with the token handed off via URL fragment.
// Runs in recipes.epetersons.com's origin, so localStorage is visible.
//
// Kept deliberately small + dependency-free. Catches its own errors so a
// breakage here can never take down Mealie itself.

(function () {
    "use strict";
    if (window.__mealAgentShim) return;        // idempotent
    window.__mealAgentShim = true;

    const CHAT_URL = "https://mealie-agent.epetersons.com";
    const LOGIN_HINT = "Open Mealie's login page first, then try again.";

    function _clean(t) {
        if (!t) return null;
        t = String(t).trim();
        if (t.startsWith("Bearer ")) t = t.slice(7);
        return t || null;
    }

    function _cookie(name) {
        const pairs = (document.cookie || "").split("; ");
        for (const p of pairs) {
            const eq = p.indexOf("=");
            if (eq > -1 && p.slice(0, eq) === name) {
                return decodeURIComponent(p.slice(eq + 1));
            }
        }
        return null;
    }

    function getToken() {
        // Mealie v3 stores the JWT in a couple of places depending on
        // version/config. Check them all — the first hit wins.
        try {
            // 1. Nuxt @auth module (most Mealie installs).
            const a = _clean(localStorage.getItem("auth._token.local"));
            if (a) return a;
            // 2. Alternative localStorage key some Mealie builds use.
            const b = _clean(localStorage.getItem("mealie.access_token"));
            if (b) return b;
            // 3. Readable cookie (non-HttpOnly).
            const c = _clean(_cookie("mealie.access_token"));
            if (c) return c;
        } catch (_) { /* SecurityError in some iframes; fall through */ }
        return null;
    }

    function openChat() {
        const token = getToken();
        if (!token) { alert("No Mealie session found. " + LOGIN_HINT); return; }
        window.open(`${CHAT_URL}/#token=${token}`, "_blank", "noopener,noreferrer");
    }

    const BTN_ID = "meal-agent-shim-btn";

    function buildBtn() {
        const btn = document.createElement("button");
        btn.id = BTN_ID;
        btn.type = "button";
        btn.textContent = "🍳 Meal Assistant";
        btn.title = "Open Meal Assistant (opens in a new tab)";
        btn.setAttribute("aria-label", "Open Meal Assistant");
        Object.assign(btn.style, {
            position: "fixed",
            bottom: "1.25rem",
            right: "1.25rem",
            zIndex: "2147483647",   // top of the stack, beats Vuetify overlays
            padding: "0.6rem 1rem",
            borderRadius: "999px",
            border: "none",
            background: "#E58325",   // Mealie's primary
            color: "white",
            fontFamily: "inherit",
            fontSize: "0.9rem",
            fontWeight: "600",
            cursor: "pointer",
            boxShadow: "0 6px 20px -6px rgba(0,0,0,0.4)",
            transition: "transform 0.1s ease",
        });
        btn.addEventListener("mouseenter", () => btn.style.transform = "scale(1.04)");
        btn.addEventListener("mouseleave", () => btn.style.transform = "scale(1)");
        btn.addEventListener("click", openChat);
        return btn;
    }

    function ensureBtn() {
        try {
            if (!document.body) return;
            if (document.getElementById(BTN_ID)) return;   // already present
            document.body.appendChild(buildBtn());
        } catch (err) {
            console.warn("[meal-agent-shim] inject failed:", err);
        }
    }

    // Nuxt hydration replaces body contents AFTER DOMContentLoaded, which
    // wipes any early injection. A MutationObserver on <body> re-adds the
    // button whenever it disappears (route changes, hot-reloads, etc.).
    function startObserver() {
        if (!document.body) return;
        ensureBtn();
        const obs = new MutationObserver(() => ensureBtn());
        obs.observe(document.body, { childList: true, subtree: false });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", startObserver, { once: true });
    } else {
        startObserver();
    }
})();
