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
2. **sql** — structured data: budget tables, expenditure figures, grant amounts, inspection counts, metrics
3. **graph** — people, org charts, departments, reporting relationships, project ownership

Given the user's question, produce a retrieval plan as JSON:

{{
  "sources": ["vector"|"sql"|"graph"],  // list of stores to query
  "execution": "parallel"|"sequential",  // parallel if independent; sequential if store A informs store B
  "sequential_order": null or ["sql","graph"],  // only needed if sequential
  "sql_query": "SELECT ...",            // valid SQL for the expenditures/metrics/grants/vacancies tables, or null
  "vector_query": "search terms ...",   // semantic search string, or null
  "graph_query": "MATCH ...",           // Cypher query, or null
  "metadata_filters": {{}},              // department/quarter/year filters extracted from the question
  "reasoning": "..."
}}

SQL schema reference:
- expenditures(id, department, sub_department, account_number, line_item, revised_budget, ytd_expended, quarter, year, source_file)
- metrics(id, department, metric_name, metric_value, metric_unit, quarter, year, source_file)
- grants(id, department, grant_name, grant_number, amount, start_date, end_date, status, source_file)
- vacancies(id, department, position_title, status, quarter, year)

Graph schema:
- Nodes: Person(name, title, department), Department(name), Project(name, status), Grant(name, grant_number, amount), Document(filename, quarter, year)
- Relationships: DIRECTS, MANAGES, REPORTS_TO, HAS_PROJECT, REPORTED_IN, MANAGES_GRANT, MENTIONED_IN

Rules:
- Numeric/budget questions → sql (required), vector (optional for context)
- "Who manages/directs/leads X" → graph required
- Conceptual/narrative questions → vector only
- Cross-store questions → parallel with both required stores
- If the question mentions a specific department, add it to metadata_filters
- If the question mentions a specific quarter/year, add it to metadata_filters

User question: {question}

Return ONLY the JSON object, no explanation.
"""


class QueryClassifier:
    def __init__(self, settings: Optional[Settings] = None, llm: Optional[TrackedAnthropic] = None):
        self.cfg = settings or get_settings()
        self.client = llm or TrackedAnthropic(self.cfg, call_site="query.classifier")

    def classify(self, question: str, query_id: Optional[str] = None) -> QueryPlan:
        """Classify the question and return a retrieval plan."""
        try:
            msg = self.client.messages.create(
                model=self.cfg.synthesis_model,
                max_tokens=1024,
                query_id=query_id,
                messages=[{
                    "role": "user",
                    "content": _CLASSIFY_PROMPT.format(question=question),
                }],
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
    )
