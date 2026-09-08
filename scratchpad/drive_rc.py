import subprocess, sys
from playwright.sync_api import sync_playwright

cookie = subprocess.run([sys.executable, "scratchpad/mint.py"],
                        capture_output=True, text=True,
                        env={"PYTHONPATH": "/Users/darshans/Downloads/qc", "PATH": "/usr/bin:/bin"}
                        ).stdout.strip()
with sync_playwright() as p:
    b = p.chromium.launch()
    ctx = b.new_context(viewport={"width": 1500, "height": 950})
    ctx.add_cookies([{"name": "qc_session", "value": cookie,
                      "domain": "127.0.0.1", "path": "/"}])
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto("http://127.0.0.1:8000/reportcard?date=2026-08-25")
    pg.wait_for_timeout(2500)
    pg.screenshot(path="scratchpad/rc1-dashboard.png")
    # open the first failing card's sheet
    pg.locator(".ticket-card", has_text="Fail").first.click()
    pg.wait_for_timeout(1500)
    pg.screenshot(path="scratchpad/rc2-sheet.png")
    print("tabs row height:", pg.locator("#rc-tabs").bounding_box())
    print("cards:", pg.locator("#rc-cards .ticket-card").count(),
          "| sheet open:", pg.locator("#fs-sheet").is_visible(),
          "| modal hidden:", not pg.locator("#rv-scrim").is_visible())
    print("js errors:", errors or "none")
    b.close()
