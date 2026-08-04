"""PostgreSQL store for structured data (expenditures, metrics, grants, vacancies)."""

from __future__ import annotations

import json
import logging
import uuid
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

import psycopg2
import psycopg2.extras

from src.config import Settings, get_settings
from src.ingestion.entity_keys import (
    department_key, member_key, vendor_key, infer_dept_type,
)
from src.ingestion.legislative_events import (
    LegislativeEvent, doc_status_events, item_key, item_kind, parse_events,
)

logger = logging.getLogger(__name__)

psycopg2.extras.register_uuid()

# Bounded VARCHAR limits across the structured tables. The schema-driven extractor can
# emit an over-long value (e.g. a "status" written as a sentence, or a council_member
# rendered as a whole clause); without this guard a single row raises
# StringDataRightTruncation and — now that the inserts are transactional — rolls back
# the whole document. Keyed by column name (same-named columns share the tightest limit;
# clipping to the tightest is always safe). TEXT columns (goals.*, titles) are not listed.
_VARCHAR_LIMITS: dict[str, int] = {
    # quarterly-report tables
    "department": 100, "sub_department": 100, "account_number": 50, "line_item": 200,
    "metric_name": 200, "metric_unit": 50, "grant_name": 255, "grant_number": 100,
    "status": 50, "position_title": 200, "project_name": 300, "funding_source": 200,
    "quarter": 5,
    # council / legislation / meeting / appropriation tables
    "resolution_number": 50, "vendor": 255, "council_member": 120, "sponsor": 255,
    "bill_number": 50, "session_type": 60, "president": 120, "call_to_order": 20,
    "adjourned": 20, "item_type": 30, "item_number": 50, "action": 150, "committee": 120,
    "fund": 100, "category": 150,
}


def _clip(row: dict) -> dict:
    """Truncate over-long string fields to their column's VARCHAR limit, in place.
    Logs each truncation so it can be reviewed. Prevents StringDataRightTruncation."""
    for key, limit in _VARCHAR_LIMITS.items():
        v = row.get(key)
        if isinstance(v, str) and len(v) > limit:
            logger.warning("truncating %s: %d→%d chars (%r)", key, len(v), limit, v[:60])
            row[key] = v[:limit]
    return row


def _json_default(o: Any):
    """Serialize types pulled from Postgres (Decimal, date) that json can't handle."""
    if isinstance(o, Decimal):
        return float(o)
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    if isinstance(o, uuid.UUID):
        return str(o)
    return str(o)


def _dumps(obj: Any) -> str:
    return json.dumps(obj, default=_json_default)


_VOTE_MAXLEN = 50

# alias-table FK column + (canonical_name/alias width, canonical_key width) per
# dimension table, mirroring the VARCHAR limits in sql/schema.sql. Enforced in
# _resolve_entity so a pathologically long extracted value can't blow past ANY of
# those column widths and raise "value too long" mid-resolve. canonical_key is
# often NARROWER than canonical_name (departments: 120 vs 150), so it needs its
# own bound — a long, punctuation-sparse name yields an equally long key.
_ALIAS_FK = {
    "departments": ("department_aliases", "department_id", 150, 120),
    "council_members": ("council_member_aliases", "member_id", 150, 150),
    "vendors": ("vendor_aliases", "vendor_id", 200, 200),
}
_NEAR_DUP_THRESHOLD = 0.6  # pg_trgm similarity; one tunable constant
_DEPT_FK_TABLES = [
    "expenditures", "metrics", "grants", "vacancies", "documents", "resolutions",
    "appropriations", "goals", "document_chunks", "evaluation_suite",
]


def sanitize_vote(value) -> str | None:
    """Trim and cap a vote value so an over-long/garbled value can never crash the
    votes insert (votes.vote is VARCHAR(50))."""
    if value is None:
        return None
    s = str(value).strip()
    return s[:_VOTE_MAXLEN] if s else None


class SQLStore:
    # Class-level default so instances built via SQLStore.__new__ (test doubles that
    # bypass __init__ — e.g. tests/storage/test_insert_self_safe.py) still have a
    # legal value for _legislative_tracking_enabled() to read; __init__ below sets
    # the same default as an instance attribute for real instances.
    _legis_enabled: Optional[bool] = None

    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings or get_settings()
        self._conn: Optional[psycopg2.extensions.connection] = None
        self._in_txn = False
        self._legis_enabled: Optional[bool] = None

    def connect(self) -> None:
        self._conn = psycopg2.connect(
            self.settings.database_url,
            cursor_factory=psycopg2.extras.RealDictCursor,
        )
        self._conn.autocommit = False

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def _get_live_conn(self) -> psycopg2.extensions.connection:
        """Return a live connection, reconnecting if the server dropped the idle one."""
        if self._conn and not self._conn.closed:
            try:
                with self._conn.cursor() as ping:
                    ping.execute("SELECT 1")
                self._conn.rollback()
                return self._conn
            except Exception:
                pass
        self.connect()
        return self._conn

    def _txn_conn(self) -> psycopg2.extensions.connection:
        """Connection for a transaction / nested cursor: reuse the open connection
        directly. Skips the _get_live_conn ping, whose rollback would abort the
        in-progress transaction. Reconnects only if there is no open connection."""
        if self._conn and not self._conn.closed:
            return self._conn
        return self._get_live_conn()

    @contextmanager
    def cursor(self):
        # Inside a transaction() block, reuse the live connection directly and defer
        # the commit to transaction(); pinging mid-transaction would abort the work.
        conn = self._txn_conn() if self._in_txn else self._get_live_conn()
        cur = conn.cursor()
        try:
            yield cur
            if not self._in_txn:
                conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()

    @contextmanager
    def transaction(self):
        """Group multiple insert calls into one atomic commit. cursor() calls inside
        this block defer their commit; the whole block commits once on success or
        rolls back entirely on any exception."""
        conn = self._txn_conn()
        self._in_txn = True
        try:
            yield
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._in_txn = False

    # ------------------------------------------------------------------
    # Ingestion methods
    # ------------------------------------------------------------------

    def insert_expenditure_rows(self, rows: list[dict[str, Any]], source_chunk_id: str, source_file: str) -> None:
        sql = """
            INSERT INTO expenditures
                (department, sub_department, account_number, line_item,
                 revised_budget, ytd_expended, quarter, year, source_chunk_id, source_file,
                 department_id)
            VALUES
                (%(department)s, %(sub_department)s, %(account_number)s, %(line_item)s,
                 %(revised_budget)s, %(ytd_expended)s, %(quarter)s, %(year)s,
                 %(source_chunk_id)s, %(source_file)s, %(department_id)s)
        """
        # Resolve FKs BEFORE opening the insert cursor (not per-row inside it).
        # resolve_department_id opens its own self.cursor(); OUTSIDE a transaction that
        # path calls _get_live_conn(), whose "reconnect if idle" defensive rollback would
        # discard rows already cur.execute()'d earlier in this same batch. Resolving up
        # front (here and in EVERY resolver-using insert method) makes each method
        # self-safe whether or not the caller wrapped a sql_store.transaction().
        for row in rows:
            row.setdefault("sub_department", None)
            row.setdefault("account_number", None)
            row["source_chunk_id"] = uuid.UUID(source_chunk_id)
            row["source_file"] = source_file
            row["department_id"] = self.resolve_department_id(row.get("department"))
        with self.cursor() as cur:
            for row in rows:
                cur.execute(sql, _clip(row))
        logger.debug("Inserted %d expenditure rows from chunk %s", len(rows), source_chunk_id)

    def insert_metric_rows(self, rows: list[dict[str, Any]], source_chunk_id: str, source_file: str) -> None:
        sql = """
            INSERT INTO metrics
                (department, metric_name, metric_value, metric_unit, quarter, year,
                 source_chunk_id, source_file, department_id)
            VALUES
                (%(department)s, %(metric_name)s, %(metric_value)s, %(metric_unit)s,
                 %(quarter)s, %(year)s, %(source_chunk_id)s, %(source_file)s, %(department_id)s)
        """
        # See insert_expenditure_rows: resolve before opening the cursor (this path
        # also runs outside sql_store.transaction()).
        for row in rows:
            row.setdefault("metric_unit", None)
            row["source_chunk_id"] = uuid.UUID(source_chunk_id)
            row["source_file"] = source_file
            row["department_id"] = self.resolve_department_id(row.get("department"))
        with self.cursor() as cur:
            for row in rows:
                cur.execute(sql, _clip(row))

    def insert_grant_rows(self, rows: list[dict[str, Any]], source_chunk_id: str, source_file: str) -> None:
        sql = """
            INSERT INTO grants
                (department, grant_name, grant_number, amount, start_date, end_date,
                 status, source_chunk_id, source_file, department_id)
            VALUES
                (%(department)s, %(grant_name)s, %(grant_number)s, %(amount)s,
                 %(start_date)s, %(end_date)s, %(status)s, %(source_chunk_id)s, %(source_file)s,
                 %(department_id)s)
        """
        def _norm_date(v):
            # accept only full YYYY-MM-DD; coerce year-only / garbage to NULL
            if not v:
                return None
            try:
                datetime.strptime(str(v), "%Y-%m-%d")
                return str(v)
            except (ValueError, TypeError):
                return None

        # See insert_expenditure_rows: resolve before opening the cursor (this path
        # also runs outside sql_store.transaction()).
        for row in rows:
            row.setdefault("grant_number", None)
            row.setdefault("status", None)
            row["start_date"] = _norm_date(row.get("start_date"))
            row["end_date"] = _norm_date(row.get("end_date"))
            row["source_chunk_id"] = uuid.UUID(source_chunk_id)
            row["source_file"] = source_file
            row["department_id"] = self.resolve_department_id(row.get("department"))
        with self.cursor() as cur:
            for row in rows:
                cur.execute(sql, _clip(row))

    def insert_vacancy_rows(self, rows: list[dict[str, Any]], source_chunk_id: str, source_file: str) -> None:
        sql = """
            INSERT INTO vacancies
                (department, position_title, status, open_count, quarter, year, source_chunk_id,
                 source_file, department_id)
            VALUES
                (%(department)s, %(position_title)s, %(status)s, %(open_count)s,
                 %(quarter)s, %(year)s, %(source_chunk_id)s, %(source_file)s, %(department_id)s)
        """
        # See insert_expenditure_rows: resolve before opening the cursor (this path
        # also runs outside sql_store.transaction()).
        for row in rows:
            # extractor emits "count"; the column is open_count. Tolerate either.
            row.setdefault("open_count", row.get("count"))
            row["source_chunk_id"] = uuid.UUID(source_chunk_id)
            row["source_file"] = source_file
            row["department_id"] = self.resolve_department_id(row.get("department"))
        with self.cursor() as cur:
            for row in rows:
                cur.execute(sql, _clip(row))

    def record_document(self, source_file: str, department: str, document_type: str,
                        quarter: str, year: int, parser_used: str, total_chunks: int) -> None:
        # Not called inside a rows loop (one document per call) and not nested inside
        # any other open cursor, so resolving before the upsert is always safe here.
        self.ensure_document_type(document_type)
        department_id = self.resolve_department_id(department)
        sql = """
            INSERT INTO documents (source_file, department, document_type, quarter, year, parser_used,
                                    total_chunks, department_id)
            VALUES (%(source_file)s, %(department)s, %(document_type)s, %(quarter)s, %(year)s,
                    %(parser_used)s, %(total_chunks)s, %(department_id)s)
            ON CONFLICT (source_file) DO UPDATE
            SET reingested_at = NOW(), total_chunks = EXCLUDED.total_chunks,
                parser_used = EXCLUDED.parser_used,
                document_type = EXCLUDED.document_type,
                department = EXCLUDED.department,
                quarter = EXCLUDED.quarter,
                year = EXCLUDED.year,
                department_id = EXCLUDED.department_id
        """
        with self.cursor() as cur:
            cur.execute(sql, {
                "source_file": source_file, "department": department,
                "document_type": document_type, "quarter": quarter, "year": year,
                "parser_used": parser_used, "total_chunks": total_chunks,
                "department_id": department_id,
            })

    def insert_review_flag(self, source_file: str, stage: str, reason: str, detail: str = "") -> None:
        with self.cursor() as cur:
            cur.execute(
                "INSERT INTO review_flags (source_file, stage, reason, detail) "
                "VALUES (%s, %s, %s, %s)",
                (source_file, stage, reason, detail),
            )

    # ------------------------------------------------------------------
    # Entity resolvers
    # ------------------------------------------------------------------

    def _flag(self, cur, stage, reason, detail):
        cur.execute(
            "INSERT INTO review_flags (source_file, stage, reason, detail) "
            "VALUES ('entity-resolver', %s, %s, %s)", (stage, reason, detail))

    def _flag_near_dup(self, cur, table, key, raw):
        cur.execute(
            f"SELECT canonical_name FROM {table} "
            f"WHERE similarity(canonical_key, %s) > %s AND canonical_key <> %s "
            f"ORDER BY similarity(canonical_key, %s) DESC LIMIT 1",
            (key, _NEAR_DUP_THRESHOLD, key, key))
        row = cur.fetchone()
        if row:
            self._flag(cur, "validate", f"possible_duplicate_{table[:-1]}",
                       f"'{raw}' resembles existing '{row['canonical_name']}'")

    def _resolve_entity(self, table, raw, key, display, *, create, extra_cols=None):
        if not key:
            return None
        alias_table, fk, maxlen, key_maxlen = _ALIAS_FK[table]
        # Truncate BEFORE any lookup or INSERT: an over-long value would otherwise
        # exceed the canonical_name/alias/canonical_key VARCHAR width and raise
        # "value too long", aborting whatever batch/transaction called this resolver.
        # `key` is bounded ONCE here so the canonical_key SELECTs and the INSERT all
        # use the same value — a name that would overflow can't crash, and two names
        # sharing the truncated prefix resolve to one entity (fine for pathological
        # inputs).
        raw = raw[:maxlen]
        display = display[:maxlen]
        key = key[:key_maxlen]
        with self.cursor() as cur:
            cur.execute(
                f"SELECT {fk} AS id FROM {alias_table} WHERE LOWER(alias) = LOWER(%s)", (raw,))
            row = cur.fetchone()
            if row:
                return row["id"]
            cur.execute(f"SELECT id FROM {table} WHERE canonical_key = %s", (key,))
            row = cur.fetchone()
            if row:
                cur.execute(
                    f"INSERT INTO {alias_table} ({fk}, alias) VALUES (%s, %s) "
                    f"ON CONFLICT (alias) DO NOTHING", (row["id"], raw))
                return row["id"]
            if not create:
                self._flag(cur, "validate", f"unmapped_{table[:-1]}", raw)
                return None
            self._flag_near_dup(cur, table, key, raw)
            cols = ["canonical_name", "canonical_key"] + list((extra_cols or {}).keys())
            vals = [display, key] + list((extra_cols or {}).values())
            ph = ", ".join(["%s"] * len(vals))
            cur.execute(
                f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({ph}) "
                f"ON CONFLICT (canonical_key) DO NOTHING RETURNING id", vals)
            row = cur.fetchone()
            if row is None:  # concurrent create won the race
                cur.execute(f"SELECT id FROM {table} WHERE canonical_key = %s", (key,))
                row = cur.fetchone()
            entity_id = row["id"]
            cur.execute(
                f"INSERT INTO {alias_table} ({fk}, alias) VALUES (%s, %s) "
                f"ON CONFLICT (alias) DO NOTHING", (entity_id, raw))
            return entity_id

    def resolve_department_id(self, name, *, create=True):
        return self._resolve_entity(
            "departments", name, department_key(name), (name or "").strip(),
            create=create, extra_cols={"dept_type": infer_dept_type(name)})

    def resolve_member_id(self, name, *, create=True):
        from src.ingestion.names import normalize_person_name
        display = normalize_person_name(name)
        return self._resolve_entity(
            "council_members", name, member_key(name), display, create=create)

    def resolve_vendor_id(self, name, *, create=True):
        return self._resolve_entity(
            "vendors", name, vendor_key(name), (name or "").strip(), create=create)

    def _merge_simple(self, cur, dim_table, alias_table, alias_fk, fk_targets, dup_id, keep_id):
        """Repoint aliases + all FK columns to keep_id, then deactivate dup_id.

        Args:
            cur: Database cursor.
            dim_table: Main dimension table (e.g., 'departments').
            alias_table: Alias table for this dimension (e.g., 'department_aliases').
            alias_fk: FK column name in alias_table (e.g., 'department_id').
            fk_targets: List of (table, column) tuples referencing this dimension.
            dup_id: ID of the duplicate row to merge away.
            keep_id: ID of the canonical row to keep.
        """
        if dup_id == keep_id:
            return
        cur.execute(
            f"UPDATE {alias_table} SET {alias_fk} = %s WHERE {alias_fk} = %s", (keep_id, dup_id))
        for tbl, col in fk_targets:
            cur.execute(f"UPDATE {tbl} SET {col} = %s WHERE {col} = %s", (keep_id, dup_id))
        cur.execute(f"UPDATE {dim_table} SET active = FALSE WHERE id = %s", (dup_id,))

    def merge_department(self, dup_id, keep_id):
        """Merge a duplicate department into the canonical one."""
        with self.cursor() as cur:
            self._merge_simple(
                cur, "departments", "department_aliases", "department_id",
                [(t, "department_id") for t in _DEPT_FK_TABLES], dup_id, keep_id)

    def merge_member(self, dup_id, keep_id):
        """Merge a duplicate council member into the canonical one."""
        with self.cursor() as cur:
            self._merge_simple(
                cur, "council_members", "council_member_aliases", "member_id",
                [("votes", "member_id"), ("meetings", "president_id"),
                 ("legislation", "sponsor_id"), ("meeting_attendance", "member_id")],
                dup_id, keep_id)

    def merge_vendor(self, dup_id, keep_id):
        """Merge a duplicate vendor into the canonical one."""
        with self.cursor() as cur:
            self._merge_simple(
                cur, "vendors", "vendor_aliases", "vendor_id",
                [("resolutions", "vendor_id")], dup_id, keep_id)

    def ensure_document_type(self, type_name):
        if not type_name:
            return
        with self.cursor() as cur:
            cur.execute(
                "INSERT INTO document_type_registry (type_name, display_name, added_by) "
                "VALUES (%s, %s, 'auto') ON CONFLICT (type_name) DO NOTHING",
                (type_name, type_name))

    def get_unresolved_review_flags(self) -> list[dict[str, Any]]:
        with self.cursor() as cur:
            cur.execute(
                "SELECT source_file, stage, reason, detail, created_at "
                "FROM review_flags WHERE resolved = FALSE ORDER BY created_at DESC"
            )
            return [dict(r) for r in cur.fetchall()]

    def insert_type_proposal(self, source_file: str, proposed_type: str, payload: dict) -> None:
        """Queue an agent-proposed structured-data type/mapping for human review."""
        with self.cursor() as cur:
            cur.execute(
                "INSERT INTO type_proposals (source_file, proposed_type, payload) "
                "VALUES (%s, %s, %s)",
                (source_file, proposed_type, psycopg2.extras.Json(payload)),
            )

    def get_pending_type_proposals(self) -> list[dict[str, Any]]:
        with self.cursor() as cur:
            cur.execute(
                "SELECT id, source_file, proposed_type, payload, created_at "
                "FROM type_proposals WHERE status = 'pending' ORDER BY created_at DESC"
            )
            return [dict(r) for r in cur.fetchall()]

    def get_type_proposal(self, proposal_id: int) -> Optional[dict[str, Any]]:
        with self.cursor() as cur:
            cur.execute("SELECT id, source_file, proposed_type, payload, status "
                        "FROM type_proposals WHERE id = %s", (proposal_id,))
            row = cur.fetchone()
            return dict(row) if row else None

    def set_type_proposal_status(self, proposal_id: int, status: str, note: str = "") -> None:
        with self.cursor() as cur:
            cur.execute(
                "UPDATE type_proposals SET status = %s, reviewed_at = NOW(), reviewer_note = %s "
                "WHERE id = %s",
                (status, note, proposal_id),
            )

    def execute_ddl(self, ddl: str) -> None:
        """Run a DDL string (CREATE TABLE / INDEX). Callers MUST pass only DDL built by
        src.storage.ddl.build_create_table, which validates identifiers/types and never
        emits destructive statements."""
        with self.cursor() as cur:
            cur.execute(ddl)

    def upsert_document_type(self, type_name: str, description: str,
                             extraction_templates: dict, sql_tables: list[str],
                             graph_node_types: Optional[list[str]] = None) -> None:
        """Register (or replace) a data-driven document type. Delete-then-insert keeps it
        idempotent (type_name has no unique constraint)."""
        with self.cursor() as cur:
            cur.execute("DELETE FROM document_type_registry WHERE type_name = %s", (type_name,))
            cur.execute(
                "INSERT INTO document_type_registry "
                "(type_name, display_name, description, extraction_templates, sql_tables, "
                " graph_node_types, added_by, active) "
                "VALUES (%s, %s, %s, %s, %s, %s, 'triage', TRUE)",
                (type_name, type_name, description,
                 psycopg2.extras.Json(extraction_templates),
                 list(sql_tables), list(graph_node_types or [])),
            )

    def insert_dynamic_rows(self, table: str, rows: list[dict[str, Any]],
                            source_chunk_id: str, source_file: str) -> None:
        """Insert rows into a data-driven (approved) table. Table name is identifier-
        validated; only columns that actually exist on the table are written (values are
        parameterized); strings are clipped to each column's VARCHAR limit."""
        from src.storage.ddl import _valid_ident
        if not _valid_ident(table):
            raise ValueError(f"refusing to insert into invalid table name: {table!r}")
        with self.cursor() as cur:
            cur.execute(
                "SELECT column_name, character_maximum_length FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = %s", (table,))
            meta = {r["column_name"]: r["character_maximum_length"] for r in cur.fetchall()}
        if not meta:
            raise ValueError(f"table {table!r} does not exist")
        writable = set(meta) - {"id", "ingested_at"}
        with self.cursor() as cur:
            for row in rows:
                data = {k: v for k, v in row.items() if k in writable}
                data["source_chunk_id"] = uuid.UUID(source_chunk_id)
                data["source_file"] = source_file
                for k, v in list(data.items()):          # generic length clip
                    lim = meta.get(k)
                    if isinstance(v, str) and lim and len(v) > lim:
                        data[k] = v[:lim]
                cols = list(data.keys())
                sql = (f"INSERT INTO {table} (" + ", ".join(cols) + ") VALUES ("
                       + ", ".join(f"%({c})s" for c in cols) + ")")
                cur.execute(sql, data)

    # ---- Legislative status tracking (lifecycle events; spec 2026-07-29) ----

    def _legislative_tracking_enabled(self) -> bool:
        """True once the legislative-status migration has been applied. Cached per
        process; False on any probe error so ingest keeps working on an
        un-migrated database."""
        if self._legis_enabled is None:
            try:
                with self.cursor() as cur:
                    cur.execute("SELECT to_regclass('legislative_items') AS t")
                    row = cur.fetchone()
                    self._legis_enabled = bool(row and row["t"])
            except Exception:
                logger.warning("legislative_items probe failed; tracking disabled", exc_info=True)
                self._legis_enabled = False
        return self._legis_enabled

    def resolve_legislative_item(self, kind: str, raw_number: str, title: str = "",
                                 event_date: str | None = None) -> int | None:
        """Get-or-create the legislative_items row for (kind, canonical number).
        Keeps the longest title and earliest first_seen across sightings."""
        key = item_key(raw_number)
        if not kind or not key:
            return None
        with self.cursor() as cur:
            cur.execute(
                """
                INSERT INTO legislative_items (item_kind, item_key, display_number, title, first_seen)
                VALUES (%s, %s, %s, NULLIF(%s, ''), %s)
                ON CONFLICT (item_kind, item_key) DO UPDATE SET
                    title = CASE WHEN length(coalesce(EXCLUDED.title, '')) > length(coalesce(legislative_items.title, ''))
                                 THEN EXCLUDED.title ELSE legislative_items.title END,
                    first_seen = LEAST(coalesce(legislative_items.first_seen, EXCLUDED.first_seen),
                                       coalesce(EXCLUDED.first_seen, legislative_items.first_seen))
                RETURNING id
                """,
                (kind, key, (raw_number or "").strip(), title or "", event_date or None),
            )
            return cur.fetchone()["id"]

    def _insert_legislative_events(self, item_id: int, events: list[LegislativeEvent], *,
                                   event_date: str | None, origin: str,
                                   meeting_action_id: int | None,
                                   source_chunk_id: str | None, source_file: str | None) -> None:
        sql = """
            INSERT INTO legislative_events
              (legislative_item_id, event, event_date, seq, origin, meeting_action_id,
               committee, ayes, nays, note, source_chunk_id, source_file)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (meeting_action_id, seq) DO NOTHING
        """
        with self.cursor() as cur:
            for seq, ev in enumerate(events):
                cur.execute(sql, (
                    item_id, ev.event, event_date or None, seq, origin, meeting_action_id,
                    ev.committee or None, ev.ayes, ev.nays, ev.note or None,
                    source_chunk_id, source_file,
                ))

    def insert_resolution_rows(self, rows: list[dict], source_chunk_id: str, source_file: str) -> None:
        tracking = self._legislative_tracking_enabled()
        legacy_sql = """
            INSERT INTO resolutions
              (resolution_number, title, amount, vendor, department, adopted_date,
               status, source_chunk_id, source_file, department_id, vendor_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """
        tracked_sql = """
            INSERT INTO resolutions
              (resolution_number, title, amount, vendor, department, adopted_date,
               status, source_chunk_id, source_file, department_id, vendor_id,
               legislative_item_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """
        # Resolve ALL FKs — entity dimensions (schema) + legislative item (status
        # tracking) — and precompute any lifecycle events, BEFORE opening the insert
        # cursor: every resolver here opens its own self.cursor(), and outside a
        # transaction that path pings+rolls back the shared connection (see
        # insert_expenditure_rows), which would discard rows already inserted earlier
        # in this batch. Resolving up front makes this self-safe whether or not the
        # caller wrapped a sql_store.transaction(). doc_status_events()/LegislativeEvent
        # are pure (no cursor), so they're computed here too, on the raw pre-clip row,
        # matching what they'd have parsed before _clip() touches "status" below.
        for r in rows:
            r["department_id"] = self.resolve_department_id(r.get("department"))
            r["vendor_id"] = self.resolve_vendor_id(r.get("vendor"))
            if tracking:
                r["legislative_item_id"] = self.resolve_legislative_item(
                    "resolution", r.get("resolution_number") or "",
                    r.get("title") or "", r.get("adopted_date") or None)
                r["_legis_events"] = [LegislativeEvent(e) for e in doc_status_events(r.get("status"))]
            else:
                r["legislative_item_id"] = None
                r["_legis_events"] = []
        with self.cursor() as cur:
            for r in rows:
                _clip(r)
                base = (
                    r.get("resolution_number"), r.get("title"), r.get("amount"),
                    r.get("vendor"), r.get("department"), r.get("adopted_date") or None,
                    r.get("status"), source_chunk_id, source_file,
                    r.get("department_id"), r.get("vendor_id"),
                )
                if tracking:
                    cur.execute(tracked_sql, base + (r["legislative_item_id"],))
                else:
                    cur.execute(legacy_sql, base)
        # Event emission opens its own cursor (_insert_legislative_events), so it MUST
        # run after the insert cursor above has closed — same self-safe reason.
        if tracking:
            for r in rows:
                if r["legislative_item_id"] and r["_legis_events"]:
                    self._insert_legislative_events(
                        r["legislative_item_id"], r["_legis_events"],
                        event_date=r.get("adopted_date") or None,
                        origin="document", meeting_action_id=None,
                        source_chunk_id=source_chunk_id, source_file=source_file)

    def insert_vote_rows(self, rows: list[dict], source_chunk_id: str, source_file: str) -> None:
        tracking = self._legislative_tracking_enabled()
        legacy_sql = """
            INSERT INTO votes
              (resolution_number, council_member, vote, source_chunk_id, source_file, member_id)
            VALUES (%s,%s,%s,%s,%s,%s)
        """
        tracked_sql = """
            INSERT INTO votes
              (resolution_number, council_member, vote, source_chunk_id, source_file, member_id,
               legislative_item_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
        """
        # Resolve before opening the cursor — self-safe (see insert_resolution_rows).
        for r in rows:
            r["member_id"] = self.resolve_member_id(r.get("council_member"))
            r["legislative_item_id"] = (
                self.resolve_legislative_item("resolution", r.get("resolution_number") or "")
                if tracking else None
            )
        with self.cursor() as cur:
            for r in rows:
                _clip(r)
                base = (
                    r.get("resolution_number"), r.get("council_member"),
                    sanitize_vote(r.get("vote")), source_chunk_id, source_file, r.get("member_id"),
                )
                if tracking:
                    cur.execute(tracked_sql, base + (r["legislative_item_id"],))
                else:
                    cur.execute(legacy_sql, base)

    def insert_meeting_rows(self, rows: list[dict], source_chunk_id: str, source_file: str) -> None:
        sql = """
            INSERT INTO meetings
              (meeting_date, session_type, president, members_present,
               members_present_names, members_absent_names, call_to_order, adjourned,
               source_chunk_id, source_file, president_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id
        """
        # Resolve ALL member FKs (president + attendance) BEFORE opening the insert
        # cursor — self-safe (see insert_resolution_rows). meeting_id comes from
        # RETURNING id inside the cursor, so we can't pre-insert attendance, but we CAN
        # pre-resolve every (member_id, status) pair; the in-cursor step is then pure
        # junction INSERTs with no resolver calls. prepped[i] =
        # (row, president_id, attendance_pairs, expected_present).
        prepped = [
            (r,
             self.resolve_member_id(r.get("president")),
             self._resolve_attendance(r.get("members_present_names"), r.get("members_absent_names")),
             r.get("members_present"))
            for r in rows
        ]
        with self.cursor() as cur:
            for r, president_id, pairs, expected_present in prepped:
                _clip(r)
                cur.execute(sql, (
                    r.get("meeting_date") or None, r.get("session_type"), r.get("president"),
                    r.get("members_present"), r.get("members_present_names"),
                    r.get("members_absent_names"), r.get("call_to_order"), r.get("adjourned"),
                    source_chunk_id, source_file, president_id,
                ))
                meeting_id = cur.fetchone()["id"]
                self._write_attendance(cur, meeting_id, pairs, expected_present)

    def _resolve_attendance(self, present_names, absent_names):
        """Parse the comma-separated present/absent name lists into resolved
        (member_id, status) pairs. Calls resolve_member_id (which opens its own
        cursor), so this MUST run OUTSIDE the meetings insert cursor."""
        from src.ingestion.names import normalize_person_name
        pairs = []
        for names, status in ((present_names, "present"), (absent_names, "absent")):
            for raw in (names or "").split(","):
                clean = normalize_person_name(raw)
                if not clean:
                    continue
                pairs.append((self.resolve_member_id(clean), status))
        return pairs

    def _write_attendance(self, cur, meeting_id, pairs, expected_present):
        """Insert PRE-RESOLVED (member_id, status) attendance pairs for one meeting.
        No resolver calls here, so it is safe to run inside the meetings insert cursor."""
        seen = 0
        for member_id, status in pairs:
            cur.execute(
                "INSERT INTO meeting_attendance (meeting_id, member_id, status) "
                "VALUES (%s, %s, %s) ON CONFLICT (meeting_id, member_id) DO NOTHING",
                (meeting_id, member_id, status))
            if status == "present":
                seen += 1
        if expected_present is not None and seen != expected_present:
            self._flag(cur, "validate", "attendance_count_mismatch",
                       f"meeting {meeting_id}: parsed {seen} present vs stated {expected_present}")

    def insert_meeting_action_rows(self, rows: list[dict], source_chunk_id: str, source_file: str) -> None:
        tracking = self._legislative_tracking_enabled()
        legacy_sql = """
            INSERT INTO meeting_actions
              (meeting_date, item_type, item_number, title, action, committee,
               source_chunk_id, source_file)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
        """
        tracked_sql = """
            INSERT INTO meeting_actions
              (meeting_date, item_type, item_number, title, action, committee,
               source_chunk_id, source_file, legislative_item_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id
        """
        # Resolve the legislative item and precompute its lifecycle events BEFORE
        # opening the insert cursor — self-safe (see insert_resolution_rows). The
        # event rows still need the new meeting_actions.id (only known after INSERT),
        # so that piece is filled in during the insert pass below; the event INSERT
        # itself stays deferred to the post-pass, same as everywhere else.
        for r in rows:
            if tracking:
                kind = item_kind(r.get("item_type"), r.get("item_number"))
                r["legislative_item_id"] = self.resolve_legislative_item(
                    kind, r.get("item_number") or "", r.get("title") or "",
                    r.get("meeting_date") or None) if kind else None
                r["_legis_events"] = (
                    parse_events(r.get("item_type"), r.get("action"))
                    if r["legislative_item_id"] else [])
            else:
                r["legislative_item_id"] = None
                r["_legis_events"] = []
        with self.cursor() as cur:
            for r in rows:
                _clip(r)
                if tracking:
                    cur.execute(tracked_sql, (
                        r.get("meeting_date") or None, r.get("item_type"), r.get("item_number"),
                        r.get("title"), r.get("action"), r.get("committee"),
                        source_chunk_id, source_file, r["legislative_item_id"],
                    ))
                    r["_legis_action_id"] = cur.fetchone()["id"]
                else:
                    cur.execute(legacy_sql, (
                        r.get("meeting_date") or None, r.get("item_type"), r.get("item_number"),
                        r.get("title"), r.get("action"), r.get("committee"),
                        source_chunk_id, source_file,
                    ))
        # Event emission (and the unmapped-action review flag) opens its own cursor,
        # so it MUST run after the insert cursor above has closed — same self-safe
        # reason.
        if tracking:
            for r in rows:
                if r["_legis_events"]:
                    self._insert_legislative_events(
                        r["legislative_item_id"], r["_legis_events"],
                        event_date=r.get("meeting_date") or None,
                        origin="minutes", meeting_action_id=r.get("_legis_action_id"),
                        source_chunk_id=source_chunk_id, source_file=source_file)
                elif r["legislative_item_id"] and (r.get("action") or "").strip():
                    self.insert_review_flag(
                        source_file, "normalize", "unmapped meeting action",
                        f"{r.get('item_type')} {r.get('item_number')}: {r.get('action')}")

    def insert_legislation_rows(self, rows: list[dict], source_chunk_id: str, source_file: str) -> None:
        tracking = self._legislative_tracking_enabled()
        legacy_sql = """
            INSERT INTO legislation
              (bill_number, title, sponsor, amount, adopted_date, status,
               source_chunk_id, source_file, sponsor_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """
        tracked_sql = """
            INSERT INTO legislation
              (bill_number, title, sponsor, amount, adopted_date, status,
               source_chunk_id, source_file, sponsor_id, legislative_item_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """
        # Resolve before opening the cursor — self-safe (see insert_resolution_rows).
        # match-only (create=False): a "sponsor" may be a committee name, which must
        # NOT be auto-created as a council member.
        for r in rows:
            r["sponsor_id"] = self.resolve_member_id(r.get("sponsor"), create=False)
            if tracking:
                r["legislative_item_id"] = self.resolve_legislative_item(
                    "ordinance", r.get("bill_number") or "",
                    r.get("title") or "", r.get("adopted_date") or None)
                r["_legis_events"] = [LegislativeEvent(e) for e in doc_status_events(r.get("status"))]
            else:
                r["legislative_item_id"] = None
                r["_legis_events"] = []
        with self.cursor() as cur:
            for r in rows:
                _clip(r)
                base = (
                    r.get("bill_number"), r.get("title"), r.get("sponsor"), r.get("amount"),
                    r.get("adopted_date") or None, r.get("status"),
                    source_chunk_id, source_file, r.get("sponsor_id"),
                )
                if tracking:
                    cur.execute(tracked_sql, base + (r["legislative_item_id"],))
                else:
                    cur.execute(legacy_sql, base)
        # Event emission opens its own cursor (_insert_legislative_events), so it MUST
        # run after the insert cursor above has closed — same self-safe reason.
        if tracking:
            for r in rows:
                if r["legislative_item_id"] and r["_legis_events"]:
                    self._insert_legislative_events(
                        r["legislative_item_id"], r["_legis_events"],
                        event_date=r.get("adopted_date") or None,
                        origin="document", meeting_action_id=None,
                        source_chunk_id=source_chunk_id, source_file=source_file)

    def insert_appropriation_rows(self, rows: list[dict], source_chunk_id: str, source_file: str) -> None:
        sql = """
            INSERT INTO appropriations
              (department, fiscal_year, fund, category, amount, source_chunk_id, source_file,
               department_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
        """
        # Resolve before opening the cursor — self-safe (see insert_resolution_rows).
        for r in rows:
            r["department_id"] = self.resolve_department_id(r.get("department"))
        with self.cursor() as cur:
            for r in rows:
                _clip(r)
                cur.execute(sql, (
                    r.get("department"), r.get("fiscal_year"), r.get("fund"),
                    r.get("category"), r.get("amount"), source_chunk_id, source_file,
                    r.get("department_id"),
                ))

    def insert_goal_rows(self, rows: list[dict], source_chunk_id: str, source_file: str) -> None:
        sql = """
            INSERT INTO goals
              (department, year, quarter, goal_title, description, target, status,
               source_chunk_id, source_file, department_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """
        # Resolve before opening the cursor — self-safe (see insert_resolution_rows).
        for r in rows:
            r["department_id"] = self.resolve_department_id(r.get("department"))
        with self.cursor() as cur:
            for r in rows:
                cur.execute(sql, (
                    r.get("department"), r.get("year"), r.get("quarter"),
                    r.get("goal_title"), r.get("description"), r.get("target"), r.get("status"),
                    source_chunk_id, source_file, r.get("department_id"),
                ))

    def insert_project_rows(self, rows: list[dict], source_chunk_id: str, source_file: str) -> None:
        sql = """
            INSERT INTO projects
              (department, project_name, description, status, funding_source,
               quarter, year, source_chunk_id, source_file)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """
        with self.cursor() as cur:
            for r in rows:
                _clip(r)
                cur.execute(sql, (
                    r.get("department"), r.get("project_name"), r.get("description"),
                    r.get("status"), r.get("funding_source"), r.get("quarter"),
                    r.get("year"), source_chunk_id, source_file,
                ))

    def delete_structured_rows(self, source_file: str) -> None:
        """Delete only the extracted rows for a file (called before re-ingestion to
        prevent duplicates). All tables — vacancies included, since it now carries
        source_file — delete by file. The legacy fallback below also clears any old
        vacancy rows whose source_file predates the 2026-07-14 backfill (still NULL).
        legislative_events is included only once the legislative-status migration has
        been applied (see _legislative_tracking_enabled); legislative_items itself is
        NOT deleted here since — like departments/vendors — it's a cross-document
        canonical entity, not a per-file extraction."""
        from src.storage.ddl import _valid_ident
        builtin = ["expenditures", "metrics", "grants", "vacancies", "resolutions",
                   "votes", "meetings", "meeting_actions", "legislation",
                   "appropriations", "goals", "projects"]
        if self._legislative_tracking_enabled():
            # Before meeting_actions: document-origin events match by source_file;
            # minutes-origin events also cascade via meeting_action_id.
            builtin.insert(0, "legislative_events")
        with self.cursor() as cur:
            for table in builtin:
                cur.execute(f"DELETE FROM {table} WHERE source_file = %s", (source_file,))
            # Data-driven type tables (created at approval time) also delete by source_file,
            # so re-ingesting a doc replaces its rows rather than duplicating them. Names
            # come from our own registry but are identifier-validated + existence-checked.
            cur.execute("SELECT DISTINCT unnest(sql_tables) AS t FROM document_type_registry "
                        "WHERE active = TRUE")
            for row in cur.fetchall():
                t = row["t"]
                if t in builtin or not _valid_ident(t):
                    continue
                cur.execute("SELECT to_regclass(%s) AS r", (f"public.{t}",))
                if cur.fetchone()["r"] is None:
                    continue
                cur.execute(f"DELETE FROM {t} WHERE source_file = %s", (source_file,))
            # Fallback for pre-migration rows that never got a source_file backfilled.
            cur.execute("""
                DELETE FROM vacancies
                WHERE source_file IS NULL AND source_chunk_id IN (
                    SELECT chunk_id FROM document_chunks WHERE source_file = %s
                )
            """, (source_file,))

    def delete_document(self, source_file: str) -> None:
        """Remove a document and all its structured data rows."""
        self.delete_structured_rows(source_file)
        with self.cursor() as cur:
            cur.execute("DELETE FROM documents WHERE source_file = %s", (source_file,))

    def is_document_ingested(self, source_file: str) -> bool:
        with self.cursor() as cur:
            cur.execute("SELECT 1 FROM documents WHERE source_file = %s", (source_file,))
            return cur.fetchone() is not None

    def find_existing_document(self, department: str, quarter: str, year: int) -> Optional[str]:
        """Return the source_file already ingested for this department/quarter/year, or None."""
        with self.cursor() as cur:
            cur.execute(
                "SELECT source_file FROM documents WHERE department = %s AND quarter = %s AND year = %s LIMIT 1",
                (department, quarter, year),
            )
            row = cur.fetchone()
            return row["source_file"] if row else None

    # ------------------------------------------------------------------
    # Query execution
    # ------------------------------------------------------------------

    def execute_query(self, sql_query: str) -> list[dict[str, Any]]:
        """Execute a read-only SQL query and return rows as dicts."""
        with self.cursor() as cur:
            cur.execute(sql_query)
            rows = cur.fetchall()
            return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Query logging
    # ------------------------------------------------------------------

    def log_query(self, log: dict[str, Any]) -> None:
        sql = """
            INSERT INTO query_logs (
                query_id, question, timestamp, classification, sql_query,
                chunks_retrieved, stores_queried, sql_results, vector_results,
                graph_results, final_answer, citations, total_time_ms,
                clarity_assessment
            ) VALUES (
                %(query_id)s, %(question)s, %(timestamp)s, %(classification)s, %(sql_query)s,
                %(chunks_retrieved)s, %(stores_queried)s, %(sql_results)s, %(vector_results)s,
                %(graph_results)s, %(final_answer)s, %(citations)s, %(total_time_ms)s,
                %(clarity_assessment)s
            )
        """
        with self.cursor() as cur:
            cur.execute(sql, {
                "query_id": uuid.UUID(log["query_id"]),
                "question": log.get("question"),
                "timestamp": log.get("timestamp"),
                "classification": _dumps(log.get("classification")),
                "sql_query": log.get("sql_query"),
                "chunks_retrieved": _dumps(log.get("chunks_retrieved")),
                "stores_queried": log.get("stores_queried"),
                "sql_results": _dumps(log.get("sql_results")),
                "vector_results": _dumps(log.get("vector_results")),
                "graph_results": _dumps(log.get("graph_results")),
                "final_answer": log.get("final_answer"),
                "citations": _dumps(log.get("citations")),
                "total_time_ms": log.get("total_time_ms"),
                "clarity_assessment": _dumps(log.get("clarity_assessment")),
            })

    def insert_llm_usage(self, record: dict[str, Any]) -> None:
        sql = """
            INSERT INTO llm_usage (
                id, call_site, model, input_tokens, output_tokens,
                cache_read_tokens, cache_write_tokens, est_cost_usd, latency_ms,
                query_id, batch_id
            ) VALUES (
                %(id)s, %(call_site)s, %(model)s, %(input_tokens)s, %(output_tokens)s,
                %(cache_read_tokens)s, %(cache_write_tokens)s, %(est_cost_usd)s, %(latency_ms)s,
                %(query_id)s, %(batch_id)s
            )
        """
        with self.cursor() as cur:
            cur.execute(sql, record)

    def update_query_scores(self, query_id: str, scores: dict[str, Any]) -> None:
        with self.cursor() as cur:
            cur.execute(
                """UPDATE query_logs
                   SET retrieval_score=%s, accuracy_score=%s, completeness_score=%s
                   WHERE query_id=%s""",
                (scores["retrieval_score"], scores["accuracy_score"],
                 scores["completeness_score"], uuid.UUID(query_id)),
            )

    def save_user_feedback(self, query_id: str, feedback: str,
                           category: Optional[str], notes: Optional[str]) -> None:
        with self.cursor() as cur:
            cur.execute(
                """UPDATE query_logs
                   SET user_feedback=%s, user_notes=%s
                   WHERE query_id=%s""",
                (feedback, f"{category}: {notes}" if category else notes, uuid.UUID(query_id)),
            )

    # ------------------------------------------------------------------
    # Chunk performance
    # ------------------------------------------------------------------

    def update_chunk_performance(self, chunk_id: str, good: bool) -> None:
        with self.cursor() as cur:
            cur.execute(
                """INSERT INTO chunk_performance (chunk_id, times_retrieved, times_good_answer, times_bad_answer, last_retrieved)
                   VALUES (%s, 1, %s, %s, NOW())
                   ON CONFLICT (chunk_id) DO UPDATE
                   SET times_retrieved = chunk_performance.times_retrieved + 1,
                       times_good_answer = chunk_performance.times_good_answer + %s,
                       times_bad_answer = chunk_performance.times_bad_answer + %s,
                       last_retrieved = NOW(),
                       quality_score = CASE
                           WHEN (chunk_performance.times_retrieved + 1) >= 5
                           THEN (chunk_performance.times_good_answer + %s)::decimal /
                                (chunk_performance.times_retrieved + 1) * 5
                           ELSE chunk_performance.quality_score
                       END,
                       flagged_for_review = CASE
                           WHEN (chunk_performance.times_retrieved + 1) >= 5
                                AND ((chunk_performance.times_good_answer + %s)::decimal /
                                     (chunk_performance.times_retrieved + 1) * 5) < 2.5
                           THEN TRUE
                           ELSE chunk_performance.flagged_for_review
                       END""",
                (uuid.UUID(chunk_id),
                 1 if good else 0, 0 if good else 1,
                 1 if good else 0, 0 if good else 1,
                 1 if good else 0, 1 if good else 0),
            )

    # ------------------------------------------------------------------
    # Evaluation suite
    # ------------------------------------------------------------------

    def get_evaluation_suite(self) -> list[dict]:
        with self.cursor() as cur:
            cur.execute("SELECT * FROM evaluation_suite ORDER BY id")
            return [dict(r) for r in cur.fetchall()]

    def save_evaluation_result(self, run_id: str, result: dict) -> None:
        sql = """
            INSERT INTO evaluation_results
                (run_id, run_date, question_id, question, expected_answer, actual_answer,
                 retrieval_score, accuracy_score, completeness_score, passed, notes)
            VALUES
                (%(run_id)s, NOW(), %(question_id)s, %(question)s, %(expected_answer)s,
                 %(actual_answer)s, %(retrieval_score)s, %(accuracy_score)s,
                 %(completeness_score)s, %(passed)s, %(notes)s)
        """
        with self.cursor() as cur:
            cur.execute(sql, {"run_id": uuid.UUID(run_id), **result})

    def get_low_scoring_queries(self, min_score: float = 3.0, quarter: Optional[str] = None) -> list[dict]:
        params = [min_score]
        where = "retrieval_score < %s OR accuracy_score < %s OR completeness_score < %s"
        params.extend([min_score, min_score])
        if quarter:
            where += " AND timestamp >= NOW() - INTERVAL '3 months'"
        with self.cursor() as cur:
            cur.execute(f"SELECT * FROM query_logs WHERE {where} ORDER BY timestamp DESC", params)
            return [dict(r) for r in cur.fetchall()]

    def usage_report(self, start: str, end: str) -> list[dict[str, Any]]:
        sql = """
            SELECT call_site, model,
                   COUNT(*)                  AS calls,
                   SUM(input_tokens)         AS input_tokens,
                   SUM(output_tokens)        AS output_tokens,
                   SUM(cache_read_tokens)    AS cache_read_tokens,
                   SUM(cache_write_tokens)   AS cache_write_tokens,
                   SUM(est_cost_usd)         AS est_cost_usd
            FROM llm_usage
            WHERE timestamp >= %s AND timestamp < %s
            GROUP BY call_site, model
            ORDER BY est_cost_usd DESC NULLS LAST
        """
        with self.cursor() as cur:
            cur.execute(sql, (start, end))
            rows = []
            for r in cur.fetchall():
                row = dict(r)
                row["est_cost_usd"] = float(row["est_cost_usd"]) if row["est_cost_usd"] is not None else 0.0
                rows.append(row)
            return rows
