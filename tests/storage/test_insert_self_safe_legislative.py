"""Self-safe regression guard for the COMBINED entity-FK + legislative-event path.

Companion to tests/storage/test_insert_self_safe.py. That file's FakeCursor makes
`_legislative_tracking_enabled()`'s `to_regclass` probe FALSY, so its tests only
ever exercise the untracked insert path — the legislative-item resolution and,
critically, the post-batch `_insert_legislative_events(...)` emission never run
there. tests/storage/test_legislative_tracking.py DOES run the tracked path, but
its FakeConn.commit/rollback are no-ops, so it cannot detect a self-safe violation.

This file closes that gap: it drives the REAL cursor()/_get_live_conn() code with
legislative tracking ENABLED (the probe returns truthy) against the same
rollback-detecting FakeConn as test_insert_self_safe.py — one whose rollback()
flags `rolled_back_a_pending_insert` when it discards a still-pending INSERT batch.

The invariant under guard (spec 2026-07-29 + the 2026-07-28 entity-FK merge): every
insert_*_rows method resolves all FKs / legislative items in a pre-pass, runs the
pure INSERTs in one batched cursor, and emits lifecycle events in a post-pass AFTER
that cursor has closed. `_insert_legislative_events()` opens its own self.cursor();
outside a transaction that path pings + rolls back the shared connection, discarding
any rows still pending in the batch. So a future refactor that moves
`_insert_legislative_events(...)` (or an item resolver) back INSIDE the batched
insert cursor must fail (a)/(b) below.
"""
from src.storage.sql_store import SQLStore


class FakeCursor:
    def __init__(self, conn):
        self._conn = conn
        self._result = None

    def execute(self, sql, params=None):
        self._conn.pending.append((sql, params))
        low = sql.lower()
        if "to_regclass" in low:
            # Legislative-status migration is "applied": the tracking probe is truthy
            # so the tracked / event-emitting code paths run (this is the one behavior
            # that differs from test_insert_self_safe.py's FakeCursor).
            self._result = {"t": "legislative_items"}
        elif "alias" in low or "returning" in low:
            # Resolver alias-hit short-circuit (one cursor open, no create-path INSERT),
            # and `INSERT ... RETURNING id` (legislative_items get-or-create, and the
            # meeting_actions tracked insert) both hand back an id row.
            self._result = {"id": 42}
        else:
            self._result = None

    def fetchone(self):
        return self._result

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    """psycopg2-connection stand-in where rollback discards uncommitted work,
    exactly as a real transaction would (identical in behavior to the FakeConn in
    tests/storage/test_insert_self_safe.py)."""

    def __init__(self):
        self.closed = False
        self.pending = []        # executed since the last commit/rollback
        self.committed = []      # survived a commit
        self.commits = 0
        self.rollbacks = 0
        self.rolled_back_a_pending_insert = False

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1
        self.committed.extend(self.pending)
        self.pending = []

    def rollback(self):
        self.rollbacks += 1
        # In the alias-hit / probe path the resolvers never leave an INSERT pending,
        # so any pending INSERT here is a batch target row about to be lost.
        if any("INSERT INTO" in sql for sql, _ in self.pending):
            self.rolled_back_a_pending_insert = True
        self.pending = []


def _store():
    s = SQLStore.__new__(SQLStore)   # bypass connect()
    s._conn = FakeConn()
    s._in_txn = False                # OUTSIDE a transaction — the vulnerable path
    s._legis_enabled = None          # force the to_regclass probe to run (-> truthy)
    return s


def _committed_inserts(conn, table):
    return [(sql, p) for sql, p in conn.committed if f"INSERT INTO {table}" in sql]


def test_resolution_insert_emits_events_self_safe():
    """Tracked resolutions: entity FKs + legislative item resolved pre-batch, rows
    inserted in one batch, events emitted post-batch — all rows survive."""
    store = _store()
    store.insert_resolution_rows(
        [{"resolution_number": "20-2026", "title": "A", "amount": None, "vendor": "Acme",
          "department": "Public Works", "adopted_date": "2026-03-03", "status": "Adopted"},
         {"resolution_number": "21-2026", "title": "B", "amount": None, "vendor": "Beta",
          "department": "Parks", "adopted_date": "2026-03-03", "status": "Adopted"}],
        source_chunk_id="00000000-0000-0000-0000-000000000000", source_file="res.pdf")

    assert store._legis_enabled is True, "legislative tracking probe should have been truthy"
    rows = _committed_inserts(store._conn, "resolutions")
    assert len(rows) == 2, f"expected both resolution rows committed, got {len(rows)}"
    events = _committed_inserts(store._conn, "legislative_events")
    assert len(events) >= 2, f"expected a lifecycle event per row emitted, got {len(events)}"
    assert store._conn.rolled_back_a_pending_insert is False, \
        "event emission (or an item resolver) rolled back the connection while the resolution batch was pending"


def test_legislation_insert_emits_events_self_safe():
    """Tracked legislation: sponsor + legislative item resolved pre-batch, rows
    inserted in one batch, 'signed' events emitted post-batch — all rows survive."""
    store = _store()
    store.insert_legislation_rows(
        [{"bill_number": "3-2026", "title": "T3", "sponsor": "Jane Doe", "amount": None,
          "adopted_date": "2026-04-01", "status": "Signed"},
         {"bill_number": "4-2026", "title": "T4", "sponsor": "John Smith", "amount": None,
          "adopted_date": "2026-04-01", "status": "Signed"}],
        source_chunk_id="00000000-0000-0000-0000-000000000000", source_file="bills.pdf")

    assert store._legis_enabled is True, "legislative tracking probe should have been truthy"
    rows = _committed_inserts(store._conn, "legislation")
    assert len(rows) == 2, f"expected both legislation rows committed, got {len(rows)}"
    events = _committed_inserts(store._conn, "legislative_events")
    assert len(events) >= 2, f"expected a lifecycle event per row emitted, got {len(events)}"
    assert store._conn.rolled_back_a_pending_insert is False, \
        "event emission (or an item resolver) rolled back the connection while the legislation batch was pending"


def test_meeting_action_insert_emits_events_self_safe():
    """Tracked meeting actions: item resolved pre-batch; rows inserted with
    RETURNING id in one batch; events (keyed to that new id) emitted post-batch —
    all rows survive. This is the trickiest path: the batch needs the row id back,
    yet the event INSERT must still wait until the batch cursor has closed."""
    store = _store()
    store.insert_meeting_action_rows(
        [{"meeting_date": "2026-03-03", "item_type": "Resolution", "item_number": "20-2026",
          "title": "A", "action": "Passed 7-0", "committee": ""},
         {"meeting_date": "2026-03-03", "item_type": "Resolution", "item_number": "21-2026",
          "title": "B", "action": "Passed 6-1", "committee": ""}],
        source_chunk_id="00000000-0000-0000-0000-000000000000", source_file="minutes.pdf")

    assert store._legis_enabled is True, "legislative tracking probe should have been truthy"
    rows = _committed_inserts(store._conn, "meeting_actions")
    assert len(rows) == 2, f"expected both meeting_action rows committed, got {len(rows)}"
    events = _committed_inserts(store._conn, "legislative_events")
    assert len(events) >= 2, f"expected a lifecycle event per row emitted, got {len(events)}"
    assert store._conn.rolled_back_a_pending_insert is False, \
        "event emission rolled back the connection while the meeting_action batch was pending"
