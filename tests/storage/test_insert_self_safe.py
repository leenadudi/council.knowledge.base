"""Regression guard for the 'resolve-before-cursor' self-safe invariant.

Every resolver-using insert_*_rows method MUST resolve all its FK ids BEFORE
opening its own insert cursor. A resolver opens its own self.cursor(); OUTSIDE a
transaction, SQLStore.cursor() -> _get_live_conn() issues a defensive
conn.rollback() ("reconnect if idle") that discards the uncommitted rows already
inserted earlier in the same batch. So if a resolver is called WHILE an insert
batch is pending, every row but the last is silently lost — which bites operator
scripts and any caller that didn't wrap a sql_store.transaction().

Unlike the other storage fakes (which stub out cursor()), this test drives the
REAL cursor()/_get_live_conn() code against a FakeConn that models rollback as
discarding pending (uncommitted) executes, so it actually reproduces the hazard.
Reverting any insert method to per-row-inline resolution makes both assertions
below fail.
"""
from src.storage.sql_store import SQLStore


class FakeCursor:
    def __init__(self, conn):
        self._conn = conn
        self._result = None

    def execute(self, sql, params=None):
        self._conn.pending.append((sql, params))
        low = sql.lower()
        # Resolver alias lookups (`... FROM *_aliases WHERE LOWER(alias) = ...`)
        # expect a row with an "id"; returning one makes resolve_* short-circuit on
        # the alias-hit path after a single cursor open (so no create-path INSERTs).
        # `INSERT INTO meetings ... RETURNING id` likewise needs an id row back.
        self._result = {"id": 42} if ("alias" in low or "returning" in low) else None

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
    exactly as a real transaction would."""

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
        # In the alias-hit path resolvers never issue an INSERT, so any pending
        # INSERT here is a batch target row about to be lost.
        if any("INSERT INTO" in sql for sql, _ in self.pending):
            self.rolled_back_a_pending_insert = True
        self.pending = []


def _store():
    s = SQLStore.__new__(SQLStore)   # bypass connect()
    s._conn = FakeConn()
    s._in_txn = False                # OUTSIDE a transaction — the vulnerable path
    return s


def _committed_inserts(conn, table):
    return [(sql, p) for sql, p in conn.committed if f"INSERT INTO {table}" in sql]


def test_expenditure_insert_is_self_safe_outside_transaction():
    """Dict-style path: both rows survive, no pending-batch rollback."""
    store = _store()
    store.insert_expenditure_rows(
        [{"department": "Bureau of Fire", "line_item": "a"},
         {"department": "Bureau of Fire", "line_item": "b"}],
        source_chunk_id="00000000-0000-0000-0000-000000000000", source_file="f.pdf")
    inserts = _committed_inserts(store._conn, "expenditures")
    assert len(inserts) == 2, f"expected both rows committed, got {len(inserts)}"
    assert store._conn.rolled_back_a_pending_insert is False, \
        "a resolver rolled back the connection while an insert batch was pending"


def test_vote_insert_is_self_safe_outside_transaction():
    """Positional-tuple path (one of the methods fixed in the review round)."""
    store = _store()
    store.insert_vote_rows(
        [{"resolution_number": "1-2026", "council_member": "Jane Doe", "vote": "yes"},
         {"resolution_number": "1-2026", "council_member": "John Smith", "vote": "no"}],
        source_chunk_id="00000000-0000-0000-0000-000000000000", source_file="v.pdf")
    inserts = _committed_inserts(store._conn, "votes")
    assert len(inserts) == 2, f"expected both vote rows committed, got {len(inserts)}"
    assert store._conn.rolled_back_a_pending_insert is False, \
        "a resolver rolled back the connection while a vote batch was pending"


def test_meeting_insert_is_self_safe_outside_transaction():
    """Meetings pre-resolve president + attendance before the RETURNING-id cursor."""
    store = _store()
    store.insert_meeting_rows(
        [{"meeting_date": "2026-01-05", "session_type": "regular", "president": "Jane Doe",
          "members_present": 2, "members_present_names": "Jane Doe, John Smith",
          "members_absent_names": "Bob Jones"},
         {"meeting_date": "2026-02-05", "session_type": "regular", "president": "Jane Doe",
          "members_present": 1, "members_present_names": "Jane Doe",
          "members_absent_names": ""}],
        source_chunk_id="00000000-0000-0000-0000-000000000000", source_file="m.pdf")
    inserts = _committed_inserts(store._conn, "meetings")
    assert len(inserts) == 2, f"expected both meeting rows committed, got {len(inserts)}"
    assert store._conn.rolled_back_a_pending_insert is False, \
        "a resolver rolled back the connection while a meeting batch was pending"
