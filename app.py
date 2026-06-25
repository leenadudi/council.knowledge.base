"""
Flask web app for the Harrisburg Knowledge Base.
Calls the Python pipeline directly — no FastAPI layer.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import uuid

from flask import Flask, jsonify, render_template, request, Response

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
    return render_template("index.html")


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

        threading.Thread(
            target=_run_evaluation, args=(response,), daemon=True
        ).start()

        return jsonify(response.model_dump(mode="json"))
    except Exception as e:
        logger.exception("Ask failed")
        return jsonify({"error": str(e)}), 500


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


@app.route("/upload", methods=["POST"])
def upload():
    if not _ready:
        return jsonify({"error": "Pipeline not ready"}), 503

    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    f = request.files["file"]
    if not f.filename or not f.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Only PDF files are supported"}), 400

    import pathlib
    from src.config import get_settings
    from src.ingestion.metadata import extract_file_metadata
    from src.ingestion.pipeline import IngestionPipeline

    # Reject if this exact file was already ingested
    if _sql_store.is_document_ingested(f.filename):
        return jsonify({"error": f"'{f.filename}' is already in the knowledge base."}), 409

    # Reject if another document for the same department/quarter/year is already ingested
    file_meta = extract_file_metadata(f.filename)
    if file_meta["department"] and file_meta["quarter"] and file_meta["year"]:
        existing = _sql_store.find_existing_document(
            file_meta["department"], file_meta["quarter"], file_meta["year"]
        )
        if existing:
            return jsonify({
                "error": f"{file_meta['department']} {file_meta['quarter']} {file_meta['year']} is already ingested as '{existing}'."
            }), 409

    docs_dir = pathlib.Path(get_settings().docs_dir)
    docs_dir.mkdir(parents=True, exist_ok=True)
    dest = docs_dir / f.filename
    f.save(str(dest))

    job_id = str(uuid.uuid4())
    q: queue.Queue = queue.Queue()
    _jobs[job_id] = {"queue": q, "status": "running"}

    def _run():
        handler = _JobLogHandler(q)
        handler.setFormatter(logging.Formatter("%(levelname)s  %(message)s"))
        src_logger = logging.getLogger("src")
        src_logger.addHandler(handler)
        try:
            pipeline = IngestionPipeline(get_settings())
            pipeline.initialize_stores()
            chunks = pipeline.ingest_document(dest)
            _jobs[job_id]["status"] = "done"
            q.put(("done", {"filename": f.filename, "chunks": len(chunks)}))
        except Exception as exc:
            _jobs[job_id]["status"] = "error"
            q.put(("error", str(exc)))
        finally:
            src_logger.removeHandler(handler)

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"job_id": job_id})


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
                if event_type in ("done", "error"):
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
