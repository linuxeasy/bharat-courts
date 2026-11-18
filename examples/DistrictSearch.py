import asyncio
import os

from bharat_courts import DistrictCourtClient
from bharat_courts.captcha.ocr import OCRCaptchaSolver
from bharat_courts.districtcourts.parser import parse_complex_value
from bharat_courts.integrations.ms_lists import PersonalListsClient

LIST_URL = "https://lists.live.com/?listId=2448d6908f6a4d75af37cc138991094e%5Ff5b969a95d74bb55"
FORCE_LOGIN = os.getenv("MS_LISTS_FORCE_LOGIN", "").lower() in ("1", "true", "yes")


async def main():
    solver = OCRCaptchaSolver()

    async with PersonalListsClient(list_url=LIST_URL, force_login=FORCE_LOGIN) as lists_client:
        async with DistrictCourtClient(captcha_solver=solver) as client:
            # 1. Discover the court hierarchy (no CAPTCHA)
            states = await client.list_states()
            print(f"States found: {len(states)}")

            districts = await client.list_districts("1")  # Maharashtra
            print(f"Districts in Maharashtra: {len(districts)}")

            complexes = await client.list_complexes("1", "37")  # Mumbai City Civil Court
            print(f"Complexes in Mumbai City Civil Court: {len(complexes)}")

            # 2. Parse the complex value → (complex_code, est_codes, needs_establishment)
            complex_iter = iter(complexes.items())
            next(complex_iter)
            next(complex_iter)  # move to 3rd value Dindoshi, City Civil Court
            complex_item = next(complex_iter)
            complex_val = complex_item[0]
            complex_name = complex_item[1]
            complex_code, est_codes, needs_est = parse_complex_value(complex_val)
            est_code = est_codes[0] if needs_est else ""

            # # 2b. If the complex needs an establishment, list them and pick one
            # if needs_est:
            #     establishments = await client.list_establishments("1", "37", complex_code)
            #     est_code = next(iter(establishments))
            #
            # # 3. Discover case types for this court (no CAPTCHA)
            # case_types = await client.list_case_types("1", "37", complex_code, est_code)
            # print(f"Case types available: {len(case_types)}")
            #
            # # 4. Search by party name (CAPTCHA auto-solved and retried)
            # cases = await client.case_status_by_party(
            #     state_code="1",
            #     dist_code="37",
            #     court_complex_code=complex_code,
            #     est_code=est_code,
            #     party_name="kumar",
            #     year="2024",
            # )
            # for c in cases:
            #     print(f"{c.case_number}: {c.petitioner} vs {c.respondent}  [{c.cnr_number}]")
            #
            # 4b. Search by advocate name (CAPTCHA auto-solved and retried)
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
                parties = (
                    f"{c.petitioner} vs {c.respondent}"
                    if (c.petitioner or c.respondent)
                    else (c.case_number or "Unknown")
                )
                print(
                    f"{c.case_number}: {parties}  [{c.cnr_number}] - "
                    f"Next hearing: {case_detail.next_hearing_date}, Court: {complex_name}"
                )

                sync_res = await lists_client.upsert_hearing(
                    cnr=c.cnr_number,
                    case_no=c.case_number,
                    parties=parties,
                    next_hearing_date=case_detail.next_hearing_date,
                    court_name=complex_name,
                )
                print(f"  -> Microsoft Lists: {sync_res}")


if __name__ == "__main__":
    asyncio.run(main())
