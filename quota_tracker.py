# quota_tracker.py
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

LA = ZoneInfo("America/Los_Angeles")  # PT (PST/PDT を自動吸収)
JST = ZoneInfo("Asia/Tokyo")

DEFAULT_LIMIT = int(os.environ.get("QUOTA_LIMIT", "10000"))  # 既定 10,000/日

# 代表的なコスト（必要に応じて増やしてOK）
COST = {
    "search.list": 100,           # 超高い
    "videos.list": 1,
    "channels.list": 1,
    "commentThreads.list": 1,
    "comments.list": 1,
}

@dataclass
class QuotaSnapshot:
    limit: int
    used_est: int
    remaining_est: int
    next_reset_pt: str
    next_reset_jst: str

class QuotaTracker:
    def __init__(self, limit: int = DEFAULT_LIMIT):
        self.limit = limit
        self.used = 0
        self._day_pt = self._today_pt()

    def _today_pt(self):
        return datetime.now(LA).date()

    def _rollover_if_needed(self):
        today = self._today_pt()
        if today != self._day_pt:
            self._day_pt = today
            self.used = 0

    def add(self, method: str, times: int = 1):
        self._rollover_if_needed()
        cost = COST.get(method, 1)
        self.used += cost * max(1, int(times))

    def snapshot(self) -> QuotaSnapshot:
        self._rollover_if_needed()

        now_pt = datetime.now(LA)
        next_midnight_pt = (now_pt + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)

        next_midnight_jst = next_midnight_pt.astimezone(JST)

        remaining = max(0, self.limit - self.used)

        return QuotaSnapshot(
            limit=self.limit,
            used_est=self.used,
            remaining_est=remaining,
            next_reset_pt=next_midnight_pt.strftime("%Y-%m-%d %H:%M:%S PT"),
            next_reset_jst=next_midnight_jst.strftime("%Y-%m-%d %H:%M:%S JST"),
        )

quota = QuotaTracker()
