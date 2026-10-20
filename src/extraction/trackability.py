"""Goal trackability classification (AI-suggested + human-confirmed).

Classifies each *logical* goal (canonical department + normalized title,
mirroring the dashboard's cross-quarter collapse) as:

  - measurable  — has a numeric metric checkable against quarterly data
  - milestone   — binary deliverable/deadline whose completion is observable
  - aspiration  — open-ended intent with no checkable endpoint

Low-confidence results are stored as ``needs_review`` (triage) instead of a
real label, mirroring the profiler's quarantine pattern. Labels are written to
every row of the logical goal; rows with ``trackability_source='human'`` are
never overwritten by the classifier.

Design: docs/design/specs/2026-07-29-goal-trackability-design.md
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from src.config import Settings, get_settings
from src.dashboard.aggregator import DashboardAggregator
from src.llm.client import TrackedAnthropic

logger = logging.getLogger(__name__)

LABELS = ("measurable", "milestone", "aspiration")
NEEDS_REVIEW = "needs_review"
TRACKABLE_LABELS = ("measurable", "milestone")

_CLASSIFY_ATTEMPTS = 3

_PROMPT = """You classify a city-department goal from a quarterly report as one of three labels:

- "measurable": the goal states (or clearly implies) a numeric metric that future quarterly data can be checked against — a count, percentage, dollar amount, or rate. A baseline/target number must be present or strongly implied.
- "milestone": the goal is a binary deliverable or has a deadline — something that is observably done or not done in a future report (launch, hire, complete, convert, adopt, "by Q4").
- "aspiration": open-ended intent with no checkable endpoint — "maintain", "improve", "increase", "continue" with no number, baseline, or completion event.

Examples:
- "Maintain a homicide clearance rate above the national average (57%)" -> measurable (rate with explicit target)
- "Reduce overtime spending to under $500,000" -> measurable (dollar target)
- "Cease utilization of METRO functions within the Bureau of Police" -> milestone (done/not-done conversion)
- "Complete the RMS data conversion by Q3 2025" -> milestone (deadline + deliverable)
- "Safeguard the constitutional rights of all citizens" -> aspiration (no checkable endpoint)
- "Maintain and improve Community Service Aides integration into the community" -> aspiration (ongoing, no metric)

Return ONLY a JSON object:
{{"label": "measurable|milestone|aspiration",
  "confidence": 0.0-1.0,
  "rationale": "<one sentence>",
  "metric_hint": "<the metric and target if measurable, else null>",
  "deadline": "<the deadline (e.g. 'Q3 2025') if stated, else null>"}}

Goal to classify:
- Department: {department}
- Title: {title}
- Description: {description}
- Stated target: {target}
- Recent progress notes: {notes}"""


@dataclass
class TrackabilityResult:
    label: str
    confidence: float
    rationale: str = ""
    metric_hint: Optional[str] = None
    deadline: Optional[str] = None

    def stored_label(self, threshold: float) -> str:
        """The label actually persisted — low confidence quarantines to triage."""
        return self.label if self.confidence >= threshold else NEEDS_REVIEW


@dataclass
class LogicalGoal:
    """One logical goal: all quarterly restatements of the same objective."""
    department: str
    title: str
    row_ids: list[int] = field(default_factory=list)
    description: str = ""
    target: str = ""
    status_notes: list[str] = field(default_factory=list)
    has_human_label: bool = False
    trackability: Optional[str] = None


def goal_group_key(department: str, title: str) -> tuple[str, str]:
    """Grouping key for a logical goal — canonical department + normalized
    title, mirroring the dashboard UI's goalKey() collapse."""
    norm_title = re.sub(r"\s+", " ", (title or "").strip().lower())
    return (DashboardAggregator._dept_key(department or ""), norm_title)


def collapse_goals(rows: list[dict[str, Any]]) -> list[LogicalGoal]:
    """Collapse goal rows into logical goals, keeping the richest description/
    target and up to the 2 most recent non-empty status notes as context."""
    def period(r) -> tuple:
        return (r.get("year") or 0, r.get("quarter") or "")

    groups: dict[tuple[str, str], LogicalGoal] = {}
    for r in sorted(rows, key=period):
        key = goal_group_key(r.get("department") or "", r.get("goal_title") or "")
        if not key[1]:
            continue  # blank titles cannot be grouped or classified safely
        g = groups.setdefault(key, LogicalGoal(
            department=r.get("department") or "", title=r.get("goal_title") or ""))
        g.row_ids.append(int(r["id"]))
        if len(r.get("description") or "") > len(g.description):
            g.description = r["description"]
        if len(r.get("target") or "") > len(g.target):
            g.target = r["target"]
        if (r.get("status") or "").strip():
            g.status_notes.append(r["status"].strip())
        if (r.get("trackability_source") or "") == "human":
            g.has_human_label = True
        if r.get("trackability"):
            g.trackability = r["trackability"]
    for g in groups.values():
        g.status_notes = g.status_notes[-2:]
    return list(groups.values())


def build_prompt(goal: LogicalGoal) -> str:
    notes = " | ".join(goal.status_notes) or "(none)"
    return _PROMPT.format(
        department=goal.department or "(unknown)",
        title=goal.title,
        description=(goal.description or "(none)")[:1000],
        target=goal.target or "(none)",
        notes=notes[:800],
    )


def parse_result(raw: str) -> TrackabilityResult:
    raw = raw[raw.find("{"): raw.rfind("}") + 1]
    data = json.loads(raw)
    label = str(data.get("label", "")).strip().lower()
    if label not in LABELS:
        raise ValueError(f"invalid label {label!r}")
    confidence = float(data.get("confidence", 0.0) or 0.0)
    confidence = min(1.0, max(0.0, confidence))

    def _opt(key: str) -> Optional[str]:
        v = data.get(key)
        if v in (None, "", "null", "none", "N/A"):
            return None
        return str(v)[:300]

    return TrackabilityResult(
        label=label,
        confidence=confidence,
        rationale=str(data.get("rationale", "") or "")[:500],
        metric_hint=_opt("metric_hint"),
        deadline=_opt("deadline"),
    )


def classify_goal(
    goal: LogicalGoal,
    client: Optional[TrackedAnthropic] = None,
    settings: Optional[Settings] = None,
) -> TrackabilityResult:
    """One LLM call → validated result. Retries transient failures so a flaky
    response never quarantines a goal that would classify cleanly."""
    cfg = settings or get_settings()
    llm = client or TrackedAnthropic(cfg, call_site="extraction.trackability")
    last_err: Optional[Exception] = None
    for attempt in range(_CLASSIFY_ATTEMPTS):
        try:
            msg = llm.messages.create(
                model=cfg.trackability_model,
                max_tokens=300,
                messages=[{"role": "user", "content": build_prompt(goal)}],
            )
            return parse_result(msg.content[0].text.strip())
        except Exception as e:
            last_err = e
            if attempt < _CLASSIFY_ATTEMPTS - 1:
                logger.warning("trackability attempt %d/%d failed for %r: %s — retrying",
                               attempt + 1, _CLASSIFY_ATTEMPTS, goal.title[:60], e)
    raise RuntimeError(f"trackability classification failed for {goal.title!r}") from last_err


def apply_result(cur, goal: LogicalGoal, result: TrackabilityResult,
                 threshold: float) -> str:
    """Persist one result to every row of the logical goal (AI source).
    Human-labeled rows are excluded at the SQL level as a second guard."""
    stored = result.stored_label(threshold)
    cur.execute(
        "UPDATE goals SET trackability=%s, trackability_confidence=%s, "
        "trackability_rationale=%s, trackability_metric=%s, trackability_deadline=%s, "
        "trackability_source='ai', trackability_at=NOW() "
        "WHERE id = ANY(%s) AND COALESCE(trackability_source,'') <> 'human'",
        (stored, result.confidence, result.rationale, result.metric_hint,
         result.deadline, goal.row_ids),
    )
    return stored
