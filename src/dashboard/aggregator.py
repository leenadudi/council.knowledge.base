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
            if year is None:
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
                   FROM grants""",
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

    # -- Timeline -------------------------------------------------------------
    def _build_timeline(self) -> dict:
        def iso(d):
            if d is None:
                return None
            if hasattr(d, "isoformat"):
                return d.isoformat()
            raise TypeError(f"Expected date/datetime, got {type(d)}: {d!r}")

        def _plus_one_year(d: datetime.date) -> datetime.date:
            try:
                return d.replace(year=d.year + 1)
            except ValueError:
                return d.replace(year=d.year + 1, day=28)

        with self.sql.cursor() as cur:
            cur.execute(
                "SELECT id, grant_name, department, start_date, end_date, status, amount "
                "FROM grants WHERE start_date IS NOT NULL ORDER BY start_date"
            )
            grants = []
            for r in cur.fetchall():
                start = r["start_date"]
                end = r["end_date"] if r["end_date"] is not None else (_plus_one_year(start) if start else None)
                grants.append({
                    "id": f"grant-{r['id']}",
                    "label": r.get("grant_name") or "Grant",
                    "department": r.get("department"),
                    "start": iso(start),
                    "end": iso(end),
                    "status": r.get("status"),
                    "amount": float(r["amount"]) if r.get("amount") is not None else None,
                })

            cur.execute(
                "SELECT id, department, quarter, year, document_type FROM documents "
                "WHERE year IS NOT NULL AND quarter IS NOT NULL AND quarter <> '' "
                "ORDER BY year, quarter"
            )
            reports = []
            for r in cur.fetchall():
                reports.append({
                    "id": f"report-{r['id']}",
                    "department": r.get("department"),
                    "date": quarter_start(int(r["year"]), r.get("quarter")).isoformat(),
                    "quarter": r.get("quarter"),
                    "year": int(r["year"]),
                    "document_type": r.get("document_type"),
                })

            cur.execute(
                "SELECT id, resolution_number, title, adopted_date, amount, status "
                "FROM resolutions WHERE adopted_date IS NOT NULL ORDER BY adopted_date"
            )
            resolutions = []
            for r in cur.fetchall():
                resolutions.append({
                    "id": f"res-{r['id']}",
                    "label": r.get("resolution_number") or r.get("title") or "Resolution",
                    "date": iso(r["adopted_date"]),
                    "amount": float(r["amount"]) if r.get("amount") is not None else None,
                    "status": r.get("status"),
                })

            cur.execute(
                "SELECT year, quarter, COALESCE(SUM(ytd_expended), 0) AS ytd "
                "FROM expenditures WHERE year IS NOT NULL "
                "GROUP BY year, quarter ORDER BY year, quarter"
            )
            spending = [
                {"period": f"{r['year']} {r['quarter']}", "ytd_expended": float(r["ytd"] or 0)}
                for r in cur.fetchall()
            ]

        return {"grants": grants, "reports": reports, "resolutions": resolutions, "spending": spending}

    # -- Tables ---------------------------------------------------------------
    def _build_tables(self) -> dict:
        with self.sql.cursor() as cur:
            cur.execute(
                "SELECT grant_name, department, amount, start_date, end_date, status "
                "FROM grants ORDER BY start_date DESC NULLS LAST LIMIT 200"
            )
            grants = [dict(r) for r in cur.fetchall()]
            cur.execute(
                "SELECT department, COALESCE(SUM(revised_budget),0) AS revised_budget, "
                "COALESCE(SUM(ytd_expended),0) AS ytd_expended FROM expenditures "
                "GROUP BY department ORDER BY ytd_expended DESC"
            )
            spending_by_dept = [dict(r) for r in cur.fetchall()]
            cur.execute(
                "SELECT department, quarter, year, document_type FROM documents "
                "ORDER BY year DESC, quarter DESC LIMIT 200"
            )
            reports = [dict(r) for r in cur.fetchall()]

        def clean(rows):
            for row in rows:
                for k, v in list(row.items()):
                    if hasattr(v, "isoformat"):
                        row[k] = v.isoformat()
                    elif isinstance(v, (int, float)) or v is None or isinstance(v, str):
                        pass
                    else:
                        row[k] = float(v)  # Decimal
            return rows

        return {
            "grants": clean(grants),
            "spending_by_dept": clean(spending_by_dept),
            "reports": clean(reports),
        }

    # -- Error isolation helper -----------------------------------------------
    def _safe(self, name: str, fn, errors: dict):
        try:
            return fn()
        except Exception as e:
            logger.warning("dashboard panel %s failed: %s", name, e)
            errors[name] = str(e)
            return None

    # -- Top-level assembly ---------------------------------------------------
    def build(self) -> dict:
        errors: dict = {}
        out = {
            "generated_at": self.now.isoformat(),
            "kpis": self._safe("kpis", self._build_kpis, errors),
            "timeline": self._safe("timeline", self._build_timeline, errors),
            "tables": self._safe("tables", self._build_tables, errors),
        }
        if errors:
            out["errors"] = errors
        return out
