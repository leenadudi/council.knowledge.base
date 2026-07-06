"""Extraction contract for budget documents.

Budgets are heterogeneous — a 280-page scanned annual budget, small native
bureau budget presentations, Q&A docs, a veto letter. They all become
searchable via the vector store regardless. The ONLY structured thing worth
extracting (and only where it appears in a clean summary table) is
department-level appropriations: department, fiscal year, fund, amount. The
extractor is expected to return few or zero rows for presentation/narrative
budgets — that is fine.
"""
from __future__ import annotations
from typing import Optional
from pydantic import BaseModel, Field


class AppropriationRow(BaseModel):
    department: str = ""                    # department / bureau the appropriation is for
    fiscal_year: Optional[int] = None       # e.g. 2026
    fund: str = ""                          # e.g. "General Fund", "Capital", "Grants"
    category: str = ""                      # optional line category, if summarized
    amount: Optional[float] = None          # appropriated dollar amount
    source_text: str
    confidence: str


class BudgetExtraction(BaseModel):
    appropriations: list[AppropriationRow] = Field(default_factory=list)
