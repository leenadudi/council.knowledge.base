"""Read-only aggregation of timeline data for the /dashboard page."""
from __future__ import annotations

import datetime
import logging
import re
from datetime import timezone

logger = logging.getLogger(__name__)

_Q_MONTH = {"Q1": 1, "Q2": 4, "Q3": 7, "Q4": 10}
_ACTIVE_STATUSES = ("active", "in_progress", "open", "pending", "awarded")

_VOTE_BUCKETS = {
    "yea": "yea", "yes": "yea", "aye": "yea", "y": "yea", "for": "yea", "in favor": "yea",
    "nay": "nay", "no": "nay", "n": "nay", "against": "nay",
    "abstain": "abstain", "abstained": "abstain", "abstention": "abstain",
    "absent": "absent", "away": "absent",
}


def _vote_bucket(vote: str) -> str:
    return _VOTE_BUCKETS.get((vote or "").strip().lower(), "other")


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
                "SELECT COALESCE(SUM(amount),0) AS funds FROM grants "
                "WHERE (LOWER(status) = ANY(%s) OR end_date >= %s)",
                (statuses, today),
            )
            gf = cur.fetchone() or {}
            # Latest reporting period only (YTD is cumulative within a year), and
            # exclude Munis subtotal/total rows — otherwise we double-count across
            # years and sum subtotals into the headline figures.
            cur.execute(
                "SELECT COALESCE(SUM(ytd_expended),0) AS ytd, COALESCE(SUM(revised_budget),0) AS budget "
                "FROM expenditures WHERE line_item NOT ILIKE '%total%' AND (year, quarter) = "
                "(SELECT year, quarter FROM expenditures WHERE year IS NOT NULL "
                " ORDER BY year DESC, quarter DESC LIMIT 1)",
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
            "grant_funds_active": float(gf.get("funds", 0) or 0),
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

    # -- Departments ----------------------------------------------------------
    # Actual city departments only — quarterly reports + budgets. Council actions
    # (resolutions, minutes, legislation) are NOT departments and are excluded.
    _DEPT_DOC_TYPES = ("quarterly_report", "budget")

    @staticmethod
    def _dept_key(name: str) -> str:
        """Canonical grouping key so name variants merge into one department
        (e.g. 'Parks & Recreation' == 'Bureau of Parks & Recreation')."""
        k = (name or "").strip().lower().replace(" and ", " & ")
        for p in ("bureau of ", "department of ", "office of ", "the "):
            if k.startswith(p):
                k = k[len(p):]
                break
        return re.sub(r"[^a-z0-9&]+", " ", k).strip()

    def _build_departments(self) -> list[dict]:
        with self.sql.cursor() as cur:
            cur.execute(
                "SELECT department, document_type FROM documents "
                "WHERE department IS NOT NULL AND department <> '' "
                "AND document_type = ANY(%s)",
                (list(self._DEPT_DOC_TYPES),),
            )
            doc_rows = cur.fetchall()
            cur.execute("SELECT department, COALESCE(SUM(revised_budget),0) AS rb, "
                        "COALESCE(SUM(ytd_expended),0) AS ytd FROM expenditures "
                        "WHERE department IS NOT NULL AND line_item NOT ILIKE '%total%' "
                        "AND (year, quarter) = (SELECT year, quarter FROM expenditures "
                        "WHERE year IS NOT NULL ORDER BY year DESC, quarter DESC LIMIT 1) "
                        "GROUP BY department")
            spend_rows = cur.fetchall()

        # group documents by canonical key; display the longest (most official) variant
        groups: dict = {}
        for r in doc_rows:
            key = self._dept_key(r["department"])
            if not key:
                continue
            g = groups.setdefault(key, {"names": [], "reports": 0})
            g["names"].append(r["department"])
            if r["document_type"] == "quarterly_report":
                g["reports"] += 1

        # sum spend across the variant names that share a canonical key
        spend_by_key: dict = {}
        for r in spend_rows:
            acc = spend_by_key.setdefault(self._dept_key(r["department"]), {"rb": 0.0, "ytd": 0.0})
            acc["rb"] += float(r["rb"] or 0)
            acc["ytd"] += float(r["ytd"] or 0)

        out = []
        for key, g in groups.items():
            sp = spend_by_key.get(key, {})
            out.append({
                "department": max(g["names"], key=len),
                "revised_budget": float(sp.get("rb") or 0),
                "ytd_expended": float(sp.get("ytd") or 0),
                "report_count": g["reports"],
            })
        out.sort(key=lambda x: x["department"])
        return out

    # -- Resolutions ----------------------------------------------------------
    def _build_resolutions(self) -> list[dict]:
        with self.sql.cursor() as cur:
            cur.execute("SELECT resolution_number, title, status, amount, vendor, adopted_date "
                        "FROM resolutions ORDER BY resolution_number")
            rows = [dict(r) for r in cur.fetchall()]
        for r in rows:
            if r.get("amount") is not None:
                r["amount"] = float(r["amount"])
            if hasattr(r.get("adopted_date"), "isoformat"):
                r["adopted_date"] = r["adopted_date"].isoformat()
        return rows

    # -- Votes (roll-call + member records, joined to resolutions) ------------
    def _build_votes(self) -> dict:
        with self.sql.cursor() as cur:
            # JOIN so each roll-call carries what the resolution actually was,
            # not just its number. LEFT JOIN keeps votes whose resolution row is missing.
            cur.execute("SELECT v.resolution_number, v.council_member, v.vote, "
                        "r.title, r.amount, r.status FROM votes v "
                        "LEFT JOIN resolutions r ON r.resolution_number = v.resolution_number "
                        "WHERE v.council_member IS NOT NULL "
                        "ORDER BY v.resolution_number, v.council_member")
            rows = [dict(r) for r in cur.fetchall()]

        empty_tally = {"yea": 0, "nay": 0, "abstain": 0, "absent": 0, "other": 0}
        by_res: dict = {}
        by_member: dict = {}
        for r in rows:
            rn = r.get("resolution_number") or "—"
            member = r.get("council_member")
            bucket = _vote_bucket(r.get("vote"))

            res = by_res.setdefault(rn, {
                "resolution_number": rn,
                "title": r.get("title"),
                "amount": float(r["amount"]) if r.get("amount") is not None else None,
                "status": r.get("status"),
                "tally": dict(empty_tally), "votes": [],
            })
            res["tally"][bucket] += 1
            res["votes"].append({"member": member, "vote": r.get("vote")})

            m = by_member.setdefault(member, {"member": member, "total": 0, **dict(empty_tally)})
            m["total"] += 1
            m[bucket] += 1

        by_resolution = sorted(by_res.values(), key=lambda x: x["resolution_number"])
        members = sorted(by_member.values(), key=lambda x: -x["total"])
        return {"by_resolution": by_resolution, "by_member": members}

    # -- Budget vs actual (account-level, latest period) ----------------------
    def _build_budget(self) -> dict:
        """Account-level budget-vs-actual for the latest reporting period only,
        excluding Munis subtotal/total rows. Returns a per-department rollup plus
        the underlying account lines (for drill-down), both canonical-department keyed."""
        with self.sql.cursor() as cur:
            cur.execute("SELECT year, quarter FROM expenditures WHERE year IS NOT NULL "
                        "ORDER BY year DESC, quarter DESC LIMIT 1")
            p = cur.fetchone()
            if not p:
                return {"period": None, "by_department": [], "lines": []}
            cur.execute(
                "SELECT department, account_number, line_item, "
                "COALESCE(revised_budget,0) AS revised_budget, COALESCE(ytd_expended,0) AS ytd_expended "
                "FROM expenditures WHERE year=%s AND quarter=%s "
                "AND line_item NOT ILIKE '%%total%%' "
                "AND (COALESCE(revised_budget,0) <> 0 OR COALESCE(ytd_expended,0) <> 0) "
                "ORDER BY ytd_expended DESC",
                (p["year"], p["quarter"]),
            )
            raw = [dict(r) for r in cur.fetchall()]

        by: dict = {}
        lines = []
        for r in raw:
            key = self._dept_key(r["department"])
            rb = float(r["revised_budget"] or 0)
            ytd = float(r["ytd_expended"] or 0)
            lines.append({"_key": key, "department": r["department"],
                          "account_number": r["account_number"], "line_item": r["line_item"],
                          "revised_budget": rb, "ytd_expended": ytd})
            acc = by.setdefault(key, {"names": [], "rb": 0.0, "ytd": 0.0})
            acc["names"].append(r["department"])
            acc["rb"] += rb
            acc["ytd"] += ytd
        by_department = [{"_key": k, "department": max(v["names"], key=len),
                          "revised_budget": v["rb"], "ytd_expended": v["ytd"]}
                         for k, v in by.items()]
        by_department.sort(key=lambda x: -x["revised_budget"])
        disp = {b["_key"]: b["department"] for b in by_department}
        for r in lines:
            r["department"] = disp.get(r["_key"], r["department"])
            r.pop("_key", None)
        for b in by_department:
            b.pop("_key", None)
        return {"period": {"year": p["year"], "quarter": p["quarter"]},
                "by_department": by_department, "lines": lines}

    # -- Legislation ----------------------------------------------------------
    def _build_legislation(self) -> list[dict]:
        with self.sql.cursor() as cur:
            cur.execute("SELECT bill_number, title, sponsor, amount, adopted_date, status "
                        "FROM legislation ORDER BY bill_number")
            rows = [dict(r) for r in cur.fetchall()]
        for r in rows:
            if r.get("amount") is not None:
                r["amount"] = float(r["amount"])
            if hasattr(r.get("adopted_date"), "isoformat"):
                r["adopted_date"] = r["adopted_date"].isoformat()
        return rows

    # -- Meetings (minutes) ---------------------------------------------------
    def _build_meetings(self) -> list[dict]:
        with self.sql.cursor() as cur:
            cur.execute("SELECT meeting_date, session_type, president, members_present, "
                        "members_present_names, members_absent_names, call_to_order, adjourned, "
                        "source_file FROM meetings ORDER BY meeting_date DESC")
            meetings = [dict(r) for r in cur.fetchall()]
            cur.execute("SELECT meeting_date, item_type, item_number, title, action, committee "
                        "FROM meeting_actions ORDER BY meeting_date DESC, item_number")
            actions = [dict(r) for r in cur.fetchall()]
        # attach each meeting's actions
        by_date: dict = {}
        for a in actions:
            d = a.get("meeting_date")
            if hasattr(d, "isoformat"):
                a["meeting_date"] = d.isoformat()
            by_date.setdefault(a["meeting_date"], []).append(a)
        for m in meetings:
            d = m.get("meeting_date")
            key = d.isoformat() if hasattr(d, "isoformat") else d
            m["meeting_date"] = key
            m["actions"] = by_date.get(key, [])
        return meetings

    # -- Vacancies (open positions) -------------------------------------------
    def _build_vacancies(self) -> list[dict]:
        with self.sql.cursor() as cur:
            cur.execute("SELECT department, position_title, status FROM vacancies "
                        "WHERE LOWER(status) = 'open' AND department IS NOT NULL "
                        "ORDER BY department, position_title")
            return [dict(r) for r in cur.fetchall()]

    # -- Goals ----------------------------------------------------------------
    def _build_goals(self) -> list[dict]:
        with self.sql.cursor() as cur:
            cur.execute("SELECT department, year, quarter, goal_title, description, target, status "
                        "FROM goals ORDER BY department, id")
            return [dict(r) for r in cur.fetchall()]

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
            "departments": self._safe("departments", self._build_departments, errors),
            "resolutions": self._safe("resolutions", self._build_resolutions, errors),
            "goals": self._safe("goals", self._build_goals, errors),
            "legislation": self._safe("legislation", self._build_legislation, errors),
            "meetings": self._safe("meetings", self._build_meetings, errors),
            "budget": self._safe("budget", self._build_budget, errors),
            "vacancies": self._safe("vacancies", self._build_vacancies, errors),
        }
        if errors:
            out["errors"] = errors
        return out
