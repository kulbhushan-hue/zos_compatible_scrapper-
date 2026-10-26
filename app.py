"""
z/OS Product Compatibility Dashboard (Streamlit)

Tab 1  z/OS Version      : choose the z/OS version -> compatibility matrix for IBM, Broadcom, BMC,
                           Rocket, Precisely, ASG (compatible? PTF required? EOS, latest version) -> Excel
Tab 2  Check my products : pick vendor / product / your existing VRM (or upload an inventory) -> verdict -> Excel
Tab 3  Ask               : plain-English questions answered from the matrix rows
Tab 4  Sources           : official vendor links (all clickable)
Maintenance details      : fetches linked vendor pages for PTF / USERMOD / SYSMOD / prerequisite text

Run locally:   pip install -r requirements.txt
               streamlit run app.py
Written answers (optional): IBM watsonx.ai Granite model. Set WATSONX_APIKEY and WATSONX_PROJECT_ID
as environment variables / Code Engine secrets (never commit them). Without them the app shows matching rows.
"""

import os
from datetime import datetime

import pandas as pd
import streamlit as st

import broadcom_scraper as bs
import vendor_scraper as vsc
from compatibility_db import SOURCES, VENDORS, VER, ZOS_VERSIONS, CompatibilityDB, build_excel, to_output
from rag_engine import RagIndex, generate_answer, watsonx_configured
import ptf_details as pdx
from scrape_excel import build_ptf_workbook, build_scrape_workbook
from vendor_excel import build_vendor_workbook
from vendor_details import enrich_vendor_details

st.set_page_config(page_title="z/OS Product Compatibility", page_icon="🖥️", layout="wide")

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
LINK_COL = st.column_config.LinkColumn("Open link", display_text="Open ↗")
ALL_STATUS = ["Yes", "No", "In Progress", "Unverified", "N/A", "Unknown"]

st.markdown(
    """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600;700&display=swap');
    :root {
        --cds-blue: #0f62fe;
        --cds-blue-hover: #0353e9;
        --cds-text: #161616;
        --cds-muted: #525252;
        --cds-border: #e0e0e0;
        --cds-layer: #f4f4f4;
    }
    html, body, [class*="st-"], [data-testid="stAppViewContainer"] {
        font-family: "IBM Plex Sans", "Helvetica Neue", Arial, sans-serif;
    }
    .stApp { background: #f4f4f4; color: var(--cds-text); }
    [data-testid="stHeader"] { background: rgba(244, 244, 244, 0.92); }
    [data-testid="stSidebar"] { background: #ffffff; border-right: 1px solid var(--cds-border); }
    [data-testid="stSidebar"] [data-testid="stMetric"] {
        background: var(--cds-layer); border-left: 3px solid var(--cds-blue);
        padding: 0.85rem 1rem; border-radius: 0;
    }
    .block-container { padding-top: 2rem; padding-bottom: 3rem; max-width: 1600px; }
    .dashboard-hero {
        background: #161616; color: #ffffff; padding: 1.6rem 2rem;
        border-left: 5px solid var(--cds-blue); margin: 0 0 1.5rem 0;
    }
    .dashboard-hero .eyebrow {
        color: #a6c8ff; font-size: 0.75rem; font-weight: 600;
        letter-spacing: 0.1em; text-transform: uppercase; margin-bottom: 0.55rem;
    }
    .dashboard-hero h1 {
        color: #ffffff; font-size: 2rem; line-height: 1.2; font-weight: 500;
        margin: 0 0 0.45rem 0;
    }
    .dashboard-hero p { color: #c6c6c6; margin: 0; font-size: 0.95rem; }
    .section-kicker {
        color: var(--cds-muted); font-size: 0.75rem; font-weight: 600;
        letter-spacing: 0.08em; text-transform: uppercase; margin: 0 0 0.35rem 0;
    }
    [data-testid="stMetric"] {
        background: #ffffff; border: 1px solid var(--cds-border);
        border-radius: 0; padding: 1rem 1.1rem; min-height: 108px;
    }
    [data-testid="stMetricLabel"] { color: var(--cds-muted); font-size: 0.85rem; }
    [data-testid="stMetricValue"] { color: var(--cds-text); font-weight: 600; }
    [data-testid="stTabs"] [role="tablist"] {
        gap: 0.35rem; border-bottom: 1px solid var(--cds-border);
    }
    [data-testid="stTabs"] button[role="tab"] {
        color: var(--cds-muted); font-weight: 500; padding: 0.8rem 1rem;
    }
    [data-testid="stTabs"] button[role="tab"][aria-selected="true"] {
        color: var(--cds-blue); border-bottom: 2px solid var(--cds-blue);
    }
    [data-testid="stDownloadButton"] button, [data-testid="stLinkButton"] a {
        border-radius: 0;
    }
    [data-testid="stDataFrame"] { border: 1px solid var(--cds-border); }
    </style>
    """,
    unsafe_allow_html=True,
)


# ------------------------------------------------------------------ data
@st.cache_resource
def load_base() -> CompatibilityDB:
    return CompatibilityDB.load(DATA_DIR)


def get_db() -> CompatibilityDB:
    return load_base().merged_with(st.session_state.get("extra"))


def show_table(d: pd.DataFrame, version: str, height: int = 420):
    st.dataframe(
        to_output(d, with_link_column=True, version=version),
        width="stretch", hide_index=True, height=height,
        column_config={"Open link": LINK_COL,
                       "Comments / Links": st.column_config.TextColumn(width="large"),
                       "PTF / USERMOD / SYSMOD / Prerequisites": st.column_config.TextColumn(width="large"),
                       "PTF IDs": st.column_config.TextColumn(width="medium"),
                       "USERMOD / SYSMOD IDs": st.column_config.TextColumn(width="medium"),
                       "Additional PTFs (linked pages)": st.column_config.TextColumn(width="medium")},
    )


def metrics(d: pd.DataFrame, db: CompatibilityDB, version: str):
    s = db.summarize(d, version)
    c = st.columns(6)
    c[0].metric("Products", s["total"])
    c[1].metric("✅ Compatible", s["yes"])
    c[2].metric("🔧 Need PTFs", s["ptf"])
    c[3].metric("⛔ Not compatible", s["no"])
    c[4].metric("⏳ In progress", s["in_progress"])
    c[5].metric("❔ Unverified", s["unverified"])


try:
    db = get_db()
except FileNotFoundError as e:
    st.error(f"Data files not found. {e}")
    st.stop()

with st.spinner("Checking linked vendor pages for PTF, USERMOD, SYSMOD, and prerequisite details..."):
    enriched_rows, maintenance_fetch = enrich_vendor_details(db.df)
db = CompatibilityDB(df=enriched_rows)

use_llm = watsonx_configured()

# ------------------------------------------------------------------ header + sidebar
st.markdown(
    """
    <div class="dashboard-hero">
        <div class="eyebrow">IBM Z · Migration readiness</div>
        <h1>z/OS product compatibility</h1>
        <p>A single view of vendor support, required maintenance, and migration risks.</p>
    </div>
    """,
    unsafe_allow_html=True,
)

with st.sidebar:
    st.markdown("### Workspace")
    st.caption("IBM Z compatibility intelligence")
    _sv = st.session_state.get("zos_ver", ZOS_VERSIONS[0])
    _scol = VER[_sv][0]
    verified = int((~db.df[_scol].isin(["Unverified", "Unknown", ""])).sum())
    st.metric("Vendor statements", f"{verified} / {len(db.df)}")
    st.metric("Vendors covered", db.df["vendor"].nunique())
    st.metric("Source pages", len(SOURCES))
    st.metric(
        "Readable vendor pages",
        f"{maintenance_fetch['urls_readable']} / {maintenance_fetch['urls_total']}",
    )
    if maintenance_fetch.get("broadcom_scraped"):
        st.metric("Broadcom rows with scraped PTF data", f"{maintenance_fetch['broadcom_rows']}")
        st.caption(
            f"Broadcom grid scraped {maintenance_fetch['broadcom_scraped_at']}; "
            f"{maintenance_fetch['broadcom_links']} extra page(s) followed."
            + (f" {maintenance_fetch['broadcom_unmatched']} row(s) not found on the grid." if maintenance_fetch["broadcom_unmatched"] else "")
        )
    else:
        st.caption("Broadcom grid scraper: not active (no data returned, or BROADCOM_SCRAPER=0).")
    for _v, _st in (maintenance_fetch.get("vendors") or {}).items():
        if "error" in _st:
            st.caption(f"{_v}: scraper error ({_st['error']})")
        else:
            st.caption(f"{_v}: {_st['rows_with_ids']}/{_st['rows']} products with scraped IDs, {_st['pages']} page(s) searched"
                       + (f", {_st['login_required']} source(s) need a login" if _st["login_required"] else ""))
    if maintenance_fetch["urls_unavailable"]:
        st.warning(
            f"{maintenance_fetch['urls_unavailable']} page(s) were blocked, unavailable, "
            "or had no readable text; check the source links."
        )
    st.caption("Unverified means no vendor statement has been sourced for that release.")
    st.caption(
        "Maintenance details are best-effort and cached for 6 hours. "
        "No extracted detail does not mean no maintenance is required."
    )
    st.divider()
    st.markdown("**watsonx.ai assistant**")
    st.caption("Granite enabled" if use_llm else "Answer generation is not configured")

overview_tab, tab_ver, tab_check, tab_ask, tab_src, tab_scrape, tab_vendors = st.tabs(
    ["Overview", "Compatibility matrix", "Check my products", "Ask watsonx", "Sources",
     "Broadcom PTF scraper (test)", "Vendor PTF scraper (all vendors)"]
)

# ================================================================== OVERVIEW
with overview_tab:
    version_col, note_col = st.columns([1, 4], vertical_alignment="bottom")
    ver = version_col.selectbox(
        "Target z/OS release",
        ZOS_VERSIONS,
        key="zos_ver",
        help="Choose a release to update the overview, compatibility matrix, and inventory check.",
    )
    scol = VER[ver][0]
    pcol = VER[ver][1]
    overview_stats = db.summarize(db.df, ver)
    note_col.markdown(
        f"<div class='section-kicker'>Portfolio snapshot · {len(db.df):,} product records "
        f"across {db.df['vendor'].nunique()} vendors</div>",
        unsafe_allow_html=True,
    )

    st.markdown("#### Migration readiness")
    kpis = st.columns(5)
    kpis[0].metric("Products tracked", f"{overview_stats['total']:,}")
    kpis[1].metric(
        "Compatible",
        f"{overview_stats['yes']:,}",
        delta=f"{overview_stats['yes'] / overview_stats['total']:.0%} of portfolio"
        if overview_stats["total"] else None,
    )
    kpis[2].metric("PTFs to apply", f"{overview_stats['ptf']:,}")
    kpis[3].metric("Not compatible", f"{overview_stats['no']:,}")
    kpis[4].metric("Needs verification", f"{overview_stats['unverified']:,}")

    chart_col, vendors_col = st.columns([1, 1], gap="large")
    with chart_col:
        st.markdown("#### Compatibility status")
        status_counts = (
            db.df[scol]
            .replace({"Unknown": "Unverified", "": "Unverified"})
            .value_counts()
            .reindex(["Yes", "No", "In Progress", "Unverified", "N/A"], fill_value=0)
        )
        st.bar_chart(status_counts.rename("Products"), color="#0f62fe", height=280)
        st.caption(f"Status distribution for z/OS {ver}. Unverified items require vendor confirmation.")
    with vendors_col:
        st.markdown("#### Vendor coverage")
        vendor_counts = (
            db.df.assign(_status=db.df[scol].replace({"Unknown": "Unverified", "": "Unverified"}))
            .groupby(["vendor", "_status"])
            .size()
            .unstack(fill_value=0)
            .reindex(columns=["Yes", "No", "In Progress", "Unverified", "N/A"], fill_value=0)
        )
        st.bar_chart(vendor_counts, height=280)
        st.caption("Product records by vendor and reported compatibility status.")

    left, right = st.columns([1.35, 1], gap="large")
    with left:
        st.markdown("#### Vendor summary")
        vendor_summary = (
            db.df.assign(_status=db.df[scol].replace({"Unknown": "Unverified", "": "Unverified"}))
            .groupby(["vendor", "_status"])
            .size()
            .unstack(fill_value=0)
            .reindex(columns=["Yes", "No", "In Progress", "Unverified", "N/A"], fill_value=0)
            .rename_axis("Vendor")
            .reset_index()
        )
        st.dataframe(vendor_summary, width="stretch", hide_index=True)
    with right:
        st.markdown("#### Items to review")
        review = db.df[
            db.df[scol].isin(["No", "In Progress", "Unverified", "Unknown", ""])
            | ((db.df[scol] == "Yes") & db.df[pcol].str.startswith("Yes"))
        ].copy()
        review["_priority"] = review[scol].map(
            {"No": 0, "In Progress": 1, "Unverified": 2, "Unknown": 2, "": 2}
        ).fillna(3)
        review = review.sort_values(["_priority", "vendor", "product"]).drop(columns="_priority").head(8)
        if review.empty:
            st.success(f"No compatibility or PTF follow-ups are currently flagged for z/OS {ver}.")
        else:
            show_table(review, ver, height=320)

# ================================================================== TAB 1 : z/OS version
with tab_ver:
    st.markdown("#### Compatibility matrix")
    st.caption(f"Filter the vendor statements for the selected target release: z/OS {ver}.")
    st.caption(
        "IBM maintenance details come from public SPCR/APAR pages; no IBM API key is needed for lookup. "
        "PTF IDs may also be SMP/E SYSMOD IDs. USERMODs are shown only when IBM explicitly publishes them."
    )
    vendors = st.multiselect("Vendors", VENDORS, default=VENDORS, key="ver_vendors")

    f1, f2, f3 = st.columns([2, 2, 3])
    status = f1.multiselect(f"R{ver} compatible", ALL_STATUS, default=ALL_STATUS, key="ver_status")
    ptf = f2.selectbox("PTF", ["Any", "PTF required", "No PTF needed"], key="ver_ptf")
    q = f3.text_input("Search", key="ver_q", placeholder="product, VRM, comment…")
    breaking = False
    prev_ver = VER[ver][3]
    if prev_ver:
        breaking = st.checkbox(f"Only products that work on z/OS {prev_ver} but not on {ver}", key=f"ver_break_{ver}")

    view = db.filter(vendors, status, None if ptf == "Any" else ptf, breaking, q, version=ver)
    st.markdown(f"#### Products for z/OS {ver}")
    metrics(view, db, ver)
    show_table(view, ver, height=520)

    book = {f"zOS {ver} - All Vendors": db.df}
    for v in VENDORS:
        d = db.df[db.df["vendor"] == v]
        if not d.empty:
            book[v] = d
    c1, c2 = st.columns(2)
    c1.download_button(f"⬇️ Download z/OS {ver} workbook (all vendors)", build_excel(book, version=ver),
                       file_name=f"zOS_{ver}_compatibility.xlsx", mime=XLSX, type="primary", key="ver_dl_all")
    c2.download_button("⬇️ Download current filtered view", build_excel({"Filtered": view}, version=ver),
                       file_name=f"zOS_{ver}_filtered.xlsx", mime=XLSX, key="ver_dl_view")
    st.caption("Excel links are real hyperlinks: click the Comments / Links cell (or the Sources sheet) to open the vendor page. "
               "If Excel opens the file in Protected View, click Enable Editing first.")

    with st.expander("➕ Add or correct data (upload a CSV with the same columns as data/*.csv)"):
        st.write("Rows are matched on Vendor + Product + VRM and override the built-in data for this session. "
                 "To make them permanent, edit the CSV files in `data/` and redeploy.")
        upd = st.file_uploader("CSV", type=["csv"], key="ver_upd")
        if upd is not None and st.button("Apply uploaded data", key="ver_apply"):
            try:
                st.session_state["extra"] = pd.read_csv(upd, dtype=str, keep_default_na=False)
                st.rerun()
            except Exception as e:
                st.error(f"Could not read the file: {e}")

# ================================================================== TAB 2 : check my products
with tab_check:
    cver = st.session_state.get("zos_ver", ZOS_VERSIONS[0])
    st.markdown(f"#### Check your existing products against z/OS {cver}")
    st.caption("The z/OS version follows your choice on the first tab.")
    vendors2 = st.multiselect("Vendors", VENDORS, default=VENDORS, key="chk_vendors")
    pool = db.df[db.df["vendor"].isin(vendors2)]
    picked = st.multiselect(f"Products and existing versions ({len(pool)} available)", pool["label"].tolist(),
                            placeholder="Search e.g. 'Endevor', 'CICS', 'MFX' ...  (Vendor | Product | VRM)",
                            key="chk_picked")
    up = st.file_uploader("…or upload your product inventory (CSV/XLSX with columns Vendor, Product, VRM)",
                          type=["csv", "xlsx"], key="chk_inv")

    chosen = db.df[db.df["label"].isin(picked)]
    misses = []
    if up is not None:
        try:
            inv = (pd.read_csv(up, dtype=str, keep_default_na=False) if up.name.lower().endswith(".csv")
                   else pd.read_excel(up, dtype=str, keep_default_na=False))
            matched, misses = db.match_inventory(inv)
            chosen = pd.concat([chosen, matched], ignore_index=True).drop_duplicates(subset=["vendor", "product", "vrm"])
        except Exception as e:
            st.error(f"Could not read the inventory: {e}")

    if chosen.empty:
        st.info("Select at least one product above (or upload an inventory) to see the answer.")
    else:
        st.markdown("#### Result")
        metrics(chosen, db, cver)
        s = db.summarize(chosen, cver)
        scol = VER[cver][0]
        blockers = chosen[chosen[scol] == "No"]
        if not blockers.empty:
            st.error(f"{len(blockers)} selected product(s) are NOT compatible with z/OS {cver} and need an upgrade first: "
                     + "; ".join(f"{r.product} {r.vrm}" for r in blockers.itertuples()))
        if s["ptf"]:
            st.warning(f"{s['ptf']} compatible product(s) need PTFs applied.")
        if s["unverified"]:
            st.info(f"{s['unverified']} product(s) have no vendor statement in the data yet. Open their link and confirm.")
        if s["no"] == 0 and s["unverified"] == 0 and s["in_progress"] == 0:
            st.success(f"All selected products are compatible with z/OS {cver}." +
                       (" Apply the listed PTFs first." if s["ptf"] else " No PTFs required."))
        show_table(chosen, cver)
        for m in misses:
            st.caption("⚠️ " + m)
        xl = build_excel(
            {f"My Products z{cver}": chosen, "Full Matrix": db.df},
            notes=[f"Generated {datetime.now():%Y-%m-%d %H:%M}. First sheet = your selection; 'Full Matrix' = everything."],
            version=cver,
        )
        st.download_button("⬇️ Download Excel (my products + full matrix)", xl,
                           file_name=f"zOS_{cver}_my_products.xlsx", mime=XLSX, type="primary", key="chk_dl")

# ================================================================== TAB 3 : ask
with tab_ask:
    st.markdown("Ask in plain English, e.g. *Which Broadcom products are not supported on z/OS 3.2?* "
                "or *Does Endevor 18.1 need PTFs?*")
    question = st.text_input("Question", key="ask_q")
    k = st.slider("Rows to retrieve", 5, 30, 12, key="ask_k")
    ask_ver = st.session_state.get("zos_ver", ZOS_VERSIONS[0])
    if question:
        rows = RagIndex.build(db.df).retrieve(question, k)
        if rows.empty:
            st.warning("No matching rows. Try a product name or vendor.")
        else:
            answered = False
            if use_llm:
                with st.spinner("Preparing answer…"):
                    try:
                        st.markdown(generate_answer(question, rows))
                        answered = True
                    except Exception:
                        st.info("The written answer is unavailable right now. The matching rows are shown below.")
            else:
                st.info("Showing the matching rows.")
            with st.expander("Matching rows", expanded=not answered):
                show_table(rows, ask_ver, height=320)

# ================================================================== TAB 4 : sources
with tab_src:
    st.markdown("Official places to confirm compatibility, PTFs, EOS dates and latest versions.")
    for v in VENDORS:
        items = [s for s in SOURCES if s[0] == v]
        if not items:
            continue
        st.markdown(f"**{v}**")
        for _, name, url, what in items:
            a, b = st.columns([6, 1])
            a.markdown(f"{name}  \n<small>{what}</small>", unsafe_allow_html=True)
            b.link_button("Open ↗", url)
        st.divider()

# ================================================================== TAB 5 : Broadcom PTF scraper (test)
with tab_scrape:
    st.markdown("#### Broadcom grid: PTF and USERMOD / SYSMOD scraper")
    st.caption(
        "Opens the Broadcom z/OS compatibility grid, reads each row's z/OS V3R2 / V3R1 / V2R5 cells, follows the "
        "'PTFs' and 'Additional Information' links to their sections, reads the PTF and USERMOD / SYSMOD IDs, "
        "and follows the extra hyperlinks found there. Use it to test the extraction and download the result as Excel."
    )
    c1, c2, c3 = st.columns([1, 2, 2])
    sc_limit = c1.number_input("Products to scrape (0 = all)", min_value=0, max_value=400, value=20, step=10, key="sc_limit")
    sc_filter = c2.text_input("Only products containing", key="sc_filter", placeholder="e.g. Dispatch, CA 1, Endevor")
    sc_url = c3.text_input("Grid URL", value=bs.GRID_URL, key="sc_url")
    o1, o2 = st.columns(2)
    sc_follow = o1.checkbox("Follow hyperlinks found in the PTF / Additional Information sections", value=True, key="sc_follow")
    sc_products = o2.checkbox("Also read each product's own page (slower)", value=False, key="sc_products")
    sc_upload = st.file_uploader("…or test with a saved copy of the grid page (HTML)", type=["html", "htm"], key="sc_upload")

    if st.button("Scrape Broadcom grid", type="primary", key="sc_run"):
        bar = st.progress(0.0, text="Reading the grid page…")

        def _progress(done, total):
            bar.progress(done / max(total, 1), text=f"Scraped {done} of {total} hyperlinked pages")

        if sc_url.strip() and not bs.allowed_url(sc_url.strip()) and sc_upload is None:
            st.error("That URL is not on the approved vendor host list (broadcom.com, ca.com, ibm.com over HTTPS).")
        else:
            bs.clear_cache()
            result = bs.scrape_grid(
                grid_url=sc_url.strip() or bs.GRID_URL,
                html=sc_upload.getvalue() if sc_upload is not None else None,
                follow_links=sc_follow, follow_product_pages=sc_products,
                limit=int(sc_limit) or None, name_filter=sc_filter.strip(), progress=_progress,
            )
            bar.empty()
            st.session_state["scrape_result"] = result

    result = st.session_state.get("scrape_result")
    if result is not None:
        if not result.records:
            st.error(result.status)
            st.info(
                "If this is a network problem, ask for outbound HTTPS to ftpdocs.broadcom.com, knowledge.broadcom.com "
                "and techdocs.broadcom.com (or set HTTPS_PROXY). You can also save the grid page from a browser and upload it above."
            )
        else:
            flat = pd.DataFrame(bs.flatten(result))
            m = st.columns(5)
            m[0].metric("Products", len(result.records))
            m[1].metric("Product / release rows", len(flat))
            m[2].metric("Rows with PTF IDs", int((flat["PTFs (grid sections)"] != "").sum()))
            m[3].metric("Rows with USERMOD / SYSMOD", int((flat["USERMOD / SYSMOD IDs"] != "").sum()))
            ok = sum(1 for p in result.linked_pages.values() if p.status == "Scraped")
            m[4].metric("Hyperlinked pages read", f"{ok} / {len(result.linked_pages)}")
            rel = st.multiselect("z/OS release", ["3.2", "3.1", "2.5"], default=["3.2", "3.1", "2.5"], key="sc_rel")
            only_ids = st.checkbox("Only rows that have PTF or USERMOD / SYSMOD IDs", value=False, key="sc_only_ids")
            view_df = flat[flat["z/OS release"].isin(rel)]
            if only_ids:
                view_df = view_df[(view_df["PTFs (grid sections)"] != "") | (view_df["USERMOD / SYSMOD IDs"] != "")
                                  | (view_df["Additional PTFs (linked pages)"] != "")]
            st.dataframe(
                view_df[["Product", "VRM", "z/OS release", "Status (grid)", "PTFs (grid sections)", "USERMOD / SYSMOD IDs",
                         "Additional PTFs (linked pages)", "Grid links", "Section notes"]],
                width="stretch", hide_index=True, height=480,
                column_config={"Section notes": st.column_config.TextColumn(width="large")},
            )
            st.download_button(
                "⬇️ Download scrape as Excel", build_scrape_workbook(result),
                file_name="Broadcom_first_page_PTFs.xlsx", mime=XLSX, type="primary", key="sc_dl",
            )
            failed = [p for p in result.linked_pages.values() if p.status != "Scraped"]
            if failed:
                with st.expander(f"{len(failed)} hyperlinked page(s) could not be read"):
                    st.dataframe(pd.DataFrame([{"URL": p.url, "Status": p.status} for p in failed]),
                                 width="stretch", hide_index=True)
            st.caption("An empty ID cell means no ID was found in the fetched text, not that no maintenance is required.")

    # ---------------------------------------------------------------- per-PTF detail table
    st.divider()
    st.markdown("#### PTF detail table")
    st.caption(
        "One row per PTF: product / release, PTF ID, FMID, publish date, APAR, prerequisites, superseding PTF, full description, "
        "and an extraction status. Publish date and the short description come from Broadcom's public Solutions List; FMID, APAR, "
        "prerequisites, superseding PTF and the full description come from the solution page, which Broadcom restricts to signed-in "
        "support users. Those fields are filled only from a source you are entitled to use (an export you upload, or an authorized "
        "session configured as a secret). Anything not read is labelled, never guessed."
    )
    d1, d2, d3 = st.columns([1, 2, 2])
    pt_n = d1.number_input("PTFs", min_value=1, max_value=200, value=10, step=5, key="pt_n")
    pt_source = d2.radio("Which PTFs", ["First PTFs found on the compatibility matrix", "Newest PTFs in Broadcom's Solutions List"],
                         key="pt_source")
    pt_zos = d3.selectbox("Matrix z/OS release", ["3.2", "3.1", "2.5", "all"], key="pt_zos",
                          disabled=not pt_source.startswith("First"))
    u1, u2 = st.columns(2)
    pt_export = u1.file_uploader("Detail export (CSV: PTF ID, FMID, APAR ID, Prerequisites, Superseding PTF, Full description, PTF publish date)",
                                 type=["csv"], key="pt_export")
    pt_catalog = u2.file_uploader("…or a saved copy of the Solutions List page (AllProducts.HTML) if the server cannot reach it",
                                  type=["html", "htm"], key="pt_catalog")
    if pdx.authorized_session_configured():
        st.success("An authorized Broadcom support session is configured; solution pages will be read with it.")
    else:
        st.info("No authorized Broadcom session is configured (BROADCOM_PTF_DETAIL_URL + BROADCOM_SUPPORT_COOKIE). "
                "Detail fields will show 'Not accessible' unless you upload an export.")
    if st.button("Build PTF detail table", type="primary", key="pt_run"):
        with st.spinner("Reading Broadcom's Solutions List and PTF details…"):
            catalog, cat_msg = pdx.load_catalog(html=pt_catalog.getvalue() if pt_catalog is not None else None)
            if pt_source.startswith("First"):
                base = st.session_state.get("scrape_result")
                if base is None or not base.records:
                    base = bs.scrape_grid(follow_links=False)
                selection = pdx.ptfs_from_matrix(base, int(pt_n), None if pt_zos == "all" else pt_zos)
            else:
                selection = pdx.newest_in_catalog(catalog, int(pt_n))
            overrides = {}
            if pt_export is not None:
                try:
                    overrides = pdx.load_detail_export(pt_export)
                except Exception as exc:
                    st.error(f"Could not read the detail export: {exc}")
            details = pdx.gather_details([p for p, _, _ in selection], overrides)
            st.session_state["ptf_table"] = (pdx.build_table(selection, catalog, details), cat_msg)
    stored = st.session_state.get("ptf_table")
    if stored is not None:
        table, cat_msg = stored
        st.caption(cat_msg)
        if table.empty:
            st.warning("No PTFs were found for that selection. Scrape the matrix first or check the source options.")
        else:
            st.dataframe(table[pdx.TABLE_COLUMNS], width="stretch", hide_index=True, height=min(60 + 36 * len(table), 520),
                         column_config={"Full description": st.column_config.TextColumn(width="large"),
                                        "Product / Release": st.column_config.TextColumn(width="large"),
                                        "Detail Extraction Status": st.column_config.TextColumn(width="medium")})
            counts = table["Detail Extraction Status"].str.extract(r"^([A-Za-z ]+?)(?: \(|$)")[0].value_counts()
            st.caption("Status: " + ", ".join(f"{k} {v}" for k, v in counts.items()) +
                       ". Not accessible = could not be read with the access available; Not found in source = source read but field absent.")
            st.download_button("⬇️ Download PTF detail table as Excel", build_ptf_workbook(table),
                               file_name="Broadcom_PTF_details.xlsx", mime=XLSX, type="primary", key="pt_dl")


# ================================================================== TAB 7 : Vendor PTF scraper (all vendors)
with tab_vendors:
    st.markdown("#### PTF and USERMOD / SYSMOD scraper for IBM, BMC, Rocket, Precisely and ASG")
    st.caption(
        "Opens each vendor source page from the compatibility data, searches at least 10 pages when a source is paginated "
        "(next links, numbered pages, ?page=N, multi-page PDFs), finds the passages that name each product, and reads the PTF, "
        "USERMOD / SYSMOD and APAR IDs. If a page needs a login, enter the vendor user name and password below and run it again. "
        "Credentials stay in this browser session's memory, are sent only to that vendor's own hosts, and are never saved or exported."
    )
    VENDOR_LIST = ["IBM", "BMC", "Rocket", "Precisely", "ASG"]
    v1, v2, v3 = st.columns([3, 1, 1])
    pick = v1.multiselect("Vendors", VENDOR_LIST, default=VENDOR_LIST, key="vs_pick")
    vs_pages = v2.number_input("Pages per source (min 10)", min_value=vsc.MIN_PAGES, max_value=200, value=vsc.DEFAULT_MAX_PAGES, step=5, key="vs_pages")
    vs_pdf = v3.number_input("PDF pages (min 10)", min_value=vsc.MIN_PAGES, max_value=300, value=vsc.DEFAULT_PDF_PAGES, step=10, key="vs_pdf")
    vs_follow = st.checkbox("Follow hyperlinks found in matching passages (one level)", value=True, key="vs_follow")

    METHODS = {"Form login (user name + password)": "auto", "HTTP Basic": "basic", "Signed-in browser cookie": "cookie"}

    def _creds(vendor: str) -> "vsc.Credentials":
        method = METHODS.get(st.session_state.get(f"vs_method_{vendor}", "Form login (user name + password)"), "auto")
        env = creds_from_env_for_ui(vendor)
        return vsc.Credentials(
            username=st.session_state.get(f"vs_user_{vendor}", "") or env.username,
            password=st.session_state.get(f"vs_pw_{vendor}", "") or env.password,
            method=method if (st.session_state.get(f"vs_user_{vendor}") or st.session_state.get(f"vs_cookie_{vendor}")) else env.method,
            login_url=st.session_state.get(f"vs_loginurl_{vendor}", "") or env.login_url,
            cookie=st.session_state.get(f"vs_cookie_{vendor}", "") or env.cookie,
        )

    def creds_from_env_for_ui(vendor: str) -> "vsc.Credentials":
        import os as _os
        k = vendor.upper()
        return vsc.Credentials(_os.environ.get(f"{k}_USERNAME", ""), _os.environ.get(f"{k}_PASSWORD", ""),
                               _os.environ.get(f"{k}_AUTH_METHOD", "auto"), _os.environ.get(f"{k}_LOGIN_URL", ""),
                               _os.environ.get(f"{k}_COOKIE", ""))

    with st.expander("Vendor logins (optional; use when a source says 'Login required')", expanded=False):
        for _v in pick:
            st.markdown(f"**{vsc.PROFILES[_v].label}**")
            st.caption(vsc.PROFILES[_v].login_note)
            a, b = st.columns(2)
            a.selectbox("Sign-in method", list(METHODS), key=f"vs_method_{_v}")
            a.text_input("User name", key=f"vs_user_{_v}")
            a.text_input("Password", type="password", key=f"vs_pw_{_v}")
            b.text_input("Login page URL (optional)", key=f"vs_loginurl_{_v}",
                         placeholder="leave blank to use the page that asked for the login")
            b.text_input("Cookie header (for SSO / multi-factor sites)", type="password", key=f"vs_cookie_{_v}")

    def _rows_for(vendor: str):
        sub = db.df[db.df["vendor"].str.casefold() == vendor.casefold()]
        return sub[["product", "vrm", "link", "zos32", "zos31", "zos25"]].to_dict("records")

    def _run(vendors: list[str]):
        bar = st.progress(0.0, text="Starting…")
        store = st.session_state.setdefault("vendor_scrapes", {})
        for i, vendor in enumerate(vendors):
            bar.progress(i / max(len(vendors), 1), text=f"Scraping {vendor}…")
            store[vendor] = vsc.scrape_vendor(vendor, _rows_for(vendor), _creds(vendor), max_pages=int(vs_pages),
                                              pdf_pages=int(vs_pdf), follow_links=vs_follow)
        bar.empty()

    if st.button("Scrape selected vendors", type="primary", key="vs_run", disabled=not pick):
        _run(pick)

    scrapes = st.session_state.get("vendor_scrapes", {})
    shown = [scrapes[v] for v in VENDOR_LIST if v in scrapes]
    if shown:
        cols = st.columns(len(shown))
        for c, sc in zip(cols, shown):
            ok = sum(1 for p in sc.pages if p.status == "Scraped")
            hit = sum(1 for r in sc.rows if any(not x.empty() for x in r.releases.values()))
            c.metric(sc.vendor, f"{hit}/{len(sc.rows)} with IDs", f"{ok} page(s) searched", delta_color="off")
        needs = [(sc.vendor, u) for sc in shown for u in sc.login_required]
        if needs:
            st.warning(
                "Login required for " + ", ".join(sorted({v for v, _ in needs})) + ". Open **Vendor logins** above, enter the user name "
                "and password (or a signed-in cookie), then press **Scrape selected vendors** or a retry button below."
            )
            for v in sorted({v for v, _ in needs}):
                msg = scrapes[v].login_message
                r1, r2 = st.columns([1, 3])
                if r1.button(f"Retry {v} with login", key=f"vs_retry_{v}"):
                    _run([v])
                    st.rerun()
                r2.caption((msg + ". " if msg else "") + f"{sum(1 for x in needs if x[0] == v)} source(s) need a login: "
                           + "; ".join(u for vv, u in needs if vv == v)[:300])
        flat = pd.DataFrame([d for sc in shown for d in vsc.flatten(sc)])
        if flat.empty:
            st.info("No PTF, USERMOD or APAR IDs were found in the pages that could be read. Pages that need a login are listed above.")
        else:
            only = st.checkbox("Only rows with IDs", value=True, key="vs_only")
            view = flat[(flat["PTFs"] != "") | (flat["USERMOD / SYSMOD IDs"] != "") | (flat["APAR IDs"] != "")
                        | (flat["Additional PTFs (linked pages)"] != "")] if only else flat
            st.dataframe(view[["Vendor", "Product", "VRM", "z/OS release", "Status (dashboard data)", "PTFs", "USERMOD / SYSMOD IDs",
                               "APAR IDs", "Additional PTFs (linked pages)", "Note"]],
                         width="stretch", hide_index=True, height=420)
        d1, d2 = st.columns(2)
        d1.download_button("⬇️ Download all vendors as Excel", build_vendor_workbook(shown), file_name="Vendor_PTFs.xlsx",
                           mime=XLSX, type="primary", key="vs_dl_all")
        one = d2.selectbox("Single vendor workbook", [sc.vendor for sc in shown], key="vs_one")
        d2.download_button(f"⬇️ Download {one} as Excel", build_vendor_workbook([scrapes[one]]), file_name=f"{one}_PTFs.xlsx",
                           mime=XLSX, key="vs_dl_one")
        with st.expander("Pages searched"):
            st.dataframe(pd.DataFrame([{"Vendor": sc.vendor, "Source": p.source_url, "Page": p.page_no, "URL": p.url, "Status": p.status,
                                        "Passages": p.blocks, "Note": p.note} for sc in shown for p in sc.pages]),
                         width="stretch", hide_index=True)
        st.caption("An empty ID cell means no ID was found in the text that could be read, not that no maintenance is required. "
                   "IDs whose passage does not name a z/OS release are shown as 'Release not stated'.")

st.caption("Always confirm on the live vendor page before production changes.")
