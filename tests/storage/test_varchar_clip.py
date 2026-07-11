"""_clip guards the bounded-VARCHAR quarterly tables against over-long extractor
values (the live batch test hit projects.status VARCHAR(50) overflow)."""
from src.storage.sql_store import _clip


def test_clip_truncates_status_to_column_limit():
    row = {"project_name": "P", "status": "x" * 200, "department": "y" * 300, "year": 2026}
    _clip(row)
    assert len(row["status"]) == 50
    assert len(row["department"]) == 100
    assert row["year"] == 2026  # non-string / non-listed fields untouched


def test_clip_leaves_short_values_alone():
    row = {"position_title": "Patrol Officer", "status": "open"}
    _clip(row)
    assert row == {"position_title": "Patrol Officer", "status": "open"}
