import asyncio
from collections import Counter
from dotenv import load_dotenv
load_dotenv("/Users/darshans/Downloads/qc/.env")
import httpx
import pylon

async def main():
    filt = {"field": "slack_channel_id", "operator": "equals",
            "value": "C03KBJNNN9X"}
    found = {}
    srcs = Counter()
    async with httpx.AsyncClient(timeout=30, headers=pylon._headers()) as client:
        cursor, pages = None, 0
        while pages < 40:
            p = await pylon._search_page(client, filt, cursor, "chan")
            if not p.ok: break
            pages += 1
            for i in p.issues:
                srcs[i.get("source") or "?"] += 1
                if i.get("number") in (76226, 76247, 76250):
                    found[i["number"]] = {
                        "source": i.get("source"),
                        "slack": i.get("slack"),
                    }
            if not p.has_next: break
            cursor = p.cursor
    print("pages:", pages, "| sources in this channel:", dict(srcs))
    for n, d in sorted(found.items()):
        print(f"#{n} IS in channel C03KBJNNN9X — search payload source={d['source']!r} slack={d['slack']}")
    for n in (76226, 76247, 76250):
        if n not in found:
            print(f"#{n} NOT found in this channel's search results")
asyncio.run(main())
