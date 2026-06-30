"""Read-only aggregation of timeline data for the /dashboard page."""
from __future__ import annotations

import datetime
import logging
from datetime import timezone

logger = logging.getLogger(__name__)

_Q_MONTH = {"Q1": 1, "Q2": 4, "Q3": 7, "Q4": 10}
_ACTIVE_STATUSES = ("active", "in_progress", "open", "pending")


def quarter_start(year: int, quarter: str) -> datetime.date:
    return datetime.date(year, _Q_MONTH.get((quarter or "").upper(), 1), 1)


class DashboardAggregator:
    def __init__(self, sql_store, now: datetime.datetime | None = None):
        self.sql = sql_store
        self.now = now or datetime.datetime.now(timezone.utc)

    # -- KPIs -------------------------------------------------------------
    def _latest_period(self):
        with self.sql.cursor() as cur:
            cur.execute("SELECT MAX(year) AS year FROM documents")
            row = cur.fetchone()
            year = row and row.get("year")
            if not year:
                return None
            cur.execute("SELECT MAX(quarter) AS quarter FROM documents WHERE year = %s", (year,))
            q = cur.fetchone()
            return {"year": int(year), "quarter": (q and q.get("quarter")) or ""}

    def _build_kpis(self) -> dict:
        today = self.now.date()
        soon = today + datetime.timedelta(days=90)
        statuses = list(_ACTIVE_STATUSES)
        with self.sql.cursor() as cur:
            cur.execute(
                """SELECT
                     COUNT(*) FILTER (WHERE LOWER(status) = ANY(%s) OR end_date >= %s) AS active,
                     COUNT(*) FILTER (WHERE (LOWER(status) = ANY(%s) OR end_date >= %s)
                                       AND end_date IS NOT NULL AND end_date <= %s) AS expiring
                   FROM grants WHERE TRUE""",
                (statuses, today, statuses, today, soon),
            )
            g = cur.fetchone() or {}
            cur.execute(
                "SELECT COALESCE(SUM(ytd_expended),0) AS ytd, COALESCE(SUM(revised_budget),0) AS budget FROM expenditures",
            )
            e = cur.fetchone() or {}
            cur.execute("SELECT COUNT(*) AS c FROM resolutions")
            res = cur.fetchone() or {}
            cur.execute("SELECT COUNT(*) AS c FROM documents WHERE document_type='unclassified'")
            unc = cur.fetchone() or {}

        latest = self._latest_period()
        coverage = {"filed": 0, "total_departments": 0}
        if latest:
            with self.sql.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(DISTINCT department) AS filed FROM documents "
                    "WHERE document_type='quarterly_report' AND year=%s AND quarter=%s  -- coverage_filed",
                    (latest["year"], latest["quarter"]),
                )
                f = cur.fetchone() or {}
                cur.execute("SELECT COUNT(DISTINCT department) AS total FROM documents  -- coverage_total")
                t = cur.fetchone() or {}
            coverage = {"filed": int(f.get("filed", 0) or 0), "total_departments": int(t.get("total", 0) or 0)}

        return {
            "active_grants": int(g.get("active", 0) or 0),
            "grants_expiring_soon": int(g.get("expiring", 0) or 0),
            "ytd_spend": float(e.get("ytd", 0) or 0),
            "revised_budget": float(e.get("budget", 0) or 0),
            "latest_period": latest,
            "report_coverage": coverage,
            "resolutions_count": int(res.get("c", 0) or 0),
            "unclassified_docs": int(unc.get("c", 0) or 0),
        }
