# Batch District Court Search Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enable batch district court scanning in `examples/DistrictSearch.py` across a user-provided list of "State - District - Court Complex" entries with hierarchy matching, failure reporting, and Microsoft Lists synchronization.

**Architecture:** Two-phase execution: Phase 1 resolves and validates target strings against cached eCourts hierarchy dropdowns (`list_states`, `list_districts`, `list_complexes`) using normalized fuzzy matching, reporting any resolution failures upfront. Phase 2 sequentially scans all resolved courts by advocate name, fetches CNR details, and syncs to Microsoft Lists, finishing with a comprehensive summary.

**Tech Stack:** Python 3.11+, `bharat_courts` (`DistrictCourtClient`, `PersonalListsClient`, `parse_complex_value`), `asyncio`, `dataclasses`, `re`, `pytest`.

---

## File Structure

- **Modify:** `examples/DistrictSearch.py`
  - Defines `COURT_TARGETS`, `ADVOCATE_NAME`, `STATUS_FILTER`.
  - Defines `ResolvedCourt` and `UnresolvedCourt` dataclasses.
  - Implements `normalize()`, `parse_target_line()`, `find_match()`.
  - Implements `resolve_court_targets()` with in-memory caching for states, districts, and complexes.
  - Implements `scan_resolved_courts()` and orchestrates `main()`.
- **Create:** `tests/test_district_search_resolver.py`
  - Unit tests for string normalization, line parsing, candidate matching, hierarchy resolution (with mock clients), and scanning loop.

---

### Task 1: Core Data Models & Matching Helpers

**Files:**
- Create: `tests/test_district_search_resolver.py`
- Modify: `examples/DistrictSearch.py`

- [x] **Step 1: Write failing unit test for string normalization and candidate matching**

Create `tests/test_district_search_resolver.py`:

```python
import sys
from pathlib import Path

# Ensure examples is importable
examples_dir = str(Path(__file__).parent.parent / "examples")
if examples_dir not in sys.path:
    sys.path.insert(0, examples_dir)

from DistrictSearch import (
    ResolvedCourt,
    UnresolvedCourt,
    find_match,
    normalize,
    parse_target_line,
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
```

- [x] **Step 2: Run test to verify it fails**

Run:
```bash
.venv/bin/pytest tests/test_district_search_resolver.py -v
```
Expected: FAIL (`ImportError: cannot import name 'ResolvedCourt' from 'DistrictSearch'`)

- [x] **Step 3: Implement data models and matching helpers in DistrictSearch.py**

In `examples/DistrictSearch.py`, add the dataclasses and matching helper functions:

```python
from dataclasses import dataclass
import re

@dataclass
class ResolvedCourt:
    raw_target: str
    state_name: str
    state_code: str
    dist_name: str
    dist_code: str
    complex_name: str
    complex_code: str
    est_code: str
    needs_establishment: bool


@dataclass
class UnresolvedCourt:
    raw_target: str
    failure_level: str  # "State" | "District" | "Court Complex" | "Format Error"
    attempted_query: str
    available_options: list[str]


def normalize(s: str) -> str:
    """Normalize string for fuzzy comparison."""
    s = s.lower().replace("&", " and ")
    s = re.sub(r"[,\.\-_/]+", " ", s)
    return " ".join(s.split())


def parse_target_line(line: str) -> tuple[str, str, str] | None:
    """Parse 'State - District - Court Complex' line into 3 parts."""
    parts = [p.strip() for p in line.split(" - ")]
    if len(parts) == 3 and all(parts):
        return parts[0], parts[1], parts[2]
    return None


def find_match(target: str, candidates: dict[str, str]) -> tuple[str, str] | None:
    """Find matching (code, name) in candidates dict {code: name}.

    Tries:
    1. Exact normalized match
    2. Substring containment match (target in candidate or candidate in target)
    """
    target_norm = normalize(target)
    if not target_norm:
        return None

    # 1. Exact normalized match
    for code, name in candidates.items():
        if normalize(name) == target_norm:
            return code, name

    # 2. Substring containment match
    for code, name in candidates.items():
        cand_norm = normalize(name)
        if target_norm in cand_norm or cand_norm in target_norm:
            return code, name

    return None
```

- [x] **Step 4: Run test to verify it passes**

Run:
```bash
.venv/bin/pytest tests/test_district_search_resolver.py -v
```
Expected: PASS (3 tests passed)

---

### Task 2: Hierarchy Resolver with Caching & Establishment Resolution

**Files:**
- Modify: `tests/test_district_search_resolver.py`
- Modify: `examples/DistrictSearch.py`

- [x] **Step 1: Write failing unit test for `resolve_court_targets`**

Add to `tests/test_district_search_resolver.py`:

```python
from unittest.mock import AsyncMock
import pytest
from DistrictSearch import resolve_court_targets


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
        }
        if dist == "37"
        else {"1012001@N": "Thane, District and Sessions Court"}
        if dist == "12"
        else {}
    )

    targets = [
        "Maharashtra - Mumbai City Civil Court - Dindoshi, City Civil Court",
        "Maharashtra - Thane - Thane, District and Sessions Court",
        "Maharashtra - Unknown District - Some Court",
        "Unknown State - District - Some Court",
        "Maharashtra - Mumbai City Civil Court - Nonexistent Complex",
        "Malformed Line Without Dashes",
    ]

    resolved, unresolved = await resolve_court_targets(client, targets)

    # Check resolved
    assert len(resolved) == 2
    r1 = resolved[0]
    assert r1.state_code == "1"
    assert r1.dist_code == "37"
    assert r1.complex_code == "1037001"
    assert r1.est_code == "5"
    assert r1.needs_establishment is True

    r2 = resolved[1]
    assert r2.state_code == "1"
    assert r2.dist_code == "12"
    assert r2.complex_code == "1012001"
    assert r2.est_code == ""
    assert r2.needs_establishment is False

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
```

- [x] **Step 2: Run test to verify it fails**

Run:
```bash
.venv/bin/pytest tests/test_district_search_resolver.py::test_resolve_court_targets_success_and_failures -v
```
Expected: FAIL (`ImportError: cannot import name 'resolve_court_targets' from 'DistrictSearch'`)

- [x] **Step 3: Implement `resolve_court_targets` in DistrictSearch.py**

In `examples/DistrictSearch.py`, implement `resolve_court_targets`:

```python
async def resolve_court_targets(
    client: DistrictCourtClient, targets: list[str]
) -> tuple[list[ResolvedCourt], list[UnresolvedCourt]]:
    """Resolve a list of 'State - District - Court Complex' strings into codes."""
    cached_states: dict[str, str] = {}
    cached_districts: dict[str, dict[str, str]] = {}
    cached_complexes: dict[tuple[str, str], dict[str, str]] = {}

    resolved: list[ResolvedCourt] = []
    unresolved: list[UnresolvedCourt] = []

    for raw in targets:
        parsed = parse_target_line(raw)
        if not parsed:
            unresolved.append(
                UnresolvedCourt(
                    raw_target=raw,
                    failure_level="Format Error",
                    attempted_query=raw,
                    available_options=[],
                )
            )
            continue

        state_query, dist_query, complex_query = parsed

        # 1. State resolution
        if not cached_states:
            cached_states = await client.list_states()

        state_match = find_match(state_query, cached_states)
        if not state_match:
            unresolved.append(
                UnresolvedCourt(
                    raw_target=raw,
                    failure_level="State",
                    attempted_query=state_query,
                    available_options=list(cached_states.values()),
                )
            )
            continue
        state_code, state_name = state_match

        # 2. District resolution
        if state_code not in cached_districts:
            cached_districts[state_code] = await client.list_districts(state_code)

        districts = cached_districts[state_code]
        dist_match = find_match(dist_query, districts)
        if not dist_match:
            unresolved.append(
                UnresolvedCourt(
                    raw_target=raw,
                    failure_level="District",
                    attempted_query=dist_query,
                    available_options=list(districts.values()),
                )
            )
            continue
        dist_code, dist_name = dist_match

        # 3. Complex resolution
        comp_key = (state_code, dist_code)
        if comp_key not in cached_complexes:
            cached_complexes[comp_key] = await client.list_complexes(state_code, dist_code)

        complexes = cached_complexes[comp_key]
        comp_match = find_match(complex_query, complexes)
        if not comp_match:
            unresolved.append(
                UnresolvedCourt(
                    raw_target=raw,
                    failure_level="Court Complex",
                    attempted_query=complex_query,
                    available_options=list(complexes.values()),
                )
            )
            continue
        complex_val, complex_name = comp_match

        # Parse complex value
        complex_code, est_codes, needs_est = parse_complex_value(complex_val)
        est_code = est_codes[0] if (needs_est and est_codes) else ""
        if needs_est and not est_code:
            try:
                establishments = await client.list_establishments(
                    state_code, dist_code, complex_code
                )
                if establishments:
                    est_code = next(iter(establishments))
            except Exception:
                est_code = ""

        resolved.append(
            ResolvedCourt(
                raw_target=raw,
                state_name=state_name,
                state_code=state_code,
                dist_name=dist_name,
                dist_code=dist_code,
                complex_name=complex_name,
                complex_code=complex_code,
                est_code=est_code,
                needs_establishment=needs_est,
            )
        )

    return resolved, unresolved
```

- [x] **Step 4: Run test to verify it passes**

Run:
```bash
.venv/bin/pytest tests/test_district_search_resolver.py -v
```
Expected: PASS (4 tests passed)

---

### Task 3: Scanning Loop, Main Orchestration & Summary Reporting

**Files:**
- Modify: `tests/test_district_search_resolver.py`
- Modify: `examples/DistrictSearch.py`

- [x] **Step 1: Write unit test for `scan_resolved_courts`**

Add to `tests/test_district_search_resolver.py`:

```python
from unittest.mock import MagicMock
from bharat_courts.models import CaseDetail, CaseInfo
from DistrictSearch import scan_resolved_courts


@pytest.mark.asyncio
async def test_scan_resolved_courts():
    client = AsyncMock()
    lists_client = AsyncMock()

    mock_case = MagicMock(spec=CaseInfo)
    mock_case.case_number = "CC/101/2024"
    mock_case.petitioner = "Ramesh Kumar"
    mock_case.respondent = "Suresh Sharma"
    mock_case.cnr_number = "MHDD010012342024"

    client.case_status_by_advocate_name.return_value = [mock_case]

    mock_detail = MagicMock(spec=CaseDetail)
    mock_detail.next_hearing_date = "2026-12-15"
    client.case_status_by_cnr.return_value = mock_detail

    lists_client.upsert_hearing.return_value = "Created item 42"

    courts = [
        ResolvedCourt(
            raw_target="Maharashtra - Mumbai City Civil Court - Dindoshi, City Civil Court",
            state_name="Maharashtra",
            state_code="1",
            dist_name="Mumbai City Civil Court",
            dist_code="37",
            complex_name="Dindoshi, City Civil Court",
            complex_code="1037001",
            est_code="5",
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
    assert total_cases == 1
    client.case_status_by_advocate_name.assert_called_once_with(
        state_code="1",
        dist_code="37",
        court_complex_code="1037001",
        est_code="5",
        advocate_name="Kaushik and Co",
        status_filter="Pending",
    )
    client.case_status_by_cnr.assert_called_once_with("MHDD010012342024")
    lists_client.upsert_hearing.assert_called_once_with(
        cnr="MHDD010012342024",
        case_no="CC/101/2024",
        parties="Ramesh Kumar vs Suresh Sharma",
        next_hearing_date="2026-12-15",
        court_name="Dindoshi, City Civil Court",
    )
```

- [x] **Step 2: Run test to verify it fails**

Run:
```bash
.venv/bin/pytest tests/test_district_search_resolver.py::test_scan_resolved_courts -v
```
Expected: FAIL (`ImportError: cannot import name 'scan_resolved_courts' from 'DistrictSearch'`)

- [x] **Step 3: Implement `scan_resolved_courts` and update `main()` in DistrictSearch.py**

In `examples/DistrictSearch.py`:
- Define `COURT_TARGETS` list at top of file with the 12 court targets.
- Define `ADVOCATE_NAME = "Kaushik and Co"` and `STATUS_FILTER = "Pending"`.
- Implement `scan_resolved_courts`.
- Update `main()` to perform Phase 1 resolution, display matched and failed items, then run Phase 2 scan, and print final summary.

```python
COURT_TARGETS = [
    "Maharashtra - Thane - Thane, District and Sessions Court",
    "Maharashtra - Thane - Mira Bhayandar, Civil and Criminal Court",
    "Maharashtra - Thane - Vasai, District and Addl Sessions",
    "Maharashtra - Maharashtra Family Courts - Bandra, Family Court",
    "Maharashtra - Mumbai Small Causes Court - Mumbai, Small Causes Court",
    "Maharashtra - Mumbai Small Causes Court - Bandra, Small Causes Court",
    "Maharashtra - Mumbai City Civil Court - Sewree, City Civil Court",
    "Maharashtra - Mumbai City Civil Court - Mumbai City Sessions Court",
    "Maharashtra - Mumbai City Civil Court - Dindoshi, City Civil Court",
    "Maharashtra - Mumbai City Civil Court - Mumbai, City Civil Court",
    "Maharashtra - Mumbai CMM Courts - Borivali, MM Court",
    "Maharashtra - Mumbai CMM Courts - Andheri, MM Court",
]

ADVOCATE_NAME = "Kaushik and Co"
STATUS_FILTER = "Pending"


async def scan_resolved_courts(
    client: DistrictCourtClient,
    lists_client: PersonalListsClient | None,
    resolved_courts: list[ResolvedCourt],
    advocate_name: str,
    status_filter: str = "Pending",
) -> tuple[int, int]:
    """Scan all resolved courts for advocate cases and sync to Microsoft Lists."""
    total_cases = 0
    scanned_count = 0

    for court in resolved_courts:
        print(f"\n{'='*70}")
        print(f"Scanning: {court.complex_name} ({court.dist_name}, {court.state_name})")
        print(f"Codes: state={court.state_code}, dist={court.dist_code}, complex={court.complex_code}, est={court.est_code or 'none'}")
        print(f"{'='*70}")

        try:
            adv_cases = await client.case_status_by_advocate_name(
                state_code=court.state_code,
                dist_code=court.dist_code,
                court_complex_code=court.complex_code,
                est_code=court.est_code,
                advocate_name=advocate_name,
                status_filter=status_filter,
            )
            scanned_count += 1
            print(f"Found {len(adv_cases)} {status_filter.lower()} case(s) in {court.complex_name}")

            for c in adv_cases:
                total_cases += 1
                try:
                    case_detail = await client.case_status_by_cnr(c.cnr_number)
                    hearing_date = case_detail.next_hearing_date or "Unknown"
                except Exception as ex:
                    print(f"  Warning: failed to fetch details for CNR {c.cnr_number}: {ex}")
                    hearing_date = "Unknown"

                parties = (
                    f"{c.petitioner} vs {c.respondent}"
                    if (c.petitioner or c.respondent)
                    else (c.case_number or "Unknown")
                )
                print(
                    f"  [{c.cnr_number}] {c.case_number}: {parties} (Next hearing: {hearing_date})"
                )

                if lists_client:
                    try:
                        sync_res = await lists_client.upsert_hearing(
                            cnr=c.cnr_number,
                            case_no=c.case_number,
                            parties=parties,
                            next_hearing_date=hearing_date,
                            court_name=court.complex_name,
                        )
                        print(f"    -> MS Lists: {sync_res}")
                    except Exception as ex:
                        print(f"    -> MS Lists sync error: {ex}")

        except Exception as e:
            print(f"Error querying {court.complex_name}: {e}")

    return total_cases, scanned_count
```

And update `main()`:
```python
async def main():
    solver = OCRCaptchaSolver()

    async with PersonalListsClient(list_url=LIST_URL, force_login=FORCE_LOGIN) as lists_client:
        async with DistrictCourtClient(captcha_solver=solver) as client:
            print(f"Phase 1: Resolving {len(COURT_TARGETS)} court hierarchy targets...")
            resolved, unresolved = await resolve_court_targets(client, COURT_TARGETS)

            print(f"\nResolution Results:")
            print(f"  Successfully matched: {len(resolved)} / {len(COURT_TARGETS)}")
            for r in resolved:
                print(f"    [MATCHED] {r.state_name} > {r.dist_name} > {r.complex_name}")

            if unresolved:
                print(f"\n  Failed to match: {len(unresolved)} item(s):")
                for u in unresolved:
                    print(f"    [FAILED] {u.raw_target}")
                    print(f"      Level: {u.failure_level} (searched: '{u.attempted_query}')")
                    if u.available_options:
                        opts_preview = ", ".join(u.available_options[:10])
                        if len(u.available_options) > 10:
                            opts_preview += f", ... (+{len(u.available_options) - 10} more)"
                        print(f"      Available at this level: [{opts_preview}]")

            if not resolved:
                print("\nNo courts could be resolved. Exiting.")
                return

            print(f"\nPhase 2: Scanning {len(resolved)} court complexes for advocate '{ADVOCATE_NAME}'...")
            total_cases, scanned_count = await scan_resolved_courts(
                client=client,
                lists_client=lists_client,
                resolved_courts=resolved,
                advocate_name=ADVOCATE_NAME,
                status_filter=STATUS_FILTER,
            )

            print(f"\n{'='*70}")
            print(f"FINAL SUMMARY")
            print(f"{'='*70}")
            print(f"Targets configured:  {len(COURT_TARGETS)}")
            print(f"Matched and scanned: {scanned_count}")
            print(f"Unmatched targets:   {len(unresolved)}")
            print(f"Total cases found:   {total_cases}")
            print(f"{'='*70}")
```

- [x] **Step 4: Run test to verify it passes**

Run:
```bash
.venv/bin/pytest tests/test_district_search_resolver.py -v
```
Expected: PASS (all 5 tests pass)

- [x] **Step 5: Code quality, formatting, and linting**

Run:
```bash
.venv/bin/ruff check examples/DistrictSearch.py tests/test_district_search_resolver.py
.venv/bin/ruff format --check examples/DistrictSearch.py tests/test_district_search_resolver.py
```
Expected: All checks pass cleanly without errors or warnings.

---

### Task 4: Regression Testing & Final Validation

**Files:**
- Entire repository

- [x] **Step 1: Run full unit test suite**

Run:
```bash
.venv/bin/pytest
```
Expected: All 250+ tests pass without any regression.

- [x] **Step 2: Verify Python compilation of DistrictSearch.py**

Run:
```bash
.venv/bin/python -m py_compile examples/DistrictSearch.py
```
Expected: Compiles cleanly with exit code 0.
