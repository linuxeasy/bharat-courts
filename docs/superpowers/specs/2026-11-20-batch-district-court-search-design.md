# Design Document: Batch District Court Search in DistrictSearch.py

**Date:** 2026-11-20  
**Status:** Approved  
**Target:** `examples/DistrictSearch.py`  

---

## 1. Overview & Objective

The existing `examples/DistrictSearch.py` script targets a hardcoded single court complex (Mumbai City Civil Court -> Dindoshi, City Civil Court). The user needs the ability to scan across a user-defined list of multiple courts provided in the format:

```text
State - District - Court Complex
```

The script must:
1. Parse and match each entry against live eCourts district court dropdown listings (`list_states`, `list_districts`, `list_complexes`).
2. Identify and clearly report any item that fails to match (stating the level at which resolution failed and listing the available valid choices).
3. Scan all successfully resolved courts for cases matching advocate name criteria (`"Kaushik and Co"`, status `"Pending"`).
4. Fetch case details by CNR and synchronize with Microsoft Lists.
5. Provide a clear summary report of scanned courts, unmatched courts, and total cases found/synced.

---

## 2. Target Court Complexes

The initial configured list of court complexes is:

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
```

---

## 3. Architecture & Data Structures

### 3.1 Data Models
We define two dataclasses inside `examples/DistrictSearch.py`:

```python
@dataclass
class ResolvedCourt:
    raw_target: str
    state_name: str
    state_code: str
    dist_name: str
    dist_code: str
    complex_name: str
    complex_code: str
    est_codes: list[str]
    needs_establishment: bool

@dataclass
class UnresolvedCourt:
    raw_target: str
    failure_level: str  # "State" | "District" | "Court Complex"
    attempted_query: str
    available_options: list[str]
```

### 3.2 String Normalization and Matching Strategy
To handle minor spacing, punctuation, and case differences between user input and portal dropdowns:
- Helper `normalize(s: str) -> str`: lowercases, replaces `&` with `and`, replaces punctuation (`[,.\-_/]+`) with spaces, and collapses whitespace.
- Helper `find_match(target: str, candidates: dict[str, str]) -> tuple[str, str] | None`:
  1. Compares `normalize(target) == normalize(name)`.
  2. If no exact normalized match, checks containment: `normalize(target) in normalize(name)` or `normalize(name) in normalize(target)`.
  3. Returns `(code, name)` if a match is found, or `None`.

### 3.3 Discovery Cache
To minimize redundant network calls across targets sharing the same state or district:
- `cached_states: dict[str, str] = {}`
- `cached_districts: dict[str, dict[str, str]] = {}` (`state_code -> {dist_code: dist_name}`)
- `cached_complexes: dict[tuple[str, str], dict[str, str]] = {}` (`(state_code, dist_code) -> {complex_val: complex_name}`)

---

## 4. Execution Flow

### 4.1 Phase 1: Upfront Hierarchy Resolution
1. Connect via `DistrictCourtClient` and `PersonalListsClient`.
2. For each line in `COURT_TARGETS`:
   - Parse into `(state_query, dist_query, complex_query)`.
   - Match state against `list_states()` (cached). If unmatched, record `UnresolvedCourt` (level `"State"`) and continue.
   - Match district against `list_districts(state_code)` (cached). If unmatched, record `UnresolvedCourt` (level `"District"`) and continue.
   - Match complex against `list_complexes(state_code, dist_code)` (cached). If unmatched, record `UnresolvedCourt` (level `"Court Complex"`) and continue.
   - If matched: call `parse_complex_value(complex_val)` to get `(complex_code, est_codes, needs_est)`. If `needs_est` and not `est_codes`, dynamically fetch `list_establishments` to get all establishment keys. If not `needs_est`, `est_codes = []`.
   - Append to `resolved_courts`.
3. Print resolution table:
   - List all resolved courts with codes and all establishment codes.
   - List all unresolved courts with specific failure reasons and available options.

### 4.2 Phase 2: Sequential Court Scanning & Synchronization
For each `ResolvedCourt`:
1. Print court header banner: `=== Scanning {complex_name} ({dist_name}, {state_name}) ===`.
2. Determine establishment targets: `target_ests = court.est_codes if (court.needs_establishment and court.est_codes) else [""]`.
3. For each establishment code in `target_ests`:
   - Query `case_status_by_advocate_name(state_code, dist_code, court_complex_code, est_code, advocate_name, status_filter)`.
4. Wrap court queries in try/except so individual network or CAPTCHA errors are logged without aborting remaining courts.
5. Deduplicate cases by CNR and for each case:
   - Query `case_status_by_cnr(c.cnr_number)`.
   - Format parties and next hearing date.
   - Upsert hearing into Microsoft Lists via `lists_client.upsert_hearing(...)`.
   - Print case details.

### 4.3 Phase 3: Final Summary
Print a summary table:
- Total targets configured.
- Successfully resolved and scanned courts count.
- Unresolved courts count (with breakdown).
- Total pending cases discovered and synchronized.

---

## 5. Error Handling & Edge Cases
- **Missing or malformed target lines:** Validates that target has 3 segments separated by ` - `. Malformed lines are reported as `"Format Error"` and skipped.
- **Portal anti-bot / CAPTCHA failures:** Handled by existing `DistrictCourtClient` retry mechanisms; any per-court exception is caught and logged.
- **Establishment requirement:** If a complex flag is `Y` and `est_codes` is empty in the complex value, dynamically calls `client.list_establishments` to resolve the first establishment code.
