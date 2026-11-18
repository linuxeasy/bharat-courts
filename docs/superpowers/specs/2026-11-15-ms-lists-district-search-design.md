# Design Document: Microsoft Lists Sync for District Court Hearings

**Date:** 2026-11-15  
**Status:** Approved  
**Target:** `src/bharat_courts/integrations/ms_lists.py`, `src/bharat_courts/cli.py`, `examples/DistrictSearch.py`  

---

## 1. Overview & Goal

The user monitors pending court cases in District Courts using `examples/DistrictSearch.py` and the CLI. The goal is to synchronize pending court case hearings directly into a personal Microsoft List on `lists.live.com`:
- **List URL:** `https://lists.live.com/?listId=2448d6908f6a4d75af37cc138991094e%5Ff5b969a95d74bb55`
- **List ID:** `2448d690-8f6a-4d75-af37-cc138991094e`
- **Account / Personal Site ID:** `f5b969a95d74bb55`
- **Target Columns:**
  - `Parties`: `"Petitioner vs Respondent"`
  - `next hearing date`: Date / DateTime ISO string (e.g. `YYYY-MM-DD` or `YYYY-MM-DDTHH:MM:SSZ`)
  - `case no`: e.g. `CC/123/2024`
  - `CNR No`: Unique Case National Record number (e.g. `MHDD010012342024`)
  - `Court`: Complex name (e.g. `Dindoshi, City Civil Court`)

Both programmatic access (via `examples/DistrictSearch.py` / SDK) and CLI access (via `bharat-courts districtcourts sync-lists`) will be provided.

---

## 2. Architecture & Authentication

### 2.1 Why Playwright Persistent Context?
`lists.live.com` is Microsoft Lists for personal accounts (preview). Standard Microsoft Graph API (`graph.microsoft.com/v1.0/sites/.../lists`) returns `Not supported` for personal accounts (`Sites.ReadWrite.All` delegated permissions are restricted to work/school tenants).

`lists.live.com` is powered by an internal SharePoint Online engine hosted at:
`https://lists.live.com/personal/f5b969a95d74bb55/_api/...`

By launching Playwright with a persistent user data directory (`user_data_dir=".ms_lists_session"`):
1. **First-time / Expired Run:** Playwright launches a non-headless Chromium window. The user logs in to their Microsoft account once. Playwright waits until navigation reaches `lists.live.com`.
2. **Subsequent Runs:** Playwright runs headlessly, instantly reusing the cached session cookies and local storage from `.ms_lists_session`.
3. **API Execution via `page.evaluate()` / `page.request`:** Requests executed inside the browser session automatically inherit authentication cookies, anti-CSRF request digests, and tokens without manual header extraction.

---

## 3. Data Mapping & Schema Introspection

### 3.1 Dynamic Field Introspection
Microsoft Lists / SharePoint internal column names often differ from display names (e.g., `next hearing date` might be `next_x0020_hearing_x0020_date` or `field_2`, `case no` might be `case_x0020_no`, and `Parties` might be mapped to `Title`).

On initialization, the client calls:
```
GET https://lists.live.com/personal/f5b969a95d74bb55/_api/Web/Lists(guid'2448d690-8f6a-4d75-af37-cc138991094e')/Fields
Accept: application/json;odata=nometadata
```
It builds a case-insensitive map of `Title` (Display Name) -> `InternalName`, matching:
- `"Parties"` -> internal field name (or `Title` fallback)
- `"next hearing date"` -> internal field name
- `"case no"` -> internal field name
- `"CNR No"` -> internal field name
- `"Court"` -> internal field name

### 3.2 Upsert & Deduplication Strategy
For each case:
1. **Query Existing:**
   ```
   GET .../items?$filter={cnr_field} eq '{cnr_number}'
   ```
2. **If Existing:**
   - Check if `next hearing date` or other fields differ.
   - If changed, issue a `POST` with `X-HTTP-Method: MERGE` and `If-Match: *` to update the row.
   - If unchanged, skip to avoid unnecessary writes.
3. **If Not Existing:**
   - Issue a `POST` to `.../items` with the new item payload and `X-RequestDigest`.

---

## 4. Components & File Layout

1. **`src/bharat_courts/integrations/ms_lists.py`**
   - Class `PersonalListsClient`:
     - URL parsing (`parse_lists_url`).
     - `__aenter__` / `__aexit__`: Manages `async_playwright()`, launches persistent browser context with `.ms_lists_session`.
     - `ensure_login(list_url)`: Checks if logged in; opens headful browser if needed and prompts user.
     - `resolve_fields()`: Fetches field definitions and maps display names to internal names.
     - `upsert_hearing(cnr, case_no, parties, next_hearing_date, court_name)`: Queries and creates/updates list item.
2. **`src/bharat_courts/cli.py`**
   - New CLI command under `districtcourts`:
     ```bash
     bharat-courts districtcourts sync-lists \
       --list-url "https://lists.live.com/?listId=..." \
       --state 1 --dist 37 --complex 10370000000000 \
       --advocate "Kaushik and Co" --status pending
     ```
3. **`examples/DistrictSearch.py`**
   - Integrates `PersonalListsClient` into the existing loop:
     ```python
     from bharat_courts.integrations.ms_lists import PersonalListsClient

     async with PersonalListsClient(list_url=LIST_URL) as lists_client:
         ...
         for c in adv_cases:
             case_detail = await client.case_status_by_cnr(c.cnr_number)
             ...
             await lists_client.upsert_hearing(
                 cnr=c.cnr_number,
                 case_no=c.case_number,
                 parties=f"{c.petitioner} vs {c.respondent}",
                 next_hearing_date=case_detail.next_hearing_date,
                 court_name=complex_name,
             )
     ```
4. **`.gitignore`**
   - Adds `.ms_lists_session/` to prevent committing browser profile and credentials to git.

---

## 5. Error Handling & Edge Cases

- **Playwright Missing:** If `playwright` is not installed, accessing `PersonalListsClient` or the CLI command gives a clear, helpful error message:
  `"Microsoft Lists integration requires playwright. Run: pip install playwright && playwright install chromium"`
- **Session Expiry:** If the API returns `401 Unauthorized` or redirects to `login.live.com`, the client catches it and prompts the user with a visible browser window to re-authenticate.
- **Null Next Hearing Date:** If `case_detail.next_hearing_date` is `None`, the script either clears or omits the date field and logs that no next hearing date is currently scheduled.

---

## 6. Verification Plan

1. Verify `playwright` installation in the environment.
2. Unit test `parse_lists_url` and field mapping normalization in `tests/test_ms_lists.py`.
3. Test CLI command registration and argument validation in `tests/test_cli.py`.
4. Run standalone test for `PersonalListsClient` against `https://lists.live.com/?listId=2448d6908f6a4d75af37cc138991094e_f5b969a95d74bb55`.
5. Execute `examples/DistrictSearch.py` and verify pending cases appear on Microsoft Lists.
