/* Shared app chrome: left nav, session, fetch 401. Pages set body[data-page]. */
(function () {
  const PAGES = [
    { id: "dashboard",   href: "/",                   label: "Dashboard" },
    { id: "analytics",   href: "/?view=analytics",    label: "Analytics" },
    { id: "leaderboard", href: "/leaderboard",        label: "Leaderboard" },
    { id: "runs",        href: "/runs",               label: "Runs" },
    { id: "rules",       href: "/rules",              label: "Rules" },
    { id: "admin",       href: "/admin",              label: "Admin", memberLabel: "Settings" },
  ];

  window.QC = window.QC || {};

  QC.esc = function (s) {
    return String(s ?? "")
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  };

  QC.fmtTime = function (iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    return isNaN(d) ? iso : d.toLocaleString();
  };

  QC.show = function (el, text, kind) {
    if (!el) return;
    el.className = "msg " + kind;
    el.textContent = text;
    if (kind === "ok") setTimeout(() => { el.className = "msg"; }, 4000);
  };

  QC.api = async function (url, opts = {}) {
    const r = await fetch(url, {
      headers: { "Content-Type": "application/json" },
      ...opts,
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || r.statusText || `HTTP ${r.status}`);
    return data;
  };

  // A session can expire while the tab is open.
  const _fetch = window.fetch;
  window.fetch = async (...args) => {
    const resp = await _fetch(...args);
    if (resp.status === 401) {
      location.href = "/login?next=" + encodeURIComponent(location.pathname + location.search);
    }
    return resp;
  };

  function currentPage() {
    const fromBody = document.body.dataset.page;
    if (fromBody) return fromBody;
    if (location.pathname === "/runs") return "runs";
    if (location.pathname === "/rules") return "rules";
    if (location.pathname === "/admin") return "admin";
    if (location.pathname === "/leaderboard") return "leaderboard";
    if (new URLSearchParams(location.search).get("view") === "analytics") return "analytics";
    return "dashboard";
  }

  function renderNav(me) {
    const host = document.getElementById("app-nav");
    if (!host) return;
    const page = currentPage();
    const adminLabel = me && me.role === "member" ? "Settings" : "Admin";
    const links = PAGES.map(p => {
      const href = p.id === "admin" ? "/admin" : p.href;
      const label = p.id === "admin" ? adminLabel : p.label;
      const cls = p.id === page ? "active" : "";
      return `<a href="${href}" class="${cls}" data-nav="${p.id}">${QC.esc(label)}</a>`;
    }).join("");

    const pic = me && me.picture
      ? `<img src="${QC.esc(me.picture)}" alt="">`
      : "";
    const name = me ? (me.name || me.email || "") : "";

    host.innerHTML = `
      <div class="app-nav-brand">Pylon <span>QC</span></div>
      <div class="app-nav-links">${links}</div>
      <div class="app-nav-foot">
        <div class="app-nav-user">${pic}<span title="${QC.esc(name)}">${QC.esc(name)}</span></div>
        <a class="app-nav-signout" href="/auth/logout">Sign out</a>
      </div>`;
  }

  QC.ready = (async function () {
    let me = null;
    try { me = await QC.api("/api/me"); } catch (e) {}
    QC.me = me;
    renderNav(me);
    return me;
  })();
})();
