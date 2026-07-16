from src.ingestion import registry


class _FakeCur:
    def __init__(self, rows): self._rows = rows
    def execute(self, *a, **k): pass
    def fetchall(self): return self._rows
    def __enter__(self): return self
    def __exit__(self, *a): return False


class _FakeStore:
    def __init__(self, rows): self._rows = rows
    def cursor(self): return _FakeCur(self._rows)


_BOARDS_ROW = {
    "type_name": "board_memberships",
    "description": "Board rosters",
    "extraction_templates": {"record_types": [
        {"name": "board_members", "proposed_columns": [
            {"name": "board_name", "type": "VARCHAR(255)"},
            {"name": "is_vacant", "type": "BOOLEAN"}]}]},
    "sql_tables": ["board_members"],
    "graph_node_types": [],
}


def test_refresh_registers_db_type_with_compiled_schema():
    try:
        n = registry.refresh_from_db(_FakeStore([_BOARDS_ROW]))
        assert n == 1
        dt = registry.get_document_type("board_memberships")
        assert dt is not None
        assert dt.sql_targets == ["board_members"]
        assert dt.extraction_schema is not None
        assert set(dt.extraction_schema.model_fields.keys()) == {"board_members"}
    finally:
        registry.unregister("board_memberships")   # don't leak into other tests


def test_refresh_never_clobbers_builtin_types():
    # A DB row claiming an existing built-in name must NOT replace the code definition.
    builtin = registry.get_document_type("quarterly_report")
    rogue = dict(_BOARDS_ROW, type_name="quarterly_report", sql_tables=["bogus"])
    registry.refresh_from_db(_FakeStore([rogue]))
    after = registry.get_document_type("quarterly_report")
    assert after is builtin and "expenditures" in after.sql_targets and "bogus" not in after.sql_targets
