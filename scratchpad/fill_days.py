from dotenv import load_dotenv
load_dotenv("/Users/darshans/Downloads/qc/.env")
import db; db.init_db()
import reportcard
from datetime import date
with db.get_conn() as c:
    lo = c.execute("SELECT MIN(fetch_date) m FROM fetch_log").fetchone()["m"]
res = reportcard.backfill(lo, date.today().isoformat(), "darshan@spotdraft.com")
print(f"captured {res['count']} new day(s): {res['captured']}; kept {res['skipped_existing']}")
