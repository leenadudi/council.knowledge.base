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

_dept_key = DashboardAggregator._dept_key
_dept_display = DashboardAggregator._dept_display


def _norm_title(t: str) -> str:
    return re.sub(r"\s+", " ", (t or "").strip().lower())


def _period_label(year, quarter) -> str:
    return f"{(quarter or '').strip()} {year}".strip() if year else (quarter or "").strip()


def _period_tuple(year, quarter):
    return (int(year) if year is not None else 0, (quarter or "").strip())


class ReviewQuestions:
    """Builds a per-department prep sheet of data-grounded follow-up questions."""

    def __init__(self, sql_store):
        self.sql = sql_store

    # -- signal: goals with no reported progress ------------------------------
    def _goal_findings(self):
        """Returns (findings_by_key, names_by_key, stalled_titles_by_key).

        `stalled_titles` lets the caller suppress a redundant no-progress finding
        for a goal already flagged as stalled (multi-quarter is the sharper signal).
        """
        with self.sql.cursor() as cur:
            cur.execute("SELECT id, department, year, quarter, goal_title, description, "
                        "target, status FROM goals ORDER BY department, id")
            rows = [dict(r) for r in cur.fetchall()]

        names_by_key: dict = {}
        for r in rows:
            names_by_key.setdefault(_dept_key(r["department"]), set()).add(r["department"])

        # latest goal period overall (drives the "as of" label + no-progress scope)
        periods = {_period_tuple(r.get("year"), r.get("quarter")) for r in rows}
        latest = max(periods) if periods else (0, "")

        # group by (dept_key, normalized title) across periods for the stalled signal
        history: dict = {}
        for r in rows:
            k = (_dept_key(r["department"]), _norm_title(r["goal_title"]))
            history.setdefault(k, []).append(r)

        no_progress: dict = {}   # key -> [finding]
        stalled: dict = {}       # key -> [finding]
        stalled_titles: dict = {}  # key -> {normalized titles}

        for (dkey, ntitle), hist in history.items():
            if not dkey or not ntitle:
                continue
            hist.sort(key=lambda r: _period_tuple(r.get("year"), r.get("quarter")))
            hp = [_period_tuple(r.get("year"), r.get("quarter")) for r in hist]
            distinct_periods = sorted(set(hp))
            statuses = {(r.get("status") or "").strip() for r in hist}
            statuses.discard("")
            display = _dept_display(dkey, names_by_key[dkey])
            latest_row = hist[-1]
            title = latest_row.get("goal_title") or ntitle

            # stalled: carried across >=2 distinct quarters with no status change
            # (either never a status, or one unchanging status the whole time)
            if len(distinct_periods) >= 2 and len(statuses) <= 1:
                first_lbl = _period_label(*distinct_periods[0])
                last_lbl = _period_label(*distinct_periods[-1])
                stalled.setdefault(dkey, []).append({
                    "signal": "goal_stalled",
                    "department": display,
                    "question": (f"“{title}” has carried across {len(distinct_periods)} "
                                 f"quarters ({first_lbl}→{last_lbl}) with no reported update "
                                 f"— what's blocking completion?"),
                    "evidence": {"goal_title": title, "periods": [_period_label(*p) for p in distinct_periods],
                                 "count": len(distinct_periods)},
                })
                stalled_titles.setdefault(dkey, set()).add(ntitle)
                continue

            # no progress: appears in the latest period, has a target, no status
            if _period_tuple(latest_row.get("year"), latest_row.get("quarter")) == latest \
                    and (latest_row.get("target") or "").strip() \
                    and not (latest_row.get("status") or "").strip():
                no_progress.setdefault(dkey, []).append({
                    "signal": "goal_no_progress",
                    "department": display,
                    "question": (f"{display}'s goal “{title}” (target: "
                                 f"{str(latest_row['target']).strip()}) shows no reported progress "
                                 f"as of {_period_label(*latest)}. What's the current status?"),
                    "evidence": {"goal_title": title, "target": str(latest_row["target"]).strip(),
                                 "year": latest_row.get("year"), "quarter": latest_row.get("quarter")},
                })

        merged: dict = {}
        for k, v in stalled.items():
            merged.setdefault(k, []).extend(v)
        for k, v in no_progress.items():
            merged.setdefault(k, []).extend(v)
        return merged, names_by_key, {"period": _period_label(*latest) if latest[0] else ""}

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
                "signal": "budget_pace",
                "department": display,
                "question": q,
                "evidence": {"revised_budget": v["rb"], "ytd_expended": v["ytd"],
                             "pace": round(pace, 3), "expected": expected,
                             "direction": "ahead" if ahead else "behind"},
            })
        return findings

    # -- assembly -------------------------------------------------------------
    def build(self) -> dict:
        goals, _names, meta = self._goal_findings()
        budget = self._budget_findings()

        by_key: dict = {}
        for k, v in goals.items():
            by_key.setdefault(k, []).extend(v)
        for k, v in budget.items():
            by_key.setdefault(k, []).extend(v)

        departments = [
            {"department": findings[0]["department"], "findings": findings}
            for findings in by_key.values() if findings
        ]
        departments.sort(key=lambda d: d["department"].lower())
        return {"period": meta.get("period") or None, "departments": departments}


# ── On-demand phrasing (Haiku) — the only cost; never called from build() ────

_PHRASE_SYSTEM = (
    "You are helping a city clerk prepare pointed follow-up questions for a "
    "department's next quarterly report. You will receive a JSON array of draft "
    "questions already grounded in real data. Rewrite each into one natural, "
    "specific, professional question a clerk could put directly into the report "
    "request. RULES: (1) preserve every number, percentage, target, goal name and "
    "department name exactly — do not invent, drop, or alter any fact; (2) return "
    "ONLY a JSON array of strings, same length and order as the input; (3) one "
    "question per item, no preamble."
)


def phrase_questions(questions, settings, client=None):
    """Rewrite templated questions into clerk-ready wording via Haiku.

    Returns a list of strings aligned 1:1 with `questions`. Raises ValueError if
    the model returns the wrong count (caller should fall back to the templated
    wording). Anthropic/transport errors propagate for the caller to handle.
    """
    if not questions:
        return []
    from src.llm.client import TrackedAnthropic
    llm = client or TrackedAnthropic(settings, call_site="dashboard.review_questions")
    msg = llm.messages.create(
        model=settings.profiler_model,
        max_tokens=1200,
        system=_PHRASE_SYSTEM,
        messages=[{"role": "user", "content": json.dumps(questions, ensure_ascii=False)}],
    )
    raw = msg.content[0].text.strip()
    raw = raw[raw.find("["): raw.rfind("]") + 1]
    out = json.loads(raw)
    if not isinstance(out, list) or len(out) != len(questions):
        raise ValueError(f"phrasing returned {len(out) if isinstance(out, list) else '?'} "
                         f"items, expected {len(questions)}")
    return [str(x).strip() for x in out]
