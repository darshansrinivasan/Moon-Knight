"""Static checks on the Nocturne migration.

Not a substitute for looking at it — it cannot tell you the page is handsome.
It can tell you no page still pins the old palette, no `var()` points at a
token nobody defines, and the system's hard rules are not broken.
"""
import pathlib
import re

# Resolved from this file, not the working directory or anyone's home folder —
# a hardcoded absolute path made this suite fail on every machine but one.
STATIC = pathlib.Path(__file__).resolve().parent.parent / "static"
SHELL = (STATIC / "shell.css").read_text()
PAGES = sorted(STATIC.glob("*.html"))

fails = []


def check(name, ok, detail=""):
    print(f"  {'OK ' if ok else 'FAIL'} {name}{' — ' + detail if detail else ''}")
    if not ok:
        fails.append(name)


print("=== Nocturne tokens present in shell.css ===")
for tok in ["--color-bg: #161826", "--color-text: #e9e9ed",
            "--color-accent: #9184d9", "--color-accent-300",
            "--space-3: 8.4px", "--radius-md: 8px", "--shadow-lg",
            "--font-heading"]:
    check(f"defines {tok.split(':')[0]}", tok in SHELL)

print()
print("=== the old palette is gone ===")
OLD = ["#0f172a", "#1e293b", "#6366f1", "#22c55e", "#ef4444", "#f59e0b",
       "#334155", "#f1f5f9", "#94a3b8"]
for page in PAGES:
    text = page.read_text()
    hits = [h for h in OLD if h in text]
    check(f"{page.name} free of old hexes", not hits, ", ".join(hits))
shell_hits = [h for h in OLD if h in SHELL]
check("shell.css free of old hexes", not shell_hits, ", ".join(shell_hits))

print()
print("=== every var() resolves to a defined token ===")
defined = set(re.findall(r"(--[a-z0-9-]+)\s*:", SHELL))
for page in PAGES:
    text = page.read_text()
    defined_here = set(re.findall(r"(--[a-z0-9-]+)\s*:", text))
    used = set(re.findall(r"var\((--[a-z0-9-]+)", text))
    missing = sorted(used - defined - defined_here)
    check(f"{page.name}: no orphan vars", not missing, ", ".join(missing[:6]))

used_in_shell = set(re.findall(r"var\((--[a-z0-9-]+)", SHELL))
check("shell.css: no orphan vars", not (used_in_shell - defined),
      ", ".join(sorted(used_in_shell - defined)[:6]))

print()
print("=== Nocturne's hard rules ===")
# Pure white/black are forbidden outside shadows. white-space is not a colour.
for page in PAGES:
    text = page.read_text()
    bad = re.findall(r"(?:color|background)\s*:\s*(?:#fff\b|#ffffff|white\b|#000\b|#000000)",
                     text, re.I)
    # login.html's Google button is white by Google's own branding requirement.
    allowed = 1 if page.name == "login.html" else 0
    check(f"{page.name}: no pure white/black fills", len(bad) <= allowed,
          f"{len(bad)} found")

check("buttons are outlined, not filled",
      "button {" in SHELL and "background: transparent;" in
      SHELL.split("button {")[1].split("}")[0])
check("focus-visible carries the accent",
      "outline: 2px solid var(--color-accent)" in SHELL)
check("Inter linked on every page",
      all("family=Inter" in p.read_text() for p in PAGES))

print()
print("=== semantic colours stay separate from the accent ===")
root = SHELL.split(":root {")[1].split("\n}")[0]
for name in ["--pass", "--fail", "--review"]:
    line = next((l for l in root.splitlines() if l.strip().startswith(name + ":")), "")
    check(f"{name} is not the accent",
          "--color-accent" not in line and line.strip() != "", line.strip())

print()
print("=== the three verdict cells share one holding ===")
# Pass was a 22% tint with a coloured glyph while Fail and Attention were solid
# fills with a near-black glyph, so a pass read as a weaker class of statement
# than the other two. All three are verdicts and are now drawn the same way;
# only the states that carry no verdict (N/A, not evaluated) stay neutral.
INDEX = (STATIC / "index.html").read_text()
for cls, token in (("c-pass", "--pass"), ("c-fail", "--fail"),
                   ("c-warn", "--review")):
    rule = next((l for l in INDEX.splitlines()
                 if l.strip().startswith(f".{cls} {{")), "")
    check(f".{cls} is a solid fill of its own hue",
          f"background: var({token})" in rule, rule.strip())
    check(f".{cls} sets a glyph colour against that fill",
          "color: #" in rule, rule.strip())
    check(f".{cls} is not a tint",
          "color-mix" not in rule, rule.strip())

for cls in ("c-na", "c-none"):
    rule = next((l for l in INDEX.splitlines()
                 if l.strip().startswith(f".{cls} {{")), "")
    check(f".{cls} stays neutral, not a verdict hue",
          all(tok not in INDEX.split(f".{cls} {{")[1].split("}")[0]
              for tok in ("var(--pass)", "var(--fail)", "var(--review)")),
          "no verdict hue")

print()
print("=== no old-palette rgba() survivals ===")
# The hex sweep above cannot see these: the same colours were also written as
# rgba() triples, which is how the sidebar stayed indigo after the migration.
RGBA = re.compile(r"rgba\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})")
for page in [*PAGES, STATIC / "shell.css"]:
    bad = [
        m.group(0) for m in RGBA.finditer(page.read_text())
        # rgba(0,0,0,x) is ambient shade, which the system allows for shadows.
        if m.groups() != ("0", "0", "0")
    ]
    check(f"{page.name}: colours come from tokens", not bad, ", ".join(bad[:3]))

print()
print("=== Rules page: prompt editors and learned suggestions ===")
# These pin the Rules page against the two contracts it reads: prompts.py's
# section list (which it must NOT hardcode) and suggestions.build's payload.
# There is no browser here, so these are structural — the behavioural pass runs
# in jsdom, which is not a dependency of this suite.
import prompts as _prompts

RULES = (STATIC / "rules.html").read_text()

# The section list lives in prompts.SECTION_KEYS. The page builds its editors
# from /api/rules, so a hardcoded key here would drift the day a section is
# added or renamed.
hardcoded = [k for k in _prompts.SECTION_KEYS if k in RULES]
check("rules.html does not hardcode section keys", not hardcoded,
      ", ".join(hardcoded))
check("rules.html builds editors from prompt_sections",
      "prompt_sections" in RULES and "ps-" in RULES)
check("rules.html renders the read-only fixed blocks", "prompt_fixed" in RULES)
check("rules.html shows the per-section limit", "prompt_limit" in RULES)
check("each section offers a reset to default", "data-reset" in RULES)
check("a diverged section is marked", "data-edited" in RULES)

# rules.save persists only the keys the PUT carries and _load fills the rest
# from defaults, so a save that omitted a section would silently reset it.
check("every save carries every prompt section",
      "Object.assign(rules, draftPrompt())" in RULES)
check("the dry-run tests the whole rubric, not just guidance",
      "{...draftPrompt(), limit: DRY_LIMIT}" in RULES)
check("the dry-run sends no key the server would ignore",
      "{guidance:" not in RULES)

# The assembled prompt is now mostly editable, so the old label was a lie.
check("the assembled-prompt view is not called the fixed part",
      "the fixed part of every run" not in RULES)
for word in ["stale", "regrade", "re-billed"]:
    check(f"the prompt editors warn about {word}", word in RULES)

# A stored Pylon link is outside text: escaping leaves the scheme intact, so
# `javascript:` survives esc() and stays clickable.
check("ticket links are scheme-guarded", r"/^https?:\/\//i" in RULES)
check("an unlinkable ticket is plain text, not an anchor",
      'class="num"' in RULES)
check("the noise gate is explained, not silent",
      "gated_out" in RULES and "five overrides" in RULES)
check("the window selector offers 7 / 30 / 90",
      all('data-days="%d"' % d in RULES for d in (7, 30, 90)))
check("members cannot spend money or requery",
      '$("dry-run").disabled = true' in RULES
      and '$("sug-days").querySelectorAll("button").forEach(b => b.disabled = true)' in RULES)

print()
print("=== excluded statuses have exactly one editor ===")
# Two editors for one setting means two chances to disagree, and the loser
# silently wins whichever saved last. Admin owns it; Rules shows it.
ADMIN = (STATIC / "admin.html").read_text()
RULES = (STATIC / "rules.html").read_text()

check("Admin has the statuses section", 'id="sec-statuses"' in ADMIN)
check("Admin has a nav entry for it", 'data-section="statuses"' in ADMIN)
check("Admin saves it", 'id="status-save"' in ADMIN)
check("Admin PUTs only that key",
      'JSON.stringify({rules: {excluded_states: chosen}})' in ADMIN)
# QC.api throws on 422 without surfacing `errors`, so this save must use raw
# fetch or a rejected status list reports "Unprocessable Entity" and no reason.
check("Admin reads the 422 body itself", "r.status === 422" in ADMIN)

# The list must come from the data, not a literal, or a status Pylon adds later
# is invisible until someone edits the page.
check("Admin builds the list from fetched tickets",
      '"/api/ticket-states"' in ADMIN)
for literal in ("waiting_on_customer", "waiting_on_engg", "investigating"):
    check(f"Admin does not hardcode '{literal}'", literal not in ADMIN)

# Rules may read it, but must not send it.
check("Rules still shows the scope", "renderStates" in RULES)
check("Rules does not write it",
      "rules.excluded_states =" not in RULES)
check("Rules points at the owner", "Admin → Ticket statuses" in RULES)
check("Rules renders it as chips, not controls",
      'data-state=' not in RULES)

print()
print("=== the read-only sweep covers buttons, not just fields ===")
# It swept input/select/textarea only, so every action button in every section
# stayed live for a member. The endpoints are admin-gated, so pressing one gave
# a 403 rather than a change — but offering a control that cannot work is a
# defect of its own.
check("buttons are disabled too", "#content button" in ADMIN)
check("the section nav is left alone",
      '#admin-nav' in ADMIN and 'id="admin-nav"' in ADMIN)

print()
print("=== the removed chrome toggles left nothing behind ===")
for gone in ("rail-toggle", "cal-toggle", "CHROME_KEY", "saveChrome",
             "loadChrome", "setRailCollapsed", "setCalCollapsed",
             "rail.collapsed"):
    check(f"no trace of {gone}", gone not in INDEX)
# A viewer who collapsed a pane before the buttons went would otherwise be stuck
# with a hidden rail and nothing to click.
check("the stale preference is cleared, not just ignored",
      'removeItem("qc.chrome.v1")' in INDEX)
check("the rail and its calendar are still there",
      'id="rail"' in INDEX and 'id="cal-panel"' in INDEX)

print()
if fails:
    print(f"FAILURES ({len(fails)}): {fails}")
    raise SystemExit(1)
print("THEME CHECKS PASSED")
print()
print("NOT verified here: whether it actually looks right. Headless Chrome is")
print("blocked by macOS permissions in this environment, so the visual pass is")
print("yours — load the app and look at it.")
