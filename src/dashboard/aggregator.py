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
    "yea": "yea", "yeas": "yea", "yes": "yea", "aye": "yea", "ayes": "yea",
    "y": "yea", "for": "yea", "in favor": "yea",
    "nay": "nay", "nays": "nay", "no": "nay", "noes": "nay", "n": "nay", "against": "nay",
    "abstain": "abstain", "abstained": "abstain", "abstention": "abstain", "abstentions": "abstain",
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
            # Coverage against the canonical roster of quarterly-report filers (dedup
            # name variants via _dept_key), NOT raw distinct department strings across
            # all doc types — otherwise the denominator is inflated by variants and
            # non-reporting entities. Recomputed every build, so it tracks ingestion.
            with self.sql.cursor() as cur:
                cur.execute(
                    "SELECT department, year, quarter FROM documents "
                    "WHERE document_type='quarterly_report' AND department IS NOT NULL "
                    "AND department <> '' AND quarter IS NOT NULL AND quarter <> ''  -- coverage_rows"
                )
                qr_rows = cur.fetchall()
            roster = {self._dept_key(r["department"]) for r in qr_rows}
            roster.discard("")
            filed = {self._dept_key(r["department"]) for r in qr_rows
                     if r["year"] == latest["year"] and r["quarter"] == latest["quarter"]}
            filed.discard("")
            coverage = {"filed": len(filed), "total_departments": len(roster)}

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

    # Explicit merges for name variants the prefix/`&` rules don't catch — same
    # real department, different wording in the source docs. Non-destructive
    # (grouping-only), so it survives re-ingestion.
    _DEPT_ALIASES = {
        "planning bureau": "planning",
        "harrisburg city council": "city council",
        "city of harrisburg city council": "city council",
        "finance": "budget & finance",
        "park maintenance": "parks & recreation",
        "economic development & building & housing": "building & housing development",
        "codes health department": "codes",
    }

    @staticmethod
    def _dept_key_base(name: str) -> str:
        """Base canonical key from prefix/`&` normalization, before aliasing."""
        k = (name or "").strip().lower().replace(" and ", " & ")
        for p in ("bureau of ", "department of ", "office of ", "the "):
            if k.startswith(p):
                k = k[len(p):]
                break
        return re.sub(r"[^a-z0-9&]+", " ", k).strip()

    @staticmethod
    def _dept_key(name: str) -> str:
        """Canonical grouping key so name variants merge into one department
        (e.g. 'Parks & Recreation' == 'Bureau of Parks & Recreation'), plus
        explicit _DEPT_ALIASES merges."""
        base = DashboardAggregator._dept_key_base(name)
        return DashboardAggregator._DEPT_ALIASES.get(base, base)

    @staticmethod
    def _dept_display(key: str, names) -> str:
        """Pick the display label native to the canonical key — i.e. a name that
        maps to this key WITHOUT an alias hop (so 'codes' shows 'Bureau of Codes',
        not the aliased-in 'Codes/Health Department'). Longest wins among ties."""
        native = [n for n in names if DashboardAggregator._dept_key_base(n) == key]
        return max(native or list(names), key=len)

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
                "department": self._dept_display(key, g["names"]),
                "revised_budget": float(sp.get("rb") or 0),
                "ytd_expended": float(sp.get("ytd") or 0),
                "report_count": g["reports"],
            })
        out.sort(key=lambda x: x["department"])
        return out

    # -- Resolutions ----------------------------------------------------------
    def _build_resolutions(self) -> list[dict]:
        with self.sql.cursor() as cur:
            cur.execute("SELECT resolution_number, title, status, amount, vendor, department, adopted_date "
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
        by_department = [{"_key": k, "department": self._dept_display(k, v["names"]),
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
            cur.execute("SELECT department, position_title, status, quarter, year FROM vacancies "
                        "WHERE LOWER(status) = 'open' AND department IS NOT NULL "
                        "AND position_title IS NOT NULL AND position_title <> '' AND LOWER(position_title) <> 'none' "
                        "ORDER BY department, position_title")
            rows = [dict(r) for r in cur.fetchall()]
        # Collapse the same opening reported across multiple quarters into one entry,
        # keyed by canonical department + normalized title; keep the latest period.
        def period(r): return (r.get("year") or 0, r.get("quarter") or "")
        dedup: dict = {}
        for r in rows:
            key = (self._dept_key(r["department"]), (r["position_title"] or "").strip().lower())
            if key not in dedup or period(r) > period(dedup[key]):
                dedup[key] = r
        out = [{"department": r["department"], "position_title": r["position_title"],
                "status": r["status"], "quarter": r.get("quarter"), "year": r.get("year")}
               for r in dedup.values()]
        out.sort(key=lambda r: (r["department"], r["position_title"]))
        return out

    # -- Goals ----------------------------------------------------------------
    def _build_goals(self) -> list[dict]:
        with self.sql.cursor() as cur:
            cur.execute("SELECT id, department, year, quarter, goal_title, description, target, "
                        "status, user_status FROM goals ORDER BY department, id")
            return [dict(r) for r in cur.fetchall()]

    # -- Metrics (latest performance metric per department) -------------------
    def _build_metrics(self) -> list[dict]:
        with self.sql.cursor() as cur:
            cur.execute("SELECT department, metric_name, metric_value, metric_unit, quarter, year "
                        "FROM metrics WHERE department IS NOT NULL AND metric_name IS NOT NULL "
                        "ORDER BY year DESC NULLS LAST, quarter DESC NULLS LAST")
            rows = [dict(r) for r in cur.fetchall()]

        seen: set = set()          # (department, metric_name) already captured (latest wins)
        by_dept: dict = {}
        for r in rows:
            dept, name = r["department"], r["metric_name"]
            if (dept, name) in seen:
                continue
            seen.add((dept, name))
            val = r.get("metric_value")
            by_dept.setdefault(dept, []).append({
                "name": name,
                "value": float(val) if val is not None else None,
                "unit": r.get("metric_unit"),
                "quarter": r.get("quarter"),
                "year": int(r["year"]) if r.get("year") is not None else None,
            })
        return [{"department": d, "metrics": m} for d, m in sorted(by_dept.items())]

    # -- Vendor spend (aggregate council-authorized spend by vendor) ----------
    def _build_vendor_spend(self) -> list[dict]:
        with self.sql.cursor() as cur:
            cur.execute("SELECT vendor, amount, department FROM resolutions\n"
                        "            WHERE vendor IS NOT NULL AND vendor <> '' AND amount IS NOT NULL")
            rows = [dict(r) for r in cur.fetchall()]

        by_vendor: dict = {}
        for r in rows:
            v = by_vendor.setdefault(r["vendor"], {"vendor": r["vendor"], "total": 0.0, "count": 0, "departments": set()})
            v["total"] += float(r["amount"] or 0)
            v["count"] += 1
            if r.get("department"):
                v["departments"].add(r["department"])
        out = [{"vendor": v["vendor"], "total": v["total"], "count": v["count"],
                "departments": sorted(v["departments"])} for v in by_vendor.values()]
        out.sort(key=lambda x: -x["total"])
        return out

    # -- Commitments (authorized vs. actual + expiring grants) ----------------
    def _build_commitments(self, expiring_days: int = 180) -> dict:
        today = self.now.date()
        window_end = today + datetime.timedelta(days=expiring_days)
        with self.sql.cursor() as cur:
            cur.execute("SELECT department, COALESCE(SUM(amount),0) AS authorized_total "
                        "FROM (SELECT department, amount FROM resolutions "
                        "      WHERE department IS NOT NULL AND amount IS NOT NULL) AS authorized "
                        "GROUP BY department")
            auth_rows = [dict(r) for r in cur.fetchall()]

            cur.execute("SELECT department, COALESCE(SUM(ytd_expended),0) AS ytd_spend FROM expenditures "
                        "WHERE department IS NOT NULL AND line_item NOT ILIKE '%%total%%' "
                        "AND (year, quarter) = (SELECT year, quarter FROM expenditures "
                        "WHERE year IS NOT NULL ORDER BY year DESC, quarter DESC LIMIT 1) "
                        "GROUP BY department")
            spend_rows = [dict(r) for r in cur.fetchall()]

            cur.execute("SELECT grant_name, department, end_date, amount FROM grants\n"
                        "            WHERE end_date IS NOT NULL ORDER BY end_date")
            grant_rows = [dict(r) for r in cur.fetchall()]

        # merge authorized + spend on canonical department key
        merged: dict = {}
        for r in auth_rows:
            key = self._dept_key(r["department"])
            m = merged.setdefault(key, {"names": [], "authorized_total": 0.0, "ytd_spend": 0.0})
            m["names"].append(r["department"])
            m["authorized_total"] += float(r["authorized_total"] or 0)
        for r in spend_rows:
            key = self._dept_key(r["department"])
            m = merged.setdefault(key, {"names": [], "authorized_total": 0.0, "ytd_spend": 0.0})
            m["names"].append(r["department"])
            m["ytd_spend"] += float(r["ytd_spend"] or 0)
        authorized_vs_spent = [
            {"department": self._dept_display(k, m["names"]), "authorized_total": m["authorized_total"], "ytd_spend": m["ytd_spend"]}
            for k, m in merged.items() if m["names"]
        ]
        authorized_vs_spent.sort(key=lambda x: -x["authorized_total"])

        grants_expiring = []
        for r in grant_rows:
            end = r["end_date"]
            if end is None or end < today or end > window_end:
                continue
            grants_expiring.append({
                "grant_name": r.get("grant_name") or "Grant",
                "department": r.get("department"),
                "end_date": end.isoformat(),
                "days_left": (end - today).days,
                "amount": float(r["amount"]) if r.get("amount") is not None else None,
            })
        grants_expiring.sort(key=lambda x: x["days_left"])
        return {"authorized_vs_spent": authorized_vs_spent, "grants_expiring": grants_expiring}

    # -- Review questions (deterministic gap detection; NO LLM) ---------------
    def _build_review_questions(self) -> dict:
        from src.dashboard.review_questions import ReviewQuestions
        return ReviewQuestions(self.sql).build()

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
            "votes": self._safe("votes", self._build_votes, errors),
            "metrics": self._safe("metrics", self._build_metrics, errors),
            "vendor_spend": self._safe("vendor_spend", self._build_vendor_spend, errors),
            "commitments": self._safe("commitments", self._build_commitments, errors),
            "review_questions": self._safe("review_questions", self._build_review_questions, errors),
        }
        if errors:
            out["errors"] = errors
        return out
