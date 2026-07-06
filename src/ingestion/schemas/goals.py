"""Extraction contract for department goals stated in quarterly reports.

Quarterly reports carry an "Annual Goals" / "20XX Goals" section: named goals
(a short title) with a narrative description, sometimes a quantified target
(e.g. "65% capacity", "15% increase") and sometimes a progress/status note.
Format varies by department, so this is LLM-extracted with a verbatim source
quote and a confidence tag per goal.
"""
from __future__ import annotations
from typing import Optional
from pydantic import BaseModel, Field


class GoalRow(BaseModel):
    goal_title: str = ""          # short name of the goal, e.g. "Increase Programs Hosted"
    description: str = ""         # what the goal is (1-2 sentences)
    target: str = ""             # quantified target if stated, else "" (e.g. "65% capacity")
    status: str = ""             # progress/status if stated, else "" (e.g. "in progress", "achieved")
    source_text: str
    confidence: str


class GoalsExtraction(BaseModel):
    goals: list[GoalRow] = Field(default_factory=list)
