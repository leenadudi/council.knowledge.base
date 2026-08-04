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


def test_resolver_auto_create_loses_race_then_reselects():
    # INSERT returns no row (another tx won the ON CONFLICT); resolver re-SELECTs the winner.
    store = _store_with([
        ("FROM department_aliases", None),                     # no alias
        ("FROM departments WHERE canonical_key", None),        # key miss -> create path
        ("similarity", None),                                  # no near-dup
        ("INSERT INTO departments", None),                     # our INSERT loses the race
        ("FROM departments WHERE canonical_key", {"id": 55}),  # re-select finds the winner
        ("INSERT INTO department_aliases", None),              # alias recorded
    ])
    assert store.resolve_department_id("Bureau of Fire") == 55


def test_resolver_match_only_returns_none_and_flags():
    store = _store_with([
        ("FROM council_member_aliases", None),
        ("FROM council_members WHERE canonical_key", None),
        ("INSERT INTO review_flags", None),
    ])
    assert store.resolve_member_id("Public Works Committee", create=False) is None
    assert any("review_flags" in s for s, _ in store._fake_cursor.executed)


def test_empty_name_returns_none_without_touching_db():
    store = _store_with([])
    assert store.resolve_department_id("") is None
    assert store._fake_cursor.executed == []


def test_resolver_truncates_overlong_name_before_insert():
    """A pathologically long extracted name must not overflow the VARCHAR(200)
    vendors.canonical_name/alias width and abort the resolve with 'value too long'."""
    long_name = "A" * 250  # > vendors' 200-char canonical_name/alias width
    store = _store_with([
        ("FROM vendor_aliases", None),
        ("FROM vendors WHERE canonical_key", None),
        ("similarity", None),
        ("INSERT INTO vendors", {"id": 1}),
        ("INSERT INTO vendor_aliases", None),
    ])
    assert store.resolve_vendor_id(long_name) == 1

    vendor_params = next(p for s, p in store._fake_cursor.executed if s.startswith("INSERT INTO vendors"))
    alias_params = next(p for s, p in store._fake_cursor.executed if s.startswith("INSERT INTO vendor_aliases"))
    # INSERT INTO vendors (canonical_name, canonical_key) VALUES (%s, %s)
    assert len(vendor_params[0]) == 200, "canonical_name must be truncated to the column width"
    assert len(vendor_params[1]) == 200, "canonical_key must be truncated to the column width"
    assert len(alias_params[1]) == 200, "alias must be truncated to the column width"


def test_resolver_truncates_canonical_key_to_its_own_narrower_width():
    """canonical_key is narrower than canonical_name for departments (120 vs 150).
    A long, punctuation-sparse name yields an equally long key, so the key needs
    its OWN bound: department_key('A'*130) -> 130 chars would overflow VARCHAR(120).
    The name (130) fits canonical_name (150) untouched, proving the key is bounded
    by its own width, not the name's."""
    long_name = "A" * 130
    store = _store_with([
        ("FROM department_aliases", None),
        ("FROM departments WHERE canonical_key", None),
        ("similarity", None),
        ("INSERT INTO departments", {"id": 1}),
        ("INSERT INTO department_aliases", None),
    ])
    assert store.resolve_department_id(long_name) == 1

    # INSERT INTO departments (canonical_name, canonical_key, dept_type) VALUES (...)
    dept_params = next(p for s, p in store._fake_cursor.executed if s.startswith("INSERT INTO departments"))
    assert len(dept_params[0]) == 130, "canonical_name (150 width) is untouched when the name fits"
    assert len(dept_params[1]) == 120, "canonical_key must be truncated to departments' 120-char width"
