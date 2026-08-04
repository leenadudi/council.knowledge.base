"""Entity-resolver wiring on the ingest insert paths (Task 5).

Uses the fake-cursor harness from tests/storage/test_entity_resolvers.py — no live
DB. Resolver methods are monkeypatched so each test isolates the insert method's
own wiring (column list + value) rather than re-exercising resolver internals
(already covered by tests/storage/test_entity_resolvers.py).

NOTE: the task brief's own sample test monkeypatches `store._executemany`, but no
such method exists on SQLStore — every insert_*_rows method does
`with self.cursor() as cur: for row in rows: cur.execute(sql, row)`. These tests
capture via `store._fake_cursor.executed` instead (the same pattern already used
by tests/storage/test_entity_merge.py).
"""
import inspect

from tests.storage.test_entity_resolvers import _store_with


def test_expenditure_insert_sets_department_id(monkeypatch):
    store = _store_with([])
    monkeypatch.setattr(store, "resolve_department_id", lambda name: 42)
    store.insert_expenditure_rows(
        [{"department": "Bureau of Fire", "line_item": "x"}],
        source_chunk_id="00000000-0000-0000-0000-000000000000", source_file="f.pdf")
    ins = [(s, p) for s, p in store._fake_cursor.executed if "INSERT INTO expenditures" in s]
    assert ins, "no expenditure insert issued"
    sql, params = ins[0]
    assert "department_id" in sql and params["department_id"] == 42


def test_vacancy_insert_accepts_source_file(monkeypatch):
    store = _store_with([])
    monkeypatch.setattr(store, "resolve_department_id", lambda name: 7)
    # Should not raise on the new source_file argument.
    assert "source_file" in inspect.signature(store.insert_vacancy_rows).parameters


def test_vacancy_insert_sets_department_id_and_source_file(monkeypatch):
    store = _store_with([])
    monkeypatch.setattr(store, "resolve_department_id", lambda name: 7)
    store.insert_vacancy_rows(
        [{"department": "Bureau of Police", "position_title": "Patrol Officer", "count": 3}],
        source_chunk_id="00000000-0000-0000-0000-000000000000", source_file="v.pdf")
    ins = [(s, p) for s, p in store._fake_cursor.executed if "INSERT INTO vacancies" in s]
    assert ins, "no vacancy insert issued"
    sql, params = ins[0]
    assert "department_id" in sql and params["department_id"] == 7
    assert "source_file" in sql and params["source_file"] == "v.pdf"


def test_legislation_insert_uses_match_only_resolution(monkeypatch):
    """A legislation sponsor may be a committee, which must NOT auto-create a
    council member — resolve_member_id must be called with create=False."""
    store = _store_with([])
    calls = []

    def fake_resolve(name, *, create=True):
        calls.append({"name": name, "create": create})
        return None

    monkeypatch.setattr(store, "resolve_member_id", fake_resolve)
    store.insert_legislation_rows(
        [{"bill_number": "12-2026", "title": "An Ordinance", "sponsor": "Public Works Committee"}],
        source_chunk_id="00000000-0000-0000-0000-000000000000", source_file="l.pdf")
    assert calls, "resolve_member_id was never called for the sponsor"
    assert calls[0]["name"] == "Public Works Committee"
    assert calls[0]["create"] is False, "sponsor resolution must be match-only (create=False)"
    ins = [(s, p) for s, p in store._fake_cursor.executed if "INSERT INTO legislation" in s]
    assert ins, "no legislation insert issued"
    sql, params = ins[0]
    assert "sponsor_id" in sql
    assert params[-1] is None  # sponsor_id appended last; unresolved committee -> NULL


def test_resolution_insert_sets_department_and_vendor_id(monkeypatch):
    store = _store_with([])
    monkeypatch.setattr(store, "resolve_department_id", lambda name: 5)
    monkeypatch.setattr(store, "resolve_vendor_id", lambda name: 9)
    store.insert_resolution_rows(
        [{"resolution_number": "1-2026", "title": "Award contract", "vendor": "TestCo LLC",
          "department": "Public Works"}],
        source_chunk_id="00000000-0000-0000-0000-000000000000", source_file="r.pdf")
    ins = [(s, p) for s, p in store._fake_cursor.executed if "INSERT INTO resolutions" in s]
    assert ins, "no resolution insert issued"
    sql, params = ins[0]
    assert "department_id" in sql and "vendor_id" in sql
    assert params[-2:] == (5, 9)  # department_id, vendor_id appended last


def test_vote_insert_sets_member_id(monkeypatch):
    store = _store_with([])
    monkeypatch.setattr(store, "resolve_member_id", lambda name, **kw: 11)
    store.insert_vote_rows(
        [{"resolution_number": "1-2026", "council_member": "Jane Doe", "vote": "yes"}],
        source_chunk_id="00000000-0000-0000-0000-000000000000", source_file="v.pdf")
    ins = [(s, p) for s, p in store._fake_cursor.executed if "INSERT INTO votes" in s]
    assert ins, "no vote insert issued"
    sql, params = ins[0]
    assert "member_id" in sql and params[-1] == 11


def test_meeting_insert_writes_present_and_absent_attendance(monkeypatch):
    store = _store_with([("INSERT INTO meetings", {"id": 123})])
    member_ids = {"Jane Doe": 10, "John Smith": 11, "Bob Jones": 12}
    monkeypatch.setattr(store, "resolve_member_id", lambda name, **kw: member_ids.get(name))
    store.insert_meeting_rows(
        [{
            "meeting_date": "2026-01-05", "session_type": "regular", "president": "Jane Doe",
            "members_present": 2,
            "members_present_names": "Jane Doe, John Smith",
            "members_absent_names": "Bob Jones",
        }],
        source_chunk_id="00000000-0000-0000-0000-000000000000", source_file="m.pdf")

    executed = store._fake_cursor.executed
    meeting_ins = [(s, p) for s, p in executed if "INSERT INTO meetings" in s]
    assert meeting_ins, "no meeting insert issued"
    sql, params = meeting_ins[0]
    assert "president_id" in sql and "RETURNING id" in sql
    assert params[-1] == 10  # president_id for "Jane Doe"

    attendance = [(s, p) for s, p in executed if "INSERT INTO meeting_attendance" in s]
    assert len(attendance) == 3, f"expected 3 attendance rows, got {len(attendance)}"
    by_status = {(p[1], p[2]) for _, p in attendance}
    assert (10, "present") in by_status
    assert (11, "present") in by_status
    assert (12, "absent") in by_status
    assert all(p[0] == 123 for _, p in attendance), "attendance rows must reference the new meeting id"

    # count matched (2 present names == members_present) -> no mismatch flag
    flags = [(s, p) for s, p in executed if "review_flags" in s]
    assert not flags, "no mismatch expected when parsed count matches stated count"


def test_meeting_insert_flags_attendance_count_mismatch(monkeypatch):
    store = _store_with([("INSERT INTO meetings", {"id": 5})])
    monkeypatch.setattr(store, "resolve_member_id", lambda name, **kw: 1)
    store.insert_meeting_rows(
        [{
            "meeting_date": "2026-01-05", "session_type": "regular", "president": "Jane Doe",
            "members_present": 99,  # stated count deliberately wrong
            "members_present_names": "Jane Doe",
            "members_absent_names": "",
        }],
        source_chunk_id="00000000-0000-0000-0000-000000000000", source_file="m.pdf")
    flags = [(s, p) for s, p in store._fake_cursor.executed if "review_flags" in s]
    assert flags, "attendance count mismatch should have been flagged"
    _, params = flags[0]
    assert params[1] == "attendance_count_mismatch"
    assert "5" in params[2] and "99" in params[2]


def test_record_document_ensures_type_and_resolves_department(monkeypatch):
    store = _store_with([])
    ensured = []
    monkeypatch.setattr(store, "ensure_document_type", lambda t: ensured.append(t))
    monkeypatch.setattr(store, "resolve_department_id", lambda name: 3)
    store.record_document(
        source_file="d.pdf", department="Bureau of Fire", document_type="quarterly_report",
        quarter="Q1", year=2026, parser_used="vision_llm", total_chunks=10)
    assert ensured == ["quarterly_report"]
    ins = [(s, p) for s, p in store._fake_cursor.executed if "INSERT INTO documents" in s]
    assert ins, "no document upsert issued"
    sql, params = ins[0]
    assert "department_id" in sql and params["department_id"] == 3
