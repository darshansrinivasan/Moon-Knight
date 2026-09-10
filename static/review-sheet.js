/* The frozen-record review sheet, as ONE shared component.
 *
 * Used by the Report Card (its native home) and embedded in the Open Tickets
 * tab, so reviewing an open ticket is zero redirects. One implementation on
 * purpose: two hand-rolled copies of "what a frozen record looks like and how
 * a review is recorded" is exactly the two-homes drift this codebase keeps
 * paying for.
 *
 * Contract: QCReviewSheet.open({date, number, onReviewed}) fetches the frozen
 * record from /api/reportcard/ticket/{date}/{number}, renders it with the
 * live conversation beneath, and records reviews through the single
 * /api/ticket/{id}/review stream (note mandatory; revert is lowercase — the
 * server treats capital-R "Revert" as a sign-off needing a note).
 * `onReviewed` fires after any successful verdict change, so the host page
 * can refresh its own rows. Also exported: CHECKS, MATRIX, cellState,
 * CELL_GLYPH — the one copy of the check vocabulary the Report Card's card
 * grid reads too.
 */
(function () {
  const esc = s => (window.QC ? QC.esc(s) : String(s ?? ""));

  const CHECKS = {
    r1: { label: "Functionality" }, r2: { label: "Category" },
    r3: { label: "Account" }, r4: { label: "Response Time" },
    r5: { label: "Status Owner" }, r7: { label: "Rootly/Jira" },
    r8: { label: "Oncall Check" }, r10: { label: "SpotAssist (advisory)" },
    r11: { label: "Follow-through" },
    a1: { label: "Cat. Accuracy" }, a2: { label: "Sentiment" },
    a3: { label: "Response" }, a4: { label: "Status Check" },
    a5: { label: "Closure" },
  };
  const MATRIX = [
    ["r1", "Fn"], ["r2", "Ct"], ["r3", "Ac"], ["r4", "RT"],
    ["r5", "SO"], ["r7", "RJ"], ["r8", "OC"], ["r10", "SA"], ["r11", "FT"],
    ["a1", "CA"], ["a2", "Sn"], ["a3", "Rs"], ["a4", "SC"], ["a5", "Cl"],
  ];
  const CELL_GLYPH = { pass: "✓", fail: "✕", warn: "!", na: "–", none: "" };
  const GRADE_CLS = { "Pass": "g-Pass", "Fail": "g-Fail", "Needs Review": "g-NR" };
  const DELTA_LABEL = {
    remediated: "Remediated — failing at snapshot, passing now",
    outstanding: "Outstanding — still failing",
    regressed: "Regressed — passing at snapshot, failing now",
  };

  function cellState(key, v) {
    if (v === null || v === undefined || v === "") return "none";
    if (v === "N/A") return "na";
    if (key === "a2") return "na";              // sentiment is never a verdict
    if (v === "Pass" || v === "Good" || v === "Accurate" || v === "Consistent")
      return "pass";
    if (v === "Fail" || v === "Poor" || v === "Inaccurate" || v === "Inconsistent")
      return "fail";
    return "warn";
  }

  const CSS = `
  .rvs-scrim { position: fixed; inset: 0; z-index: 40;
    background: color-mix(in srgb, var(--color-neutral-900) 70%, transparent); }
  .rvs-scrim[hidden] { display: none; }
  .rvs-sheet { position: fixed; top: 0; right: 0; bottom: 0; z-index: 50;
    width: min(560px, 94vw); background: var(--surface);
    border-left: 1px solid var(--border); display: flex; flex-direction: column;
    font-size: 13px; }
  .rvs-sheet[hidden] { display: none; }
  .rvs-head { display: flex; gap: 10px; align-items: flex-start;
    padding: 16px 18px 10px; border-bottom: 1px solid var(--border); }
  .rvs-head-main { flex: 1; min-width: 0; }
  .rvs-num { color: var(--accent2); font-weight: 700; font-size: 13px; }
  .rvs-num a { color: var(--accent2); text-decoration: none; }
  .rvs-num a:hover { text-decoration: underline; }
  .rvs-title { margin: 2px 0 4px; font-size: 15px; line-height: 1.35; }
  .rvs-meta { color: var(--muted); font-size: 12px; }
  .rvs-meta a { color: var(--accent2); text-decoration: none; }
  .rvs-close { background: none; border: 1px solid var(--border);
    border-radius: 8px; color: var(--muted); cursor: pointer;
    padding: 4px 10px; font-size: 13px; }
  .rvs-close:hover { border-color: var(--accent); color: var(--text); }
  .rvs-tags { display: flex; gap: 8px; flex-wrap: wrap; padding: 10px 18px 0; }
  .rvs-badge { padding: 2px 10px; border-radius: 999px; font-size: 11px;
    font-weight: 700; }
  .rvs-badge.pass { background: color-mix(in srgb, var(--pass) 18%, transparent); color: var(--pass); }
  .rvs-badge.fail { background: color-mix(in srgb, var(--fail) 18%, transparent); color: var(--fail); }
  .rvs-badge.review { background: color-mix(in srgb, var(--review) 20%, transparent); color: var(--review); }
  .rvs-badge.pending { background: var(--surface2); color: var(--muted); }
  .rvs-tag { font-size: 11px; padding: 1px 8px; border-radius: 999px;
    background: var(--surface2); color: var(--muted);
    border: 1px solid var(--border); }
  .rvs-chip { display: inline-block; padding: 1px 8px; border-radius: 999px;
    font-size: 11px; font-weight: 600; white-space: nowrap; }
  .rvs-chip.remediated { background: color-mix(in srgb, var(--pass) 15%, transparent); color: var(--pass); }
  .rvs-chip.outstanding { background: color-mix(in srgb, var(--fail) 15%, transparent); color: var(--fail); }
  .rvs-chip.regressed { background: color-mix(in srgb, var(--review) 18%, transparent); color: var(--review); }
  .rvs-body { flex: 1; overflow-y: auto; padding: 6px 18px 24px; }
  .rvs-section { margin-top: 16px; }
  .rvs-section-title { font-size: 11px; text-transform: uppercase;
    letter-spacing: .5px; color: var(--muted); font-weight: 600;
    margin-bottom: 8px; }
  .rvs-check { display: flex; gap: 10px; padding: 7px 0; font-size: 12.5px;
    border-bottom: 1px solid color-mix(in srgb, var(--color-text) 6%, transparent); }
  .rvs-check-head { display: flex; justify-content: space-between; gap: 10px; flex: 1; }
  .g-Pass { color: var(--pass); font-weight: 600; }
  .g-Fail { color: var(--fail); font-weight: 600; }
  .g-NR { color: var(--review); font-weight: 600; }
  .g-none { color: var(--muted); }
  .rvs-cell { width: 16px; height: 20px; border-radius: 3px; flex: none;
    display: flex; align-items: center; justify-content: center;
    font-size: 9px; font-weight: 600; line-height: 1; }
  .rvs-cell.c-pass { background: var(--pass); color: #10201a; }
  .rvs-cell.c-fail { background: var(--fail); color: #20111a; }
  .rvs-cell.c-warn { background: var(--review); color: #241a0d; }
  .rvs-cell.c-na { background: color-mix(in srgb, var(--color-text) 7%, transparent);
    border: 1px solid color-mix(in srgb, var(--color-text) 14%, transparent);
    color: color-mix(in srgb, var(--color-text) 30%, transparent); }
  .rvs-cell.c-none { border: 1px dashed color-mix(in srgb, var(--color-text) 18%, transparent);
    color: transparent; }
  .rvs-msgs { display: flex; flex-direction: column; gap: 10px; }
  .rvs-bubble { background: var(--surface2); border: 1px solid var(--border);
    border-radius: 10px; padding: 8px 12px; font-size: 12.5px; }
  .rvs-bubble.customer { border-color: color-mix(in srgb, var(--accent) 45%, transparent); }
  .rvs-bubble.private { opacity: .75; border-style: dashed; }
  .rvs-author { font-weight: 600; font-size: 11.5px; margin-bottom: 3px; }
  .rvs-author.customer { color: var(--accent2); }
  .rvs-time { color: var(--muted); font-size: 10.5px; margin-top: 4px; }
  .rvs-foot { border-top: 1px solid var(--border); padding: 12px 18px;
    display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  .rvs-foot .rvs-who { color: var(--muted); font-size: 12px; flex: 1;
    min-width: 160px; }
  .rvs-btn { padding: 6px 12px; border-radius: 8px; font-size: 13px;
    cursor: pointer; border: 1px solid var(--border);
    background: var(--surface2); color: var(--text); }
  .rvs-btn:hover { border-color: var(--accent); }
  .rvs-btn.pass { border-color: color-mix(in srgb, var(--pass) 50%, transparent); color: var(--pass); }
  .rvs-btn.fail { border-color: color-mix(in srgb, var(--fail) 50%, transparent); color: var(--fail); }
  .rvs-note-cell { color: var(--muted); font-size: 12px; }
  .rvs-empty { color: var(--muted); padding: 20px 0; font-size: 12.5px; }
  .rvs-modal-scrim { position: fixed; inset: 0; z-index: 60;
    background: rgba(0,0,0,.45);
    display: flex; align-items: center; justify-content: center; }
  .rvs-modal-scrim[hidden] { display: none; }
  .rvs-modal { background: var(--surface); border: 1px solid var(--border);
    border-radius: var(--radius); padding: 20px; width: min(440px, 92vw); }
  .rvs-modal h3 { margin: 0 0 4px; font-size: 15px; }
  .rvs-modal .rvs-sub { color: var(--muted); font-size: 12.5px; margin: 0 0 12px; }
  .rvs-modal textarea { width: 100%; min-height: 84px; box-sizing: border-box;
    resize: vertical; background: var(--surface2); color: var(--text);
    font: inherit; font-size: 13px; border: 1px solid var(--border);
    border-radius: 8px; padding: 8px 10px; }
  .rvs-modal .rvs-row { display: flex; gap: 8px; justify-content: flex-end;
    margin-top: 12px; }
  .rvs-modal .rvs-msgline { color: var(--fail); font-size: 12px;
    min-height: 16px; margin-top: 6px; }
  `;

  const HTML = `
  <div class="rvs-scrim" id="rvs-scrim" hidden></div>
  <aside class="rvs-sheet" id="rvs-sheet" hidden role="dialog" aria-modal="true"
         aria-labelledby="rvs-title">
    <header class="rvs-head">
      <div class="rvs-head-main">
        <div class="rvs-num" id="rvs-num"></div>
        <h2 class="rvs-title" id="rvs-title"></h2>
        <div class="rvs-meta" id="rvs-meta"></div>
      </div>
      <button class="rvs-close" id="rvs-close" type="button"
              aria-label="Close review sheet">✕</button>
    </header>
    <div class="rvs-tags" id="rvs-tags"></div>
    <div class="rvs-body" id="rvs-body"></div>
    <footer class="rvs-foot" id="rvs-foot"></footer>
  </aside>
  <div class="rvs-modal-scrim" id="rvs-mscrim" hidden>
    <div class="rvs-modal" role="dialog" aria-modal="true">
      <h3 id="rvs-mtitle"></h3>
      <p class="rvs-sub" id="rvs-msub"></p>
      <textarea id="rvs-note" placeholder="Why — required. This note is the audit trail for changing the record."></textarea>
      <div class="rvs-msgline" id="rvs-mmsg"></div>
      <div class="rvs-row">
        <button class="rvs-btn" id="rvs-mcancel">Cancel</button>
        <button class="rvs-btn pass" id="rvs-mpass">Pass</button>
        <button class="rvs-btn fail" id="rvs-mfail">Fail</button>
        <button class="rvs-btn" id="rvs-mrevert"
                title="Remove the human verdict; the frozen machine grade stands again">Revert to AI</button>
      </div>
    </div>
  </div>`;

  let mounted = false;
  let state = null;   // {date, number, onReviewed, data}

  const $ = id => document.getElementById(id);

  function mount() {
    if (mounted) return;
    mounted = true;
    const style = document.createElement("style");
    style.textContent = CSS;
    document.head.appendChild(style);
    const host = document.createElement("div");
    host.innerHTML = HTML;
    document.body.appendChild(host);

    $("rvs-close").addEventListener("click", close);
    $("rvs-scrim").addEventListener("click", close);
    document.addEventListener("keydown", e => {
      if (e.key !== "Escape") return;
      if (!$("rvs-mscrim").hidden) $("rvs-mscrim").hidden = true;
      else if (!$("rvs-sheet").hidden) close();
    });
    $("rvs-mcancel").addEventListener("click", () => { $("rvs-mscrim").hidden = true; });
    $("rvs-mpass").addEventListener("click", () => sendReview("Pass"));
    $("rvs-mfail").addEventListener("click", () => sendReview("Fail"));
    // Lowercase on the wire: the server reads capital-R Revert as a sign-off
    // and demands the note reverts deliberately do not need.
    $("rvs-mrevert").addEventListener("click", () => sendReview("revert"));
  }

  function close() {
    $("rvs-sheet").hidden = true;
    $("rvs-scrim").hidden = true;
    $("rvs-mscrim").hidden = true;
    state = null;
  }

  function gradeHtml(g) {
    return `<span class="${GRADE_CLS[g] || "g-none"}">${esc(g || "—")}</span>`;
  }

  async function open(opts) {
    mount();
    state = { date: opts.date, number: opts.number,
              onReviewed: opts.onReviewed || null, data: null };
    $("rvs-num").textContent = `#${opts.number}`;
    $("rvs-title").textContent = "Loading frozen record…";
    $("rvs-meta").textContent = "";
    $("rvs-tags").innerHTML = "";
    $("rvs-body").innerHTML = "";
    $("rvs-foot").innerHTML = "";
    $("rvs-sheet").hidden = false;
    $("rvs-scrim").hidden = false;
    $("rvs-close").focus({ preventScroll: true });

    let d;
    try {
      d = await QC.api(`/api/reportcard/ticket/${encodeURIComponent(opts.date)}`
                       + `/${encodeURIComponent(opts.number)}`);
    } catch (e) {
      $("rvs-title").textContent = "Could not load the record";
      $("rvs-body").innerHTML = `<div class="rvs-empty">${esc(e.message)}</div>`;
      return;
    }
    if (!state || state.number !== opts.number) return;
    state.data = d;
    render(d);
  }

  function dashLink(d) {
    return `/?date=${encodeURIComponent(d.date)}&ticket=${encodeURIComponent(state.number)}`;
  }

  function render(d) {
    const t = d.ticket;
    if (!t) {
      // No frozen record: say exactly why, and hand over the live view. This
      // is the Report Card philosophy surfacing in the Open tab — a review
      // judges the record, and today's tickets get their record at the next
      // scheduled run.
      $("rvs-title").textContent = "No frozen record yet";
      $("rvs-meta").innerHTML =
        `<a href="${esc(dashLink(d))}">open in live dashboard →</a>`;
      $("rvs-body").innerHTML = `<div class="rvs-empty">${
        d.hole || d.snapshot === null
          ? esc(`${d.date} has no snapshot yet — the record is taken at the `
                + `scheduled morning run, so this ticket becomes reviewable `
                + `after that. Until then, work it from the live dashboard.`)
          : esc(`#${state.number} is not in ${d.date}'s frozen record — it was `
                + `fetched after the snapshot was taken.`)}</div>`;
      $("rvs-foot").innerHTML =
        `<span class="rvs-who">Reviews judge the frozen record only.</span>`;
      return;
    }

    const official = t.effective_result || "Pending";
    const badgeCls = { "Pass": "pass", "Fail": "fail",
                       "Needs Review": "review" }[official] || "pending";
    const pylonLink = /^https?:\/\//i.test(t.link || "") ? t.link : "";
    $("rvs-num").innerHTML = pylonLink
      ? `<a href="${esc(pylonLink)}" target="_blank" rel="noopener noreferrer"
           title="Open in Pylon">#${esc(String(t.number))}</a>`
      : `#${esc(String(t.number))}`;
    $("rvs-title").textContent = t.title || "(no title)";
    const frozenBy = d.snapshot.created_by === "scheduler"
      ? "scheduled run" : `backfill by ${d.snapshot.created_by}`;
    $("rvs-meta").innerHTML =
      `${esc(t.assignee_name || "Unassigned")} · ${esc(t.account_name || "—")} · `
      + `frozen ${esc(new Date(d.snapshot.created_at).toLocaleString())} `
      + `(${esc(frozenBy)}) · <a href="${esc(dashLink(d))}">live dashboard →</a>`;
    $("rvs-tags").innerHTML = `
      <span class="rvs-badge ${badgeCls}">${esc(official)}</span>
      <span class="rvs-tag">${esc((t.state || "—").replace(/_/g, " "))}</span>
      ${t.delta && t.delta !== "unchanged"
        ? `<span class="rvs-chip ${esc(t.delta)}">${esc(DELTA_LABEL[t.delta] || t.delta)}</span>` : ""}
      ${t.review_decision
        ? `<span class="rvs-tag">signed off · ${esc(t.reviewer_name || "")}</span>` : ""}`;

    const checks = MATRIX.map(([key]) => {
      const st = cellState(key, t[key]);
      const label = (CHECKS[key] || {}).label || key.toUpperCase();
      const shown = t[key] || "—";
      return `<div class="rvs-check">
        <span class="rvs-cell c-${st}">${CELL_GLYPH[st]}</span>
        <div class="rvs-check-head">
          <span>${esc(key.toUpperCase())} · ${esc(label)}</span>
          <span class="${GRADE_CLS[shown] || ""}">${esc(shown)}</span>
        </div>
      </div>`;
    }).join("");

    const cur = t.current_effective;
    const when = t.current_checked_at
      ? ` (as of ${esc(new Date(t.current_checked_at).toLocaleString())})` : "";
    const since = (t.delta && t.delta !== "unchanged")
      ? `Frozen ${gradeHtml(t.overall_result)} → currently ${gradeHtml(cur)}${when}.
         The frozen record does not change; this is what happened after it.`
      : `<span class="rvs-note-cell">No change — the live board still agrees with the snapshot.</span>`;

    $("rvs-body").innerHTML = `
      <section class="rvs-section">
        <div class="rvs-section-title">Checks — as frozen at the snapshot</div>
        ${checks}
      </section>
      <section class="rvs-section">
        <div class="rvs-section-title">Since the snapshot</div>
        <div style="font-size:12.5px">${since}</div>
      </section>
      ${t.ai_notes ? `
      <section class="rvs-section">
        <div class="rvs-section-title">Notes at the snapshot</div>
        <div class="rvs-note-cell">${esc(t.ai_notes)}</div>
      </section>` : ""}
      <section class="rvs-section">
        <div class="rvs-section-title">Conversation (live)</div>
        <div class="rvs-msgs" id="rvs-msgs">
          <div class="rvs-note-cell">Loading conversation…</div>
        </div>
      </section>`;

    const who = t.review_decision
      ? `Signed off ${t.review_decision} by ${t.reviewer_name || ""}`
        + (t.review_note ? ` — “${t.review_note}”` : "")
      : "No sign-off yet. Your verdict overrides the frozen grade everywhere.";
    $("rvs-foot").innerHTML = `
      <span class="rvs-who">${esc(who)}</span>
      <button class="rvs-btn" id="rvs-review-btn">${t.review_decision ? "Change verdict" : "Review"}</button>`;
    $("rvs-review-btn").addEventListener("click", () => openModal(t));

    loadConversation(t.ticket_id);
  }

  async function loadConversation(ticketId) {
    const box = $("rvs-msgs");
    try {
      const data = await QC.api(`/api/ticket/${encodeURIComponent(ticketId)}`);
      if (!state || !box.isConnected) return;
      const msgs = data.messages || [];
      if (!msgs.length) {
        box.innerHTML = `<div class="rvs-note-cell">No messages.</div>`;
        return;
      }
      box.innerHTML = msgs.map(m => {
        const div = document.createElement("div");
        div.innerHTML = m.message_html || "";
        const text = (div.textContent || "").trim();
        const ts = m.timestamp ? new Date(m.timestamp).toLocaleString() : "";
        const cls = `rvs-bubble ${m.is_customer ? "customer" : ""}${m.is_private ? " private" : ""}`;
        const label = m.is_private ? `${m.author_name || ""} (private)`
                                   : (m.author_name || "Unknown");
        return `<div class="${cls}">
          <div class="rvs-author ${m.is_customer ? "customer" : ""}">${esc(label)}</div>
          <div>${esc(text)}</div>
          <div class="rvs-time">${esc(ts)}</div>
        </div>`;
      }).join("");
    } catch (e) {
      if (box.isConnected) box.innerHTML = `<div class="rvs-note-cell">${esc(e.message)}</div>`;
    }
  }

  function openModal(t) {
    $("rvs-mtitle").textContent = `Sign off #${t.number}`;
    $("rvs-msub").textContent =
      `Frozen grade: ${t.overall_result || "not graded"} · your verdict `
      + `overrides it in the Report Card AND every live view.`;
    $("rvs-note").value = "";
    $("rvs-mmsg").textContent = "";
    $("rvs-mrevert").style.display = t.review_decision ? "" : "none";
    $("rvs-mscrim").hidden = false;
    $("rvs-note").focus();
  }

  async function sendReview(decision) {
    const t = state && state.data && state.data.ticket;
    if (!t) { $("rvs-mscrim").hidden = true; return; }
    const note = $("rvs-note").value.trim();
    if (decision !== "revert" && !note) {
      $("rvs-mmsg").textContent = "A note is required — it is the audit trail.";
      return;
    }
    try {
      await QC.api(`/api/ticket/${encodeURIComponent(t.ticket_id)}/review`, {
        method: "POST", body: JSON.stringify({ decision, note }),
      });
      $("rvs-mscrim").hidden = true;
      const cb = state.onReviewed;
      // Repaint the sheet with the fresh record before telling the host.
      await open({ date: state.date, number: state.number, onReviewed: cb });
      if (cb) cb();
    } catch (e) {
      $("rvs-mmsg").textContent = e.message;
    }
  }

  window.QCReviewSheet = {
    open, close,
    CHECKS, MATRIX, CELL_GLYPH, cellState,
  };
})();
