from dotenv import load_dotenv
load_dotenv("/Users/darshans/Downloads/qc/.env")
import db; db.init_db()
import reportcard
with db.get_conn() as c:
    lo = c.execute("SELECT MIN(fetch_date) m FROM fetch_log").fetchone()["m"]
from datetime import date
res = reportcard.backfill(lo, date.today().isoformat(), "darshan@spotdraft.com")
print(f"backfilled {res['count']} days ({res['start']}..{res['end']}), "
      f"{res['skipped_existing']} already frozen (kept)")
