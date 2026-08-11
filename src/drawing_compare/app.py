"""
Streamlit UI.

    streamlit run src/drawing_compare/app.py

Upload the old and new drawing PDFs, review how sheets were matched up,
run the comparison across the whole set, browse per-sheet differences,
and download the report.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

# Streamlit runs this file directly, which only adds this file's own folder
# (src/drawing_compare/) to sys.path — not src/. Add src/ explicitly so
# "import drawing_compare.xxx" resolves, both locally and on Streamlit Cloud.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cv2
import pandas as pd
import streamlit as st

from drawing_compare.page_matcher import match_pages
from drawing_compare.pdf_io import scan_pdf_pages
from drawing_compare.pipeline import compare_documents
from drawing_compare.classify import classify_records
from drawing_compare.report import records_to_dicts

st.set_page_config(page_title="Drawing Compare", layout="wide")
st.title("Engineering Drawing Compare")
st.caption(
    "Vector-first PDF drawing comparison — geometry, dimensions, and "
    "annotations, grouped by zone. Handles multi-sheet drawing sets."
)

MATCH_MODES = {
    "Auto (title block, then content, then page order)": "auto",
    "Title block / sheet number": "sheet_label",
    "Content similarity": "content",
    "Page order (1↔1, 2↔2, ...)": "sequential",
}


def _write_temp(uploaded, tmp_dir: Path, name: str) -> Path:
    path = tmp_dir / name
    path.write_bytes(uploaded.getvalue())
    return path


col_old, col_new = st.columns(2)
with col_old:
    old_file = st.file_uploader("Old drawing (PDF)", type=["pdf"], key="old")
with col_new:
    new_file = st.file_uploader("New drawing (PDF)", type=["pdf"], key="new")

if not (old_file and new_file):
    st.info("Upload both PDFs to begin. Multi-page sets are supported.")
    st.stop()

# Persist the uploads for the session so re-runs don't re-read the widgets.
tmp_dir = Path(tempfile.mkdtemp(prefix="drawing_compare_"))
old_path = _write_temp(old_file, tmp_dir, "old.pdf")
new_path = _write_temp(new_file, tmp_dir, "new.pdf")

# Cheap text-only scan so we can show the sheet list before committing to
# a full 300-DPI comparison run.
old_summaries = scan_pdf_pages(old_path)
new_summaries = scan_pdf_pages(new_path)

st.success(
    f"Old PDF: {len(old_summaries)} page(s) · New PDF: {len(new_summaries)} page(s)"
)

st.subheader("Sheet matching")
mode_label = st.selectbox("How should sheets be paired?", list(MATCH_MODES.keys()))
mode = MATCH_MODES[mode_label]

plan = match_pages(old_summaries, new_summaries, mode=mode)
st.write(plan.summary())

plan_rows = []
for pair in plan.pairs:
    plan_rows.append(
        {
            "Old page": pair.old_index + 1 if pair.old_index is not None else "—",
            "New page": pair.new_index + 1 if pair.new_index is not None else "—",
            "Sheet": (pair.old_identity or pair.new_identity).label()
            if (pair.old_identity or pair.new_identity)
            else "",
            "Matched by": pair.method,
            "Score": f"{pair.score:.2f}",
        }
    )
st.dataframe(pd.DataFrame(plan_rows), hide_index=True, use_container_width=True)

with st.expander("Override matching manually"):
    st.caption(
        "Enter one pair per line as `old_page,new_page` using 1-based page "
        "numbers — e.g. `1,1` then `2,4`. Leave blank to use the automatic "
        "matching above."
    )
    manual_text = st.text_area("Manual page pairs", value="", height=110)

manual_pairs = None
if manual_text.strip():
    try:
        manual_pairs = []
        for line in manual_text.strip().splitlines():
            if not line.strip():
                continue
            o, n = (int(v.strip()) for v in line.split(","))
            manual_pairs.append((o - 1, n - 1))
    except Exception:
        st.error("Couldn't parse the manual pairs. Use `old_page,new_page` per line.")
        manual_pairs = None

only_changed = st.checkbox("Show only sheets with differences", value=True)
run = st.button("Compare", type="primary")

if not run:
    st.stop()

progress_bar = st.progress(0.0, text="Starting...")


def _progress(done: int, total: int, label: str) -> None:
    progress_bar.progress(done / max(total, 1), text=f"Comparing {label} ({done}/{total})")


doc = compare_documents(
    old_path,
    new_path,
    match_mode=mode,
    page_pairs=manual_pairs,
    progress=_progress,
)
progress_bar.empty()

# ---------------------------------------------------------------- summary
st.subheader(f"{doc.total_records} difference(s) across {len(doc.plan.matched)} sheet(s)")

sev = doc.severity_summary()
if sev:
    order = ["Critical", "Major", "Minor", "Info"]
    cols = st.columns(len(order))
    for col, name in zip(cols, order):
        col.metric(name, sev.get(name, 0))

critical = doc.critical_changes()
if critical:
    st.subheader("Critical changes — what gets made or bought")
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Sheet": label.split("—")[0].strip(),
                    "Zone": c.zone,
                    "Category": c.category.value,
                    "Change": c.describe(),
                }
                for label, c in critical
            ]
        ),
        hide_index=True,
        use_container_width=True,
    )

with st.expander("All changes by type"):
    summary = doc.summary()
    if summary:
        st.dataframe(
            pd.DataFrame(
                {"Change type": list(summary.keys()), "Count": list(summary.values())}
            ),
            hide_index=True,
            use_container_width=True,
        )

unreliable = doc.unreliable_pages()
if unreliable:
    st.warning(
        f"{len(unreliable)} sheet(s) had unreliable alignment: "
        + ", ".join(p.pair.label() for p in unreliable)
        + ". Differences on those sheets may include false positives."
    )

if doc.plan.added or doc.plan.removed:
    st.info(
        f"{len(doc.plan.added)} sheet(s) added and {len(doc.plan.removed)} "
        "sheet(s) removed in the new revision — these have no counterpart to diff."
    )

# --------------------------------------------------------------- per sheet
pages = doc.changed_pages if only_changed else doc.pages
if not pages:
    st.success("No differences detected anywhere in the set.")
else:
    tab_labels = [
        f"{p.pair.label()} ({p.record_count})" if p.status == "compared"
        else f"{p.pair.label()} [{p.status}]"
        for p in pages
    ]
    for tab, page in zip(st.tabs(tab_labels), pages):
        with tab:
            if page.error:
                st.error(f"This sheet failed to compare: {page.error}")
                continue
            if page.status == "added":
                st.info("This sheet exists only in the new PDF.")
                continue
            if page.status == "removed":
                st.info("This sheet exists only in the old PDF — deleted in the new revision.")
                continue

            result = page.result
            if not result.alignment.reliable:
                st.warning(
                    f"Alignment unreliable ({result.alignment.good_matches} matched "
                    "features) — verify these differences manually."
                )

            st.markdown("**Overlay (old sheet, changes highlighted)**")
            st.image(
                cv2.cvtColor(result.overlay_image, cv2.COLOR_BGR2RGB),
                use_container_width=True,
            )

            st.markdown("**Difference list — most significant first**")
            classified = classify_records(result.records)
            df = pd.DataFrame(
                [
                    {
                        "Severity": c.severity.value,
                        "Category": c.category.value,
                        "Zone": c.zone,
                        "Old Value": c.record.old_value,
                        "New Value": c.record.new_value,
                    }
                    for c in classified
                ]
            )
            st.dataframe(df, hide_index=True, use_container_width=True)

# --------------------------------------------------------------- downloads
html_path = tmp_dir / "report.html"
json_path = tmp_dir / "report.json"
doc.to_html(html_path)
doc.to_json(json_path)

dl_col1, dl_col2 = st.columns(2)
with dl_col1:
    st.download_button(
        "Download HTML report (all sheets)",
        data=html_path.read_bytes(),
        file_name="drawing_set_comparison_report.html",
        mime="text/html",
    )
with dl_col2:
    st.download_button(
        "Download JSON report (all sheets)",
        data=json_path.read_bytes(),
        file_name="drawing_set_comparison_report.json",
        mime="application/json",
    )
