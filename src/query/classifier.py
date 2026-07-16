"""
Query classifier — reads a user question and produces a QueryPlan.
Determines which stores to query, in what order, with what queries.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Optional

from src.llm.client import TrackedAnthropic

from src.config import Settings, get_settings
from src.models import QueryPlan

logger = logging.getLogger(__name__)

_CLASSIFY_PROMPT = """You are the query router for a city government knowledge base.

The knowledge base has three stores:
1. **vector** — narrative text, descriptions, goals, summaries, community engagement
2. **sql** — structured data: budget/expenditure figures, grant amounts, metrics, vacancies, AND council actions — resolutions (authorized $), legislation/bills, meeting/session records, and department goals
3. **graph** — people, org charts, departments, reporting relationships, project ownership

Given the user's question, produce a retrieval plan as JSON:

{{
  "sources": ["vector"|"sql"|"graph"],  // list of stores to query
  "execution": "parallel"|"sequential",  // parallel if independent; sequential if store A informs store B
  "sequential_order": null or ["sql","graph"],  // only needed if sequential
  "sql_query": "SELECT ...",            // valid SQL over any table in the schema reference below (resolutions, legislation, meetings, goals, expenditures, etc.), or null
  "vector_query": "search terms ...",   // semantic search string, or null
  "graph_query": "MATCH ...",           // Cypher query, or null
  "metadata_filters": {{}},              // department/quarter/year filters extracted from the question
  "reasoning": "...",
  "resolved_question": "the question rewritten as a fully standalone question, or the original question unchanged if it is already self-contained"
}}

SQL schema reference:
- expenditures(id, department, sub_department, account_number, line_item, revised_budget, ytd_expended, quarter, year, source_file)
- metrics(id, department, metric_name, metric_value, metric_unit, quarter, year, source_file)
- grants(id, department, grant_name, grant_number, amount, start_date, end_date, status, source_file)
- vacancies(id, department, position_title, status, quarter, year)
- resolutions(id, resolution_number, title, amount, vendor, department, adopted_date, status, source_file) — City Council resolutions (authorizations of contracts/spending/policy). `amount` is the authorized dollar figure (may be NULL). `status` e.g. 'Passed'.
- votes(id, resolution_number, council_member, vote, source_file) — per-member votes on a resolution ('yes'|'no'|'abstain').
- legislation(id, bill_number, title, sponsor, amount, adopted_date, status, source_file) — ordinances/bills. `status` e.g. 'Introduced','Signed','Vetoed','Approved'.
- meetings(id, meeting_date, session_type, president, members_present, members_present_names, members_absent_names, call_to_order, adjourned, source_file) — one row per council legislative session (from minutes).
- meeting_actions(id, meeting_date, item_type, item_number, title, action, committee, source_file) — what happened to each resolution/ordinance at a session (item_type 'resolution'|'ordinance'; action e.g. 'read into record','referred to committee','final passage').
- goals(id, department, year, quarter, goal_title, description, target, status, source_file) — department goals stated in quarterly reports.
- appropriations(id, department, fiscal_year, fund, category, amount, source_file) — department budget appropriations from the annual budget.

Column notes (MUST follow exactly):
- `quarter` is a STRING like 'Q1', 'Q2', 'Q3', 'Q4' — always quote it: `quarter = 'Q1'`. Never write `quarter = 1`.
- `year` is an INTEGER: `year = 2026` (no quotes). `appropriations` uses `fiscal_year` (INTEGER), NOT `year`.
- Only expenditures, metrics, vacancies, and goals have `quarter`/`year`. `grants`, `resolutions`, `legislation`, `meetings`, `meeting_actions`, `appropriations` have NO `quarter`/`year` — never reference them there. Filter grants by `department`/`status`/dates; resolutions & legislation by `adopted_date`/`status`/text; meetings by `meeting_date`.
- For "which resolutions authorized spending / how much", query `resolutions` (e.g. `SELECT resolution_number, title, amount, vendor FROM resolutions WHERE amount IS NOT NULL ORDER BY amount DESC`).
- For "what bills/ordinances passed", query `legislation`. For "what happened at the <date> meeting" or "which resolutions were read/referred", query `meeting_actions` (join on item_number) and/or `meetings`.

Graph schema:
- Nodes: Person(name, title, department), Department(name), Project(name, status), Grant(name, grant_number, amount), Document(filename, quarter, year)
- Relationships: DIRECTS, MANAGES, REPORTS_TO, HAS_PROJECT, REPORTED_IN, MANAGES_GRANT, MENTIONED_IN

Query generation rules (MUST follow exactly):
- `sql_query` and `graph_query` are executed verbatim with NO parameter binding. Use literal values inline.
- CITATIONS: every `sql_query` MUST return a `source_file` column so the answer can cite the source PDF (users know documents, not the database). For row-level queries, add `source_file` to the SELECT list. For aggregate queries (GROUP BY / SUM / COUNT / AVG), include `STRING_AGG(DISTINCT source_file, '; ') AS source_file` so the originating documents are still returned. Example: `SELECT resolution_number, title, amount, vendor, source_file FROM resolutions WHERE amount IS NOT NULL ORDER BY amount DESC`.
- `graph_query` (Cypher): never use `$parameters` (e.g. `$department_name`) — they will not be bound and the query will fail. Inline the literal value: `MATCH (p:Person)-[:DIRECTS]->(d:Department {{name: 'Public Works'}}) RETURN p.name, p.title`.

Rules:
- Numeric/budget questions → sql (required), vector (optional for context)
- Council actions — resolutions, bills/ordinances/legislation, "what did the council authorize/pass/vote on", meetings/sessions, department goals → sql (required), vector (optional for narrative context)
- "Who manages/directs/leads X" → graph required
- Conceptual/narrative questions → vector only
- Cross-store questions → parallel with both required stores
- If the question mentions a specific department, add it to metadata_filters
- If the question mentions a specific quarter/year, add it to metadata_filters
- FOLLOW-UPS: If a "Prior conversation" section is present AND the user question depends on it (pronouns like "that"/"those"/"it", ellipsis, "what about <X>", "break that down"), rewrite the question into a fully self-contained question in `resolved_question` and build the store queries from that rewrite. If the question is already self-contained or is a fresh unrelated topic, set `resolved_question` to the original question and ignore the prior conversation.

{prior_conversation}User question: {question}

Return ONLY the JSON object, no explanation.
"""


def _build_prior_conversation(history: Optional[list[dict]]) -> str:
    """Render prior turns as a delimited prompt block, or '' when none."""
    if not history:
        return ""
    lines = ["Prior conversation (most recent last):"]
    for turn in history:
        q = str(turn.get("question", "")).strip()
        a = str(turn.get("answer", "")).strip()
        lines.append(f"- Q: {q}\n  A: {a}")
    return "\n".join(lines) + "\n\n"


class QueryClassifier:
    def __init__(self, settings: Optional[Settings] = None, llm: Optional[TrackedAnthropic] = None):
        self.cfg = settings or get_settings()
        self.client = llm or TrackedAnthropic(self.cfg, call_site="query.classifier")

    def classify(
        self,
        question: str,
        history: Optional[list[dict]] = None,
        query_id: Optional[str] = None,
    ) -> QueryPlan:
        """Classify the question and return a retrieval plan."""
        try:
            prompt = _CLASSIFY_PROMPT.format(
                question=question,
                prior_conversation=_build_prior_conversation(history),
            )
            msg = self.client.messages.create(
                model=self.cfg.query_classifier_model,
                max_tokens=1024,
                query_id=query_id,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = msg.content[0].text
            return _parse_plan(raw)
        except Exception as e:
            logger.error("Query classification failed: %s — defaulting to vector-only", e)
            return QueryPlan(
                sources=["vector"],
                execution="parallel",
                vector_query=question,
                reasoning=f"Classification error: {e}",
            )


def _parse_plan(raw: str) -> QueryPlan:
    match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", raw)
    json_str = match.group(1) if match else raw

    try:
        data = json.loads(json_str)
    except json.JSONDecodeError:
        logger.warning("QueryClassifier returned non-JSON: %s", raw[:200])
        return QueryPlan(sources=["vector"], execution="parallel", reasoning="parse error")

    return QueryPlan(
        sources=data.get("sources", ["vector"]),
        execution=data.get("execution", "parallel"),
        sequential_order=data.get("sequential_order"),
        sql_query=data.get("sql_query"),
        vector_query=data.get("vector_query"),
        graph_query=data.get("graph_query"),
        metadata_filters=data.get("metadata_filters", {}),
        reasoning=data.get("reasoning", ""),
        resolved_question=data.get("resolved_question"),
    )
