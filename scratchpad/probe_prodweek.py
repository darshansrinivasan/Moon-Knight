import asyncio
from datetime import date
from dotenv import load_dotenv
load_dotenv("/Users/darshans/Downloads/qc/.env")
import db; db.init_db()
import channels, pylon, rules as qc_rules

async def main():
    dropped = tuple({*qc_rules.excluded_states(), "archived"})
    ids = channels.internal_ticket_ids()
    chans = set(channels.internal_channel_ids())
    for lo, hi in [(date(2026,8,31), date(2026,9,6)), (date(2026,8,24), date(2026,8,30))]:
        counts, complete = await pylon.count_resolved_issues(lo, hi, dropped,
                                                             internal_ids=ids,
                                                             internal_channels=chans)
        print(f"{lo}..{hi}: {counts} complete={complete}")
asyncio.run(main())
