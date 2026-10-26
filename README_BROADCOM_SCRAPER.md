# Broadcom PTF / USERMOD scraper and PTF detail table

## Run it
    pip install -r requirements.txt
    streamlit run app.py                    # tab "Broadcom PTF scraper (test)"
    python scrape_broadcom_first_page.py --limit 20            # command line, writes Broadcom_first_page_PTFs.xlsx
    python scrape_broadcom_first_page.py --ptf-details 10      # adds the per-PTF detail sheet

## What it does
1. Opens the Broadcom grid (`zos_compat.HTML`) and reads every product row with its z/OS V3R2 / V3R1 / V2R5 cells.
2. Follows each cell's "PTFs" / "Additional Information" link to its section on the page and extracts PTF IDs
   (two letters + five digits, e.g. LU18187) and USERMOD / SYSMOD IDs (IDs on a line that names a USERMOD or SYSMOD).
3. Follows the extra hyperlinks found in those sections (and optionally each product page), one level deep, and lists IDs
   found there separately as "Additional PTFs (linked pages)" because they can belong to another release.
4. Writes the IDs into the dashboard columns: "If Yes, PTF Required?" (e.g. `Yes - LU18187`), "PTF IDs",
   "USERMOD / SYSMOD IDs", "Additional PTFs (linked pages)" and the per-release text column. Excel output is IBM Carbon styled.

## PTF detail table (columns: # | Product / Release | PTF ID | FMID | PTF publish date | APAR ID | Prerequisites | Superseding PTF | Full description | Detail Extraction Status)
- Publish date, product/release and the short description come from Broadcom's public Solutions List (last three years).
- FMID, APAR, prerequisites, superseding PTF and the full description live on the solution page, which needs a Broadcom Support login.
  Provide them from a source you are entitled to use: an uploaded CSV (see `examples/ptf_details_export_example.csv`) or an authorized session
  (`BROADCOM_PTF_DETAIL_URL`, `BROADCOM_SUPPORT_COOKIE`). Without one, those cells read **Not accessible**.
- Status values: Verified | Partial | Not found in source | Access denied | Fetch failed | Not attempted.
  *Not accessible* and *Not found in source* never mean "none".

## Tests (offline, no network needed)
    python tests/test_scraper.py
    python tests/test_ptf_details.py
    python tests/test_app_integration.py      # needs streamlit installed

---
# Other vendors (IBM, BMC, Rocket, Precisely, ASG)
    streamlit run app.py                         # tab "Vendor PTF scraper (all vendors)"
    python scrape_vendors.py                     # all five vendors -> Vendor_PTFs.xlsx
    python scrape_vendors.py --vendors BMC --login BMC --max-pages 25 --per-vendor

- **Pages:** if a source has more than one page, at least 10 pages are searched (HTML next links, numbered pagination, `?page=N`; PDFs page by page). Every page visited is listed on the "Pages searched" sheet.
- **Login:** a page that needs one is reported as `Login required`. Enter user name + password (form login or HTTP Basic) or a signed-in cookie in the dashboard, or set `<VENDOR>_USERNAME` / `_PASSWORD` / `_COOKIE`. Credentials go only to that vendor's own hosts, are never written to disk, logs or Excel, and a login form that posts to a foreign host is refused.
- **Release attribution:** IDs are attached to z/OS 3.2 / 3.1 / 2.5 only when the passage names the release; otherwise they appear as "Release not stated".
- **Excel:** same sheets and Carbon styling as `Broadcom_first_page_PTFs_SAMPLE_snapshot.xlsx`, plus "Pages searched".
- `Vendor_PTFs_LAYOUT_EXAMPLE_synthetic.xlsx` shows the layout only; its IDs are synthetic test values.
- Tests: `python tests/test_vendor_scraper.py`
