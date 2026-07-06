"""Extraction contract for City Council legislative session minutes.

Minutes follow a fixed agenda; we structure two things: the meeting record
(date, session type, presiding officer, attendance, times) and the actions
taken on resolutions / ordinances (the connective tissue linking a session to
the resolutions and legislation tables). The full narrative (public comment,
new business) stays searchable via the vector store and is not extracted here.
"""
from __future__ import annotations
from typing import Optional
from pydantic import BaseModel, Field


class MeetingRow(BaseModel):
    meeting_date: Optional[str] = None          # YYYY-MM-DD
    session_type: str = ""                       # "Legislative Session", "Special Legislative Session"
    president: str = ""                          # presiding officer
    members_present: Optional[int] = None        # count present at roll call
    members_present_names: str = ""              # comma-separated
    members_absent_names: str = ""               # comma-separated ("" if all present)
    call_to_order: str = ""                      # e.g. "6:00PM"
    adjourned: str = ""                          # e.g. "7:01PM"
    source_text: str
    confidence: str


class MeetingActionRow(BaseModel):
    item_type: str = ""      # "resolution" | "ordinance" | "minutes_approval" | "other"
    item_number: str = ""    # e.g. "1-2026" (bare number; "" if none)
    title: str = ""          # short title / subject read into record
    action: str = ""         # "read into record", "referred to committee", "final passage", "first reading", "approved"
    committee: str = ""      # committee referred to, if any
    source_text: str
    confidence: str


class MinutesExtraction(BaseModel):
    meetings: list[MeetingRow] = Field(default_factory=list)
    meeting_actions: list[MeetingActionRow] = Field(default_factory=list)
