"""Extraction contract for city council resolutions."""
from __future__ import annotations
from typing import Optional
from pydantic import BaseModel, Field


class ResolutionRow(BaseModel):
    resolution_number: str
    title: str = ""
    amount: Optional[float] = None
    vendor: str = ""
    department: str = ""
    adopted_date: Optional[str] = None
    status: str = ""
    source_text: str
    confidence: str


class VoteRow(BaseModel):
    resolution_number: str
    council_member: str
    vote: str            # "yes" | "no" | "abstain"
    source_text: str
    confidence: str


class ResolutionExtraction(BaseModel):
    resolutions: list[ResolutionRow] = Field(default_factory=list)
    votes: list[VoteRow] = Field(default_factory=list)
