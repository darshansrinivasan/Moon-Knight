/* Shared app chrome: left nav, session, fetch 401. Pages set body[data-page]. */
(function () {
  // Two platforms, one shell. The switcher swaps which page list renders;
  // /rootly/* paths select the Rootly platform automatically.
  const ROOTLY_PAGES = [
    { id: "rootly",       href: "/rootly",       label: "Incidents" },
    { id: "rootly-runs",  href: "/rootly/runs",  label: "Runs" },
    { id: "rootly-rules", href: "/rootly/rules", label: "Rules" },
    { id: "rootly-admin", href: "/rootly/admin", label: "Admin", memberLabel: "Settings" },
  ];

  const PAGES = [
    { id: "dashboard",   href: "/",                   label: "Dashboard" },
    { id: "analytics",   href: "/?view=analytics",    label: "Analytics" },
    { id: "open",        href: "/open",               label: "Open Tickets" },
    { id: "funcheck",    href: "/funcheck",           label: "Functionality Check" },
    { id: "weekly",      href: "/weekly",             label: "Weekly Dashboard" },
    { id: "reports",     href: "/reports",            label: "Product Report" },
    { id: "leaderboard", href: "/leaderboard",        label: "Leaderboard" },
    { id: "reportcard",  href: "/reportcard",         label: "Report Card" },
    { id: "runs",        href: "/runs",               label: "Runs" },
    { id: "rules",       href: "/rules",              label: "Rules" },
    { id: "admin",       href: "/admin",              label: "Admin", memberLabel: "Settings" },
    // Last on purpose: the CSM page is the one surface here whose reader is not
    // the support team, so it sits below the QC tools rather than among them.
    { id: "csm",         href: "/csm",                label: "CSM Page" },
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
    if (!r.ok) {
      // The status rides along: some refusals are not failures. A 409 from a
      // shared job means someone else is already doing the work, which a
      // caller should report differently from a real error.
      const err = new Error(data.detail || r.statusText || `HTTP ${r.status}`);
      err.status = r.status;
      throw err;
    }
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

  function currentPlatform() {
    return location.pathname.startsWith("/rootly") ? "rootly" : "pylon";
  }

  function currentPage() {
    const fromBody = document.body.dataset.page;
    // runs.html is served on both platforms; its hardcoded data-page must not
    // pin the Rootly copy to the Pylon nav item.
    if (fromBody === "runs" && currentPlatform() === "rootly") return "rootly-runs";
    if (fromBody) return fromBody;
    if (location.pathname.startsWith("/rootly/rules")) return "rootly-rules";
    if (location.pathname.startsWith("/rootly/admin")) return "rootly-admin";
    if (location.pathname.startsWith("/rootly")) return "rootly";
    if (location.pathname === "/runs") return "runs";
    if (location.pathname === "/rules") return "rules";
    if (location.pathname === "/admin") return "admin";
    if (location.pathname === "/leaderboard") return "leaderboard";
    if (location.pathname === "/reportcard") return "reportcard";
    if (location.pathname === "/weekly") return "weekly";
    if (new URLSearchParams(location.search).get("view") === "analytics") return "analytics";
    return "dashboard";
  }

  function renderNav(me) {
    const host = document.getElementById("app-nav");
    if (!host) return;
    const page = currentPage();
    const platform = currentPlatform();
    const pages = platform === "rootly" ? ROOTLY_PAGES : PAGES;
    const adminLabel = me && me.role === "member" ? "Settings" : "Admin";
    const links = pages.map(p => {
      const label = p.memberLabel && me && me.role === "member" ? p.memberLabel : p.label;
      const cls = p.id === page ? "active" : "";
      return `<a href="${p.href}" class="${cls}" data-nav="${p.id}">${QC.esc(label)}</a>`;
    }).join("");

    const pic = me && me.picture
      ? `<img src="${QC.esc(me.picture)}" alt="">`
      : "";
    const name = me ? (me.name || me.email || "") : "";
    const brand = platform === "rootly" ? "Rootly <span>QC</span>" : "Pylon <span>QC</span>";

    host.innerHTML = `
      <div class="app-nav-brand">${brand}</div>
      <div class="app-nav-spend" id="app-nav-spend" hidden></div>
      <div class="app-nav-switch" role="tablist" aria-label="Platform">
        <a href="/" class="${platform === "pylon" ? "on" : ""}"
           title="Support-ticket QC (Pylon)">Pylon</a>
        <a href="/rootly" class="${platform === "rootly" ? "on" : ""}"
           title="Incident QC (Rootly)">Rootly</a>
      </div>
      <div class="app-nav-links">${links}</div>
      <div class="app-nav-foot">
        <div class="app-nav-user">${pic}<span title="${QC.esc(name)}">${QC.esc(name)}</span></div>
        <a class="app-nav-signout" href="/auth/logout">Sign out</a>
      </div>`;
  }

  // What the tool has cost, on every page. Operator-gated because cost is an
  // operations figure; a member reviewing tickets has no use for it and no say
  // in it. One fetch per page load — qc_runs is small and the figure is
  // cumulative, so it never needs polling.
  async function renderSpend(me) {
    const host = document.getElementById("app-nav-spend");
    if (!host || !me || me.role === "member") return;
    try {
      const d = await QC.api("/api/spend/total");
      const usd = Number(d.total_usd || 0);
      const split = (d.by_kind || [])
        .map(b => `${b.kind}: $${Number(b.usd).toFixed(2)} (${b.runs} run${b.runs === 1 ? "" : "s"})`)
        .join("\n");
      host.hidden = false;
      host.innerHTML =
        `<span class="k">AI spend</span>` +
        `<span class="v">${d.estimated ? "~" : ""}$${usd.toFixed(2)}</span>`;
      host.title =
        `${d.runs} run${d.runs === 1 ? "" : "s"} since this install began\n\n${split}` +
        `\n\nEstimated from a local price table, not a billed amount.`;
    } catch {
      /* a spend figure is never worth breaking the nav over */
    }
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
    renderSpend(me);          // fire-and-forget: the nav must not wait on it
    return me;
  })();
})();
