import asyncio, json
from dotenv import load_dotenv
load_dotenv("/Users/darshans/Downloads/qc/.env")
import httpx
import pylon, sqlite3

async def main():
    conn = sqlite3.connect("qc.db"); conn.row_factory = sqlite3.Row
    ids = {r["number"]: r["id"] for r in conn.execute(
        "SELECT number, id FROM tickets WHERE number IN (76226, 76247, 76250)")}
    async with httpx.AsyncClient(timeout=30, headers=pylon._headers()) as client:
        for num, tid in sorted(ids.items()):
            r = await client.get(f"{pylon.BASE_URL}/issues/{tid}",
                                 headers=pylon._headers())
            d = r.json().get("data") or {}
            print(f"#{num}: Pylon source={d.get('source')!r} "
                  f"slack={json.dumps(d.get('slack'))} "
                  f"channel={ (d.get('slack') or {}).get('channel_id') }")
asyncio.run(main())
