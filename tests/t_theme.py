"""Static checks on the Nocturne migration.

Not a substitute for looking at it — it cannot tell you the page is handsome.
It can tell you no page still pins the old palette, no `var()` points at a
token nobody defines, and the system's hard rules are not broken.
"""
import pathlib
import re

STATIC = pathlib.Path("/Users/ashwinjayaram/Documents/Moon-Knight/static")
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
if fails:
    print(f"FAILURES ({len(fails)}): {fails}")
    raise SystemExit(1)
print("THEME CHECKS PASSED")
print()
print("NOT verified here: whether it actually looks right. Headless Chrome is")
print("blocked by macOS permissions in this environment, so the visual pass is")
print("yours — load the app and look at it.")
