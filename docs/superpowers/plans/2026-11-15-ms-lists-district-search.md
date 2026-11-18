# Microsoft Lists Sync for District Court Hearings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automatically upsert pending district court hearings found in `DistrictSearch.py` and via a new CLI command `bharat-courts districtcourts sync-lists` into a personal Microsoft List on `lists.live.com` using Playwright persistent browser session and SharePoint REST API.

**Architecture:** A reusable async client `PersonalListsClient` in `src/bharat_courts/integrations/ms_lists.py` launches a Chromium persistent browser context in `.ms_lists_session` (prompting interactive login on first run and running headlessly afterwards). It queries the list's field definitions to map display names (`Parties`, `next hearing date`, `case no`, `CNR No`, `Court`) to internal SharePoint column names, and performs deduplication/upserts via SharePoint REST APIs. Both `examples/DistrictSearch.py` and `bharat-courts districtcourts sync-lists` call this client.

**Tech Stack:** Python 3.11+, Playwright (`async_playwright`), `click`, `asyncio`.

---

### Task 1: Environment & Git Configuration

**Files:**
- Modify: `.gitignore`

- [ ] **Step 1: Check existing .gitignore**
Verify if `.ms_lists_session/` or Playwright cache files are ignored.

- [ ] **Step 2: Update .gitignore**
Add `.ms_lists_session/` to `.gitignore` so browser authentication profiles and credentials are never tracked by git.

```gitignore
# Microsoft Lists browser session cache
.ms_lists_session/
```

- [ ] **Step 3: Verify git status**
Run: `git status`
Expected: `.gitignore` modified, `.ms_lists_session` ignored.

---

### Task 2: Create `src/bharat_courts/integrations/ms_lists.py` Core Client and Unit Tests

**Files:**
- Create: `src/bharat_courts/integrations/__init__.py`
- Create: `src/bharat_courts/integrations/ms_lists.py`
- Create: `tests/test_ms_lists.py`

- [ ] **Step 1: Write unit tests for URL parsing and field mapping**

Write `tests/test_ms_lists.py`:
```python
from bharat_courts.integrations.ms_lists import parse_lists_url, normalize_field_map
import pytest


def test_parse_lists_url_valid():
    url = "https://lists.live.com/?listId=2448d6908f6a4d75af37cc138991094e%5Ff5b969a95d74bb55"
    guid, user_id = parse_lists_url(url)
    assert guid == "2448d690-8f6a-4d75-af37-cc138991094e"
    assert user_id == "f5b969a95d74bb55"


def test_parse_lists_url_invalid():
    with pytest.raises(ValueError):
        parse_lists_url("https://lists.live.com/?listId=invalid")


def test_normalize_field_map():
    raw_fields = [
        {"Title": "Title", "InternalName": "Title"},
        {"Title": "Next Hearing Date", "InternalName": "next_x0020_hearing_x0020_date"},
        {"Title": "Case No", "InternalName": "case_x0020_no"},
        {"Title": "CNR No", "InternalName": "CNR_x0020_No"},
        {"Title": "Court", "InternalName": "Court"},
    ]
    mapping = normalize_field_map(raw_fields)
    assert mapping["parties"] == "Title"
    assert mapping["next hearing date"] == "next_x0020_hearing_x0020_date"
    assert mapping["case no"] == "case_x0020_no"
    assert mapping["cnr no"] == "CNR_x0020_No"
    assert mapping["court"] == "Court"
```

- [ ] **Step 2: Run test to verify it fails**
Run: `pytest tests/test_ms_lists.py`
Expected: FAIL (module not found).

- [ ] **Step 3: Implement `parse_lists_url`, `normalize_field_map`, and `PersonalListsClient`**

Create `src/bharat_courts/integrations/__init__.py`:
```python
"""Third-party and external service integrations."""
```

Create `src/bharat_courts/integrations/ms_lists.py`:
```python
"""Personal Microsoft Lists client using Playwright persistent browser context."""

from __future__ import annotations

import asyncio
from datetime import date, datetime
import logging
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

logger = logging.getLogger(__name__)


def parse_lists_url(url: str) -> tuple[str, str]:
    """Extract (list_guid, user_id) from personal lists.live.com URL."""
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    raw_list_id = qs.get("listId", [""])[0]
    raw_list_id = unquote(raw_list_id)

    parts = raw_list_id.split("_")
    if len(parts) != 2:
        raise ValueError(
            f"Invalid listId format '{raw_list_id}'. Expected '{{list_guid}}_{{user_id}}'"
        )

    list_hex, user_id = parts[0], parts[1]
    if len(list_hex) == 32 and "-" not in list_hex:
        list_guid = (
            f"{list_hex[:8]}-{list_hex[8:12]}-{list_hex[12:16]}-{list_hex[16:20]}-{list_hex[20:]}"
        )
    else:
        list_guid = list_hex

    return list_guid, user_id


def normalize_field_map(fields: list[dict[str, Any]]) -> dict[str, str]:
    """Map display names (lowercase) to SharePoint InternalName."""
    mapping: dict[str, str] = {}
    for f in fields:
        title = f.get("Title", "").strip().lower()
        internal = f.get("InternalName", "")
        if title and internal:
            mapping[title] = internal

    # Fallbacks
    if "parties" not in mapping and "title" in mapping:
        mapping["parties"] = mapping["title"]

    return mapping


class PersonalListsClient:
    """Async client for personal Microsoft Lists (lists.live.com)."""

    def __init__(
        self,
        list_url: str,
        session_dir: str | Path = ".ms_lists_session",
    ):
        self.list_url = list_url
        self.session_dir = Path(session_dir)
        self.list_guid, self.user_id = parse_lists_url(list_url)
        self.base_site = f"https://lists.live.com/personal/{self.user_id}"
        self.api_list = f"{self.base_site}/_api/Web/Lists(guid'{self.list_guid}')"

        self._playwright = None
        self._context = None
        self._page = None
        self._request_digest: str | None = None
        self._field_map: dict[str, str] = {}

    async def __aenter__(self) -> "PersonalListsClient":
        try:
            from playwright.async_api import async_playwright
        except ImportError as err:
            raise RuntimeError(
                "Microsoft Lists sync requires playwright. "
                "Install with: pip install playwright && playwright install chromium"
            ) from err

        self._playwright = await async_playwright().start()
        self.session_dir.mkdir(parents=True, exist_ok=True)

        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.session_dir.resolve()),
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        self._page = await self._context.new_page()

        is_authed = await self._check_auth()
        if not is_authed:
            print("\n" + "=" * 60)
            print("Microsoft Lists: Login required.")
            print("Opening browser for you to sign in to your Microsoft Account...")
            print("=" * 60 + "\n")
            await self._context.close()

            self._context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.session_dir.resolve()),
                headless=False,
                args=["--disable-blink-features=AutomationControlled"],
            )
            self._page = await self._context.new_page()
            await self._page.goto(self.list_url, wait_until="load")

            print("Waiting for login to complete... (navigate to list)")
            await self._page.wait_for_url(
                lambda url: "lists.live.com" in url and "login.live.com" not in url,
                timeout=300000,
            )
            await self._page.wait_for_timeout(3000)
            print("Successfully authenticated and session saved!\n")

        await self._init_session_tokens()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._context:
            await self._context.close()
        if self._playwright:
            await self._playwright.stop()

    async def _check_auth(self) -> bool:
        try:
            resp = await self._page.request.get(
                f"{self.api_list}?$select=Id,Title",
                headers={"Accept": "application/json;odata=nometadata"},
            )
            return resp.status == 200
        except Exception:
            return False

    async def _init_session_tokens(self):
        resp = await self._page.request.post(
            f"{self.base_site}/_api/contextinfo",
            headers={"Accept": "application/json;odata=nometadata"},
        )
        if resp.status == 200:
            data = await resp.json()
            self._request_digest = data.get("FormDigestValue")
        else:
            logger.warning("Failed to obtain FormDigestValue: %s", resp.status)

    async def resolve_fields(self) -> dict[str, str]:
        if self._field_map:
            return self._field_map

        resp = await self._page.request.get(
            f"{self.api_list}/Fields?$select=Title,InternalName,TypeAsString,Hidden",
            headers={"Accept": "application/json;odata=nometadata"},
        )
        if resp.status != 200:
            text = await resp.text()
            raise RuntimeError(f"Failed to fetch fields (HTTP {resp.status}): {text}")

        data = await resp.json()
        self._field_map = normalize_field_map(data.get("value", []))
        return self._field_map

    def _get_headers(self, merge: bool = False) -> dict[str, str]:
        headers = {
            "Accept": "application/json;odata=nometadata",
            "Content-Type": "application/json;odata=nometadata",
        }
        if self._request_digest:
            headers["X-RequestDigest"] = self._request_digest
        if merge:
            headers["X-HTTP-Method"] = "MERGE"
            headers["If-Match"] = "*"
        return headers

    async def find_item_by_cnr(self, cnr: str) -> dict[str, Any] | None:
        fields = await self.resolve_fields()
        cnr_field = fields.get("cnr no") or fields.get("cnr") or "CNR_x0020_No"

        query_url = f"{self.api_list}/items?$filter={cnr_field} eq '{cnr}'&$top=1"
        resp = await self._page.request.get(
            query_url,
            headers={"Accept": "application/json;odata=nometadata"},
        )
        if resp.status == 200:
            data = await resp.json()
            items = data.get("value", [])
            return items[0] if items else None
        return None

    async def add_item(self, payload: dict[str, Any]) -> dict[str, Any]:
        resp = await self._page.request.post(
            f"{self.api_list}/items",
            headers=self._get_headers(),
            data=payload,
        )
        if resp.status not in (200, 201):
            text = await resp.text()
            raise RuntimeError(f"Failed to create list item (HTTP {resp.status}): {text}")
        return await resp.json()

    async def update_item(self, item_id: int, payload: dict[str, Any]):
        resp = await self._page.request.post(
            f"{self.api_list}/items({item_id})",
            headers=self._get_headers(merge=True),
            data=payload,
        )
        if resp.status not in (200, 204):
            text = await resp.text()
            raise RuntimeError(f"Failed to update list item {item_id} (HTTP {resp.status}): {text}")

    async def upsert_hearing(
        self,
        cnr: str,
        case_no: str,
        parties: str,
        next_hearing_date: date | datetime | str | None,
        court_name: str,
    ) -> str:
        field_map = await self.resolve_fields()

        def get_field_key(display_name: str, fallback: str) -> str:
            return field_map.get(display_name.lower(), fallback)

        k_parties = get_field_key("parties", "Title")
        k_hearing = get_field_key("next hearing date", "next_x0020_hearing_x0020_date")
        k_case_no = get_field_key("case no", "case_x0020_no")
        k_cnr = get_field_key("cnr no", "CNR_x0020_No")
        k_court = get_field_key("court", "Court")

        date_str: str | None = None
        if next_hearing_date:
            if isinstance(next_hearing_date, datetime):
                date_str = next_hearing_date.strftime("%Y-%m-%dT00:00:00Z")
            elif isinstance(next_hearing_date, date):
                date_str = f"{next_hearing_date.isoformat()}T00:00:00Z"
            else:
                date_str = str(next_hearing_date)

        payload = {
            k_parties: parties,
            k_case_no: case_no,
            k_cnr: cnr,
            k_court: court_name,
        }
        if date_str is not None:
            payload[k_hearing] = date_str

        existing = await self.find_item_by_cnr(cnr)
        if existing:
            item_id = existing.get("Id") or existing.get("ID")
            changed = False
            for k, v in payload.items():
                existing_val = existing.get(k)
                if k == k_hearing and existing_val and v:
                    if str(existing_val)[:10] != str(v)[:10]:
                        changed = True
                elif existing_val != v:
                    changed = True

            if changed:
                await self.update_item(item_id, payload)
                return "updated"
            return "unchanged"
        else:
            await self.add_item(payload)
            return "created"
```

- [ ] **Step 4: Run test to verify it passes**
Run: `pytest tests/test_ms_lists.py`
Expected: PASS.

---

### Task 3: Add `sync-lists` Command to `src/bharat_courts/cli.py`

**Files:**
- Modify: `src/bharat_courts/cli.py`
- Modify: `tests/test_cli.py`

- [ ] **Step 1: Write CLI command test in `tests/test_cli.py`**
Verify `bharat-courts districtcourts sync-lists --help` prints available options.

```python
def test_dc_sync_lists_help():
    from click.testing import CliRunner
    from bharat_courts.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["districtcourts", "sync-lists", "--help"])
    assert result.exit_code == 0
    assert "--list-url" in result.output
    assert "--advocate" in result.output
    assert "--state" in result.output
```

- [ ] **Step 2: Run test to verify it fails**
Run: `pytest tests/test_cli.py::test_dc_sync_lists_help`
Expected: FAIL (No such command 'sync-lists').

- [ ] **Step 3: Implement `dc_sync_lists` in `src/bharat_courts/cli.py`**

Under `# districtcourts group`:
```python
@districtcourts.command("sync-lists")
@click.option(
    "--list-url",
    required=True,
    envvar="BHARAT_COURTS_MS_LISTS_URL",
    help="Microsoft Lists URL.",
)
@click.option("--state", "state_code", required=True, help="State code.")
@click.option("--dist", "dist_code", required=True, help="District code.")
@click.option("--complex", "complex_code", required=True, help="Court complex code.")
@click.option("--est", "est_code", default="", help="Establishment code (optional).")
@click.option("--court-name", default="District Court", help="Court complex display name.")
@click.option("--advocate", "advocate_name", required=True, help="Advocate name to search.")
@click.option(
    "--status",
    type=click.Choice(["pending", "disposed", "both"]),
    default="pending",
    help="Filter by case status (default: pending).",
)
@click.pass_context
def dc_sync_lists(
    ctx: click.Context,
    list_url: str,
    state_code: str,
    dist_code: str,
    complex_code: str,
    est_code: str,
    court_name: str,
    advocate_name: str,
    status: str,
):
    """Search district court cases by advocate and sync next hearing dates to Microsoft Lists."""
    from bharat_courts.districtcourts.client import DistrictCourtClient
    from bharat_courts.integrations.ms_lists import PersonalListsClient

    status_filter = {"pending": "Pending", "disposed": "Disposed", "both": "Both"}[status]

    async def _go():
        synced_records = []
        async with PersonalListsClient(list_url=list_url) as lists_client:
            async with DistrictCourtClient() as client:
                cases = await client.case_status_by_advocate_name(
                    state_code=state_code,
                    dist_code=dist_code,
                    court_complex_code=complex_code,
                    est_code=est_code,
                    advocate_name=advocate_name,
                    status_filter=status_filter,
                )
                if not _is_json(ctx):
                    click.echo(f"Found {len(cases)} case(s) for advocate '{advocate_name}'")

                for c in cases:
                    detail = await client.case_status_by_cnr(c.cnr_number)
                    parties = (
                        f"{c.petitioner} vs {c.respondent}"
                        if (c.petitioner or c.respondent)
                        else (c.case_number or "Unknown")
                    )
                    res = await lists_client.upsert_hearing(
                        cnr=c.cnr_number,
                        case_no=c.case_number,
                        parties=parties,
                        next_hearing_date=detail.next_hearing_date,
                        court_name=court_name,
                    )
                    rec = {
                        "case_number": c.case_number,
                        "cnr_number": c.cnr_number,
                        "parties": parties,
                        "next_hearing_date": str(detail.next_hearing_date)
                        if detail.next_hearing_date
                        else None,
                        "sync_status": res,
                    }
                    synced_records.append(rec)
                    if not _is_json(ctx):
                        click.echo(
                            f"[{res.upper()}] {c.case_number} ({c.cnr_number}) — "
                            f"Next hearing: {detail.next_hearing_date}"
                        )
        return synced_records

    records = _run(_go())
    if _is_json(ctx):
        _emit_json(records)
```

- [ ] **Step 4: Run test to verify it passes**
Run: `pytest tests/test_cli.py::test_dc_sync_lists_help`
Expected: PASS.

---

### Task 4: Update `examples/DistrictSearch.py`

**Files:**
- Modify: `examples/DistrictSearch.py`

- [ ] **Step 1: Integrate `PersonalListsClient` into `DistrictSearch.py`**

Update `examples/DistrictSearch.py` to import `from bharat_courts.integrations.ms_lists import PersonalListsClient`, initialize it with the user's list URL, and call `upsert_hearing`:

```python
import asyncio

from bharat_courts import DistrictCourtClient
from bharat_courts.captcha.ocr import OCRCaptchaSolver
from bharat_courts.districtcourts.parser import parse_complex_value
from bharat_courts.integrations.ms_lists import PersonalListsClient

LIST_URL = "https://lists.live.com/?listId=2448d6908f6a4d75af37cc138991094e%5Ff5b969a95d74bb55"


async def main():
    solver = OCRCaptchaSolver()

    async with PersonalListsClient(list_url=LIST_URL) as lists_client:
        async with DistrictCourtClient(captcha_solver=solver) as client:
            # 1. Discover the court hierarchy (no CAPTCHA)
            states = await client.list_states()
            print(f"States found: {len(states)}")

            districts = await client.list_districts("1")  # Maharashtra
            print(f"Districts in Maharashtra: {len(districts)}")

            complexes = await client.list_complexes("1", "37")  # Mumbai City Civil Court
            print(f"Complexes in Mumbai City Civil Court: {len(complexes)}")

            # 2. Parse the complex value -> (complex_code, est_codes, needs_establishment)
            complex_iter = iter(complexes.items())
            next(complex_iter)
            next(complex_iter)  # move to 3rd value Dindoshi, City Civil Court
            complex_item = next(complex_iter)
            complex_val = complex_item[0]
            complex_name = complex_item[1]
            complex_code, est_codes, needs_est = parse_complex_value(complex_val)
            est_code = est_codes[0] if needs_est else ""

            # 3. Search by advocate name (CAPTCHA auto-solved and retried)
            adv_cases = await client.case_status_by_advocate_name(
                state_code="1",
                dist_code="37",
                court_complex_code=complex_code,
                est_code=est_code,
                advocate_name="Kaushik and Co",
                status_filter="Pending",
            )
            print(f"\nFound {len(adv_cases)} pending case(s) for 'Kaushik and Co'")

            for c in adv_cases:
                case_detail = await client.case_status_by_cnr(c.cnr_number)
                print(
                    f"{c.case_number}: {c.petitioner} vs {c.respondent}  "
                    f"[{c.cnr_number}] - Next hearing: {case_detail.next_hearing_date}, "
                    f"Court: {complex_name}"
                )

                sync_res = await lists_client.upsert_hearing(
                    cnr=c.cnr_number,
                    case_no=c.case_number,
                    parties=f"{c.petitioner} vs {c.respondent}",
                    next_hearing_date=case_detail.next_hearing_date,
                    court_name=complex_name,
                )
                print(f"  -> Microsoft Lists: {sync_res}")


if __name__ == "__main__":
    asyncio.run(main())
```

---

### Task 5: Run Linting, Type Checks, and Tests

- [ ] **Step 1: Run ruff check and format**
Run: `ruff check . && ruff format --check .`
Expected: 0 errors.

- [ ] **Step 2: Run full unit test suite**
Run: `pytest`
Expected: All tests pass.
