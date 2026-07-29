from contextlib import contextmanager

from src.storage.sql_store import SQLStore, normalize_item_number


def test_resolution_prefix_stripped():
    assert normalize_item_number("Resolution 24-2026") == "24-2026"


def test_bill_prefix_stripped():
    assert normalize_item_number("Bill 6-2026") == "6-2026"


def test_stacked_prefixes_stripped():
    assert normalize_item_number("Ordinance Bill 2-2026") == "2-2026"


def test_bare_number_unchanged():
    assert normalize_item_number("24-2026") == "24-2026"


def test_case_insensitive():
    assert normalize_item_number("resolution 24-2026") == "24-2026"


def test_no_dot_prefix_stripped():
    assert normalize_item_number("No. 24-2026") == "24-2026"


def test_none_stays_none():
    assert normalize_item_number(None) is None


def test_whitespace_trimmed():
    assert normalize_item_number("  24-2026  ") == "24-2026"


def test_prefix_only_value_kept():
    # A value that is nothing but a prefix word must not be emptied out.
    assert normalize_item_number("Resolution") == "Resolution"


class _CaptureCursor:
    def __init__(self):
        self.params = []

    def execute(self, sql, params=None):
        self.params.append(params)


def test_insert_meeting_action_rows_normalizes_item_number():
    # The 04-14 minutes extracted "Resolution 24-2026" while the 04-28 minutes
    # extracted "24-2026" for the same resolution, splitting one item into two
    # on the dashboard pipeline. The insert must store the canonical form.
    store = SQLStore.__new__(SQLStore)
    cur = _CaptureCursor()

    @contextmanager
    def fake_cursor(commit=True):
        yield cur

    store.cursor = fake_cursor
    store.insert_meeting_action_rows(
        [{"meeting_date": "2026-04-14", "item_type": "resolution",
          "item_number": "Resolution 24-2026", "title": "T", "action": "Referred",
          "committee": "Public Works"}],
        "chunk-1", "minutes.pdf",
    )
    assert cur.params[0][2] == "24-2026"
