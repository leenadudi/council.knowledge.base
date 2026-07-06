"""Extraction contract for City Council legislation (ordinances / bills).

Legislation is the near-twin of a resolution: the City uses the same
Legislative Approval Form, and the docs are scanned (OCR) the same way. The
difference is the identifier (BILL NO. rather than RESOLUTION NO.) and a richer
status set (introduced / amended / signed / vetoed / veto-overridden). Per the
product decision, individual member votes are not tracked — only the outcome,
captured in `status`.
"""
from __future__ import annotations
from typing import Optional
from pydantic import BaseModel, Field


class LegislationRow(BaseModel):
    bill_number: str                       # e.g. "1-2026"
    title: str = ""                        # short subject of the ordinance/bill
    sponsor: str = ""                       # introducing council member(s), if stated
    amount: Optional[float] = None          # appropriation amount, if the bill authorizes one
    adopted_date: Optional[str] = None      # YYYY-MM-DD — date signed/enacted, if stated
    status: str = ""                        # introduced | amended | signed | vetoed | veto_overridden | passed
    source_text: str
    confidence: str


class LegislationExtraction(BaseModel):
    legislation: list[LegislationRow] = Field(default_factory=list)
