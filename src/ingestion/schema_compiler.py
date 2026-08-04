"""Compile a data-driven document type's stored column spec into a Pydantic model that
the schema-driven extractor (SQLExtractor.extract_for_type) can use exactly like a
hand-authored schema. New types onboarded via triage/approval live as data (a
document_type_registry row); this turns that data into the extraction contract at runtime.

The top-level model fields are the record-type (SQL table) names, each a list of row
models built from the proposed columns (+ the extraction-metadata fields source_text and
confidence). Standard/managed columns are excluded — they are added by the DDL step and
stamped at insert time, not extracted by the LLM."""
from __future__ import annotations
from typing import Optional
from pydantic import BaseModel, Field, create_model

# Columns the DDL/insert layer owns; never part of the LLM extraction contract.
_STANDARD_COLUMNS = {"id", "source_chunk_id", "source_file", "ingested_at"}


def _py_type(sql_type: str):
    s = (sql_type or "").strip().upper()
    if s.startswith("VARCHAR") or s == "TEXT":
        return str
    if s.startswith("INTEGER") or s == "INT":
        return int
    if s.startswith("DECIMAL") or s.startswith("NUMERIC") or s == "FLOAT":
        return float
    if s == "DATE":
        return str          # dates flow as ISO strings, normalized at insert time
    if s in ("BOOLEAN", "BOOL"):
        return bool
    return str              # safe fallback for anything unrecognized


def compile_type_schema(type_name: str, record_types: list[dict]) -> type[BaseModel]:
    """Build a Pydantic model for a data-driven type. `record_types` is the proposal's
    record_types list (each: name + proposed_columns[{name,type}])."""
    top_fields: dict[str, tuple] = {}
    for rt in record_types:
        row_fields: dict[str, tuple] = {}
        for col in (rt.get("proposed_columns") or []):
            cname = col.get("name")
            if not cname or cname in _STANDARD_COLUMNS:
                continue
            row_fields[cname] = (Optional[_py_type(col.get("type"))], None)
        # extraction metadata the extractor relies on (confidence drives the filter)
        row_fields["source_text"] = (str, ...)
        row_fields["confidence"] = (str, ...)
        row_model = create_model(f"{type_name}__{rt['name']}", **row_fields)
        top_fields[rt["name"]] = (list[row_model], Field(default_factory=list))
    return create_model(type_name, **top_fields)
