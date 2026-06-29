"""Extraction contract for quarterly reports — mirrors the existing SQL extractor output."""
from __future__ import annotations
from typing import Optional
from pydantic import BaseModel, Field


class ExpenditureRow(BaseModel):
    account_number: str = ""
    line_item: str = ""
    sub_department: str = ""
    revised_budget: Optional[float] = None
    ytd_expended: Optional[float] = None
    source_text: str
    confidence: str


class MetricRow(BaseModel):
    metric_name: str
    metric_value: float
    metric_unit: str = "count"
    source_text: str
    confidence: str


class GrantRow(BaseModel):
    grant_name: str
    grant_number: str = ""
    amount: Optional[float] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    status: str = ""
    source_text: str
    confidence: str


class VacancyRow(BaseModel):
    position_title: str
    status: str
    source_text: str
    confidence: str


class QuarterlyReportExtraction(BaseModel):
    expenditures: list[ExpenditureRow] = Field(default_factory=list)
    metrics: list[MetricRow] = Field(default_factory=list)
    grants: list[GrantRow] = Field(default_factory=list)
    vacancies: list[VacancyRow] = Field(default_factory=list)
