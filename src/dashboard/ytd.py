"""Year-to-date report: council activity aggregated across quarters within a
year (clerk priority #1, 2026-07-29). Pure SQL aggregation — NO LLM calls."""
from __future__ import annotations

import datetime
from datetime import timezone

# Complete-status keywords, same convention as review_questions._STATUS_COMPLETE.
_STATUS_COMPLETE = ("complete", "done", "achieved", "finished", "closed", "met")
_QUARTERS = ("Q1", "Q2", "Q3", "Q4")


def _quarter_of(d: datetime.date) -> str:
    return f"Q{(d.month - 1) // 3 + 1}"


def _goal_bucket(status: str, user_status: str) -> str:
    """Clerk-set user_status wins; else infer from the report's status text."""
    us = (user_status or "").strip().lower()
    if us in ("not_started", "in_progress", "completed"):
        return us
    s = (status or "").strip().lower()
    if not s:
        return "not_started"
    if any(w in s for w in _STATUS_COMPLETE):
        return "completed"
    return "in_progress"


class YTDReport:
    def __init__(self, sql_store, now: datetime.datetime | None = None):
        self.sql = sql_store
        self.now = now or datetime.datetime.now(timezone.utc)

    def _fetch(self) -> dict:
        with self.sql.cursor() as cur:
            # Cumulative-within-year snapshots per (year, quarter, department);
            # Munis subtotal/total rows excluded as everywhere else.
            cur.execute(
                "SELECT year, quarter, department, "
                "COALESCE(SUM(ytd_expended),0) AS ytd, COALESCE(SUM(revised_budget),0) AS rb "
                "FROM expenditures WHERE year IS NOT NULL "
                "AND quarter IS NOT NULL AND quarter <> '' "
                "AND line_item NOT ILIKE '%total%' "
                "GROUP BY year, quarter, department"
            )
            spend = [dict(r) for r in cur.fetchall()]
            cur.execute("SELECT resolution_number, amount, adopted_date FROM resolutions")
            resolutions = [dict(r) for r in cur.fetchall()]
            cur.execute("SELECT bill_number, amount, adopted_date FROM legislation")
            legislation = [dict(r) for r in cur.fetchall()]
            cur.execute("SELECT meeting_date FROM meetings WHERE meeting_date IS NOT NULL")
            meetings = [dict(r) for r in cur.fetchall()]
            cur.execute("SELECT id, grant_name, grant_number, department, amount, "
                        "start_date, end_date, status FROM grants")
            grants = [dict(r) for r in cur.fetchall()]
            cur.execute("SELECT department, year, status, user_status FROM goals "
                        "WHERE year IS NOT NULL")
            goals = [dict(r) for r in cur.fetchall()]
        return {"spend": spend, "resolutions": resolutions, "legislation": legislation,
                "meetings": meetings, "grants": grants, "goals": goals}

    # -- per-domain builders --------------------------------------------------

    @staticmethod
    def _spending(spend_rows: list, year: int) -> dict:
        from src.dashboard.aggregator import DashboardAggregator as Agg
        rows = [r for r in spend_rows if r["year"] == year]
        if not rows:
            return {"ytd_expended": 0.0, "revised_budget": 0.0, "pct_used": 0,
                    "expected_pct": 0, "by_quarter": [], "by_department": []}
        quarters = sorted({r["quarter"] for r in rows})
        # ytd_expended is cumulative within the year — never sum across quarters.
        # Departments don't all file every quarter, so the citywide latest-quarter
        # snapshot undercounts: take each department's LATEST snapshot in the year
        # and sum those. Quarter rows carry a filer count so uneven coverage
        # (and the negative "increments" it can cause) is visible, not hidden.
        by_quarter, prev = [], 0.0
        for q in quarters:
            qrows = [r for r in rows if r["quarter"] == q]
            snap = sum(float(r["ytd"] or 0) for r in qrows)
            by_quarter.append({"quarter": q, "ytd_expended": snap, "increment": snap - prev,
                               "departments": len({Agg._dept_key(r["department"])
                                                   for r in qrows if r.get("department")})})
            prev = snap
        by_key: dict = {}
        for r in rows:
            if not r.get("department"):
                continue
            acc = by_key.setdefault(Agg._dept_key(r["department"]),
                                    {"names": [], "by_q": {}})
            acc["names"].append(r["department"])
            slot = acc["by_q"].setdefault(r["quarter"], {"rb": 0.0, "ytd": 0.0})
            slot["rb"] += float(r["rb"] or 0)
            slot["ytd"] += float(r["ytd"] or 0)
        by_department = []
        for k, v in by_key.items():
            latest_q = max(v["by_q"])
            snap = v["by_q"][latest_q]
            by_department.append({
                "department": Agg._dept_display(k, v["names"]),
                "as_of_quarter": latest_q,
                "revised_budget": snap["rb"], "ytd_expended": snap["ytd"],
                "pct": round(snap["ytd"] / snap["rb"] * 100) if snap["rb"] else 0,
            })
        by_department.sort(key=lambda x: -x["ytd_expended"])
        total_ytd = sum(d["ytd_expended"] for d in by_department)
        total_rb = sum(d["revised_budget"] for d in by_department)
        # pace vs the latest quarter with spending data (not overall activity)
        elapsed = _QUARTERS.index(quarters[-1]) + 1 if quarters[-1] in _QUARTERS else 0
        return {"ytd_expended": total_ytd, "revised_budget": total_rb,
                "pct_used": round(total_ytd / total_rb * 100) if total_rb else 0,
                "expected_pct": round(elapsed / 4 * 100),
                "by_quarter": by_quarter, "by_department": by_department}

    @staticmethod
    def _legislative(resolutions: list, legislation: list, meetings: list, year: int) -> dict:
        def dated(rows):
            return [r for r in rows if r.get("adopted_date") is not None
                    and r["adopted_date"].year == year]
        res = dated(resolutions)
        leg = dated(legislation)
        mtgs = [m for m in meetings if m["meeting_date"].year == year]
        undated = sum(1 for r in resolutions + legislation if r.get("adopted_date") is None)
        quarters = sorted({_quarter_of(r["adopted_date"]) for r in res + leg}
                          | {_quarter_of(m["meeting_date"]) for m in mtgs})
        by_quarter = [{
            "quarter": q,
            "resolutions": sum(1 for r in res if _quarter_of(r["adopted_date"]) == q),
            "ordinances": sum(1 for r in leg if _quarter_of(r["adopted_date"]) == q),
            "meetings": sum(1 for m in mtgs if _quarter_of(m["meeting_date"]) == q),
            "authorized": sum(float(r["amount"]) for r in res
                              if _quarter_of(r["adopted_date"]) == q and r.get("amount") is not None),
        } for q in quarters]
        return {"resolutions_adopted": len(res), "ordinances_adopted": len(leg),
                "meetings_held": len(mtgs),
                "council_authorized": sum(float(r["amount"]) for r in res
                                          if r.get("amount") is not None),
                "undated_items": undated, "by_quarter": by_quarter}

    def _grants(self, deduped: list, year: int) -> dict:
        today = self.now.date()
        from src.dashboard.aggregator import _ACTIVE_STATUSES

        def live(r) -> bool:
            st = (r.get("status") or "").strip().lower()
            return st in _ACTIVE_STATUSES or (r.get("end_date") is not None
                                              and r["end_date"] >= today)
        new = sorted((r for r in deduped if r.get("start_date") is not None
                      and r["start_date"].year == year),
                     key=lambda r: r["start_date"])
        return {
            "new_count": len(new),
            "new_amount": sum(float(r["amount"]) for r in new if r.get("amount") is not None),
            "active_count": sum(1 for r in deduped if live(r)),
            "new_grants": [{
                "grant_name": r.get("grant_name") or "Grant",
                "department": r.get("department"),
                "amount": float(r["amount"]) if r.get("amount") is not None else None,
                "start_date": r["start_date"].isoformat(),
                "status": r.get("status"),
            } for r in new],
        }

    @staticmethod
    def _goals(goal_rows: list, year: int) -> dict:
        out = {"total": 0, "completed": 0, "in_progress": 0, "not_started": 0}
        for g in goal_rows:
            if g["year"] != year:
                continue
            out["total"] += 1
            out[_goal_bucket(g.get("status"), g.get("user_status"))] += 1
        return out

    # -- assembly -------------------------------------------------------------

    def build(self) -> dict:
        from src.dashboard.aggregator import DashboardAggregator as Agg
        d = self._fetch()
        deduped_grants = Agg._dedupe_grants(d["grants"])
        # Years with council activity or filings. Grants deliberately don't
        # drive this list — an old grant alone shouldn't create an empty report.
        years = sorted(
            {int(r["year"]) for r in d["spend"]}
            | {r["adopted_date"].year for r in d["resolutions"] + d["legislation"]
               if r.get("adopted_date") is not None}
            | {m["meeting_date"].year for m in d["meetings"]}
            | {int(g["year"]) for g in d["goals"]},
            reverse=True)

        reports = {}
        for year in years:
            spending = self._spending(d["spend"], year)
            legislative = self._legislative(d["resolutions"], d["legislation"],
                                            d["meetings"], year)
            all_quarters = ({q["quarter"] for q in spending["by_quarter"]}
                            | {q["quarter"] for q in legislative["by_quarter"]})
            as_of = max(all_quarters) if all_quarters else None
            elapsed = _QUARTERS.index(as_of) + 1 if as_of in _QUARTERS else 0
            reports[year] = {
                "year": year,
                "as_of_quarter": as_of,
                "quarters_elapsed": elapsed,
                "legislative": legislative,
                "spending": spending,
                "grants": self._grants(deduped_grants, year),
                "goals": self._goals(d["goals"], year),
            }
        return {"years": years, "reports": reports}
