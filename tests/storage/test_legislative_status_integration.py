"""End-to-end legislative status tracking against a scratch database.

Requires SCRATCH_DATABASE_URL (a throwaway DB). Applies schema + migration,
seeds observed-shape raw rows, runs the backfill, and asserts stages, the
multi-meeting trail, review flags, and idempotency. Never touches DATABASE_URL.
"""
import os
import subprocess
import uuid

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("SCRATCH_DATABASE_URL"),
                       reason="SCRATCH_DATABASE_URL not set"),
]

from scripts.backfill_legislative_events import rebuild
from src.storage.sql_store import SQLStore


@pytest.fixture()
def store():
    url = os.environ["SCRATCH_DATABASE_URL"]
    subprocess.run(["psql", url, "-q", "-f", "sql/schema.sql"], check=True)
    subprocess.run(["psql", url, "-q", "-f",
                    "sql/migrate_2026_07_29_legislative_status.sql"], check=True)
    s = SQLStore.__new__(SQLStore)
    from src.config import get_settings
    base = get_settings()
    s.settings = base.model_copy(update={"database_url": url})
    s._conn = None
    s._in_txn = False
    s._legis_enabled = None
    s.connect()
    with s.cursor() as cur:
        for t in ("legislative_events", "votes", "meeting_actions", "resolutions",
                  "legislation", "review_flags", "legislative_items"):
            cur.execute(f"DELETE FROM {t}")
    yield s
    s.close()


def _seed(store):
    # Bill 1-2026: multi-meeting ordinance trail (observed shape)
    store.insert_meeting_action_rows(
        [{"meeting_date": "2026-02-10", "item_type": "ordinance", "item_number": "1-2026",
          "title": "Budget amendment", "action": "first reading; referred to committee",
          "committee": "Administration Committee"}],
        str(uuid.uuid4()), "minutes_feb.pdf")
    store.insert_meeting_action_rows(
        [{"meeting_date": "2026-03-03", "item_type": "Ordinance for Final Passage",
          "item_number": "Bill 1-2026", "title": "Budget amendment",
          "action": "Passed 7-0 as amended", "committee": ""}],
        str(uuid.uuid4()), "minutes_mar.pdf")
    # Resolution 20-2026: referred, never passed
    store.insert_meeting_action_rows(
        [{"meeting_date": "2026-03-03", "item_type": "Resolution", "item_number": "20-2026",
          "title": "Grant application", "action": "Referred to Public Safety Committee",
          "committee": ""}],
        str(uuid.uuid4()), "minutes_mar.pdf")
    # Resolution 5-2026: tabled
    store.insert_meeting_action_rows(
        [{"meeting_date": "2026-03-03", "item_type": "resolution", "item_number": "5-2026",
          "title": "T", "action": "tabled", "committee": ""}],
        str(uuid.uuid4()), "minutes_mar.pdf")
    # Unmapped action on a trackable item -> review flag
    store.insert_meeting_action_rows(
        [{"meeting_date": "2026-03-03", "item_type": "Resolution", "item_number": "9-2026",
          "title": "T", "action": "Announced for March 31, 2026 at 5:30 PM",
          "committee": ""}],
        str(uuid.uuid4()), "minutes_mar.pdf")
    # Signed bill via its own document
    store.insert_legislation_rows(
        [{"bill_number": "1-2026", "title": "Budget amendment", "sponsor": "",
          "amount": None, "adopted_date": "2026-03-20", "status": "Signed"}],
        str(uuid.uuid4()), "bill1.pdf")


def _stage(store, kind, key):
    with store.cursor() as cur:
        cur.execute("SELECT current_stage FROM legislative_item_status "
                    "WHERE item_kind=%s AND item_key=%s", (kind, key))
        row = cur.fetchone()
        return row["current_stage"] if row else None


def test_end_to_end_stages_and_trail(store):
    _seed(store)
    assert _stage(store, "ordinance", "1-2026") == "signed"       # doc beats minutes
    assert _stage(store, "resolution", "20-2026") == "in_committee"
    assert _stage(store, "resolution", "5-2026") == "tabled"

    with store.cursor() as cur:
        cur.execute("SELECT event, origin FROM legislative_item_trail "
                    "WHERE item_kind='ordinance' AND item_key='1-2026' ORDER BY event_date, seq")
        trail = [(r["event"], r["origin"]) for r in cur.fetchall()]
    assert ("first_reading", "minutes") in trail
    assert ("referred_to_committee", "minutes") in trail
    assert ("passed", "minutes") in trail
    assert ("amended", "minutes") in trail
    assert ("signed", "document") in trail

    with store.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM review_flags "
                    "WHERE stage='normalize' AND resolved=FALSE")
        assert cur.fetchone()["n"] == 1

    # "Bill 1-2026" and "1-2026" resolved to ONE item
    with store.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM legislative_items "
                    "WHERE item_kind='ordinance' AND item_key='1-2026'")
        assert cur.fetchone()["n"] == 1


def test_backfill_rebuild_is_idempotent(store):
    _seed(store)
    first = rebuild(store)
    second = rebuild(store)
    assert first["events"] == second["events"] > 0
    assert _stage(store, "ordinance", "1-2026") == "signed"
    with store.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM legislative_events")
        n = cur.fetchone()["n"]
    with store.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM legislative_items")
        items = cur.fetchone()["n"]
    assert n == second["events"]
    assert items == second["items"]


def test_dry_run_writes_nothing(store):
    _seed(store)
    with store.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM legislative_events")
        before = cur.fetchone()["n"]
    stats = rebuild(store, dry_run=True)
    assert stats.get("events", 0) > 0
    with store.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM legislative_events")
        assert cur.fetchone()["n"] == before
