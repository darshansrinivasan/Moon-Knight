import asyncio
from collections import Counter
from datetime import date
from dotenv import load_dotenv
load_dotenv("/Users/darshans/Downloads/qc/.env")
import httpx
import pylon

async def main():
    lo, hi = pylon._ist_range(date(2026, 9, 1), date(2026, 9, 7))
    filt = {"field": "resolved_at", "operator": "time_range", "values": [lo, hi]}
    states, sources = Counter(), Counter()
    async with httpx.AsyncClient(timeout=30, headers=pylon._headers()) as client:
        page = await pylon._search_page(client, filt, None, "probe")
        issues = list(page.issues)
        cursor, has_next = page.cursor, page.has_next
        while has_next:
            nxt = await pylon._search_page(client, filt, cursor, "probe")
            if not nxt.ok: break
            issues.extend(nxt.issues)
            cursor, has_next = nxt.cursor, nxt.has_next
    for i in issues:
        states[i.get("state") or "?"] += 1
        sources[i.get("source") or "?"] += 1
    print("total:", len(issues))
    print("by state:", dict(states.most_common()))
    print("by source:", dict(sources.most_common()))

asyncio.run(main())
