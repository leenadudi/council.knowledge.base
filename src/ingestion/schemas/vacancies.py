"""Extraction contract for position vacancies.

Quarterly reports carry a 'Department Vacancy Updates' / 'Vacancies' section listing
open (or recently filled) positions, usually with a COUNT in parentheses, e.g.
"Patrol Officer- (25)", "Supervisors- (4)", "Civilian — (2)". The count is the number
of positions of that title — capturing it preserves the magnitude of the shortage,
which the old position-title-only schema discarded.
"""
from __future__ import annotations
from typing import Optional
from pydantic import BaseModel, Field


class VacancyRow(BaseModel):
    position_title: str                   # role name, singular, without the count
    status: str                           # "open" | "filled"
    count: Optional[int] = None           # number of positions stated for this title, or null
    source_text: str
    confidence: str


class VacanciesExtraction(BaseModel):
    vacancies: list[VacancyRow] = Field(default_factory=list)
