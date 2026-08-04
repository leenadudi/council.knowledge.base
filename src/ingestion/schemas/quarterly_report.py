"""Extraction contract for quarterly reports — one schema, all structured targets.

Field descriptions carry the precision rules (e.g. grants = external awards, not
budget lines) so accuracy is declarative and department-agnostic, not baked into
bespoke per-table prompt code."""
from __future__ import annotations
from typing import Optional
from pydantic import BaseModel, Field


class ExpenditureRow(BaseModel):
    account_number: str = Field("", description="account number from a structured budget/Munis table; not a narrative dollar mention")
    line_item: str = ""
    sub_department: str = ""
    revised_budget: Optional[float] = None
    ytd_expended: Optional[float] = None
    source_text: str
    confidence: str


class MetricRow(BaseModel):
    metric_name: str
    metric_value: float = Field(..., description="explicitly stated count/total/rate; never inferred or calculated")
    metric_unit: str = "count"
    source_text: str
    confidence: str


class GrantRow(BaseModel):
    grant_name: str = Field(..., description="EXTERNAL award to the City (federal/state/county/foundation); NOT a budget line, appropriation, spending figure, or salary")
    grant_number: str = ""
    amount: Optional[float] = Field(None, description="TOTAL award amount to the City, not a spending line")
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    status: str = ""
    source_text: str
    confidence: str


class VacancyRow(BaseModel):
    position_title: str = Field(..., description="role name, singular, WITHOUT the count")
    status: str = Field(..., description='exactly "open" or "filled"')
    count: Optional[int] = Field(None, description="number of open positions for this title, e.g. the parenthesized number in 'Patrol Officer- (25)', else null")
    source_text: str
    confidence: str


class GoalRow(BaseModel):
    goal_title: str = Field(..., description="the goal's heading/name, however the section is labeled (Annual Goals, Objectives, Priorities, or an unlabeled list)")
    description: str = ""
    target: str = Field("", description="only if a quantified aim is stated, else ''")
    status: str = Field("", description="only if progress is stated, else ''")
    source_text: str
    confidence: str


class ProjectRow(BaseModel):
    project_name: str = Field(..., description="a department initiative / special project named in the report, e.g. 'Porch Lights & Ring Doorbells', 'Saturation Details', 'Funding a Forensic Investigator Position'")
    description: str = ""
    status: str = Field("", description="only if stated, else ''")
    funding_source: str = Field("", description="grant/fund name only if stated, else ''")
    source_text: str
    confidence: str


class QuarterlyReportExtraction(BaseModel):
    expenditures: list[ExpenditureRow] = Field(default_factory=list)
    metrics: list[MetricRow] = Field(default_factory=list)
    grants: list[GrantRow] = Field(default_factory=list)
    vacancies: list[VacancyRow] = Field(default_factory=list)
    goals: list[GoalRow] = Field(default_factory=list)
    projects: list[ProjectRow] = Field(default_factory=list)
