"""Personal Microsoft Lists client using Playwright persistent browser context."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

logger = logging.getLogger(__name__)


def parse_lists_url(url: str) -> tuple[str, str]:
    """Extract (list_guid, user_id) from personal lists.live.com URL.

    Example URL:
    https://lists.live.com/?listId=2448d6908f6a4d75af37cc138991094e%5Ff5b969a95d74bb55
    """
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    raw_list_id = qs.get("listId", [""])[0]
    raw_list_id = unquote(raw_list_id)

    parts = raw_list_id.split("_")
    if len(parts) != 2 or not parts[0] or not parts[1]:
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
    """Map display names (lowercase) to SharePoint EntityPropertyName or InternalName."""
    mapping: dict[str, str] = {}
    for f in fields:
        title = f.get("Title", "").strip().lower()
        internal = f.get("EntityPropertyName") or f.get("InternalName", "")
        field_type = f.get("TypeAsString", "")

        # In SharePoint, LinkTitle and LinkTitleNoMenu are computed fields that display Title.
        # When a user renames the Title column (e.g. to "Parties"), LinkTitle inherits that
        # display name, but the underlying writable text column remains 'Title'.
        if internal in ("LinkTitle", "LinkTitleNoMenu"):
            if title:
                mapping[title] = "Title"
            continue

        # Skip other computed / read-only fields as they cannot be written to
        if field_type == "Computed":
            continue

        if title and internal:
            mapping[title] = internal

    # Fallbacks for standard columns
    if "parties" not in mapping and "title" in mapping:
        mapping["parties"] = mapping["title"]
    elif "parties" not in mapping:
        mapping["parties"] = "Title"

    return mapping


def lookup_field(field_map: dict[str, str], candidates: list[str], fallback: str) -> str:
    """Find property name by trying candidate display names, substring match, then fallback."""
    # 1. Exact match on normalized candidate
    for c in candidates:
        if c.lower() in field_map:
            return field_map[c.lower()]

    # 2. Substring match
    for c in candidates:
        c_low = c.lower()
        for k, v in field_map.items():
            if c_low in k or k in c_low:
                return v

    return fallback


class PersonalListsClient:
    """Async client for personal Microsoft Lists (lists.live.com)."""

    def __init__(
        self,
        list_url: str,
        session_dir: str | Path = ".ms_lists_session",
        force_login: bool = False,
    ):
        self.list_url = list_url
        self.session_dir = Path(session_dir)
        self.force_login = force_login
        self.state_file = self.session_dir / "storage_state.json"
        self.list_guid, self.user_id = parse_lists_url(list_url)
        self.base_site = f"https://lists.live.com/personal/{self.user_id}"
        self.api_list = f"{self.base_site}/_api/Web/Lists(guid'{self.list_guid}')"

        self._playwright = None
        self._context = None
        self._page = None
        self._request_digest: str | None = None
        self._field_map: dict[str, str] = {}

    async def _restore_storage_state(self):
        """Restore saved cookies into the current context."""
        if self.force_login or not self.state_file.exists():
            return
        try:
            state = json.loads(self.state_file.read_text())
            cookies = state.get("cookies", [])
            if cookies and self._context:
                await self._context.add_cookies(cookies)
        except Exception as e:
            logger.warning("Could not restore storage state: %s", e)

    async def _save_storage_state(self):
        """Export all session and persistent cookies to storage_state.json."""
        if not self._context:
            return
        try:
            await self._context.storage_state(path=str(self.state_file))
        except Exception as e:
            logger.warning("Could not save storage state: %s", e)

    async def __aenter__(self) -> "PersonalListsClient":
        try:
            from playwright.async_api import async_playwright
        except ImportError as err:
            raise RuntimeError(
                "Microsoft Lists integration requires 'playwright'. "
                "Install with: pip install playwright && playwright install chromium"
            ) from err

        self._playwright = await async_playwright().start()
        self.session_dir.mkdir(parents=True, exist_ok=True)

        if self.force_login:
            if self.state_file.exists():
                self.state_file.unlink(missing_ok=True)
            is_authed = False
        else:
            self._context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.session_dir.resolve()),
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            await self._restore_storage_state()
            self._page = await self._context.new_page()

            is_authed = await self._check_auth()
            if not is_authed:
                # Try headless navigation first to allow silent SSO / cookie refresh
                try:
                    await self._page.goto(
                        self.list_url, wait_until="domcontentloaded", timeout=15000
                    )
                    await self._page.wait_for_timeout(2000)
                    if (
                        "login.live.com" not in self._page.url
                        and "lists.live.com" in self._page.url
                    ):
                        is_authed = await self._check_auth()
                        if is_authed:
                            await self._save_storage_state()
                except Exception:
                    is_authed = False

        if not is_authed:
            print("\n" + "=" * 60)
            print("Microsoft Lists: Login required.")
            print("Opening browser for you to sign in to your Microsoft Account...")
            print("=" * 60 + "\n")
            if self._context:
                await self._context.close()

            self._context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.session_dir.resolve()),
                headless=False,
                args=["--disable-blink-features=AutomationControlled"],
            )
            await self._restore_storage_state()
            self._page = await self._context.new_page()
            await self._page.goto(self.list_url, wait_until="load")

            print("Waiting for login to complete... (navigate to list)")
            await self._page.wait_for_url(
                lambda url: "lists.live.com" in url and "login.live.com" not in url,
                timeout=300000,
            )
            await self._page.wait_for_timeout(3000)
            await self._save_storage_state()
            print("Successfully authenticated and session saved!\n")

        await self._init_session_tokens()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._context:
            await self._save_storage_state()
            await self._context.close()
        if self._playwright:
            await self._playwright.stop()

    async def _check_auth(self) -> bool:
        """Check if current session is authorized against the list API."""
        try:
            resp = await self._page.request.get(
                f"{self.api_list}?$select=Id,Title",
                headers={"Accept": "application/json;odata=nometadata"},
            )
            return resp.status == 200
        except Exception:
            return False

    async def _init_session_tokens(self):
        """Fetch X-RequestDigest form token for write operations."""
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
        """Fetch list field definitions and map display names to internal names."""
        if self._field_map:
            return self._field_map

        resp = await self._page.request.get(
            f"{self.api_list}/Fields?$select=Title,InternalName,EntityPropertyName,TypeAsString,Hidden",
            headers={"Accept": "application/json;odata=nometadata"},
        )
        if resp.status != 200:
            text = await resp.text()
            raise RuntimeError(f"Failed to fetch fields (HTTP {resp.status}): {text}")

        data = await resp.json()
        self._field_map = normalize_field_map(data.get("value", []))
        logger.info("Resolved list fields: %s", self._field_map)
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
        """Find an existing list item matching the CNR number."""
        fields = await self.resolve_fields()
        cnr_field = lookup_field(
            fields, ["cnr no", "cnr number", "cnr_no", "cnrno", "cnr"], "CNR_x0020_No"
        )

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
        """Create a new item in the Microsoft List."""
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
        """Update an existing item in the Microsoft List."""
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
        """Upsert a case hearing record into the Microsoft List.

        Returns 'created', 'updated', or 'unchanged'.
        """
        field_map = await self.resolve_fields()

        k_parties = lookup_field(field_map, ["parties", "party", "title"], "Title")
        k_hearing = lookup_field(
            field_map,
            ["next hearing", "next hearing date", "next_hearing", "hearing"],
            "Next_x0020_Hearing",
        )
        k_case_no = lookup_field(
            field_map, ["case no", "case number", "case_no", "caseno"], "Case_x0020_No"
        )
        k_cnr = lookup_field(
            field_map, ["cnr no", "cnr number", "cnr_no", "cnrno", "cnr"], "CNR_x0020_No"
        )
        k_court = lookup_field(field_map, ["court", "court name", "complex"], "Court")

        # Format date as YYYY-MM-DD date-only string
        date_str: str | None = None
        if next_hearing_date:
            if isinstance(next_hearing_date, (datetime, date)):
                date_str = next_hearing_date.strftime("%Y-%m-%d")
            else:
                s = str(next_hearing_date).strip()
                date_str = s[:10] if len(s) >= 10 and s[4] == "-" else s

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


async def _standalone_test():
    test_url = "https://lists.live.com/?listId=2448d6908f6a4d75af37cc138991094e%5Ff5b969a95d74bb55"
    print("Testing Microsoft Lists connection...")
    async with PersonalListsClient(list_url=test_url) as client:
        fields = await client.resolve_fields()
        print("\nDiscovered Column Mapping:")
        for k in ["parties", "next hearing", "case no", "cnr no", "court", "title"]:
            print(f"  - '{k}' -> '{fields.get(k, '<not found>')}'")

        print("\nAttempting test upsert...")
        status = await client.upsert_hearing(
            cnr="MHDD01TEST0001",
            case_no="TEST/1/2026",
            parties="Test Petitioner vs Test Respondent",
            next_hearing_date=date(2026, 12, 1),
            court_name="Dindoshi, City Civil Court",
        )
        print(f"Test upsert result: {status}")


if __name__ == "__main__":
    asyncio.run(_standalone_test())
