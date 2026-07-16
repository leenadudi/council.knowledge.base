"""
Flask web app for the Harrisburg Knowledge Base.
Calls the Python pipeline directly — no FastAPI layer.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import random
import threading
import time
import uuid

from flask import Flask, jsonify, redirect, render_template, request, Response

from src.config import get_settings
from src.dashboard.aggregator import DashboardAggregator

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(name)s  %(message)s")
logger = logging.getLogger(__name__)

@app.errorhandler(Exception)
def handle_exception(e):
    logger.exception("Unhandled exception")
    return jsonify({"error": str(e)}), 500

# ── Ingestion job tracking ────────────────────────────────────────────────
_jobs: dict[str, dict] = {}  # job_id -> {queue, status, result}


class _JobLogHandler(logging.Handler):
    """Forwards log records from the ingestion pipeline into a per-job queue."""
    def __init__(self, q: queue.Queue) -> None:
        super().__init__()
        self.q = q

    def emit(self, record: logging.LogRecord) -> None:
        self.q.put(("log", self.format(record)))


# ── Shared instances (initialized once at startup) ────────────────────────
_pipeline = None
_sql_store = None
_evaluator = None
_ready = False
_startup_error: str | None = None


def _init():
    global _pipeline, _sql_store, _evaluator, _ready, _startup_error
    try:
        from src.config import get_settings
        from src.evaluation.evaluator import Evaluator
        from src.query.pipeline import QueryPipeline
        from src.storage.graph_store import GraphStore
        from src.storage.sql_store import SQLStore
        from src.storage.vector_store import VectorStore

        cfg = get_settings()

        vector_store = VectorStore(cfg)
        sql_store = SQLStore(cfg)
        sql_store.connect()
        graph_store = GraphStore(cfg)
        try:
            graph_store.connect()
            graph_store.ensure_constraints()
        except Exception as graph_err:
            logger.warning("Neo4j unavailable, graph features disabled: %s", graph_err)

        _pipeline  = QueryPipeline(vector_store, sql_store, graph_store, cfg)
        _evaluator = Evaluator(cfg)
        _sql_store = sql_store
        # Load data-driven document types (approved via triage) from the DB so both
        # ingestion and query see them without a code deploy. Built-ins always win.
        try:
            from src.ingestion.registry import refresh_from_db
            logger.info("Loaded %d data-driven document type(s)", refresh_from_db(sql_store))
        except Exception as reg_err:
            logger.warning("could not load data-driven document types: %s", reg_err)
        _ready = True
        logger.info("Pipeline ready")
    except Exception as e:
        _startup_error = str(e)
        logger.error("Pipeline startup failed: %s", e)


# ── Initialize pipeline (module-level so Vercel serverless picks it up) ──
_init()


# ── Routes ────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    # New council-facing redesign is the primary app.
    return render_template("redesign.html")


@app.route("/health")
def health():
    if _ready:
        return jsonify({"status": "ok"})
    return jsonify({"status": "error", "detail": _startup_error or "not ready"}), 503


@app.route("/ask", methods=["POST"])
def ask():
    if not _ready:
        return jsonify({"error": _startup_error or "Pipeline not ready"}), 503

    data = request.get_json(silent=True) or {}
    question = (data.get("question") or "").strip()
    if not question:
        return jsonify({"error": "question is required"}), 400

    try:
        response = _pipeline.ask(question)

        # Background quality monitoring (sampled). Isolated so a failure here can
        # never turn a successful answer into a 500 — sampling is accuracy-neutral.
        try:
            from src.evaluation.evaluator import should_sample
            if should_sample(get_settings().eval_sample_rate, random.random()):
                threading.Thread(
                    target=_run_evaluation, args=(response,), daemon=True
                ).start()
        except Exception as e:
            logger.warning("Eval sampling/spawn failed (ignored): %s", e)

        return jsonify(response.model_dump(mode="json"))
    except Exception as e:
        logger.exception("Ask failed")
        return jsonify({"error": str(e)}), 500


@app.route("/admin/costs", methods=["GET"])
def admin_costs():
    if not _ready:
        return jsonify({"error": _startup_error or "not ready"}), 503
    start = request.args.get("start", "1970-01-01")
    end = request.args.get("end", "2100-01-01")
    rows = _sql_store.usage_report(start, end)
    total = round(sum(float(r["est_cost_usd"] or 0) for r in rows), 6)
    return jsonify({
        "start": start,
        "end": end,
        "total_cost_usd": total,
        "by_call_site": rows,
    })


@app.route("/proposals", methods=["GET"])
def proposals():
    """Read-only: pending structured-data type proposals from ingest triage (M1)."""
    if _sql_store is None:
        return jsonify({"error": _startup_error or "not ready"}), 503
    try:
        return jsonify(_sql_store.get_pending_type_proposals())
    except Exception:
        logger.exception("proposals route failed")
        return jsonify({"error": "could not load proposals"}), 500


@app.route("/proposals/<int:proposal_id>/approve", methods=["POST"])
def approve_proposal_route(proposal_id: int):
    """Approve a proposal: create tables + register the type (M3). Schema-mutating."""
    if _sql_store is None:
        return jsonify({"error": _startup_error or "not ready"}), 503
    from src.ingestion import approval
    try:
        return jsonify(approval.approve_proposal(_sql_store, proposal_id))
    except approval.ApprovalError as e:
        return jsonify({"error": str(e)}), 400
    except Exception:
        logger.exception("approve proposal %s failed", proposal_id)
        return jsonify({"error": "approval failed — see server logs"}), 500


@app.route("/proposals/<int:proposal_id>/reject", methods=["POST"])
def reject_proposal_route(proposal_id: int):
    if _sql_store is None:
        return jsonify({"error": _startup_error or "not ready"}), 503
    from src.ingestion import approval
    note = (request.get_json(silent=True) or {}).get("note", "") if request.data else ""
    try:
        return jsonify(approval.reject_proposal(_sql_store, proposal_id, note))
    except approval.ApprovalError as e:
        return jsonify({"error": str(e)}), 400
    except Exception:
        logger.exception("reject proposal %s failed", proposal_id)
        return jsonify({"error": "reject failed — see server logs"}), 500


@app.route("/feedback", methods=["POST"])
def feedback():
    if not _ready:
        return jsonify({"error": "Pipeline not ready"}), 503

    data = request.get_json(silent=True) or {}
    try:
        _sql_store.save_user_feedback(
            query_id=data.get("query_id"),
            feedback=data.get("feedback"),
            category=data.get("failure_category"),
            notes=data.get("notes"),
        )
        return jsonify({"status": "recorded"})
    except Exception as e:
        logger.exception("Feedback failed")
        return jsonify({"error": str(e)}), 500


@app.route("/departments")
def departments():
    if not _ready:
        return jsonify({"error": "Pipeline not ready"}), 503
    try:
        depts = _pipeline.retriever.graph_store.get_all_departments()
        return jsonify({"departments": depts})
    except Exception as e:
        logger.exception("Departments failed")
        return jsonify({"error": str(e)}), 500


@app.route("/departments/<path:department>/staff")
def department_staff(department: str):
    if not _ready:
        return jsonify({"error": "Pipeline not ready"}), 503
    try:
        staff = _pipeline.retriever.graph_store.get_department_staff(department)
        return jsonify({"department": department, "staff": staff})
    except Exception as e:
        logger.exception("Staff lookup failed")
        return jsonify({"error": str(e)}), 500


@app.route("/dashboard")
def dashboard():
    # Unified into the main app: the dashboard now lives as tabs on "/".
    return redirect("/", code=302)


@app.route("/redesign")
def redesign():
    # Preview of the new UI redesign (wired to real data). Promote to "/" when approved.
    if not _ready:
        return jsonify({"error": _startup_error or "not ready"}), 503
    return render_template("redesign.html")


_dashboard_cache: dict = {"at": 0.0, "payload": None}
_DASHBOARD_TTL = 90  # seconds; data only changes on (infrequent) ingest


@app.route("/dashboard/data")
def dashboard_data():
    if not _ready:
        return jsonify({"error": _startup_error or "not ready"}), 503
    try:
        now = time.time()
        if _dashboard_cache["payload"] is None or (now - _dashboard_cache["at"]) > _DASHBOARD_TTL \
                or request.args.get("fresh"):
            _dashboard_cache["payload"] = DashboardAggregator(_sql_store).build()
            _dashboard_cache["at"] = now
        return jsonify(_dashboard_cache["payload"])
    except Exception as e:
        logger.exception("Dashboard data failed")
        return jsonify({"error": str(e)}), 500


# Per-department phrasing cache: findings-hash -> (polished_questions, synthesis_questions).
# Keyed by the content of a department's findings, so identical data is a guaranteed cache
# hit and never re-triggers a Haiku call. Cleared implicitly on restart (cheap to rewarm).
_questions_cache: dict[str, tuple[list[str], list[str]]] = {}


@app.route("/questions/<path:department>")
def questions(department: str):
    if not _ready:
        return jsonify({"error": _startup_error or "not ready"}), 503
    try:
        import hashlib
        from src.dashboard.aggregator import DashboardAggregator
        from src.dashboard.review_questions import ReviewQuestions, phrase_questions

        data = ReviewQuestions(_sql_store).build()
        want = DashboardAggregator._dept_key(department)
        match = next((d for d in data["departments"]
                      if DashboardAggregator._dept_key(d["department"]) == want), None)
        if not match or not match["findings"]:
            return jsonify({"department": department, "questions": [], "polished": False})

        findings = match["findings"]
        templated = [f["question"] for f in findings]
        key = hashlib.sha256(json.dumps(templated, ensure_ascii=False).encode()).hexdigest()

        polished = True
        if key in _questions_cache:
            worded, synthesis = _questions_cache[key]
        else:
            try:
                res = phrase_questions(findings, get_settings())
                worded, synthesis = res["polished"], res["synthesis"]
                _questions_cache[key] = (worded, synthesis)
            except Exception as e:
                logger.warning("Question phrasing failed, using templated: %s", e)
                worded, synthesis, polished = templated, [], False

        out = [{"question": w, "signal": f["signal"], "priority": f.get("priority"),
                "evidence": f["evidence"]}
               for w, f in zip(worded, findings)]
        return jsonify({"department": match["department"], "questions": out,
                        "synthesis": synthesis, "polished": polished})
    except Exception as e:
        logger.exception("Questions failed")
        return jsonify({"error": str(e)}), 500


_GOAL_STATUSES = {"", "not_started", "in_progress", "completed"}


@app.route("/goals/<int:goal_id>/status", methods=["POST"])
def set_goal_status(goal_id: int):
    if not _ready:
        return jsonify({"error": _startup_error or "not ready"}), 503
    data = request.get_json(silent=True) or {}
    status = (data.get("status") or "").strip()
    if status not in _GOAL_STATUSES:
        return jsonify({"error": f"invalid status '{status}'"}), 400
    try:
        with _sql_store.cursor() as cur:
            cur.execute("UPDATE goals SET user_status = %s, user_status_at = NOW() WHERE id = %s",
                        (status or None, goal_id))
            updated = cur.rowcount
        if not updated:
            return jsonify({"error": "goal not found"}), 404
        # Invalidate the dashboard cache so the Next-quarter questions reflect the
        # change on the next /dashboard/data fetch (goal status feeds that generator).
        _dashboard_cache["payload"] = None
        return jsonify({"status": "ok", "id": goal_id, "user_status": status or None})
    except Exception as e:
        logger.exception("Set goal status failed")
        return jsonify({"error": str(e)}), 500


@app.route("/upload", methods=["POST"])
def upload():
    if not _ready:
        return jsonify({"error": "Pipeline not ready"}), 503

    import pathlib
    from src.config import get_settings
    from src.ingestion.metadata import filename_hint
    from src.ingestion.pipeline import IngestionPipeline

    files = request.files.getlist("files")
    if not files or (len(files) == 1 and not files[0].filename):
        return jsonify({"error": "No files provided"}), 400

    docs_dir = pathlib.Path(get_settings().docs_dir)
    docs_dir.mkdir(parents=True, exist_ok=True)

    saved: list[tuple[pathlib.Path, str]] = []
    skipped: list[dict] = []

    for f in files:
        if not f.filename or not f.filename.lower().endswith(".pdf"):
            skipped.append({"filename": f.filename or "unknown", "reason": "Not a PDF"})
            continue
        if _sql_store.is_document_ingested(f.filename):
            skipped.append({"filename": f.filename, "reason": "Already in knowledge base"})
            continue
        file_meta = filename_hint(f.filename)
        if file_meta["department"] and file_meta["quarter"] and file_meta["year"]:
            existing = _sql_store.find_existing_document(
                file_meta["department"], file_meta["quarter"], file_meta["year"]
            )
            if existing:
                skipped.append({"filename": f.filename, "reason": f"Duplicate of '{existing}'"})
                continue
        dest = docs_dir / f.filename
        f.save(str(dest))
        saved.append((dest, f.filename))

    if not saved:
        reasons = "; ".join(f"{s['filename']}: {s['reason']}" for s in skipped)
        return jsonify({"error": f"All files skipped — {reasons}"}), 409

    job_id = str(uuid.uuid4())
    q: queue.Queue = queue.Queue()
    _jobs[job_id] = {"queue": q, "status": "running", "cancelled": False}

    def _run():
        handler = _JobLogHandler(q)
        handler.setFormatter(logging.Formatter("%(levelname)s  %(message)s"))
        src_logger = logging.getLogger("src")
        src_logger.addHandler(handler)
        total = len(saved)
        files_ingested = 0
        total_chunks = 0
        try:
            cancelled = False
            for idx, (dest, filename) in enumerate(saved):
                # Cooperative cancellation — checked between files (a single
                # document's ingest is one long call we don't interrupt).
                if _jobs.get(job_id, {}).get("cancelled"):
                    cancelled = True
                    break
                q.put(("file_start", {"filename": filename, "index": idx, "total": total}))
                try:
                    pipeline = IngestionPipeline(get_settings())
                    pipeline.initialize_stores()
                    chunks = pipeline.ingest_document(dest)
                    files_ingested += 1
                    total_chunks += len(chunks)
                    q.put(("file_done", {"filename": filename, "chunks": len(chunks), "index": idx, "total": total}))
                except Exception as exc:
                    q.put(("file_error", {"filename": filename, "error": str(exc), "index": idx, "total": total}))
            if cancelled:
                _jobs[job_id]["status"] = "cancelled"
                q.put(("cancelled", {
                    "files_ingested": files_ingested,
                    "total_chunks": total_chunks,
                    "remaining": total - files_ingested,
                    "skipped": skipped,
                }))
            else:
                _jobs[job_id]["status"] = "done"
                q.put(("done", {"files_ingested": files_ingested, "total_chunks": total_chunks, "skipped": skipped}))
        finally:
            src_logger.removeHandler(handler)

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"job_id": job_id, "queued": len(saved), "skipped": skipped})


@app.route("/ingest/cancel/<job_id>", methods=["POST"])
def ingest_cancel(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        return jsonify({"error": "Unknown job"}), 404
    job["cancelled"] = True
    return jsonify({"status": "cancelling"})


@app.route("/ingest/stream/<job_id>")
def ingest_stream(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        return jsonify({"error": "Unknown job"}), 404

    def _generate():
        q = job["queue"]
        while True:
            try:
                event_type, data = q.get(timeout=60)
                payload = json.dumps(data) if not isinstance(data, str) else json.dumps(data)
                yield f"event: {event_type}\ndata: {payload}\n\n"
                if event_type in ("done", "error", "cancelled"):
                    _jobs.pop(job_id, None)
                    break
            except queue.Empty:
                yield "event: ping\ndata: {}\n\n"

    return Response(
        _generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/documents/<path:filename>", methods=["DELETE"])
def delete_document(filename: str):
    if not _ready:
        return jsonify({"error": "Pipeline not ready"}), 503
    try:
        from src.storage.vector_store import VectorStore
        from src.config import get_settings
        import pathlib

        _sql_store.delete_document(filename)

        vs = VectorStore(get_settings())
        vs.delete_by_source_file(filename)

        _pipeline.retriever.graph_store.clear_document_data(filename)

        doc_path = pathlib.Path(get_settings().docs_dir) / filename
        if doc_path.exists():
            doc_path.unlink()

        return jsonify({"status": "deleted", "filename": filename})
    except Exception as e:
        logger.exception("Delete failed")
        return jsonify({"error": str(e)}), 500


@app.route("/documents")
def documents():
    if not _ready:
        return jsonify({"error": "Pipeline not ready"}), 503
    try:
        rows = _sql_store.execute_query(
            "SELECT source_file, department, document_type, quarter, year, total_chunks, ingested_at "
            "FROM documents ORDER BY ingested_at DESC"
        )
        return jsonify({"documents": [dict(r) for r in rows]})
    except Exception as e:
        logger.exception("Documents list failed")
        return jsonify({"error": str(e)}), 500


@app.route("/auth", methods=["POST"])
def auth():
    data = request.get_json(silent=True) or {}
    password = (data.get("password") or "").strip()
    admin_pw = os.environ.get("ADMIN_PASSWORD", "")
    if admin_pw and password == admin_pw:
        return jsonify({"ok": True})
    return jsonify({"ok": False}), 401


@app.route("/document-types")
def document_types():
    if not _ready:
        return jsonify({"error": "Pipeline not ready"}), 503
    try:
        rows = _sql_store.execute_query(
            "SELECT type_name, display_name FROM document_type_registry "
            "WHERE active = TRUE ORDER BY display_name"
        )
        return jsonify({"types": [dict(r) for r in rows]})
    except Exception as e:
        logger.exception("Document types failed")
        return jsonify({"error": str(e)}), 500


@app.route("/stats")
def stats():
    if not _ready:
        return jsonify({"error": "Pipeline not ready"}), 503
    try:
        docs    = _sql_store.execute_query("SELECT COUNT(*) as count FROM documents")
        queries = _sql_store.execute_query("SELECT COUNT(*) as count FROM query_logs")
        flagged = _sql_store.execute_query(
            "SELECT COUNT(*) as count FROM chunk_performance WHERE flagged_for_review = true"
        )
        return jsonify({
            "documents_ingested":        docs[0]["count"]    if docs    else 0,
            "queries_answered":          queries[0]["count"] if queries else 0,
            "chunks_flagged_for_review": flagged[0]["count"] if flagged else 0,
        })
    except Exception as e:
        logger.exception("Stats failed")
        return jsonify({"error": str(e)}), 500


# ── Background evaluation ─────────────────────────────────────────────────

def _run_evaluation(response) -> None:
    try:
        if not (_evaluator and _sql_store):
            return
        score = _evaluator.evaluate(response, retrieved_context=response.answer)
        _sql_store.update_query_scores(response.query_id, score.model_dump())
        good = score.accuracy_score >= 3.0
        for citation in response.citations:
            if citation.chunk_id:
                _sql_store.update_chunk_performance(citation.chunk_id, good=good)
    except Exception as e:
        logger.warning("Background evaluation failed: %s", e)


# ── Entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=5001)
