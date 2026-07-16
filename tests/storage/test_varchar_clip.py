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


def test_clip_covers_council_and_legislation_columns():
    # These bounded columns feed the resolution/vote/meeting/legislation/appropriation
    # inserts; an over-long value previously raised StringDataRightTruncation and (now
    # that those inserts are transactional) rolled back the whole document.
    row = {
        "resolution_number": "R" * 80,      # VARCHAR(50)
        "vendor": "V" * 300,                # VARCHAR(255)
        "council_member": "C" * 200,        # VARCHAR(120)
        "sponsor": "S" * 300,               # VARCHAR(255)
        "action": "A" * 200,                # VARCHAR(150)
        "committee": "M" * 200,             # VARCHAR(120)
        "bill_number": "B" * 80,            # VARCHAR(50)
        "item_number": "I" * 80,            # VARCHAR(50)
        "session_type": "T" * 100,          # VARCHAR(60)
        "fund": "F" * 200,                  # VARCHAR(100)
    }
    _clip(row)
    assert len(row["resolution_number"]) == 50
    assert len(row["vendor"]) == 255
    assert len(row["council_member"]) == 120
    assert len(row["sponsor"]) == 255
    assert len(row["action"]) == 150
    assert len(row["committee"]) == 120
    assert len(row["bill_number"]) == 50
    assert len(row["item_number"]) == 50
    assert len(row["session_type"]) == 60
    assert len(row["fund"]) == 100
