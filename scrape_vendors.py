#!/usr/bin/env python3
"""Scrape PTF / USERMOD / SYSMOD IDs for IBM, BMC, Rocket, Precisely and ASG and write Excel reports
in the same layout as Broadcom_first_page_PTFs_SAMPLE_snapshot.xlsx.
(Broadcom has its own grid scraper: scrape_broadcom_first_page.py.)

Examples
    python scrape_vendors.py                                  # all five vendors, 10 pages per source, one workbook
    python scrape_vendors.py --vendors BMC Rocket --max-pages 25
    python scrape_vendors.py --per-vendor                     # also one workbook per vendor
    python scrape_vendors.py --login BMC                      # asks for the BMC user name and password (password is not echoed)
    python scrape_vendors.py --vendors Rocket --pdf-pages 80  # read more PDF pages

Login
  * When a page needs a login the run says so, and (in a terminal) offers to prompt for a user name and password.
  * Non-interactive runs read <VENDOR>_USERNAME / <VENDOR>_PASSWORD (or <VENDOR>_COOKIE, <VENDOR>_LOGIN_URL,
    <VENDOR>_AUTH_METHOD = auto|basic|cookie) from the environment. Passwords are never taken from command-line arguments.
  * Credentials are sent only to hosts that belong to that vendor.
"""

from __future__ import annotations

import argparse
import getpass
import os
import pathlib
import sys

import pandas as pd

import vendor_scraper as vs
from vendor_excel import build_vendor_workbook

VENDORS = ["IBM", "BMC", "Rocket", "Precisely", "ASG"]


def env_creds(vendor: str) -> vs.Credentials:
    k = vendor.upper()
    return vs.Credentials(os.environ.get(f"{k}_USERNAME", ""), os.environ.get(f"{k}_PASSWORD", ""),
                          os.environ.get(f"{k}_AUTH_METHOD", "auto"), os.environ.get(f"{k}_LOGIN_URL", ""),
                          os.environ.get(f"{k}_COOKIE", ""))


def prompt_creds(vendor: str) -> vs.Credentials:
    print(f"\n{vendor} login ({vs.profile_for(vendor).login_note})", file=sys.stderr)
    method = (input("  Method [form/basic/cookie] (default form): ").strip().lower() or "form")
    if method == "cookie":
        return vs.Credentials(cookie=getpass.getpass("  Paste the Cookie header value (hidden): "), method="cookie")
    user = input("  User name: ").strip()
    pw = getpass.getpass("  Password (hidden): ")
    login_url = input("  Login page URL (optional, blank = use the page that asked): ").strip()
    return vs.Credentials(user, pw, "basic" if method == "basic" else "auto", login_url)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--vendors", nargs="+", default=VENDORS, choices=VENDORS, help="vendors to scrape")
    ap.add_argument("--data-dir", default="data", help="folder with <vendor>_compat.csv files")
    ap.add_argument("--max-pages", type=int, default=vs.DEFAULT_MAX_PAGES, help="pages searched per paginated source (minimum 10)")
    ap.add_argument("--pdf-pages", type=int, default=vs.DEFAULT_PDF_PAGES, help="PDF pages searched per document (minimum 10)")
    ap.add_argument("--no-follow", action="store_true", help="do not follow hyperlinks found in matching passages")
    ap.add_argument("--login", nargs="*", default=[], metavar="VENDOR", help="prompt for credentials for these vendors up front")
    ap.add_argument("--no-prompt", action="store_true", help="never prompt; report 'Login required' and continue")
    ap.add_argument("--out", default="Vendor_PTFs.xlsx", help="combined workbook")
    ap.add_argument("--per-vendor", action="store_true", help="also write <Vendor>_PTFs.xlsx for each vendor")
    args = ap.parse_args(argv)
    if args.max_pages < vs.MIN_PAGES:
        print(f"note: --max-pages raised to the minimum of {vs.MIN_PAGES}", file=sys.stderr)

    scrapes: list[vs.VendorScrape] = []
    for vendor in args.vendors:
        csv = pathlib.Path(args.data_dir) / f"{vendor.lower()}_compat.csv"
        if not csv.exists():
            print(f"skip {vendor}: {csv} not found", file=sys.stderr)
            continue
        rows = pd.read_csv(csv, dtype=str, keep_default_na=False).to_dict("records")
        creds = prompt_creds(vendor) if vendor in args.login else env_creds(vendor)
        print(f"{vendor}: {len(rows)} products, {len({r['link'] for r in rows})} source pages ...", file=sys.stderr)
        kw = dict(max_pages=args.max_pages, pdf_pages=args.pdf_pages, follow_links=not args.no_follow)
        sc = vs.scrape_vendor(vendor, rows, creds, **kw)
        if sc.login_required and not args.no_prompt and sys.stdin.isatty():
            print(f"  {len(sc.login_required)} source(s) need a login:", file=sys.stderr)
            for u in sc.login_required:
                print("   -", u, file=sys.stderr)
            if input("  Enter credentials now? [y/N] ").strip().lower().startswith("y"):
                sc = vs.scrape_vendor(vendor, rows, prompt_creds(vendor), **kw)
                if sc.login_message:
                    print("  login:", sc.login_message, file=sys.stderr)
        found = sum(1 for r in sc.rows if any(not i.empty() for i in r.releases.values()))
        print(f"  pages searched: {sum(1 for p in sc.pages if p.status == 'Scraped')} | products with IDs: {found}/{len(sc.rows)} | "
              f"login still required for: {len(sc.login_required)} source(s)", file=sys.stderr)
        scrapes.append(sc)
        if args.per_vendor:
            pathlib.Path(f"{vendor}_PTFs.xlsx").write_bytes(build_vendor_workbook([sc]))
    if not scrapes:
        print("nothing scraped", file=sys.stderr)
        return 1
    pathlib.Path(args.out).write_bytes(build_vendor_workbook(scrapes))
    print(f"Wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
