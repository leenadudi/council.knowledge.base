"""Regression guards for the classifier prompt's SQL/Cypher generation contract.

The retriever executes the classifier-generated `sql_query`/`graph_query` strings
verbatim with NO parameter binding, so the prompt must constrain generation to
literal, schema-correct queries. These bugs were observed in production:
  - `quarter = 1` (quarter is VARCHAR 'Q1', not integer)
  - filtering `grants` by quarter/year (grants has no such columns)
  - Cypher with `$department_name` (no params are bound -> query fails)
"""

from src.query.classifier import _CLASSIFY_PROMPT


def test_prompt_states_quarter_is_quoted_string():
    assert "quarter = 'Q1'" in _CLASSIFY_PROMPT
    assert "Never write `quarter = 1`" in _CLASSIFY_PROMPT


def test_prompt_warns_grants_has_no_quarter_year():
    # The prompt lists the tables that lack quarter/year; grants must be among them.
    assert "have NO `quarter`/`year`" in _CLASSIFY_PROMPT
    assert "`grants`" in _CLASSIFY_PROMPT


def test_prompt_requires_source_file_for_citations():
    # SQL must always return source_file so answers cite the PDF, not the datastore.
    assert "source_file" in _CLASSIFY_PROMPT
    assert "STRING_AGG(DISTINCT source_file" in _CLASSIFY_PROMPT


def test_prompt_forbids_unbound_cypher_parameters():
    assert "$parameters" in _CLASSIFY_PROMPT
    assert "literal" in _CLASSIFY_PROMPT.lower()
