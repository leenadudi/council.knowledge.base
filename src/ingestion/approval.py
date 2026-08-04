"""Approve (or reject) a triage type proposal (M3).

Approving makes a proposed type real: create any NEW tables (guarded DDL), register the
type in document_type_registry as data (so it extracts + is queryable with no deploy),
mark the proposal approved, and refresh the live registry. FIT record-types map into an
existing table — no DDL, the mapping is preserved in the registered extraction template.

This is the only path that mutates schema, and it runs only on an explicit human approve."""
from __future__ import annotations
import logging

from src.ingestion import registry
from src.storage.ddl import build_create_table

logger = logging.getLogger(__name__)


class ApprovalError(RuntimeError):
    pass


def _description(payload: dict) -> str:
    parts = [payload.get("proposed_type_name", "")]
    for rt in payload.get("record_types", []):
        parts.append(f"{rt.get('name','')}: {rt.get('description','')}".strip(": ").strip())
    return " — ".join(p for p in parts if p)[:2000]


def approve_proposal(store, proposal_id: int) -> dict:
    """Create tables + register the type + mark approved. Returns a summary dict."""
    prop = store.get_type_proposal(proposal_id)
    if prop is None:
        raise ApprovalError(f"proposal {proposal_id} not found")
    if prop["status"] != "pending":
        raise ApprovalError(f"proposal {proposal_id} is {prop['status']}, not pending")

    payload = prop["payload"]
    type_name = payload.get("proposed_type_name") or prop.get("proposed_type") or "unknown_type"
    record_types = payload.get("record_types") or []
    if not record_types:
        raise ApprovalError("proposal has no record types")

    created, mapped, sql_tables = [], [], []
    for rt in record_types:
        if rt.get("target") == "existing" and rt.get("existing_table"):
            mapped.append(rt["existing_table"])
            sql_tables.append(rt["existing_table"])
        else:
            ddl = build_create_table(rt["name"], rt.get("proposed_columns") or [])
            store.execute_ddl(ddl)          # DDLError propagates if unsafe/invalid
            created.append(rt["name"])
            sql_tables.append(rt["name"])

    # dedupe, preserve order
    sql_tables = list(dict.fromkeys(sql_tables))
    store.upsert_document_type(
        type_name=type_name, description=_description(payload),
        extraction_templates={"record_types": record_types}, sql_tables=sql_tables,
    )
    store.set_type_proposal_status(proposal_id, "approved")
    registry.refresh_from_db(store)
    logger.info("approved proposal %d as type %r (created=%s mapped=%s)",
                proposal_id, type_name, created, mapped)
    return {"type_name": type_name, "created_tables": created, "mapped_tables": mapped}


def reject_proposal(store, proposal_id: int, note: str = "") -> dict:
    prop = store.get_type_proposal(proposal_id)
    if prop is None:
        raise ApprovalError(f"proposal {proposal_id} not found")
    store.set_type_proposal_status(proposal_id, "rejected", note)
    return {"proposal_id": proposal_id, "status": "rejected"}
