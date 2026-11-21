import asyncio
import os
import re
from dataclasses import dataclass

from bharat_courts import DistrictCourtClient
from bharat_courts.captcha.ocr import OCRCaptchaSolver
from bharat_courts.districtcourts.parser import parse_complex_value
from bharat_courts.integrations.ms_lists import PersonalListsClient

LIST_URL = "https://lists.live.com/?listId=2448d6908f6a4d75af37cc138991094e%5Ff5b969a95d74bb55"
FORCE_LOGIN = os.getenv("MS_LISTS_FORCE_LOGIN", "").lower() in ("1", "true", "yes")


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
        if needs_est and not est_codes:
            try:
                establishments = await client.list_establishments(
                    state_code, dist_code, complex_code
                )
                est_codes = list(establishments.keys())
            except Exception:
                est_codes = []
        elif not needs_est:
            est_codes = []

        resolved.append(
            ResolvedCourt(
                raw_target=raw,
                state_name=state_name,
                state_code=state_code,
                dist_name=dist_name,
                dist_code=dist_code,
                complex_name=complex_name,
                complex_code=complex_code,
                est_codes=est_codes,
                needs_establishment=needs_est,
            )
        )

    return resolved, unresolved


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
    seen_cnrs: set[str] = set()

    for court in resolved_courts:
        print(f"\n{'=' * 70}")
        print(f"Scanning: {court.complex_name} ({court.dist_name}, {court.state_name})")
        est_desc = (
            f"{len(court.est_codes)} establishment(s): {court.est_codes}"
            if (court.needs_establishment and court.est_codes)
            else "none"
        )
        print(
            f"Codes: state={court.state_code}, dist={court.dist_code}, "
            f"complex={court.complex_code}, est={est_desc}"
        )
        print(f"{'=' * 70}")

        target_ests = court.est_codes if (court.needs_establishment and court.est_codes) else [""]

        court_had_success = False
        for est_code in target_ests:
            try:
                adv_cases = await client.case_status_by_advocate_name(
                    state_code=court.state_code,
                    dist_code=court.dist_code,
                    court_complex_code=court.complex_code,
                    est_code=est_code,
                    advocate_name=advocate_name,
                    status_filter=status_filter,
                )
                court_had_success = True
                est_label = f" (est: {est_code})" if est_code else ""
                print(
                    f"Found {len(adv_cases)} {status_filter.lower()} case(s) in "
                    f"{court.complex_name}{est_label}"
                )

                for c in adv_cases:
                    if c.cnr_number in seen_cnrs:
                        continue
                    seen_cnrs.add(c.cnr_number)
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
                        f"  [{c.cnr_number}] {c.case_number}: {parties} "
                        f"(Next hearing: {hearing_date})"
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
                print(f"Error querying {court.complex_name} (est: {est_code or 'none'}): {e}")

        if court_had_success:
            scanned_count += 1

    return total_cases, scanned_count


async def main():
    solver = OCRCaptchaSolver()

    async with PersonalListsClient(list_url=LIST_URL, force_login=FORCE_LOGIN) as lists_client:
        async with DistrictCourtClient(captcha_solver=solver) as client:
            print(f"Phase 1: Resolving {len(COURT_TARGETS)} court hierarchy targets...")
            resolved, unresolved = await resolve_court_targets(client, COURT_TARGETS)

            print("\nResolution Results:")
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

            print(
                f"\nPhase 2: Scanning {len(resolved)} court complexes "
                f"for advocate '{ADVOCATE_NAME}'..."
            )
            total_cases, scanned_count = await scan_resolved_courts(
                client=client,
                lists_client=lists_client,
                resolved_courts=resolved,
                advocate_name=ADVOCATE_NAME,
                status_filter=STATUS_FILTER,
            )

            print(f"\n{'=' * 70}")
            print("FINAL SUMMARY")
            print(f"{'=' * 70}")
            print(f"Targets configured:  {len(COURT_TARGETS)}")
            print(f"Matched and scanned: {scanned_count}")
            print(f"Unmatched targets:   {len(unresolved)}")
            print(f"Total cases found:   {total_cases}")
            print(f"{'=' * 70}")


if __name__ == "__main__":
    asyncio.run(main())
