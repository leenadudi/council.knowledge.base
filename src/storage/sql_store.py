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

logger = logging.getLogger(__name__)

psycopg2.extras.register_uuid()


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


def sanitize_vote(value) -> str | None:
    """Trim and cap a vote value so an over-long/garbled value can never crash the
    votes insert (votes.vote is VARCHAR(50))."""
    if value is None:
        return None
    s = str(value).strip()
    return s[:_VOTE_MAXLEN] if s else None


class SQLStore:
    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings or get_settings()
        self._conn: Optional[psycopg2.extensions.connection] = None
        self._in_txn = False

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
                 revised_budget, ytd_expended, quarter, year, source_chunk_id, source_file)
            VALUES
                (%(department)s, %(sub_department)s, %(account_number)s, %(line_item)s,
                 %(revised_budget)s, %(ytd_expended)s, %(quarter)s, %(year)s,
                 %(source_chunk_id)s, %(source_file)s)
        """
        with self.cursor() as cur:
            for row in rows:
                row.setdefault("sub_department", None)
                row.setdefault("account_number", None)
                row["source_chunk_id"] = uuid.UUID(source_chunk_id)
                row["source_file"] = source_file
                cur.execute(sql, row)
        logger.debug("Inserted %d expenditure rows from chunk %s", len(rows), source_chunk_id)

    def insert_metric_rows(self, rows: list[dict[str, Any]], source_chunk_id: str, source_file: str) -> None:
        sql = """
            INSERT INTO metrics
                (department, metric_name, metric_value, metric_unit, quarter, year,
                 source_chunk_id, source_file)
            VALUES
                (%(department)s, %(metric_name)s, %(metric_value)s, %(metric_unit)s,
                 %(quarter)s, %(year)s, %(source_chunk_id)s, %(source_file)s)
        """
        with self.cursor() as cur:
            for row in rows:
                row.setdefault("metric_unit", None)
                row["source_chunk_id"] = uuid.UUID(source_chunk_id)
                row["source_file"] = source_file
                cur.execute(sql, row)

    def insert_grant_rows(self, rows: list[dict[str, Any]], source_chunk_id: str, source_file: str) -> None:
        sql = """
            INSERT INTO grants
                (department, grant_name, grant_number, amount, start_date, end_date,
                 status, source_chunk_id, source_file)
            VALUES
                (%(department)s, %(grant_name)s, %(grant_number)s, %(amount)s,
                 %(start_date)s, %(end_date)s, %(status)s, %(source_chunk_id)s, %(source_file)s)
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

        with self.cursor() as cur:
            for row in rows:
                row.setdefault("grant_number", None)
                row.setdefault("status", None)
                row["start_date"] = _norm_date(row.get("start_date"))
                row["end_date"] = _norm_date(row.get("end_date"))
                row["source_chunk_id"] = uuid.UUID(source_chunk_id)
                row["source_file"] = source_file
                cur.execute(sql, row)

    def insert_vacancy_rows(self, rows: list[dict[str, Any]], source_chunk_id: str) -> None:
        sql = """
            INSERT INTO vacancies
                (department, position_title, status, open_count, quarter, year, source_chunk_id)
            VALUES
                (%(department)s, %(position_title)s, %(status)s, %(open_count)s,
                 %(quarter)s, %(year)s, %(source_chunk_id)s)
        """
        with self.cursor() as cur:
            for row in rows:
                # extractor emits "count"; the column is open_count. Tolerate either.
                row.setdefault("open_count", row.get("count"))
                row["source_chunk_id"] = uuid.UUID(source_chunk_id)
                cur.execute(sql, row)

    def record_document(self, source_file: str, department: str, document_type: str,
                        quarter: str, year: int, parser_used: str, total_chunks: int) -> None:
        sql = """
            INSERT INTO documents (source_file, department, document_type, quarter, year, parser_used, total_chunks)
            VALUES (%(source_file)s, %(department)s, %(document_type)s, %(quarter)s, %(year)s,
                    %(parser_used)s, %(total_chunks)s)
            ON CONFLICT (source_file) DO UPDATE
            SET reingested_at = NOW(), total_chunks = EXCLUDED.total_chunks,
                parser_used = EXCLUDED.parser_used,
                document_type = EXCLUDED.document_type,
                department = EXCLUDED.department,
                quarter = EXCLUDED.quarter,
                year = EXCLUDED.year
        """
        with self.cursor() as cur:
            cur.execute(sql, {
                "source_file": source_file, "department": department,
                "document_type": document_type, "quarter": quarter, "year": year,
                "parser_used": parser_used, "total_chunks": total_chunks,
            })

    def insert_review_flag(self, source_file: str, stage: str, reason: str, detail: str = "") -> None:
        with self.cursor() as cur:
            cur.execute(
                "INSERT INTO review_flags (source_file, stage, reason, detail) "
                "VALUES (%s, %s, %s, %s)",
                (source_file, stage, reason, detail),
            )

    def get_unresolved_review_flags(self) -> list[dict[str, Any]]:
        with self.cursor() as cur:
            cur.execute(
                "SELECT source_file, stage, reason, detail, created_at "
                "FROM review_flags WHERE resolved = FALSE ORDER BY created_at DESC"
            )
            return [dict(r) for r in cur.fetchall()]

    def insert_resolution_rows(self, rows: list[dict], source_chunk_id: str, source_file: str) -> None:
        sql = """
            INSERT INTO resolutions
              (resolution_number, title, amount, vendor, department, adopted_date,
               status, source_chunk_id, source_file)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """
        with self.cursor() as cur:
            for r in rows:
                cur.execute(sql, (
                    r.get("resolution_number"), r.get("title"), r.get("amount"),
                    r.get("vendor"), r.get("department"), r.get("adopted_date") or None,
                    r.get("status"), source_chunk_id, source_file,
                ))

    def insert_vote_rows(self, rows: list[dict], source_chunk_id: str, source_file: str) -> None:
        sql = """
            INSERT INTO votes
              (resolution_number, council_member, vote, source_chunk_id, source_file)
            VALUES (%s,%s,%s,%s,%s)
        """
        with self.cursor() as cur:
            for r in rows:
                cur.execute(sql, (
                    r.get("resolution_number"), r.get("council_member"),
                    sanitize_vote(r.get("vote")), source_chunk_id, source_file,
                ))

    def insert_meeting_rows(self, rows: list[dict], source_chunk_id: str, source_file: str) -> None:
        sql = """
            INSERT INTO meetings
              (meeting_date, session_type, president, members_present,
               members_present_names, members_absent_names, call_to_order, adjourned,
               source_chunk_id, source_file)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """
        with self.cursor() as cur:
            for r in rows:
                cur.execute(sql, (
                    r.get("meeting_date") or None, r.get("session_type"), r.get("president"),
                    r.get("members_present"), r.get("members_present_names"),
                    r.get("members_absent_names"), r.get("call_to_order"), r.get("adjourned"),
                    source_chunk_id, source_file,
                ))

    def insert_meeting_action_rows(self, rows: list[dict], source_chunk_id: str, source_file: str) -> None:
        sql = """
            INSERT INTO meeting_actions
              (meeting_date, item_type, item_number, title, action, committee,
               source_chunk_id, source_file)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
        """
        with self.cursor() as cur:
            for r in rows:
                cur.execute(sql, (
                    r.get("meeting_date") or None, r.get("item_type"), r.get("item_number"),
                    r.get("title"), r.get("action"), r.get("committee"),
                    source_chunk_id, source_file,
                ))

    def insert_legislation_rows(self, rows: list[dict], source_chunk_id: str, source_file: str) -> None:
        sql = """
            INSERT INTO legislation
              (bill_number, title, sponsor, amount, adopted_date, status,
               source_chunk_id, source_file)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
        """
        with self.cursor() as cur:
            for r in rows:
                cur.execute(sql, (
                    r.get("bill_number"), r.get("title"), r.get("sponsor"), r.get("amount"),
                    r.get("adopted_date") or None, r.get("status"),
                    source_chunk_id, source_file,
                ))

    def insert_appropriation_rows(self, rows: list[dict], source_chunk_id: str, source_file: str) -> None:
        sql = """
            INSERT INTO appropriations
              (department, fiscal_year, fund, category, amount, source_chunk_id, source_file)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
        """
        with self.cursor() as cur:
            for r in rows:
                cur.execute(sql, (
                    r.get("department"), r.get("fiscal_year"), r.get("fund"),
                    r.get("category"), r.get("amount"), source_chunk_id, source_file,
                ))

    def insert_goal_rows(self, rows: list[dict], source_chunk_id: str, source_file: str) -> None:
        sql = """
            INSERT INTO goals
              (department, year, quarter, goal_title, description, target, status,
               source_chunk_id, source_file)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """
        with self.cursor() as cur:
            for r in rows:
                cur.execute(sql, (
                    r.get("department"), r.get("year"), r.get("quarter"),
                    r.get("goal_title"), r.get("description"), r.get("target"), r.get("status"),
                    source_chunk_id, source_file,
                ))

    def delete_structured_rows(self, source_file: str) -> None:
        """Delete only the extracted rows for a file (called before re-ingestion to prevent duplicates)."""
        with self.cursor() as cur:
            for table in ["expenditures", "metrics", "grants", "resolutions", "votes",
                          "meetings", "meeting_actions", "legislation", "appropriations", "goals"]:
                cur.execute(f"DELETE FROM {table} WHERE source_file = %s", (source_file,))
            cur.execute("""
                DELETE FROM vacancies WHERE source_chunk_id IN (
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
