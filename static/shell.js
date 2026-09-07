/* Shared app chrome: left nav, session, fetch 401. Pages set body[data-page]. */
(function () {
  const PAGES = [
    { id: "dashboard",   href: "/",                   label: "Dashboard" },
    { id: "analytics",   href: "/?view=analytics",    label: "Analytics" },
    { id: "open",        href: "/open",               label: "Open Tickets" },
    { id: "funcheck",    href: "/funcheck",           label: "Functionality Check" },
    { id: "weekly",      href: "/weekly",             label: "Weekly Dashboard" },
    { id: "reports",     href: "/reports",            label: "Product Report" },
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
    // Cancel any pending "ok" auto-hide: a stale timer from a previous save
    // would blank an error message someone is mid-way through reading.
    if (el._qcHide) { clearTimeout(el._qcHide); el._qcHide = null; }
    el.className = "msg " + kind;
    el.textContent = text;
    if (kind === "ok") {
      el._qcHide = setTimeout(() => { el.className = "msg"; el._qcHide = null; }, 4000);
    }
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
    if (location.pathname === "/weekly") return "weekly";
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

  // Placeholder markup for a period swap. Pages swap this in at the start of
  // load() so the previous month/range is never left on screen.
  const times = (n, fn) => Array.from({ length: n }, (_, i) => fn(i)).join("");

  QC.skeleton = {
    chips(n) {
      return `<div class="sk-chips" aria-hidden="true">${
        times(n || 5, () => `<div class="sk sk-chip"></div>`)
      }</div>`;
    },
    tableRows(cols, rows, widths) {
      const w = widths || [];
      return times(rows || 8, () => {
        const tds = times(cols, c =>
          `<td><span class="sk sk-line" style="width:${w[c] || (c === 1 ? "72%" : "48%")}"></span></td>`);
        // aria-hidden like every other skeleton helper — a screen reader
        // should not announce placeholder cells as data rows.
        return `<tr class="sk-row" aria-hidden="true">${tds}</tr>`;
      });
    },
    kpis(n) {
      return times(n || 6, () =>
        `<div class="kpi-tile" aria-hidden="true">
           <span class="sk sk-line" style="width:40%;height:28px;margin-bottom:8px"></span>
           <span class="sk sk-line" style="width:56%"></span>
         </div>`);
    },
    calDays(count) {
      return times(count || 35, () =>
        `<div class="day-cell empty sk" aria-hidden="true"></div>`);
    },
    cards(n) {
      return times(n || 6, () =>
        `<div class="sk-card" aria-hidden="true">
           <span class="sk sk-line" style="width:22%"></span>
           <span class="sk sk-line" style="width:78%;height:13px"></span>
           <span class="sk sk-line" style="width:46%"></span>
         </div>`);
    },
    weeks(n) {
      return times(n || 3, () =>
        `<div class="wk" aria-hidden="true">
           <div class="wk-head"><span class="sk sk-line" style="width:160px"></span></div>
           <span class="sk sk-line" style="width:82%;margin-top:8px"></span>
           <span class="sk sk-line" style="width:64%;margin-top:8px"></span>
         </div>`);
    },
  };

  QC.ready = (async function () {
    let me = null;
    try { me = await QC.api("/api/me"); } catch (e) {}
    QC.me = me;
    renderNav(me);
    return me;
  })();
})();
