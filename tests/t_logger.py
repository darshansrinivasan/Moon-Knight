"""Every module that logs must own a logger.

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

SKIP = {"conftest"}
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
if checked == 0:
    print("FAILURE: the scan found nothing — the regex or the glob is wrong")
    raise SystemExit(1)
if fails:
    print(f"FAILURES ({len(fails)}): {fails}")
    raise SystemExit(1)
print("ALL LOGGER ASSERTIONS PASSED")
