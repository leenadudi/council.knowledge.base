# tests/storage/test_resolution_graph.py
"""Integration tests for GraphStore resolution/vendor/council-member/vote methods.

Requires a live Neo4j instance reachable at settings.neo4j_uri.
Mark: @pytest.mark.integration  — skipped by default; run with -m integration.
"""
import pytest
from src.storage.graph_store import GraphStore


@pytest.fixture(scope="module")
def graph():
    """Open a real Neo4j connection; skip the whole module if Neo4j is unreachable."""
    g = GraphStore()
    try:
        g.connect()
        # Force a real connection attempt to catch unreachable Neo4j early
        g._driver.verify_connectivity()
        g.ensure_constraints()
    except Exception as exc:
        pytest.skip(f"Neo4j unreachable: {exc}")
    yield g
    g.close()


@pytest.mark.integration
def test_upsert_resolution_and_vote_roundtrip(graph: GraphStore):
    graph.upsert_resolutions([{"resolution_number": "2026-R-99", "title": "T",
                               "amount": 1000.0, "status": "adopted",
                               "adopted_date": "2026-03-03", "vendor": "Acme"}])
    graph.upsert_council_members([{"name": "Smith"}])
    graph.upsert_votes([{"resolution_number": "2026-R-99", "council_member": "Smith", "vote": "yes"}])
    rows = graph.execute_cypher(
        "MATCH (c:CouncilMember)-[v:VOTED]->(r:Resolution {resolution_number:'2026-R-99'}) "
        "RETURN c.name AS m, v.vote AS vote")
    assert rows and rows[0]["vote"] == "yes"


@pytest.mark.integration
def test_upsert_resolution_creates_vendor_relationship(graph: GraphStore):
    graph.upsert_resolutions([{"resolution_number": "2026-R-100", "title": "Vendor Test",
                               "amount": 5000.0, "status": "adopted",
                               "adopted_date": "2026-04-01", "vendor": "BuildCo"}])
    rows = graph.execute_cypher(
        "MATCH (r:Resolution {resolution_number:'2026-R-100'})-[:AWARDS_CONTRACT_TO]->(v:Vendor) "
        "RETURN v.name AS vendor")
    assert rows and rows[0]["vendor"] == "BuildCo"


@pytest.mark.integration
def test_upsert_resolution_no_vendor(graph: GraphStore):
    """A resolution with no vendor should not create a Vendor node relationship."""
    graph.upsert_resolutions([{"resolution_number": "2026-R-101", "title": "No Vendor",
                               "amount": None, "status": "pending",
                               "adopted_date": None, "vendor": ""}])
    rows = graph.execute_cypher(
        "MATCH (r:Resolution {resolution_number:'2026-R-101'})-[:AWARDS_CONTRACT_TO]->(v:Vendor) "
        "RETURN v.name AS vendor")
    assert rows == []


@pytest.mark.integration
def test_upsert_resolution_absent_vendor(graph: GraphStore):
    """A resolution where the vendor key is absent entirely should not create a Vendor node or edge."""
    graph.upsert_resolutions([{"resolution_number": "2026-R-102", "title": "Absent Vendor",
                               "amount": None, "status": "pending",
                               "adopted_date": None}])
    rows = graph.execute_cypher(
        "MATCH (r:Resolution {resolution_number:'2026-R-102'})-[:AWARDS_CONTRACT_TO]->(v:Vendor) "
        "RETURN v.name AS vendor")
    assert rows == []


@pytest.mark.integration
def test_upsert_vendors_standalone(graph: GraphStore):
    graph.upsert_vendors([{"name": "StandaloneVendor"}])
    rows = graph.execute_cypher(
        "MATCH (v:Vendor {name: 'StandaloneVendor'}) RETURN v.name AS name")
    assert rows and rows[0]["name"] == "StandaloneVendor"


@pytest.mark.integration
def test_upsert_council_members_standalone(graph: GraphStore):
    graph.upsert_council_members([{"name": "Jones"}])
    rows = graph.execute_cypher(
        "MATCH (c:CouncilMember {name: 'Jones'}) RETURN c.name AS name")
    assert rows and rows[0]["name"] == "Jones"
