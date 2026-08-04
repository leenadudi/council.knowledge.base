from tests.storage.test_entity_resolvers import _store_with


def _executed(store):
    """Extract SQL statements (with %s placeholders) from executed queries."""
    return [s for s, _ in store._fake_cursor.executed]


def _executed_with_params(store):
    """Extract (sql, params) tuples from executed queries."""
    return store._fake_cursor.executed


def test_merge_department_repoints_all_ten_tables_and_deactivates():
    store = _store_with([])
    store.merge_department(dup_id=5, keep_id=2)
    executed = _executed_with_params(store)

    # Check that aliases are repointed
    assert any("UPDATE department_aliases SET department_id = %s" in sql and params == (2, 5)
               for sql, params in executed), "aliases not repointed"

    # Check that all 10 dept tables are updated
    dept_tables = ("expenditures", "metrics", "grants", "vacancies", "documents",
                   "resolutions", "appropriations", "goals", "document_chunks",
                   "evaluation_suite")
    for t in dept_tables:
        assert any(f"UPDATE {t} SET department_id = %s" in sql and params == (2, 5)
                   for sql, params in executed), f"{t} not updated"

    # Check that dup is deactivated
    assert any("UPDATE departments SET active = FALSE" in sql and params == (5,)
               for sql, params in executed), "dup not deactivated"


def test_merge_member_repoints_three_columns_plus_attendance():
    store = _store_with([])
    store.merge_member(dup_id=9, keep_id=1)
    executed = _executed_with_params(store)

    # Check specific columns
    assert any("UPDATE votes SET member_id = %s" in sql and params == (1, 9)
               for sql, params in executed), "votes.member_id not updated"
    assert any("UPDATE meetings SET president_id = %s" in sql and params == (1, 9)
               for sql, params in executed), "meetings.president_id not updated"
    assert any("UPDATE legislation SET sponsor_id = %s" in sql and params == (1, 9)
               for sql, params in executed), "legislation.sponsor_id not updated"
    assert any("UPDATE meeting_attendance SET member_id = %s" in sql and params == (1, 9)
               for sql, params in executed), "meeting_attendance.member_id not updated"

    # Check deactivation
    assert any("UPDATE council_members SET active = FALSE" in sql and params == (9,)
               for sql, params in executed), "dup not deactivated"


def test_merge_vendor_repoints_resolutions():
    store = _store_with([])
    store.merge_vendor(dup_id=4, keep_id=3)
    executed = _executed_with_params(store)

    # Check resolutions vendor_id update
    assert any("UPDATE resolutions SET vendor_id = %s" in sql and params == (3, 4)
               for sql, params in executed), "resolutions.vendor_id not updated"

    # Check deactivation
    assert any("UPDATE vendors SET active = FALSE" in sql and params == (4,)
               for sql, params in executed), "dup not deactivated"


def test_merge_department_is_noop_when_dup_equals_keep():
    """merge_department(id, id) must not deactivate the row the caller meant to keep."""
    store = _store_with([])
    store.merge_department(dup_id=5, keep_id=5)
    assert not any("active = FALSE" in sql for sql in _executed(store))
