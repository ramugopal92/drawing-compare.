"""
Streamlit UI.

    streamlit run src/drawing_compare/app.py

Upload the old and new drawing PDFs, run the pipeline, browse the
difference list, view the overlay, and download the report.
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

from drawing_compare.pipeline import compare_drawings
from drawing_compare.report import records_to_dicts

st.set_page_config(page_title="Drawing Compare", layout="wide")
st.title("Engineering Drawing Compare")
st.caption(
    "Vector-first PDF drawing comparison — geometry, dimensions, and "
    "annotations, grouped by zone."
)

col_old, col_new = st.columns(2)
with col_old:
    old_file = st.file_uploader("Old drawing (PDF)", type=["pdf"], key="old")
with col_new:
    new_file = st.file_uploader("New drawing (PDF)", type=["pdf"], key="new")

col_a, col_b = st.columns(2)
with col_a:
    old_page_index = st.number_input("Old PDF page (0-based)", min_value=0, value=0, step=1)
with col_b:
    new_page_index = st.number_input("New PDF page (0-based)", min_value=0, value=0, step=1)

run = st.button("Compare", type="primary", disabled=not (old_file and new_file))

if run and old_file and new_file:
    with tempfile.TemporaryDirectory() as tmp:
        old_path = Path(tmp) / "old.pdf"
        new_path = Path(tmp) / "new.pdf"
        old_path.write_bytes(old_file.getvalue())
        new_path.write_bytes(new_file.getvalue())

        with st.spinner("Aligning and comparing..."):
            result = compare_drawings(
                old_path, new_path, old_page_index=old_page_index, new_page_index=new_page_index
            )

        if not result.alignment.reliable:
            st.warning(
                f"Alignment was not reliable (only {result.alignment.good_matches} "
                "matched features). Differences below may include false positives "
                "from unaligned drift — verify manually."
            )

        st.subheader(f"{len(result.records)} differences found")
        summary = result.summary()
        if summary:
            st.dataframe(
                pd.DataFrame(
                    {"Change type": list(summary.keys()), "Count": list(summary.values())}
                ),
                hide_index=True,
                use_container_width=True,
            )

        st.subheader("Overlay (old drawing, changes highlighted)")
        overlay_rgb = cv2.cvtColor(result.overlay_image, cv2.COLOR_BGR2RGB)
        st.image(overlay_rgb, use_container_width=True)

        st.subheader("Difference list")
        df = pd.DataFrame(records_to_dicts(result.records))
        if not df.empty:
            df = df[["zone", "change_type", "old_value", "new_value", "confidence"]]
            df.columns = ["Zone", "Type", "Old Value", "New Value", "Confidence"]
        st.dataframe(df, hide_index=True, use_container_width=True)

        html_path = Path(tmp) / "report.html"
        json_path = Path(tmp) / "report.json"
        result.to_html(html_path)
        result.to_json(json_path)

        dl_col1, dl_col2 = st.columns(2)
        with dl_col1:
            st.download_button(
                "Download HTML report",
                data=html_path.read_bytes(),
                file_name="drawing_comparison_report.html",
                mime="text/html",
            )
        with dl_col2:
            st.download_button(
                "Download JSON report",
                data=json_path.read_bytes(),
                file_name="drawing_comparison_report.json",
                mime="application/json",
            )
else:
    st.info("Upload both PDFs and click Compare to begin.")
