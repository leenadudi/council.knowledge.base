"""
Graph extraction: given org_data and project chunks, calls the LLM to extract
people, departments, projects, and relationships for Neo4j ingestion.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

import anthropic

from src.config import Settings, get_settings
from src.models import Chunk

logger = logging.getLogger(__name__)

_GRAPH_EXTRACTION_PROMPT = """You are extracting entities and relationships from a government quarterly report for a knowledge graph.

The source document is from department "{department}", quarter "{quarter}", year {year}.

Extract the following:

1. People — names and roles found in the text (from org charts, signatures, narrative mentions)
2. Departments — government departments or bureaus mentioned
3. Projects — capital projects or special initiatives mentioned
4. Grants — grant programs mentioned
5. Relationships between these entities

Return this JSON structure:
{{
  "people": [
    {{"name": "Full Name", "title": "Job Title", "department": "Department Name"}}
  ],
  "departments": [
    {{"name": "Department Name", "parent_department": null}}
  ],
  "projects": [
    {{"name": "Project Name", "status": "active|completed|planned", "description": "...", "location": "..."}}
  ],
  "grants": [
    {{"name": "Grant Name", "grant_number": "...", "amount": 0.00, "status": "..."}}
  ],
  "relationships": [
    {{
      "from": "Entity Name",
      "from_type": "Person|Department|Project|Grant",
      "relationship": "DIRECTS|MANAGES|REPORTS_TO|HAS_PROJECT|MANAGES_GRANT",
      "to": "Entity Name",
      "to_type": "Person|Department|Project|Grant"
    }}
  ]
}}

Only include entities you are confident about. Omit lists that have no data.
Use only the relationship types listed above.

Text:
---
{text}
---
"""


class GraphExtractor:
    def __init__(self, settings: Optional[Settings] = None):
        self.cfg = settings or get_settings()
        self.client = anthropic.Anthropic(api_key=self.cfg.anthropic_api_key)

    def extract_batch(self, chunks: list[Chunk]) -> dict[str, Any]:
        """
        Extract graph entities from a batch of chunks in one LLM call.
        Returns {people, departments, projects, grants, relationships}.
        """
        if not chunks:
            return {}

        meta = chunks[0].metadata
        combined_text = "\n\n---CHUNK BOUNDARY---\n\n".join(c.text for c in chunks)

        prompt = _GRAPH_EXTRACTION_PROMPT.format(
            department=meta.department,
            quarter=meta.quarter,
            year=meta.year,
            text=combined_text[:6000],
        )

        try:
            msg = self.client.messages.create(
                model=self.cfg.synthesis_model,
                max_tokens=2048,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = msg.content[0].text
            return _parse_graph_response(raw)
        except Exception as e:
            logger.error("Graph extraction failed for batch of %d chunks: %s", len(chunks), e)
            return {}

    def extract_chunks_batched(self, chunks: list[Chunk]) -> dict[str, Any]:
        """Extract from all chunks in batches, merging results."""
        merged: dict[str, list] = {}
        batch_size = self.cfg.extraction_batch_size

        for i in range(0, len(chunks), batch_size):
            batch = chunks[i:i + batch_size]
            result = self.extract_batch(batch)
            for key in ("people", "departments", "projects", "grants", "relationships"):
                merged.setdefault(key, []).extend(result.get(key, []))

        # Deduplicate by name
        for key in ("people", "departments", "projects"):
            seen = set()
            deduped = []
            for item in merged.get(key, []):
                name = item.get("name", "")
                if name and name not in seen:
                    seen.add(name)
                    deduped.append(item)
            merged[key] = deduped

        return merged


def _parse_graph_response(raw: str) -> dict[str, Any]:
    """Parse and validate the JSON returned by the graph extraction LLM."""
    match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", raw)
    json_str = match.group(1) if match else raw

    try:
        data = json.loads(json_str)
    except json.JSONDecodeError:
        logger.warning("Graph extractor returned non-JSON: %s", raw[:200])
        return {}

    result = {}
    for key in ("people", "departments", "projects", "grants", "relationships"):
        items = data.get(key, [])
        if isinstance(items, list):
            result[key] = [i for i in items if isinstance(i, dict)]

    return result
