"""Neo4j graph store for entity and relationship queries."""

from __future__ import annotations

import logging
from typing import Any, Optional

from neo4j import GraphDatabase, Driver

from src.config import Settings, get_settings

logger = logging.getLogger(__name__)


class GraphStore:
    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings or get_settings()
        self._driver: Optional[Driver] = None

    def connect(self) -> None:
        self._driver = GraphDatabase.driver(
            self.settings.neo4j_uri,
            auth=(self.settings.neo4j_user, self.settings.neo4j_password),
            max_connection_lifetime=3600,
            keep_alive=True,
        )

    def close(self) -> None:
        if self._driver:
            self._driver.close()
            self._driver = None

    def _run(self, cypher: str, params: Optional[dict] = None) -> list[dict[str, Any]]:
        if not self._driver:
            self.connect()
        try:
            with self._driver.session() as session:
                result = session.run(cypher, params or {})
                return [record.data() for record in result]
        except Exception as exc:
            msg = str(exc).lower()
            # Only retry on dropped-connection errors, not SSL/auth/syntax errors
            if "write data" in msg or "connection reset" in msg or "broken pipe" in msg:
                logger.warning("Neo4j stale connection, reconnecting: %s", exc)
                self.close()
                self.connect()
                with self._driver.session() as session:
                    result = session.run(cypher, params or {})
                    return [record.data() for record in result]
            raise

    # ------------------------------------------------------------------
    # Schema initialization
    # ------------------------------------------------------------------

    def ensure_constraints(self) -> None:
        """Create uniqueness constraints for core node types."""
        constraints = [
            "CREATE CONSTRAINT IF NOT EXISTS FOR (p:Person) REQUIRE p.name IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (d:Department) REQUIRE d.name IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (pr:Project) REQUIRE pr.name IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (g:Grant) REQUIRE g.grant_number IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (doc:Document) REQUIRE doc.filename IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (c:Chunk) REQUIRE c.chunk_id IS UNIQUE",
        ]
        for stmt in constraints:
            try:
                self._run(stmt)
            except Exception as e:
                logger.debug("Constraint may already exist: %s", e)
        logger.info("Neo4j constraints ensured")

    # ------------------------------------------------------------------
    # Ingestion
    # ------------------------------------------------------------------

    def upsert_people(self, people: list[dict[str, Any]]) -> None:
        cypher = """
            UNWIND $people AS p
            MERGE (person:Person {name: p.name})
            SET person.title = p.title,
                person.department = p.department
        """
        self._run(cypher, {"people": people})

    def upsert_departments(self, departments: list[dict[str, Any]]) -> None:
        cypher = """
            UNWIND $departments AS d
            MERGE (dept:Department {name: d.name})
            SET dept.parent_department = d.parent_department
        """
        self._run(cypher, {"departments": departments})

    def upsert_projects(self, projects: list[dict[str, Any]]) -> None:
        cypher = """
            UNWIND $projects AS pr
            MERGE (project:Project {name: pr.name})
            SET project.status = pr.status,
                project.description = pr.description,
                project.location = pr.location
        """
        self._run(cypher, {"projects": projects})

    def upsert_grants(self, grants: list[dict[str, Any]]) -> None:
        valid = [g for g in grants if g.get("grant_number") is not None]
        if not valid:
            return
        cypher = """
            UNWIND $grants AS g
            MERGE (grant:Grant {grant_number: g.grant_number})
            SET grant.name = g.name,
                grant.amount = g.amount,
                grant.status = g.status
        """
        self._run(cypher, {"grants": valid})

    def upsert_document(self, filename: str, quarter: str, year: int, department: str) -> None:
        self._run(
            """MERGE (doc:Document {filename: $filename})
               SET doc.quarter = $quarter, doc.year = $year, doc.department = $department""",
            {"filename": filename, "quarter": quarter, "year": year, "department": department},
        )

    def upsert_relationships(self, relationships: list[dict[str, Any]]) -> None:
        """
        Each relationship dict: {from, from_type, relationship, to, to_type}
        Supported relationship types from spec:
          DIRECTS, MANAGES, REPORTS_TO, HAS_PROJECT, REPORTED_IN, MANAGES_GRANT, MENTIONED_IN
        """
        for rel in relationships:
            rel_type = rel.get("relationship", "").upper()
            if not rel_type:
                continue
            from_type = rel.get("from_type", "Person")
            to_type = rel.get("to_type", "Department")
            cypher = (
                f"MATCH (a:{from_type} {{name: $from_name}}) "
                f"MATCH (b:{to_type} {{name: $to_name}}) "
                f"MERGE (a)-[:{rel_type}]->(b)"
            )
            try:
                self._run(cypher, {"from_name": rel["from"], "to_name": rel["to"]})
            except Exception as e:
                logger.warning("Failed to create relationship %s: %s", rel, e)

    def link_chunks_to_entities(self, chunk_ids: list[str], graph_data: dict) -> None:
        """
        Create Chunk nodes and MENTIONS edges to every entity extracted from those chunks.
        This is what lets us answer "find all text about Public Works" via graph traversal.
        """
        if not chunk_ids or not graph_data:
            return

        # Ensure Chunk nodes exist
        self._run(
            "UNWIND $ids AS cid MERGE (c:Chunk {chunk_id: cid})",
            {"ids": chunk_ids},
        )

        # Link to departments
        for dept in graph_data.get("departments", []):
            if not dept.get("name"):
                continue
            self._run(
                """UNWIND $ids AS cid
                   MATCH (c:Chunk {chunk_id: cid})
                   MATCH (d:Department {name: $name})
                   MERGE (c)-[:MENTIONS]->(d)""",
                {"ids": chunk_ids, "name": dept["name"]},
            )

        # Link to projects
        for proj in graph_data.get("projects", []):
            if not proj.get("name"):
                continue
            self._run(
                """UNWIND $ids AS cid
                   MATCH (c:Chunk {chunk_id: cid})
                   MATCH (p:Project {name: $name})
                   MERGE (c)-[:MENTIONS]->(p)""",
                {"ids": chunk_ids, "name": proj["name"]},
            )

        # Link to grants
        for grant in graph_data.get("grants", []):
            if not grant.get("name"):
                continue
            self._run(
                """UNWIND $ids AS cid
                   MATCH (c:Chunk {chunk_id: cid})
                   MATCH (g:Grant {name: $name})
                   MERGE (c)-[:MENTIONS]->(g)""",
                {"ids": chunk_ids, "name": grant["name"]},
            )

        logger.debug(
            "Linked %d chunks to entities (%d depts, %d projects, %d grants)",
            len(chunk_ids),
            len(graph_data.get("departments", [])),
            len(graph_data.get("projects", [])),
            len(graph_data.get("grants", [])),
        )

    def get_chunk_ids_for_entities(self, entity_names: list[str]) -> list[str]:
        """Return chunk_ids that MENTION any of the given entity names."""
        if not entity_names:
            return []
        results = self._run(
            """MATCH (c:Chunk)-[:MENTIONS]->(e)
               WHERE e.name IN $names
               RETURN DISTINCT c.chunk_id AS chunk_id""",
            {"names": entity_names},
        )
        return [r["chunk_id"] for r in results]

    def link_department_to_document(self, department: str, filename: str) -> None:
        self._run(
            """MATCH (d:Department {name: $dept})
               MATCH (doc:Document {filename: $filename})
               MERGE (d)-[:REPORTED_IN]->(doc)""",
            {"dept": department, "filename": filename},
        )

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def execute_cypher(self, cypher: str) -> list[dict[str, Any]]:
        """Execute a read-only Cypher query and return results."""
        try:
            return self._run(cypher)
        except Exception as e:
            logger.error("Cypher execution failed: %s\n%s", e, cypher)
            return []

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def clear_document_data(self, filename: str) -> None:
        """Remove all data extracted from a specific source file."""
        self._run(
            "MATCH (doc:Document {filename: $filename}) DETACH DELETE doc",
            {"filename": filename},
        )

    def get_all_departments(self) -> list[str]:
        results = self._run("MATCH (d:Department) RETURN d.name AS name")
        return [r["name"] for r in results]

    def get_department_staff(self, department: str) -> list[dict]:
        return self._run(
            """MATCH (p:Person)-[r:DIRECTS|MANAGES]->(d:Department {name: $dept})
               RETURN p.name AS name, p.title AS title, type(r) AS role""",
            {"dept": department},
        )
