import asyncio
from collections import Counter
from datetime import date
from dotenv import load_dotenv
load_dotenv("/Users/darshans/Downloads/qc/.env")
import httpx
import pylon

INTERNAL = {"C03KBJNNN9X","C03LH5CFV9C","C020Z7RV0SU","C04TELM72BY","C8HMVSUTH",
            "C09PV4749GE","C05CQ616P0X","C06165JH6PQ","CNTV3DSTH","C01HRKT45NG"}

async def main():
    async with httpx.AsyncClient(timeout=30, headers=pylon._headers()) as client:
        # 1. Their exact filter shape, one internal channel
        p = await pylon._search_page(client,
            {"field": "slack_channel_id", "operator": "equals",
             "value": "C03KBJNNN9X"}, None, "channel probe")
        print("channel-filter search ok:", p.ok, "| issues on page:", len(p.issues))
        if p.issues:
            i = p.issues[0]
            print("issue keys sample:", sorted(k for k in i.keys())[:18])
            print("slack_channel_id on payload:",
                  i.get("slack_channel_id") or i.get("slack", {}))
        # 2. Does the resolved-in-range payload carry the channel?
        lo, hi = pylon._ist_range(date(2026, 9, 1), date(2026, 9, 7))
        p2 = await pylon._search_page(client,
            {"field": "resolved_at", "operator": "time_range",
             "values": [lo, hi]}, None, "resolved probe")
        chan = Counter()
        for i in p2.issues:
            cid = (i.get("slack_channel_id")
                   or (i.get("slack") or {}).get("channel_id") or "")
            if i.get("source") == "slack":
                chan["internal" if cid in INTERNAL else
                     ("external" if cid else "NO-CHANNEL-FIELD")] += 1
        print("first resolved page, slack tickets by origin:", dict(chan))
asyncio.run(main())
