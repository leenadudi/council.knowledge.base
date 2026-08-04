"""Guarded DDL generation for data-driven document types (M3).

Approving a proposal may create a NEW table from an LLM-proposed schema. Since a schema
here is not hand-written, this module is the safety boundary: it emits ONLY
`CREATE TABLE IF NOT EXISTS` with validated identifiers and whitelisted column types,
always with the standard managed columns, and never any destructive statement
(no DROP/ALTER/…). Anything outside the whitelist raises DDLError — the caller must not
execute unvalidated SQL."""
from __future__ import annotations
import re

# Column types the agent may propose (matches the triage prompt's allowed set).
_ALLOWED_TYPE = re.compile(
    r"^(TEXT|VARCHAR\(\d{1,4}\)|INTEGER|DECIMAL\(\d{1,2},\d{1,2}\)|DATE|BOOLEAN)$",
    re.IGNORECASE,
)
_IDENT = re.compile(r"^[a-z][a-z0-9_]{0,62}$")   # lowercase snake_case, starts alpha
_RESERVED = {
    "select", "from", "where", "table", "drop", "alter", "insert", "update", "delete",
    "user", "order", "group", "join", "index", "grant", "create", "into", "values",
}
# Managed columns the store owns; agent-proposed versions are ignored/deduped.
_STANDARD = ("id", "source_chunk_id", "source_file", "ingested_at")
_STANDARD_DDL = (
    "id SERIAL PRIMARY KEY",
    "source_chunk_id UUID",
    "source_file VARCHAR(255)",
    "ingested_at TIMESTAMP DEFAULT NOW()",
)


class DDLError(ValueError):
    """Raised when a proposed table/column would produce unsafe or invalid DDL."""


def _valid_ident(name: str) -> bool:
    return bool(name) and bool(_IDENT.match(name)) and name.lower() not in _RESERVED


def build_create_table(table_name: str, columns: list[dict]) -> str:
    """Return a safe CREATE TABLE statement (+ source_file index) for `table_name` with
    the given domain `columns` ([{name, type}]). Raises DDLError on any violation."""
    if not _valid_ident(table_name):
        raise DDLError(f"invalid table name: {table_name!r}")

    domain_defs, seen = [], set()
    for col in columns:
        name = (col or {}).get("name", "")
        ctype = str((col or {}).get("type", "")).strip()
        if name in _STANDARD:            # managed column — the standard set covers it
            continue
        if not _valid_ident(name):
            raise DDLError(f"invalid column name: {name!r}")
        if not _ALLOWED_TYPE.match(ctype):
            raise DDLError(f"column {name!r} has non-whitelisted type: {ctype!r}")
        if name in seen:
            continue
        seen.add(name)
        domain_defs.append(f"{name} {ctype.upper()}")

    if not domain_defs:
        raise DDLError("no valid domain columns to create")

    all_defs = ",\n    ".join(list(_STANDARD_DDL) + domain_defs)
    return (
        f"CREATE TABLE IF NOT EXISTS {table_name} (\n    {all_defs}\n);\n"
        f"CREATE INDEX IF NOT EXISTS idx_{table_name}_source_file "
        f"ON {table_name}(source_file);"
    )
