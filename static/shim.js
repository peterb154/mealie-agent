// meal-agent shim — loaded into Mealie's HTML by NPM's sub_filter rule.
//
// Injects a small floating "🧑‍🍳" button on every Mealie page. Click =
// open an iframe drawer overlaid on Mealie (in-place chat, no new tab).
// Reads the Nuxt-stored JWT from Mealie's localStorage and hands it to
// the iframe via URL fragment.
//
// Dev override: a host page can set
//   window.__chefRexConfig = { chatUrl: "http://localhost:8080/static/index.html" }
// before loading this script to point the iframe somewhere else. Use the
// explicit path to whatever serves the chat UI — the shim normalizes a
// trailing slash, but won't synthesize a missing path component.
//
// Kept deliberately small + dependency-free. Catches its own errors so a
// breakage here can never take down Mealie itself.

(function () {
    "use strict";
    if (window.__mealAgentShim) return;        // idempotent
    window.__mealAgentShim = true;

    const CFG = window.__chefRexConfig || {};
    const CHAT_URL = CFG.chatUrl || "https://mealie-agent.epetersons.com";
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

    // ---- Drawer overlay ---------------------------------------------------
    //
    // Lazy-built on first open, then kept in the DOM (hidden) so the
    // iframe — and its chat scrollback — survive close/reopen across
    // Mealie SPA route changes. Only a full page reload drops state.

    const DRAWER_ID = "meal-agent-drawer";
    const BACKDROP_ID = "meal-agent-backdrop";
    let drawerEl = null;
    let backdropEl = null;
    let iframeEl = null;
    let drawerOpen = false;

    function buildDrawer() {
        backdropEl = document.createElement("div");
        backdropEl.id = BACKDROP_ID;
        Object.assign(backdropEl.style, {
            position: "fixed",
            inset: "0",
            background: "rgba(0,0,0,0.35)",
            zIndex: "2147483645",
            opacity: "0",
            pointerEvents: "none",
            transition: "opacity 0.2s ease",
        });
        backdropEl.addEventListener("click", closeDrawer);

        drawerEl = document.createElement("div");
        drawerEl.id = DRAWER_ID;
        drawerEl.setAttribute("role", "dialog");
        drawerEl.setAttribute("aria-modal", "true");
        drawerEl.setAttribute("aria-label", "Chef Rex chat");
        Object.assign(drawerEl.style, {
            position: "fixed",
            top: "0",
            right: "0",
            bottom: "0",
            width: "min(440px, 100vw)",
            height: "100dvh",
            background: "#faf5ef",
            boxShadow: "-10px 0 30px -10px rgba(0,0,0,0.3)",
            zIndex: "2147483646",
            transform: "translateX(100%)",
            transition: "transform 0.25s ease",
            display: "flex",
            flexDirection: "column",
        });

        // Slim header bar with a close button. Sits above the iframe so
        // it's always reachable (the chat header inside the iframe stays
        // for branding).
        const bar = document.createElement("div");
        Object.assign(bar.style, {
            display: "flex",
            justifyContent: "flex-end",
            padding: "0.3rem 0.4rem",
            background: "#f3ead9",
            borderBottom: "1px solid #e7e1d8",
            flex: "0 0 auto",
        });
        const closeBtn = document.createElement("button");
        closeBtn.type = "button";
        closeBtn.textContent = "✕";
        closeBtn.title = "Close chat";
        closeBtn.setAttribute("aria-label", "Close chat");
        Object.assign(closeBtn.style, {
            border: "none",
            background: "transparent",
            fontSize: "1.25rem",
            lineHeight: "1",
            cursor: "pointer",
            padding: "0.3rem 0.6rem",
            color: "#6b6b6b",
            borderRadius: "0.4rem",
        });
        closeBtn.addEventListener("click", closeDrawer);
        bar.appendChild(closeBtn);
        drawerEl.appendChild(bar);

        // Attach to <html>, NOT <body> — Nuxt swaps body wholesale on
        // route transitions, which would detach the iframe and reload
        // the chat (losing scrollback). <html> is stable.
        document.documentElement.appendChild(backdropEl);
        document.documentElement.appendChild(drawerEl);

        // ESC closes the drawer when it's open.
        document.addEventListener("keydown", (e) => {
            if (e.key === "Escape" && drawerOpen) closeDrawer();
        });
    }

    function ensureIframe() {
        if (iframeEl) return true;
        const token = getToken();
        if (!token) { alert("No Mealie session found. " + LOGIN_HINT); return false; }
        iframeEl = document.createElement("iframe");
        // Strip a trailing slash so chatUrl works either as a bare origin
        // ("https://x.example") or as an explicit file ("…/index.html") —
        // the latter being how the local dev host points at the proto.
        const base = CHAT_URL.replace(/\/+$/, "");
        iframeEl.src = `${base}#token=${encodeURIComponent(token)}`;
        iframeEl.setAttribute("title", "Chef Rex chat");
        Object.assign(iframeEl.style, {
            flex: "1 1 auto",
            width: "100%",
            border: "none",
            background: "#faf5ef",
        });
        drawerEl.appendChild(iframeEl);
        return true;
    }

    function openDrawer() {
        if (!drawerEl) buildDrawer();
        if (!ensureIframe()) return;
        drawerOpen = true;
        backdropEl.style.opacity = "1";
        backdropEl.style.pointerEvents = "auto";
        drawerEl.style.transform = "translateX(0)";
    }

    function closeDrawer() {
        drawerOpen = false;
        if (backdropEl) {
            backdropEl.style.opacity = "0";
            backdropEl.style.pointerEvents = "none";
        }
        if (drawerEl) {
            drawerEl.style.transform = "translateX(100%)";
        }
    }

    function toggleDrawer() {
        drawerOpen ? closeDrawer() : openDrawer();
    }

    // ---- Trigger pill -----------------------------------------------------
    //
    // Bottom-right (chat-widget convention) but offset upward so Mealie's
    // own FAB (+/edit/save, ~3.5rem at bottom: 1.25rem) clears underneath.
    // On pages without a FAB the small gap below the pill is fine — it
    // still reads as a corner-floating chat widget.

    const BTN_ID = "meal-agent-shim-btn";

    function buildBtn() {
        const btn = document.createElement("button");
        btn.id = BTN_ID;
        btn.type = "button";
        btn.textContent = "💬";
        btn.title = "Chat with Chef Rex";
        btn.setAttribute("aria-label", "Chat with Chef Rex");
        Object.assign(btn.style, {
            position: "fixed",
            bottom: "5.5rem",       // clears Mealie's FAB stack
            right: "1.25rem",       // aligns with Mealie's FAB column
            zIndex: "2147483644",   // below drawer/backdrop, above app chrome
            width: "3.5rem",        // matches Mealie's FAB diameter
            height: "3.5rem",
            padding: "0",
            borderRadius: "999px",
            border: "none",
            background: "#E58325",   // Mealie's primary
            color: "white",
            fontFamily: "inherit",
            fontSize: "1.75rem",     // 💬 has internal padding — needs to be bigger
            lineHeight: "1",
            cursor: "pointer",
            boxShadow: "0 6px 20px -6px rgba(0,0,0,0.4)",
            transition: "transform 0.1s ease",
        });
        btn.addEventListener("mouseenter", () => btn.style.transform = "scale(1.06)");
        btn.addEventListener("mouseleave", () => btn.style.transform = "scale(1)");
        btn.addEventListener("click", toggleDrawer);
        return btn;
    }

    function ensureBtn() {
        try {
            const root = document.documentElement;
            if (!root) return;
            // Attach to <html> so Nuxt's body swap can't detach us.
            if (!document.getElementById(BTN_ID)) {
                root.appendChild(buildBtn());
            }
            // Defensive: if drawer/backdrop got detached somehow, rescue
            // them rather than rebuild (preserves the iframe + scrollback).
            if (drawerEl && !root.contains(drawerEl)) {
                root.appendChild(backdropEl);
                root.appendChild(drawerEl);
            }
        } catch (err) {
            console.warn("[meal-agent-shim] inject failed:", err);
        }
    }

    // Nuxt hydration replaces document.body wholesale (not just children)
    // and may do so multiple times for route transitions. We use three
    // mechanisms together so at least one catches every case:
    //   1. a MutationObserver on <html> so we see body being swapped out
    //   2. a MutationObserver on whatever <body> currently is, re-attached
    //      when it changes, so we see children-only changes
    //   3. a cheap setInterval as belt-and-suspenders for anything exotic
    //      (Safari quirks, mobile webview rendering, etc.)
    let bodyObs = null;
    function attachBodyObserver() {
        if (!document.body) return;
        if (bodyObs) bodyObs.disconnect();
        bodyObs = new MutationObserver((muts) => {
            ensureBtn();
            // Retarget any newly-added links in whichever subtree mutated.
            for (const m of muts) {
                for (const n of m.addedNodes) {
                    if (n.nodeType === 1) retargetExternalLinks(n);
                }
            }
        });
        // subtree:true so dynamically-added shopping items get caught.
        bodyObs.observe(document.body, { childList: true, subtree: true });
    }

    // Mealie auto-linkifies URLs in shopping-list notes but doesn't set
    // target=_blank, so clicking a grocery-search link navigates away
    // from the list. Sweep the DOM for <a href> pointing at a different
    // origin and rewrite them in place. Cheap and idempotent.
    function retargetExternalLinks(root) {
        const r = root || document.body;
        if (!r || !r.querySelectorAll) return;
        for (const a of r.querySelectorAll("a[href]")) {
            if (a.dataset.mealAgentRetargeted) continue;
            const href = a.getAttribute("href") || "";
            if (!/^https?:\/\//i.test(href)) continue;
            try {
                if (new URL(href).host === location.host) continue;
            } catch { continue; }
            a.setAttribute("target", "_blank");
            a.setAttribute("rel", "noopener noreferrer");
            a.dataset.mealAgentRetargeted = "1";
        }
    }

    function startAll() {
        ensureBtn();
        retargetExternalLinks();
        attachBodyObserver();
        if (document.documentElement) {
            new MutationObserver(() => {
                ensureBtn();
                retargetExternalLinks();
                attachBodyObserver();   // <body> may have been replaced
            }).observe(document.documentElement, { childList: true, subtree: false });
        }
        setInterval(() => { ensureBtn(); retargetExternalLinks(); }, 2000);
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", startAll, { once: true });
    } else {
        startAll();
    }
})();
