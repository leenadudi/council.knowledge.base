"""Strict extraction contract for grants.

A grant here is an EXTERNAL funding award to the City (federal / state / county /
foundation) — money the City receives or has applied for, identified by a grant
name and a total award amount. This deliberately EXCLUDES budget line items,
appropriations, department spending/expenditure figures, and internal transfers,
which is what the old generic extractor over-counted as "grants".
"""
from __future__ import annotations
from typing import Optional
from pydantic import BaseModel, Field


class GrantRow(BaseModel):
    grant_name: str                      # the grant/program name (e.g. "PennDOT Green Light Go Grant")
    grant_number: str = ""
    amount: Optional[float] = None        # TOTAL award amount to the City (not a spending line)
    start_date: Optional[str] = None      # YYYY-MM-DD or null
    end_date: Optional[str] = None        # YYYY-MM-DD or null
    status: str = ""                      # active | pending | closed | awarded | applied
    source_text: str
    confidence: str


class GrantsExtraction(BaseModel):
    grants: list[GrantRow] = Field(default_factory=list)
