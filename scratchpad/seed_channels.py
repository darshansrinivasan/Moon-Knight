import asyncio
from dotenv import load_dotenv
load_dotenv("/Users/darshans/Downloads/qc/.env")
import db; db.init_db()
import channels

res = asyncio.run(channels.tag_all(full=True))
for c in res["channels"]:
    print(f"  {c['channel']}: {c['seen']} tickets, complete={c['complete']}")
print("index size:", len(channels.internal_ticket_ids()), "| all complete:", res["complete"])
