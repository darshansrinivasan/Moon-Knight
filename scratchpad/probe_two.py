import asyncio, json
from dotenv import load_dotenv
load_dotenv("/Users/darshans/Downloads/qc/.env")
import httpx, pylon

async def main():
    async with httpx.AsyncClient(timeout=30, headers=pylon._headers()) as client:
        for cid in ("C020Z7RV0SU", "C01HRKT45NG"):
            r = await client.post(f"{pylon.BASE_URL}/issues/search",
                                  headers=pylon._headers(),
                                  json={"filter": {"field": "slack_channel_id",
                                                   "operator": "equals",
                                                   "value": cid}, "limit": 100})
            print(cid, "HTTP", r.status_code, "body:", json.dumps(r.json())[:200])
asyncio.run(main())
