import pytest

from src.ingestion.entity_keys import (
    department_key, member_key, vendor_key, infer_dept_type,
)
from src.dashboard.aggregator import DashboardAggregator
from src.dashboard.projects import _normalize_party


@pytest.mark.parametrize("a,b", [
    ("Bureau of Police", "Buraeu of Police"),          # typo differs — NOT auto-merged
    ("Office of Equity & Compliance", "Office of Equity and Compliance"),  # &/and merge
    ("Department of Facilities & Special Projects", "Department of Facilities and Special Projects"),
])
def test_department_key_merges_and_separates(a, b):
    # &/and variants collapse; typos do not
    if "and" in a.lower() or "&" in a:
        assert department_key(a) == department_key(b)
    else:
        assert department_key(a) != department_key(b)


def test_department_key_strips_generic_prefixes():
    assert department_key("Bureau of Fire") == "fire"
    assert department_key("Department of Public Works") == "public works"
    assert department_key("The Office of Equity and Compliance") == "office of equity & compliance"
    assert department_key(None) == ""
    assert department_key("") == ""


def test_department_key_matches_dashboard_base():
    # Parity guard: the SQL dimension key MUST equal the dashboard's own key.
    for name in ["Bureau of Codes", "Office of Equity and Compliance",
                 "Department of Public Works", "Parks & Recreation", "City Controller"]:
        assert department_key(name) == DashboardAggregator._dept_key_base(name)


def test_member_key_strips_titles_and_lowercases():
    assert member_key("Council Member Wanda Williams") == member_key("Wanda Williams")
    assert member_key("Hon. Mr. Danielle Bowers") == "danielle bowers"
    assert member_key(None) == ""


def test_vendor_key_drops_corporate_suffixes():
    assert vendor_key("Lamar Advertising, LLC") == vendor_key("Lamar Advertising")
    assert vendor_key("McCormick Law Firm Inc.") == "mccormick law firm"
    assert vendor_key(None) == ""


def test_vendor_key_matches_dashboard():
    for name in ["Lamar Advertising, LLC", "McCormick Law Firm", "PennDot"]:
        assert vendor_key(name) == _normalize_party(name)


def test_infer_dept_type():
    assert infer_dept_type("Bureau of Fire") == "bureau"
    assert infer_dept_type("Department of Public Works") == "department"
    assert infer_dept_type("Office of Equity") == "office"
    assert infer_dept_type("Harrisburg City Council") == "council"
    assert infer_dept_type("City Controller") == "other"
    assert infer_dept_type(None) is None
