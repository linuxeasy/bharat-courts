import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from bharat_courts.models import CaseDetail, CaseInfo

# Ensure examples is importable
examples_dir = str(Path(__file__).parent.parent / "examples")
if examples_dir not in sys.path:
    sys.path.insert(0, examples_dir)

from DistrictSearch import (  # noqa: E402
    ResolvedCourt,
    find_match,
    normalize,
    parse_target_line,
    resolve_court_targets,
    scan_resolved_courts,
)


def test_normalize():
    assert normalize("Thane, District and Sessions Court") == "thane district and sessions court"
    assert normalize("Civil & Criminal Court") == "civil and criminal court"
    assert normalize("  Mumbai - City   Civil  Court  ") == "mumbai city civil court"


def test_parse_target_line():
    state, dist, complex_name = parse_target_line(
        "Maharashtra - Thane - Thane, District and Sessions Court"
    )
    assert state == "Maharashtra"
    assert dist == "Thane"
    assert complex_name == "Thane, District and Sessions Court"

    # Malformed line
    assert parse_target_line("Invalid - Line") is None


def test_find_match_exact_and_containment():
    candidates = {
        "1037001@5@N": "Dindoshi, City Civil Court",
        "1037002@6@N": "Sewree, City Civil Court",
        "1037003@7@N": "Mumbai City Sessions Court",
    }
    # Exact normalized
    match = find_match("Dindoshi, City Civil Court", candidates)
    assert match == ("1037001@5@N", "Dindoshi, City Civil Court")

    # Punctuation/spacing variance
    match = find_match("Dindoshi City Civil Court", candidates)
    assert match == ("1037001@5@N", "Dindoshi, City Civil Court")

    # Containment match
    match = find_match("Dindoshi", candidates)
    assert match == ("1037001@5@N", "Dindoshi, City Civil Court")

    # No match
    assert find_match("Pune Cantonment Court", candidates) is None


@pytest.mark.asyncio
async def test_resolve_court_targets_success_and_failures():
    client = AsyncMock()
    # Mock states
    client.list_states.return_value = {"1": "Maharashtra", "2": "Delhi"}
    # Mock districts
    client.list_districts.side_effect = lambda state: (
        {"37": "Mumbai City Civil Court", "12": "Thane"} if state == "1" else {}
    )
    # Mock complexes
    client.list_complexes.side_effect = lambda state, dist: (
        {
            "1037001@5,6@Y": "Dindoshi, City Civil Court",
            "1037002@N": "Sewree, City Civil Court",
            "1037003@@Y": "Court Complex With Dynamic Est",
        }
        if dist == "37"
        else {"1012001@N": "Thane, District and Sessions Court"}
        if dist == "12"
        else {}
    )
    # Mock dynamic establishments
    client.list_establishments.return_value = {"20": "Establishment A", "21": "Establishment B"}

    targets = [
        "Maharashtra - Mumbai City Civil Court - Dindoshi, City Civil Court",
        "Maharashtra - Thane - Thane, District and Sessions Court",
        "Maharashtra - Mumbai City Civil Court - Court Complex With Dynamic Est",
        "Maharashtra - Unknown District - Some Court",
        "Unknown State - District - Some Court",
        "Maharashtra - Mumbai City Civil Court - Nonexistent Complex",
        "Malformed Line Without Dashes",
    ]

    resolved, unresolved = await resolve_court_targets(client, targets)

    # Check resolved
    assert len(resolved) == 3
    r1 = resolved[0]
    assert r1.state_code == "1"
    assert r1.dist_code == "37"
    assert r1.complex_code == "1037001"
    assert r1.est_codes == ["5", "6"]
    assert r1.needs_establishment is True

    r2 = resolved[1]
    assert r2.state_code == "1"
    assert r2.dist_code == "12"
    assert r2.complex_code == "1012001"
    assert r2.est_codes == []
    assert r2.needs_establishment is False

    r3 = resolved[2]
    assert r3.state_code == "1"
    assert r3.dist_code == "37"
    assert r3.complex_code == "1037003"
    assert r3.est_codes == ["20", "21"]
    assert r3.needs_establishment is True
    client.list_establishments.assert_called_once_with("1", "37", "1037003")

    # Check unresolved
    assert len(unresolved) == 4
    assert unresolved[0].failure_level == "District"
    assert unresolved[0].attempted_query == "Unknown District"
    assert "Mumbai City Civil Court" in unresolved[0].available_options

    assert unresolved[1].failure_level == "State"
    assert unresolved[1].attempted_query == "Unknown State"

    assert unresolved[2].failure_level == "Court Complex"
    assert unresolved[2].attempted_query == "Nonexistent Complex"

    assert unresolved[3].failure_level == "Format Error"

    # Verify caching: list_states called once despite multiple targets
    assert client.list_states.call_count == 1
    # list_districts called once for state '1'
    assert client.list_districts.call_count == 1
    # list_complexes called once for (1, 37) and once for (1, 12)
    assert client.list_complexes.call_count == 2


@pytest.mark.asyncio
async def test_scan_resolved_courts_multiple_establishments():
    client = AsyncMock()
    lists_client = AsyncMock()

    mock_case_1 = MagicMock(spec=CaseInfo)
    mock_case_1.case_number = "CC/101/2024"
    mock_case_1.petitioner = "Ramesh Kumar"
    mock_case_1.respondent = "Suresh Sharma"
    mock_case_1.cnr_number = "MHDD010012342024"

    mock_case_2 = MagicMock(spec=CaseInfo)
    mock_case_2.case_number = "CC/202/2024"
    mock_case_2.petitioner = "Anita Roy"
    mock_case_2.respondent = "Vikas Roy"
    mock_case_2.cnr_number = "MHDD010056782024"

    # Return mock_case_1 for est "5", mock_case_2 for est "6"
    client.case_status_by_advocate_name.side_effect = lambda **kwargs: (
        [mock_case_1] if kwargs.get("est_code") == "5" else [mock_case_2]
    )

    mock_detail = MagicMock(spec=CaseDetail)
    mock_detail.next_hearing_date = "2026-12-15"
    client.case_status_by_cnr.return_value = mock_detail

    lists_client.upsert_hearing.return_value = "Created item"

    courts = [
        ResolvedCourt(
            raw_target="Maharashtra - Mumbai City Civil Court - Dindoshi, City Civil Court",
            state_name="Maharashtra",
            state_code="1",
            dist_name="Mumbai City Civil Court",
            dist_code="37",
            complex_name="Dindoshi, City Civil Court",
            complex_code="1037001",
            est_codes=["5", "6"],
            needs_establishment=True,
        )
    ]

    total_cases, scanned_count = await scan_resolved_courts(
        client=client,
        lists_client=lists_client,
        resolved_courts=courts,
        advocate_name="Kaushik and Co",
        status_filter="Pending",
    )

    assert scanned_count == 1
    assert total_cases == 2
    assert client.case_status_by_advocate_name.call_count == 2
    client.case_status_by_advocate_name.assert_any_call(
        state_code="1",
        dist_code="37",
        court_complex_code="1037001",
        est_code="5",
        advocate_name="Kaushik and Co",
        status_filter="Pending",
    )
    client.case_status_by_advocate_name.assert_any_call(
        state_code="1",
        dist_code="37",
        court_complex_code="1037001",
        est_code="6",
        advocate_name="Kaushik and Co",
        status_filter="Pending",
    )
    assert lists_client.upsert_hearing.call_count == 2
