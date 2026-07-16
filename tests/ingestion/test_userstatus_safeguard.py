from scripts.reextract_quarterly import merge_user_status


def test_reapplies_matching_status_and_reports_unmatched():
    existing = [
        {"department": "Police", "year": 2025, "quarter": "Q1", "goal_title": "Cut response time",
         "user_status": "on_track", "user_status_at": "2026-01-01"},
        {"department": "Police", "year": 2025, "quarter": "Q1", "goal_title": "Old title drifted",
         "user_status": "at_risk", "user_status_at": "2026-01-02"},
    ]
    fresh = [
        {"department": "Police", "year": 2025, "quarter": "Q1", "goal_title": "Cut response time"},
        {"department": "Police", "year": 2025, "quarter": "Q1", "goal_title": "A brand new goal"},
    ]
    merged, unmatched = merge_user_status(existing, fresh)
    by_title = {r["goal_title"]: r for r in merged}
    assert by_title["Cut response time"]["user_status"] == "on_track"
    assert "user_status" not in by_title["A brand new goal"]
    assert [u["goal_title"] for u in unmatched] == ["Old title drifted"]
