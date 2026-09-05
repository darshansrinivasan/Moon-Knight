"""Every name a module uses at module level must actually be bound.

Started as a logger check and grew, because the same bug kept recurring in a
different costume. app.py called `logger` in ten places without ever binding it.
Then leaderboard and drilldown referenced `qc_rules` from new module-level
helpers while only importing it inside other functions. Then qc_fingerprint did
the same. Each one is invisible until the exact line runs, and these lines sit
on error paths, edge cases and newly added helpers — the places least likely to
be exercised by hand.

So this checks the class rather than the instance: every global name a module's
source references must resolve after import. Static analysis, because the whole
point is that these lines almost never run.

app.py used `logger` in ten places without ever binding it. Nothing caught it
because each call sits on an error or edge path: a cleanup that found something,
an evidence build that threw, a restart that interrupted a run. The last of
those is in `lifespan`, so the first deploy to interrupt a run would have raised
NameError during startup — the app would fail to boot while reporting the very
problem it was fixing.

That is a class of bug, not one bug, so this checks the class: for every project
module, if the source calls `logger.<something>`, importing the module must
produce a `logger` attribute. Static analysis rather than execution, because the
whole point is that these lines almost never run.
"""
import importlib
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# One-shot maintenance scripts that do their work at import time rather than
# under `if __name__`. Importing them here would run a migration against the
# test database, which is not this suite's job to discover.
SKIP = {"conftest", "backfill_notes", "migrate_r5", "migrate_r8_r9"}
USES_LOGGER = re.compile(r"(?<![\w.])logger\s*\.\s*\w+\s*\(")

fails = []
checked = 0

for path in sorted(ROOT.glob("*.py")):
    name = path.stem
    if name in SKIP or name.startswith("_"):
        continue
    source = path.read_text(encoding="utf-8")
    uses = USES_LOGGER.findall(source)
    if not uses:
        continue

    checked += 1
    module = importlib.import_module(name)
    has = hasattr(module, "logger")
    print(f"  {'OK  ' if has else 'FAIL'} {name}: {len(uses)} logger call(s), "
          f"module-level logger {'bound' if has else 'MISSING'}")
    if not has:
        fails.append(name)

print()
print(f"checked {checked} module(s) that log")

print()
print("=== every module-level name a module uses is bound ===")
# `qc_rules` was referenced from module-level helpers in three files while only
# being imported inside unrelated functions. Deferred imports are correct here —
# `rules` imports `db` — but the deferral has to be in every function that uses
# the name, and that is exactly what gets forgotten.
import ast

COMMON = re.compile(r"^(qc_rules|scorer|prompts|evidence|drilldown|leaderboard|"
                    r"resync_overall|dryrun|rcheck_dryrun|slack|vault|db|auth|"
                    r"review|suggestions|pylon|gcp|rules|qc_runner)$")

for path in sorted(ROOT.glob("*.py")):
    name = path.stem
    if name in SKIP or name.startswith("_"):
        continue
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    module = importlib.import_module(name)

    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        # Names this function binds itself: its own imports, args and assignments.
        local = set()
        for sub_node in ast.walk(node):
            if isinstance(sub_node, (ast.Import, ast.ImportFrom)):
                for alias in sub_node.names:
                    local.add((alias.asname or alias.name).split(".")[0])
            elif isinstance(sub_node, ast.Name) and isinstance(sub_node.ctx, ast.Store):
                local.add(sub_node.id)
            elif isinstance(sub_node, ast.arg):
                local.add(sub_node.arg)

        used = {n.id for n in ast.walk(node)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        for used_name in sorted(used - local):
            if not COMMON.match(used_name):
                continue
            if not hasattr(module, used_name):
                fails.append(f"{name}.{node.name} uses {used_name}")
                print(f"  FAIL {name}.{node.name}() uses {used_name!r}, which is "
                      f"neither imported in it nor bound on the module")

if not any(" uses " in f for f in fails):
    print("  OK   no unbound module-level references")
if checked == 0:
    print("FAILURE: the scan found nothing — the regex or the glob is wrong")
    raise SystemExit(1)
if fails:
    print(f"FAILURES ({len(fails)}): {fails}")
    raise SystemExit(1)
print("ALL LOGGER ASSERTIONS PASSED")
