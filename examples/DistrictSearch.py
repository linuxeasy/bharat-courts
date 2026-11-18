import asyncio

from bharat_courts import DistrictCourtClient
from bharat_courts.captcha.ocr import OCRCaptchaSolver
from bharat_courts.districtcourts.parser import parse_complex_value


async def main():
    solver = OCRCaptchaSolver()

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
        next(complex_iter) # move to 3rd value Dindoshi, City Civil Court
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
            status_filter="Pending"
        )
        for c in adv_cases:

            case_detail = await client.case_status_by_cnr(c.cnr_number)
            print(f"{c.case_number}: {c.petitioner} vs {c.respondent}  [{c.cnr_number}] - Next hearing: {case_detail.next_hearing_date}, Court:{complex_name}")





asyncio.run(main())
