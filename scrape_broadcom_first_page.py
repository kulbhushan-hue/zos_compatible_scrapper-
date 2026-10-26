#!/usr/bin/env python3
"""Scrape the Broadcom z/OS compatibility grid (first page) and write PTF / USERMOD IDs to Excel.

Examples
    python scrape_broadcom_first_page.py                      # live page, all products, follow hyperlinks
    python scrape_broadcom_first_page.py --limit 20           # quick test: first 20 products
    python scrape_broadcom_first_page.py --filter "Dispatch"  # only products whose name contains this text
    python scrape_broadcom_first_page.py --product-pages      # also read each product's own page
    python scrape_broadcom_first_page.py --no-follow          # grid sections only, no extra hyperlinks
    python scrape_broadcom_first_page.py --html saved_grid.html   # offline: use a saved copy of the page

PTF detail table (PTF ID, FMID, publish date, APAR, prerequisites, superseding PTF, description, status):
    python scrape_broadcom_first_page.py --ptf-details 10                  # first 10 PTFs found in the matrix
    python scrape_broadcom_first_page.py --ptf-details 10 --ptf-source newest   # 10 newest in Broadcom's catalog
    python scrape_broadcom_first_page.py --ptf-details 10 --detail-export my_export.csv
  Authorized detail pages: set BROADCOM_PTF_DETAIL_URL (template with {ptf}) and BROADCOM_SUPPORT_COOKIE.

Behind a corporate proxy, set HTTPS_PROXY (the requests library honours it) and make sure the
runtime may reach ftpdocs.broadcom.com, knowledge.broadcom.com and techdocs.broadcom.com over HTTPS.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

import broadcom_scraper as bs
import ptf_details as pd_
from scrape_excel import build_scrape_workbook


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=bs.GRID_URL, help="grid page URL (default: Broadcom public z/OS grid)")
    ap.add_argument("--html", help="read the grid from a saved HTML file instead of fetching it")
    ap.add_argument("--out", default="Broadcom_first_page_PTFs.xlsx", help="Excel file to write")
    ap.add_argument("--limit", type=int, default=None, help="only the first N products (quick test)")
    ap.add_argument("--filter", default="", help="only products whose name contains this text")
    ap.add_argument("--no-follow", action="store_true", help="do not fetch hyperlinks found in the PTF / Additional Information sections")
    ap.add_argument("--product-pages", action="store_true", help="also fetch and read each product's own compatibility page")
    ap.add_argument("--workers", type=int, default=6, help="parallel page fetches (default 6)")
    ap.add_argument("--ptf-details", type=int, default=0, metavar="N", help="also build the per-PTF detail table for N PTFs")
    ap.add_argument("--ptf-source", choices=["matrix", "newest"], default="matrix",
                    help="matrix = first N PTFs found on the compatibility matrix; newest = N newest PTFs in Broadcom's solutions list")
    ap.add_argument("--ptf-zos", default="3.2", help="z/OS release used to pick matrix PTFs (3.2, 3.1, 2.5 or 'all')")
    ap.add_argument("--detail-export", help="CSV you are entitled to use with PTF ID, FMID, APAR ID, Prerequisites, Superseding PTF, Full description, PTF publish date")
    ap.add_argument("--catalog-html", help="saved copy of Broadcom's Mainframe Products Solutions List (AllProducts.HTML)")
    args = ap.parse_args(argv)

    html = pathlib.Path(args.html).read_bytes() if args.html else None

    def progress(done: int, total: int):
        print(f"\r  hyperlinks scraped: {done}/{total}", end="", file=sys.stderr, flush=True)

    print(f"Scraping {'saved file ' + args.html if html else args.url} ...", file=sys.stderr)
    result = bs.scrape_grid(
        grid_url=args.url, html=html, follow_links=not args.no_follow,
        follow_product_pages=args.product_pages, limit=args.limit, name_filter=args.filter,
        max_workers=args.workers, progress=progress,
    )
    print(file=sys.stderr)
    if not result.records:
        print(f"ERROR: {result.status}", file=sys.stderr)
        return 1

    ptf_table = None
    if args.ptf_details:
        html_cat = pathlib.Path(args.catalog_html).read_bytes() if args.catalog_html else None
        catalog, cat_msg = pd_.load_catalog(html=html_cat)
        print(cat_msg, file=sys.stderr)
        if args.ptf_source == "matrix":
            selection = pd_.ptfs_from_matrix(result, args.ptf_details, None if args.ptf_zos == "all" else args.ptf_zos)
        else:
            selection = pd_.newest_in_catalog(catalog, args.ptf_details)
        overrides = pd_.load_detail_export(args.detail_export) if args.detail_export else {}
        if not overrides and pathlib.Path("data/ptf_details.csv").exists():
            overrides = pd_.load_detail_export("data/ptf_details.csv")
        details = pd_.gather_details([p for p, _, _ in selection], overrides)
        ptf_table = pd_.build_table(selection, catalog, details)
        print(ptf_table[pd_.TABLE_COLUMNS].to_string(index=False, max_colwidth=36), file=sys.stderr)

    rows = bs.flatten(result)
    with_ptf = sum(1 for r in rows if r["PTFs (grid sections)"])
    with_mod = sum(1 for r in rows if r["USERMOD / SYSMOD IDs"])
    note = "Run with a saved HTML copy of the grid." if html else ""
    pathlib.Path(args.out).write_bytes(build_scrape_workbook(result, note, ptf_table))
    print(f"Products: {len(result.records)} | product/release rows: {len(rows)} | rows with PTF IDs: {with_ptf} | "
          f"rows with USERMOD/SYSMOD IDs: {with_mod} | extra pages followed: {len(result.linked_pages)}")
    print(f"Wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
