/* The frozen-record review sheet, as ONE shared component.
 *
 * Used by the Report Card (its native home) and embedded in the Open Tickets
 * tab, so reviewing an open ticket is zero redirects. One implementation on
 * purpose: two hand-rolled copies of "what a frozen record looks like and how
 * a review is recorded" is exactly the two-homes drift this codebase keeps
 * paying for.
 *
 * Contract: QCReviewSheet.open({date, number, mode, ticketId, pylonLink,
 * onReviewed}). Two modes, one rendering vocabulary:
 *
 *   "review"   (Report Card) — the FROZEN record from
 *              /api/reportcard/ticket/{date}/{number}, and the ONE place
 *              verdicts are recorded (/api/ticket/{id}/review; note
 *              mandatory, revert lowercase).
 *   "navigate" (Open Tickets) — the LIVE record from /api/ticket/{id}:
 *              current checks with their evidence, current grade with any
 *              review overlay, conversation. Read-only, with Dashboard /
 *              Report Card / Pylon buttons; the Report Card button is
 *              disabled until the day's frozen record exists (checked with a
 *              parallel frozen lookup).
 *
 * `onReviewed` fires after any successful verdict change (review mode). Also exported: CHECKS, MATRIX, cellState,
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
      <div id="rvs-adjust" style="margin-bottom:10px">
        <div style="display:flex;align-items:baseline;gap:8px">
          <span style="font-size:12.5px;font-weight:600">Adjust verdicts — required</span>
          <button class="rvs-btn" id="rvs-retain-all" type="button"
                  style="padding:2px 8px;font-size:11px;margin-left:auto"
                  title="Mark every unchosen check as keeping its scored verdict">Retain all remaining</button>
        </div>
        <div style="font-size:11.5px;color:var(--muted);margin:6px 0">
          Choose for every check: retain its scored verdict, or set the one the
          scorer should have given. A ticket can still pass overall with an
          honest fail retained.</div>
        <div id="rvs-adjust-list" style="max-height:220px;overflow-y:auto"></div>
      </div>
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
    $("rvs-retain-all").addEventListener("click", () => {
      const t = state && state.data && state.data.ticket;
      if (!t) return;
      for (const k of modalKeys) {
        if (!(k in modalChoices)) modalChoices[k] = RETAIN;
      }
      renderAdjustList(t);
    });
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
              mode: opts.mode || "review",
              ticketId: opts.ticketId || null,
              pylonLink: opts.pylonLink || null,
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

    try {
      if (state.mode === "navigate") {
        // Live record + (in parallel) whether a frozen record exists, which
        // only gates the Report Card button.
        const [live, frozen] = await Promise.all([
          QC.api(`/api/ticket/${encodeURIComponent(opts.ticketId)}`),
          QC.api(`/api/reportcard/ticket/${encodeURIComponent(opts.date)}`
                 + `/${encodeURIComponent(opts.number)}`).catch(() => null),
        ]);
        if (!state || state.number !== opts.number) return;
        state.data = { live, frozenExists: !!(frozen && frozen.ticket) };
        renderLive(live, state.data.frozenExists);
      } else {
        const d = await QC.api(
          `/api/reportcard/ticket/${encodeURIComponent(opts.date)}`
          + `/${encodeURIComponent(opts.number)}`);
        if (!state || state.number !== opts.number) return;
        state.data = d;
        render(d);
      }
    } catch (e) {
      if (!state || state.number !== opts.number) return;
      $("rvs-title").textContent = "Could not load the record";
      $("rvs-body").innerHTML = `<div class="rvs-empty">${esc(e.message)}</div>`;
    }
  }

  // ── navigate mode: the ticket as it is NOW, same visual vocabulary ─────────
  function renderLive(payload, frozenExists) {
    const t = payload.ticket || {};
    const why = payload.evidence || {};
    const review = t.review || null;
    const effective = (review && ["Pass", "Fail"].includes(review.decision))
      ? review.decision : (t.overall_result || "Pending");
    const badgeCls = { "Pass": "pass", "Fail": "fail",
                       "Needs Review": "review" }[effective] || "pending";
    const pylonLink = /^https?:\/\//i.test(t.link || "") ? t.link : "";

    $("rvs-num").innerHTML = pylonLink
      ? `<a href="${esc(pylonLink)}" target="_blank" rel="noopener noreferrer"
           title="Open in Pylon">#${esc(String(t.number))}</a>`
      : `#${esc(String(t.number || state.number))}`;
    $("rvs-title").textContent = t.title || "(no title)";
    $("rvs-meta").textContent =
      `${t.assignee_name || "Unassigned"} · ${t.account_name || "—"} · live record`;
    $("rvs-tags").innerHTML = `
      <span class="rvs-badge ${badgeCls}">${esc(effective)}</span>
      <span class="rvs-tag">${esc((t.state || "—").replace(/_/g, " "))}</span>
      ${review
        ? `<span class="rvs-tag">signed off · ${esc(review.reviewer_name || review.reviewer_email || "")}</span>` : ""}`;

    const overrides = (review && review.check_overrides) || {};
    const checks = MATRIX.map(([key]) => {
      const eff = overrides[key] || t[key];
      const st = cellState(key, eff);
      const label = (CHECKS[key] || {}).label || key.toUpperCase();
      const shown = eff || "—";
      const adjusted = key in overrides
        ? `<span class="rvs-tag" title="Scored ${esc(t[key] || "—")} — adjusted by ${esc((review && review.reviewer_name) || "the reviewer")}">adjusted</span>`
        : "";
      const reason = why[key]
        ? `<div class="rvs-note-cell" style="margin-top:2px">${esc(why[key])}</div>` : "";
      return `<div class="rvs-check">
        <span class="rvs-cell c-${st}">${CELL_GLYPH[st]}</span>
        <div style="flex:1">
          <div class="rvs-check-head">
            <span>${esc(key.toUpperCase())} · ${esc(label)} ${adjusted}</span>
            <span class="${GRADE_CLS[shown] || ""}">${esc(shown)}</span>
          </div>
          ${reason}
        </div>
      </div>`;
    }).join("");

    $("rvs-body").innerHTML = `
      <section class="rvs-section">
        <div class="rvs-section-title">Checks — current</div>
        ${checks}
      </section>
      ${t.ai_notes ? `
      <section class="rvs-section">
        <div class="rvs-section-title">Notes</div>
        <div class="rvs-note-cell">${esc(t.ai_notes)}</div>
      </section>` : ""}
      <section class="rvs-section">
        <div class="rvs-section-title">Conversation</div>
        <div class="rvs-msgs" id="rvs-msgs"></div>
      </section>`;

    renderMessages(payload.messages || []);
    navFooterLive(frozenExists, pylonLink);
  }

  function renderMessages(msgs) {
    const box = $("rvs-msgs");
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
  }

  function navFooterLive(frozenExists, pylonLink) {
    const rcHref = `/reportcard?date=${encodeURIComponent(state.date)}`
      + `&ticket=${encodeURIComponent(state.number)}`;
    const dash = `/?date=${encodeURIComponent(state.date)}`
      + `&ticket=${encodeURIComponent(state.ticketId)}`;
    $("rvs-foot").innerHTML = `
      <span class="rvs-who"></span>
      <a class="rvs-btn" href="${esc(dash)}">Open in Dashboard</a>
      ${frozenExists
        ? `<a class="rvs-btn" href="${esc(rcHref)}">Open in Report Card</a>`
        : `<button class="rvs-btn" disabled
             title="No frozen record for ${esc(state.date)} yet — the Report Card has nothing to show until the day's snapshot exists">Open in Report Card</button>`}
      ${pylonLink || state.pylonLink
        ? `<a class="rvs-btn" href="${esc(pylonLink || state.pylonLink)}" target="_blank"
             rel="noopener noreferrer">Open in Pylon</a>` : ""}`;
  }

  function dashLink(d) {
    // The dashboard's deep link matches by ticket UUID, not number.
    const tid = (state.data && state.data.ticket && state.data.ticket.ticket_id)
      || state.ticketId;
    return tid
      ? `/?date=${encodeURIComponent(d.date)}&ticket=${encodeURIComponent(tid)}`
      : `/?date=${encodeURIComponent(d.date)}`;
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
      const noSnap = d.hole || d.snapshot === null;
      $("rvs-body").innerHTML = `<div class="rvs-empty">${
        noSnap
          ? esc(`${d.date} has no snapshot yet — the record is taken at the `
                + `scheduled morning run, so this ticket becomes reviewable `
                + `after that. Until then, work it from the live dashboard.`)
          : esc(`#${state.number} is not in ${d.date}'s frozen record — it was `
                + `fetched after the snapshot was taken.`)}</div>`;
      // Admins can close the gap on a PAST day right here (a hole, or a
      // pre-scheduler local copy). Today is deliberately not offered: the
      // snapshot slot is insert-once, and a partial noon capture would block
      // tonight's scheduled notary from writing the real record.
      const today = new Date().toISOString().slice(0, 10);
      const isAdmin = !!(window.QC && QC.me && QC.me.role === "admin");
      if (noSnap && isAdmin && d.date < today) {
        $("rvs-foot").innerHTML = `
          <span class="rvs-who">Reviews judge the frozen record only.</span>
          <button class="rvs-btn" id="rvs-capture">Capture ${esc(d.date)} snapshot now (admin backfill)</button>`;
        $("rvs-capture").addEventListener("click", async () => {
          const btn = $("rvs-capture");
          btn.disabled = true;
          try {
            await QC.api(`/api/reportcard/capture/${encodeURIComponent(d.date)}`,
                         { method: "POST" });
            await open({ date: state.date, number: state.number,
                         onReviewed: state.onReviewed });
          } catch (e) {
            btn.disabled = false;
            btn.textContent = e.message;
          }
        });
      } else {
        $("rvs-foot").innerHTML =
          `<span class="rvs-who">${noSnap && d.date >= today
            ? "Today's record is taken at tomorrow's scheduled run."
            : "Reviews judge the frozen record only."}</span>`;
      }
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

    const overrides = t.check_overrides || {};
    const checks = MATRIX.map(([key]) => {
      const scored = t[key];
      const eff = overrides[key] || scored;
      const st = cellState(key, eff);
      const label = (CHECKS[key] || {}).label || key.toUpperCase();
      const shown = eff || "—";
      const adjusted = key in overrides
        ? `<span class="rvs-tag" title="Scored ${esc(scored || "—")} — adjusted by ${esc(t.reviewer_name || "the reviewer")}">adjusted · scored ${esc(scored || "—")}</span>`
        : "";
      return `<div class="rvs-check">
        <span class="rvs-cell c-${st}">${CELL_GLYPH[st]}</span>
        <div class="rvs-check-head">
          <span>${esc(key.toUpperCase())} · ${esc(label)} ${adjusted}</span>
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

  // Every verdict a check can be adjudicated TO — mirrors the server's vocab
  // (R-checks Pass/Fail; A-checks their model enums, minus N/A).
  const ADJUST_VOCAB = {
    r1: ["Pass", "Fail"], r2: ["Pass", "Fail"], r3: ["Pass", "Fail"],
    r4: ["Pass", "Fail"], r5: ["Pass", "Fail"], r7: ["Pass", "Fail"],
    r8: ["Pass", "Fail"], r10: ["Pass", "Fail"], r11: ["Pass", "Fail"],
    a1: ["Pass", "Fail", "Needs Review"],
    a2: ["Positive", "Neutral", "Concerned", "Frustrated", "Urgent"],
    a3: ["Good", "Needs Improvement", "Poor"],
    a4: ["Pass", "Fail", "Needs Review"],
    a5: ["Pass", "Fail", "Needs Review"],
  };
  const RETAIN = "__retain__";

  // key -> RETAIN or a vocabulary value. EVERY listed check must be chosen
  // before a sign-off can be sent — adjudication is deliberate, not implied.
  let modalChoices = {};
  let modalKeys = [];

  function openModal(t) {
    $("rvs-mtitle").textContent = `Sign off #${t.number}`;
    $("rvs-msub").textContent =
      `Frozen grade: ${t.overall_result || "not graded"} · your verdict `
      + `overrides it in the Report Card AND every live view.`;
    $("rvs-note").value = "";
    $("rvs-mmsg").textContent = "";
    $("rvs-mrevert").style.display = t.review_decision ? "" : "none";
    // Rows: every check whose scored value is a real verdict (N/A and
    // not-evaluated have nothing to adjudicate).
    modalKeys = MATRIX.map(([k]) => k).filter(k =>
      ADJUST_VOCAB[k] && (ADJUST_VOCAB[k].includes(t[k])));
    modalChoices = {};
    const existing = t.check_overrides || {};
    for (const k of modalKeys) {
      if (k in existing) modalChoices[k] = existing[k];   // prior adjudication
    }
    renderAdjustList(t);
    $("rvs-mscrim").hidden = false;
    $("rvs-note").focus();
  }

  function renderAdjustList(t) {
    const list = $("rvs-adjust-list");
    list.innerHTML = modalKeys.map(key => {
      const scored = t[key];
      const label = (CHECKS[key] || {}).label || key.toUpperCase();
      const choice = modalChoices[key];
      const opts = [
        { v: RETAIN, text: `Retain ${scored}` },
        ...ADJUST_VOCAB[key].filter(v => v !== scored)
          .map(v => ({ v, text: v })),
      ].map(o => {
        const on = choice === o.v || (o.v !== RETAIN && choice === o.v);
        const sel = choice === o.v;
        return `<button class="rvs-btn" data-adjust="${esc(key)}"
                  data-value="${esc(o.v)}" type="button"
                  style="padding:2px 8px;font-size:11px;${sel
                    ? "border-color:var(--accent);color:var(--accent2)" : ""}">
                  ${sel ? "● " : ""}${esc(o.text)}</button>`;
      }).join(" ");
      const undecided = !(key in modalChoices);
      return `<div class="rvs-check" style="align-items:center">
        <div class="rvs-check-head" style="flex-wrap:wrap;gap:6px">
          <span>${esc(key.toUpperCase())} · ${esc(label)}
            <span class="${GRADE_CLS[scored] || ""}">${esc(scored)}</span>
            ${undecided ? `<span class="rvs-tag" style="color:var(--review)">choose</span>` : ""}</span>
          <span>${opts}</span>
        </div>
      </div>`;
    }).join("") || `<div class="rvs-note-cell">No graded checks on this ticket.</div>`;

    list.querySelectorAll("[data-adjust]").forEach(btn =>
      btn.addEventListener("click", (e) => {
        e.preventDefault();
        modalChoices[btn.dataset.adjust] = btn.dataset.value;
        renderAdjustList(t);
      }));
  }

  async function sendReview(decision) {
    const t = state && state.data && state.data.ticket;
    if (!t) { $("rvs-mscrim").hidden = true; return; }
    const note = $("rvs-note").value.trim();
    if (decision !== "revert" && !note) {
      $("rvs-mmsg").textContent = "A note is required — it is the audit trail.";
      return;
    }
    if (decision !== "revert") {
      const undecided = modalKeys.filter(k => !(k in modalChoices));
      if (undecided.length) {
        $("rvs-mmsg").textContent =
          "Choose retain-or-adjust for every check — missing: "
          + undecided.map(k => k.toUpperCase()).join(", ")
          + '. "Retain all remaining" fills the rest.';
        return;
      }
    }
    try {
      const overrides = {};
      for (const [k, v] of Object.entries(modalChoices)) {
        if (v !== RETAIN && v !== t[k]) overrides[k] = v;
      }
      await QC.api(`/api/ticket/${encodeURIComponent(t.ticket_id)}/review`, {
        method: "POST",
        body: JSON.stringify({ decision, note, check_overrides: overrides }),
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
