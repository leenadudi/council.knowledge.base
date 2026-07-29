from contextlib import contextmanager
from src.storage.sql_store import SQLStore


class FakeCursor:
    """Matches queued (needle, result) pairs by SQL substring; records executes."""
    def __init__(self, script):
        self.script = list(script)     # list of (needle, fetchone_result)
        self.executed = []
        self._result = None

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        self._result = None
        for i, (needle, res) in enumerate(self.script):
            if needle in sql:
                self._result = res
                self.script.pop(i)
                break

    def fetchone(self):
        return self._result


def _store_with(script):
    store = SQLStore.__new__(SQLStore)          # bypass connect()
    cur = FakeCursor(script)

    @contextmanager
    def _cursor():
        yield cur
    store.cursor = _cursor
    store._fake_cursor = cur
    return store


def test_resolver_returns_existing_via_alias():
    store = _store_with([("FROM department_aliases", {"id": 7})])
    assert store.resolve_department_id("Bureau of Fire") == 7


def test_resolver_returns_existing_via_key_and_records_alias():
    store = _store_with([
        ("FROM department_aliases", None),               # no alias yet
        ("FROM departments WHERE canonical_key", {"id": 12}),
        ("INSERT INTO department_aliases", None),         # alias recorded
    ])
    assert store.resolve_department_id("Bureau of Fire ") == 12
    assert any("INSERT INTO department_aliases" in s for s, _ in store._fake_cursor.executed)


def test_resolver_auto_creates_and_flags_near_dup():
    store = _store_with([
        ("FROM department_aliases", None),
        ("FROM departments WHERE canonical_key", None),
        ("similarity", {"canonical_name": "Bureau of Police"}),  # near-dup found
        ("INSERT INTO review_flags", None),
        ("INSERT INTO departments", {"id": 99}),
        ("INSERT INTO department_aliases", None),
    ])
    assert store.resolve_department_id("Buraeu of Police") == 99
    flagged = [s for s, _ in store._fake_cursor.executed if "review_flags" in s]
    assert flagged, "near-duplicate should have been flagged"


def test_resolver_match_only_returns_none_and_flags():
    store = _store_with([
        ("FROM council_member_aliases", None),
        ("FROM council_members WHERE canonical_key", None),
        ("INSERT INTO review_flags", None),
    ])
    assert store.resolve_member_id("Public Works Committee", create=False) is None


def test_empty_name_returns_none_without_touching_db():
    store = _store_with([])
    assert store.resolve_department_id("") is None
    assert store._fake_cursor.executed == []
