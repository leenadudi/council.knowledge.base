"""Unit tests for legislative-tracking hooks in SQLStore (no live DB).

A fake cursor records executed SQL and serves canned fetch results so we can
assert the resolver/event wiring without PostgreSQL. Integration coverage is
in tests/storage/test_legislative_status_integration.py.
"""
from src.storage.sql_store import SQLStore


class FakeCursor:
    def __init__(self, store):
        self.store = store
        self._next = None

    def execute(self, sql, params=None):
        self.store.executed.append((" ".join(sql.split()), params))
        s = sql.lower()
        if "to_regclass" in s:
            self._next = {"t": "legislative_items" if self.store.migrated else None}
        elif "insert into legislative_items" in s:
            self._next = {"id": 42}
        elif "returning id" in s:
            self._next = {"id": 7}
        else:
            self._next = None

    def fetchone(self):
        return self._next

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeConn:
    closed = False

    def __init__(self, store):
        self.store = store

    def cursor(self):
        return FakeCursor(self.store)

    def commit(self):
        pass

    def rollback(self):
        pass


def make_store(migrated=True):
    s = SQLStore.__new__(SQLStore)
    s.executed = []
    s.migrated = migrated
    s._conn = FakeConn(s)
    s._in_txn = False
    s._legis_enabled = None
    return s


def _sqls(store):
    return [sql for sql, _ in store.executed]


def test_meeting_action_insert_emits_events():
    s = make_store()
    s.insert_meeting_action_rows(
        [{"meeting_date": "2026-03-03", "item_type": "Resolution",
          "item_number": "20-2026", "title": "T", "action": "Passed 7-0", "committee": ""}],
        "chunk-1", "minutes.pdf")
    joined = "\n".join(_sqls(s))
    assert "INSERT INTO legislative_items" in joined
    assert "INSERT INTO legislative_events" in joined
    ev_params = [p for sql, p in s.executed if "INSERT INTO legislative_events" in sql][0]
    assert "passed" in ev_params
    assert 7 in ev_params and 0 in ev_params           # tally captured


def test_meeting_action_without_migration_is_legacy_shape():
    s = make_store(migrated=False)
    s.insert_meeting_action_rows(
        [{"meeting_date": "2026-03-03", "item_type": "Resolution",
          "item_number": "20-2026", "title": "T", "action": "Passed 7-0", "committee": ""}],
        "chunk-1", "minutes.pdf")
    joined = "\n".join(_sqls(s))
    assert "legislative_items" not in joined.replace("to_regclass('legislative_items')", "")
    assert "INSERT INTO legislative_events" not in joined
    assert "legislative_item_id" not in joined          # legacy column list untouched


def test_untrackable_item_type_inserts_without_events():
    s = make_store()
    s.insert_meeting_action_rows(
        [{"meeting_date": "2026-03-03", "item_type": "Minutes Approval",
          "item_number": "", "title": "", "action": "Approved", "committee": ""}],
        "chunk-1", "minutes.pdf")
    assert not any("legislative_events" in q for q in _sqls(s))
    assert not any("review_flags" in q for q in _sqls(s))   # untrackable != unmapped


def test_unmapped_action_flags_for_review():
    s = make_store()
    s.insert_meeting_action_rows(
        [{"meeting_date": "2026-03-03", "item_type": "Resolution",
          "item_number": "9-2026", "title": "T",
          "action": "Announced for March 31, 2026 at 5:30 PM", "committee": ""}],
        "chunk-1", "minutes.pdf")
    flags = [p for sql, p in s.executed if "review_flags" in sql]
    assert flags and flags[0][1] == "normalize"


def test_resolution_insert_emits_document_event():
    s = make_store()
    s.insert_resolution_rows(
        [{"resolution_number": "20-2026", "title": "T", "amount": None, "vendor": "",
          "department": "", "adopted_date": "2026-03-03", "status": "Adopted"}],
        "chunk-2", "res20.pdf")
    ev = [p for sql, p in s.executed if "INSERT INTO legislative_events" in sql]
    assert ev and "document" in ev[0]
    assert "passed" in ev[0]


def test_legislation_signed_status_emits_signed_event():
    s = make_store()
    s.insert_legislation_rows(
        [{"bill_number": "3-2026", "title": "T", "sponsor": "", "amount": None,
          "adopted_date": "2026-04-01", "status": "Signed"}],
        "chunk-3", "bill3.pdf")
    ev = [p for sql, p in s.executed if "INSERT INTO legislative_events" in sql]
    assert ev and "signed" in ev[0]


def test_vote_rows_link_item_without_events():
    s = make_store()
    s.insert_vote_rows(
        [{"resolution_number": "20-2026", "council_member": "A. Member", "vote": "yes"}],
        "chunk-4", "res20.pdf")
    joined = "\n".join(_sqls(s))
    assert "INSERT INTO legislative_items" in joined
    assert "INSERT INTO legislative_events" not in joined


def test_delete_structured_rows_covers_legislative_events():
    s = make_store()
    s.delete_structured_rows("minutes.pdf")
    assert any("legislative_events" in q for q in _sqls(s))
