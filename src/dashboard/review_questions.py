"""Generate per-department follow-up questions for the next quarterly report.

Detection is 100% deterministic SQL over already-extracted data (goals,
expenditures) and carries NO LLM cost — it is safe to call from the always-on
dashboard build. The optional `phrase_questions` pass (Haiku) is the ONLY part
that spends, and it is invoked on demand per department by the /questions route,
never from build(). See docs/superpowers/specs/2026-07-09-next-quarter-questions-design.md.
"""
from __future__ import annotations

import json
import logging
import re

from src.dashboard.aggregator import DashboardAggregator

logger = logging.getLogger(__name__)

# Expected fraction of the annual budget spent by the end of each quarter.
_EXPECTED_PACE = {"Q1": 0.25, "Q2": 0.50, "Q3": 0.75, "Q4": 1.00}
_PACE_AHEAD = 1.5   # pace > 1.5x expected  -> flag "elevated spend"
_PACE_BEHIND = 0.5  # pace < 0.5x expected  -> flag "behind pace"

# Priority ordering for ranking findings within a department (lower = shown first).
_PRIORITY = {"highest": 0, "high": 1, "medium": 2, "low": 3}
# A goal status containing any of these words is treated as completed.
_STATUS_COMPLETE = ("complete", "done", "achieved", "finished", "closed", "met")
_GRANTS_PER_DEPT = 5
_GRANT_CLOSED = ("closed", "complete", "expired", "terminated", "ended")

_dept_key = DashboardAggregator._dept_key
_dept_display = DashboardAggregator._dept_display


def _norm_title(t: str) -> str:
    return re.sub(r"\s+", " ", (t or "").strip().lower())


def _classify_status(s: str) -> str:
    """none (no status), completed, or in_progress (any other non-empty status)."""
    s = (s or "").strip().lower()
    if not s:
        return "none"
    if any(w in s for w in _STATUS_COMPLETE):
        return "completed"
    return "in_progress"


def _period_label(year, quarter) -> str:
    return f"{(quarter or '').strip()} {year}".strip() if year else (quarter or "").strip()


def _period_tuple(year, quarter):
    return (int(year) if year is not None else 0, (quarter or "").strip())


class ReviewQuestions:
    """Builds a per-department prep sheet of data-grounded follow-up questions."""

    def __init__(self, sql_store):
        self.sql = sql_store

    # -- signal: goal follow-ups (all statuses) -------------------------------
    def _goal_findings(self):
        """Returns (findings_by_key, meta).

        Every goal produces a forward-looking question. Priority ranks gaps above
        routine follow-ups: stalled > no-progress > in-progress > completed. A
        clerk-set user_status demotes a goal to an in-progress/completed follow-up
        rather than removing it (it still prompts a next-quarter question).
        """
        with self.sql.cursor() as cur:
            cur.execute("SELECT id, department, year, quarter, goal_title, description, "
                        "target, status, user_status FROM goals ORDER BY department, id")
            rows = [dict(r) for r in cur.fetchall()]

        def eff(r):
            return (r.get("user_status") or r.get("status") or "").strip()

        names_by_key: dict = {}
        for r in rows:
            names_by_key.setdefault(_dept_key(r["department"]), set()).add(r["department"])

        periods = {_period_tuple(r.get("year"), r.get("quarter")) for r in rows}
        latest = max(periods) if periods else (0, "")

        history: dict = {}
        for r in rows:
            k = (_dept_key(r["department"]), _norm_title(r["goal_title"]))
            history.setdefault(k, []).append(r)

        findings: dict = {}   # dept_key -> [finding]
        for (dkey, ntitle), hist in history.items():
            if not dkey or not ntitle:
                continue
            hist.sort(key=lambda r: _period_tuple(r.get("year"), r.get("quarter")))
            distinct_periods = sorted({_period_tuple(r.get("year"), r.get("quarter")) for r in hist})
            display = _dept_display(dkey, names_by_key[dkey])
            latest_row = hist[-1]
            title = latest_row.get("goal_title") or ntitle
            e = eff(latest_row)
            cls = _classify_status(e)
            in_latest = _period_tuple(latest_row.get("year"), latest_row.get("quarter")) == latest
            tgt = str(latest_row.get("target") or "").strip()
            tgt_clause = f" (target: {tgt})" if tgt else ""

            # stalled: same title across >=2 periods, no status anywhere -> sharpest gap
            if len(distinct_periods) >= 2 and all(not eff(r) for r in hist):
                first_lbl = _period_label(*distinct_periods[0])
                last_lbl = _period_label(*distinct_periods[-1])
                findings.setdefault(dkey, []).append({
                    "signal": "goal_stalled", "department": display, "priority": "highest",
                    "question": (f"“{title}” has appeared in {len(distinct_periods)} quarterly "
                                 f"reports ({first_lbl}→{last_lbl}) with no status update — what "
                                 f"progress has been made since {last_lbl}?"),
                    "evidence": {"goal_title": title, "periods": [_period_label(*p) for p in distinct_periods],
                                 "count": len(distinct_periods)},
                })
                continue

            # only ask about goals whose latest appearance is in the latest period
            if not in_latest:
                continue

            if cls == "none":
                findings.setdefault(dkey, []).append({
                    "signal": "goal_no_progress", "department": display, "priority": "high",
                    "question": (f"{display}'s goal “{title}”{tgt_clause} has no progress "
                                 f"reported for {_period_label(*latest)}. What's the current status?"),
                    "evidence": {"goal_title": title, "target": tgt or None,
                                 "year": latest_row.get("year"), "quarter": latest_row.get("quarter")},
                })
            elif cls == "completed":
                findings.setdefault(dkey, []).append({
                    "signal": "goal_completed", "department": display, "priority": "low",
                    "question": (f"{display} reported goal “{title}” as complete (“{e}”). "
                                 f"What's the follow-on objective for next quarter?"),
                    "evidence": {"goal_title": title, "status": e,
                                 "year": latest_row.get("year"), "quarter": latest_row.get("quarter")},
                })
            else:  # in_progress
                findings.setdefault(dkey, []).append({
                    "signal": "goal_in_progress", "department": display, "priority": "medium",
                    "question": (f"{display}'s goal “{title}”{tgt_clause} was reported “{e}” as of "
                                 f"{_period_label(*latest)}. What progress has been made since?"),
                    "evidence": {"goal_title": title, "target": tgt or None, "status": e,
                                 "year": latest_row.get("year"), "quarter": latest_row.get("quarter")},
                })

        return findings, {"period": _period_label(*latest) if latest[0] else ""}

    # -- signal: budget pace anomaly ------------------------------------------
    def _budget_findings(self):
        with self.sql.cursor() as cur:
            cur.execute("SELECT year, quarter FROM expenditures WHERE year IS NOT NULL "
                        "ORDER BY year DESC, quarter DESC LIMIT 1")
            p = cur.fetchone()
            if not p:
                return {}
            cur.execute("SELECT department, COALESCE(SUM(revised_budget),0) AS rb, "
                        "COALESCE(SUM(ytd_expended),0) AS ytd FROM expenditures "
                        "WHERE department IS NOT NULL AND line_item NOT ILIKE '%%total%%' "
                        "AND year=%s AND quarter=%s GROUP BY department",
                        (p["year"], p["quarter"]))
            rows = [dict(r) for r in cur.fetchall()]

        quarter = (p["quarter"] or "").strip()
        expected = _EXPECTED_PACE.get(quarter)
        if not expected:
            return {}

        by_key: dict = {}
        for r in rows:
            k = _dept_key(r["department"])
            acc = by_key.setdefault(k, {"names": set(), "rb": 0.0, "ytd": 0.0})
            acc["names"].add(r["department"])
            acc["rb"] += float(r["rb"] or 0)
            acc["ytd"] += float(r["ytd"] or 0)

        findings: dict = {}
        for k, v in by_key.items():
            if not k or v["rb"] <= 0:
                continue
            pace = v["ytd"] / v["rb"]
            ahead = pace > _PACE_AHEAD * expected
            behind = pace < _PACE_BEHIND * expected
            if not (ahead or behind):
                continue
            display = _dept_display(k, v["names"])
            pace_pct, exp_pct = round(pace * 100), round(expected * 100)
            plbl = _period_label(p["year"], quarter)
            if ahead:
                q = (f"{display} is at {pace_pct}% of its revised budget by {plbl} "
                     f"(≈{exp_pct}% expected) — what's driving the elevated spend?")
            else:
                q = (f"{display} has spent only {pace_pct}% of its revised budget by {plbl} "
                     f"(≈{exp_pct}% expected) — why is spending behind pace?")
            findings.setdefault(k, []).append({
                "signal": "budget_pace", "department": display, "priority": "high",
                "question": q,
                "evidence": {"revised_budget": v["rb"], "ytd_expended": v["ytd"],
                             "pace": round(pace, 3), "expected": expected,
                             "direction": "ahead" if ahead else "behind"},
            })
        return findings

    # -- signal: open vacancies -----------------------------------------------
    def _vacancy_findings(self):
        with self.sql.cursor() as cur:
            cur.execute("SELECT department, position_title, open_count, quarter, year "
                        "FROM vacancies WHERE LOWER(COALESCE(status,'')) = 'open' "
                        "AND department IS NOT NULL AND position_title IS NOT NULL "
                        "AND position_title <> '' AND LOWER(position_title) <> 'none'")
            rows = [dict(r) for r in cur.fetchall()]
        if not rows:
            return {}

        by_key: dict = {}
        for r in rows:
            k = _dept_key(r["department"])
            acc = by_key.setdefault(k, {"names": set(), "latest": (0, ""), "rows": {}})
            acc["names"].add(r["department"])
            p = _period_tuple(r.get("year"), r.get("quarter"))
            if p > acc["latest"]:
                acc["latest"] = p
            acc["rows"].setdefault(p, []).append(r)

        findings: dict = {}
        for k, acc in by_key.items():
            if not k:
                continue
            latest_rows = acc["rows"].get(acc["latest"], [])
            positions = [{"title": r["position_title"].strip(),
                          "count": int(r["open_count"]) if r.get("open_count") is not None else None}
                         for r in latest_rows]
            if not positions:
                continue
            total = sum(p["count"] for p in positions if p["count"]) or None
            display = _dept_display(k, acc["names"])
            plbl = _period_label(*acc["latest"])
            listing = ", ".join(f"{p['title']}" + (f" ({p['count']})" if p["count"] else "")
                                for p in positions)
            count_clause = f"{total} open position{'s' if total != 1 else ''}" if total else \
                           f"{len(positions)} open role{'s' if len(positions) != 1 else ''}"
            findings.setdefault(k, []).append({
                "signal": "vacancy", "department": display, "priority": "medium",
                "question": (f"{display} reported {count_clause} in {plbl} ({listing}). "
                             f"What's the current hiring status?"),
                "evidence": {"period": plbl, "positions": positions, "total_open": total},
            })
        return findings

    # -- signal: active grants ------------------------------------------------
    def _grant_findings(self):
        with self.sql.cursor() as cur:
            cur.execute("SELECT department, grant_name, grant_number, amount, end_date, status "
                        "FROM grants WHERE department IS NOT NULL AND grant_name IS NOT NULL "
                        "AND grant_name <> ''")
            rows = [dict(r) for r in cur.fetchall()]
        if not rows:
            return {}

        by_key: dict = {}
        for r in rows:
            status = (r.get("status") or "").strip().lower()
            if any(w in status for w in _GRANT_CLOSED):
                continue
            k = _dept_key(r["department"])
            by_key.setdefault(k, {"names": set(), "rows": []})
            by_key[k]["names"].add(r["department"])
            by_key[k]["rows"].append(r)

        findings: dict = {}
        for k, acc in by_key.items():
            if not k:
                continue
            display = _dept_display(k, acc["names"])
            ranked = sorted(acc["rows"], key=lambda r: float(r.get("amount") or 0), reverse=True)
            for r in ranked[:_GRANTS_PER_DEPT]:
                amt = r.get("amount")
                amt_clause = f" (${float(amt):,.0f})" if amt is not None else ""
                status_word = (r.get("status") or "active").strip() or "active"
                end = r.get("end_date")
                findings.setdefault(k, []).append({
                    "signal": "grant", "department": display, "priority": "low",
                    "question": (f"{display}'s grant “{r['grant_name']}”{amt_clause} was "
                                 f"reported {status_word}. What's the current status / drawdown "
                                 f"for next quarter?"),
                    "evidence": {"grant_name": r["grant_name"], "grant_number": r.get("grant_number"),
                                 "amount": float(amt) if amt is not None else None,
                                 "status": r.get("status"),
                                 "end_date": end.isoformat() if hasattr(end, "isoformat") else end},
                })
        return findings

    # -- signal: department behind on filing ----------------------------------
    def _quiet_department_findings(self):
        with self.sql.cursor() as cur:
            cur.execute("SELECT department, quarter, year FROM documents "
                        "WHERE document_type = 'quarterly_report' "
                        "AND department IS NOT NULL AND department <> ''")
            rows = [dict(r) for r in cur.fetchall()]
        if not rows:
            return {}

        by_key: dict = {}
        for r in rows:
            k = _dept_key(r["department"])
            if not k:
                continue
            acc = by_key.setdefault(k, {"names": set(), "latest": (0, "")})
            acc["names"].add(r["department"])
            p = _period_tuple(r.get("year"), r.get("quarter"))
            if p > acc["latest"]:
                acc["latest"] = p

        overall = max((acc["latest"] for acc in by_key.values()), default=(0, ""))
        if not overall[0]:
            return {}

        findings: dict = {}
        for k, acc in by_key.items():
            if acc["latest"] >= overall:
                continue
            display = _dept_display(k, acc["names"])
            last_lbl = _period_label(*acc["latest"])
            overall_lbl = _period_label(*overall)
            since = last_lbl or "the period on record"
            findings.setdefault(k, []).append({
                "signal": "quiet_department", "department": display, "priority": "high",
                "question": (f"{display} hasn't filed a quarterly report since {since} "
                             f"(latest on record is {overall_lbl}). Please provide an update."),
                "evidence": {"last_filed_period": last_lbl or None, "latest_period": overall_lbl},
            })
        return findings

    # -- assembly -------------------------------------------------------------
    def build(self) -> dict:
        goals, meta = self._goal_findings()
        budget = self._budget_findings()
        vac = self._vacancy_findings()
        grant = self._grant_findings()
        quiet = self._quiet_department_findings()

        by_key: dict = {}
        for src in (goals, budget, vac, grant, quiet):
            for k, v in src.items():
                by_key.setdefault(k, []).extend(v)

        departments = []
        for findings in by_key.values():
            if not findings:
                continue
            findings.sort(key=lambda f: (_PRIORITY.get(f.get("priority", "medium"), 9), f["signal"]))
            departments.append({"department": findings[0]["department"], "findings": findings})
        departments.sort(key=lambda d: d["department"].lower())
        return {"period": meta.get("period") or None, "departments": departments}


# ── On-demand phrasing (Haiku) — the only cost; never called from build() ────

_PHRASE_SYSTEM = (
    "You are helping a city clerk prepare pointed follow-up questions for a "
    "department's next quarterly report. You receive a JSON object with `questions` "
    "(draft questions already grounded in real data) and `signals` (the structured "
    "facts behind each). Do TWO things. (1) POLISH: rewrite each question in "
    "`questions` into one natural, specific, professional question a clerk could put "
    "directly into the report request — a neutral request for a progress update (the "
    "clerk simply lacks a newer report; do NOT assume work is blocked or failing). "
    "(2) SYNTHESIZE: from the `signals` taken together, propose 0 to 3 sharper "
    "cross-cutting questions that connect two or more facts (e.g. rising vacancies "
    "alongside a stalled goal). RULES: preserve every number, percentage, target, "
    "goal name, grant name and department name exactly — never invent, drop, or alter "
    "a fact; synthesis questions must rest only on facts present in `signals`. Return "
    "ONLY a JSON object: {\"polished\": [<same length and order as questions>], "
    "\"synthesis\": [<0-3 strings>]}. No preamble."
)


def phrase_questions(findings, settings, client=None):
    """Polish templated questions and synthesize cross-cutting ones via Haiku.

    `findings` is the list of finding dicts for one department. Returns
    {"polished": [str aligned 1:1 with findings], "synthesis": [0-3 str]}.
    Raises ValueError if the model returns the wrong `polished` count (caller
    should fall back to templated wording). Transport errors propagate.
    """
    if not findings:
        return {"polished": [], "synthesis": []}
    from src.llm.client import TrackedAnthropic
    llm = client or TrackedAnthropic(settings, call_site="dashboard.review_questions")
    questions = [f["question"] for f in findings]
    signals = [{"signal": f.get("signal"), "evidence": f.get("evidence", {})} for f in findings]
    payload = {"questions": questions, "signals": signals}
    msg = llm.messages.create(
        model=settings.profiler_model,
        max_tokens=1400,
        system=_PHRASE_SYSTEM,
        messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)}],
    )
    raw = msg.content[0].text.strip()
    raw = raw[raw.find("{"): raw.rfind("}") + 1]
    out = json.loads(raw)
    polished = out.get("polished") or []
    synthesis = out.get("synthesis") or []
    if not isinstance(polished, list) or len(polished) != len(questions):
        raise ValueError(f"phrasing returned {len(polished) if isinstance(polished, list) else '?'} "
                         f"polished items, expected {len(questions)}")
    return {"polished": [str(x).strip() for x in polished],
            "synthesis": [str(x).strip() for x in synthesis if str(x).strip()][:3]}
