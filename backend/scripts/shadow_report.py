"""Report on the v2 shadow ladders: v1 vs v2, outcomes of every recorded v2 BUY, and the provisional blue-sky ladders as their own line (with the 2-month review). Run from backend/:  python scripts/shadow_report.py"""
import json
import sys

sys.path.insert(0, ".")
from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.db import engine  # noqa: E402
from app.models import User  # noqa: E402
from app.services import pipeline_v2, shadow_outcomes  # noqa: E402

with Session(engine) as db:
    uid = db.scalar(select(User.id).where(User.email_verified.is_(True)))
    out = {"compare": {k: v for k, v in pipeline_v2.compare(db, uid, limit=50).items() if k != "v2_buy_cards"}, "outcomes": shadow_outcomes.review(db, uid)}
print(json.dumps(out, indent=1, default=str)[:6000])
