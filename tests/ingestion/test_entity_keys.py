import pytest

from src.ingestion.entity_keys import (
    department_key, member_key, vendor_key, infer_dept_type,
    grant_key, dedupe_grant_rows,
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


@pytest.mark.parametrize("variants,key", [
    # acronym-first, expansion-first, and trailing-noise forms all collapse
    (["CDBG (Community Development Block Grant)",
      "Community Development Block Grant (CDBG)",
      "Community Development Block Grant (CDBG) Program"], "cdbg"),
    (["ESG (Emergency Solutions Grant)",
      "Emergency Solutions Grant (ESG)",
      "Emergency Solutions Grants (ESG) Program",
      "Emergency Solutions Grants (ESG) Program Year 2025"], "esg"),
    (["American Rescue Plan Act (ARPA)",
      "American Rescue Plan Act (ARPA) funds"], "arpa"),
    (["HOME Investment Partnerships (HOME) Program",
      "HOME Investment Partnerships Program (HOME)"], "home"),
    (["TASA Grant", "TASA", "tasa  grant"], "tasa"),
    # hyphenated acronym survives noise words and messy nested parens
    (["CDBG-CV (Community Development Block Grant CARES Act)",
      "CDBG-CV Grant (Community Block Grant-Coronavirus Aid, Relief, and "
      "Economic Security Act (CARES))"], "cdbg cv"),
])
def test_grant_key_merges_same_grant_variants(variants, key):
    assert {grant_key(v) for v in variants} == {key}


def test_grant_key_keeps_distinct_grants_and_subawards_apart():
    # CARES-Act CDBG is a different grant than base CDBG
    assert grant_key("CDBG-CV (Community Development Block Grant CARES Act)") != grant_key("CDBG (Community Development Block Grant)")
    # HOME-ARP is a different program than HOME
    assert grant_key("HOME-ARP") != grant_key("HOME Investment Partnerships (HOME) Program")
    # sub-award descriptors stay distinct from the base grant and each other
    base = grant_key("Community Development Block Grant (CDBG)")
    broad = grant_key("Community Development Block Grant (CDBG) - Broad Street Market")
    rehab = grant_key("Community Development Block Grant (CDBG) – Housing Rehabilitation Program")
    assert len({base, broad, rehab}) == 3
    assert grant_key("Emergency Solutions Grant (ESG) - YWCA (Emergency Shelter)") != \
        grant_key("Emergency Solutions Grant (ESG) - Shalom House (Emergency Shelter)")
    # en-dash and hyphen descriptor separators are equivalent
    assert rehab == grant_key("Community Development Block Grant (CDBG) - Housing Rehabilitation Program")
    # inline vs descriptor form of the same sub-program merge (noise words drop)
    assert rehab == grant_key("CDBG Housing Rehabilitation Program Funds")


def test_grant_key_keeps_non_acronym_parentheticals_as_qualifiers():
    # when neither side of the parens is an acronym, the parenthetical is a
    # sub-award qualifier and must stay in the key
    a = grant_key("Park Improvement Projects Grant (7th & Radnor Field)")
    b = grant_key("Park Improvement Projects Grant (Reservoir Park)")
    assert a != b


def test_grant_key_blank():
    assert grant_key(None) == ""
    assert grant_key("   ") == ""


def test_dedupe_grant_rows_collapses_variants_keeping_most_complete():
    rows = [
        {"grant_name": "CDBG (Community Development Block Grant)",
         "department": "Department of Building & Housing Development",
         "amount": None, "start_date": None, "end_date": None, "status": ""},
        {"grant_name": "Community Development Block Grant (CDBG)",
         "department": "Building & Housing Development",
         "amount": 350000.0, "start_date": None, "end_date": None, "status": "active"},
        {"grant_name": "Community Development Block Grant (CDBG) Program",
         "department": "Department of Building & Housing Development",
         "amount": None, "start_date": None, "end_date": None, "status": ""},
        # same grant, genuinely different department -> kept separate
        {"grant_name": "CDBG (Community Development Block Grant)",
         "department": "Bureau of Fire",
         "amount": None, "start_date": None, "end_date": None, "status": "active"},
        # blank name preserved untouched
        {"grant_name": "", "department": "X",
         "amount": None, "start_date": None, "end_date": None, "status": ""},
    ]
    out = dedupe_grant_rows(rows)
    assert len(out) == 3  # one B&H-family CDBG + one Fire CDBG + blank
    bh = [r for r in out if "Housing" in (r.get("department") or "")]
    assert len(bh) == 1 and bh[0]["amount"] == 350000.0


def test_infer_dept_type():
    assert infer_dept_type("Bureau of Fire") == "bureau"
    assert infer_dept_type("Department of Public Works") == "department"
    assert infer_dept_type("Office of Equity") == "office"
    assert infer_dept_type("Harrisburg City Council") == "council"
    assert infer_dept_type("City Controller") == "other"
    assert infer_dept_type(None) is None
