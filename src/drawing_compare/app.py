"""
Streamlit UI.

    streamlit run src/drawing_compare/app.py

Workflow mirrors how a drawing review actually runs: identify the two
revisions, confirm how sheets pair up, run the comparison, review critical
findings first, then issue a signed report.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cv2
import pandas as pd
import streamlit as st

from drawing_compare import __version__
from drawing_compare.classify import Severity, classify_records
from drawing_compare.page_matcher import match_pages
from drawing_compare.pdf_io import scan_pdf_pages
from drawing_compare.pipeline import compare_documents
from drawing_compare.provenance import build_provenance
from drawing_compare.structured_report import (
    _where,
    save_structured_json,
    save_structured_report,
)

st.set_page_config(page_title="Drawing Compare", page_icon="◧", layout="wide")

MATCH_MODES = {
    "Automatic": "auto",
    "Title block / sheet number": "sheet_label",
    "Content similarity": "content",
    "Page order (1↔1, 2↔2)": "sequential",
}

SEVERITY_ORDER = [Severity.CRITICAL, Severity.MAJOR, Severity.MINOR, Severity.INFORMATIONAL]

# --------------------------------------------------------------- sidebar
with st.sidebar:
    st.markdown("### Review details")
    st.caption("Recorded in the report for traceability.")
    reviewer = st.text_input("Prepared by", placeholder="Name or initials")
    reference = st.text_input("Reference", placeholder="ECO / ECN / project no.")
    notes = st.text_area("Notes", placeholder="Purpose of this comparison", height=80)

    st.markdown("---")
    st.markdown("### Comparison")
    mode_label = st.selectbox("Sheet pairing", list(MATCH_MODES.keys()))
    mode = MATCH_MODES[mode_label]
    only_changed = st.checkbox("Only sheets with differences", value=True)

    st.markdown("---")
    st.caption(f"drawing-compare {__version__}")

# ----------------------------------------------------------------- header
st.title("Drawing Revision Comparison")
st.caption(
    "Vector-first comparison of engineering drawing PDFs. Differences are "
    "classified by engineering significance and localised by title-block zone."
)

col_old, col_new = st.columns(2)
with col_old:
    st.markdown("**Baseline revision**")
    old_file = st.file_uploader("Old drawing (PDF)", type=["pdf"], key="old",
                                label_visibility="collapsed")
with col_new:
    st.markdown("**Compared revision**")
    new_file = st.file_uploader("New drawing (PDF)", type=["pdf"], key="new",
                                label_visibility="collapsed")

if not (old_file and new_file):
    st.info("Upload both revisions to begin. Multi-sheet drawing sets are supported.")
    st.stop()

tmp_dir = Path(tempfile.mkdtemp(prefix="drawing_compare_"))
old_path = tmp_dir / "old.pdf"
new_path = tmp_dir / "new.pdf"
old_path.write_bytes(old_file.getvalue())
new_path.write_bytes(new_file.getvalue())

old_summaries = scan_pdf_pages(old_path)
new_summaries = scan_pdf_pages(new_path)

c1, c2, c3 = st.columns(3)
c1.metric("Baseline sheets", len(old_summaries))
c2.metric("Compared sheets", len(new_summaries))
c3.metric("Pairing method", mode_label)

# ---------------------------------------------------------- sheet pairing
plan = match_pages(old_summaries, new_summaries, mode=mode)

with st.expander(f"Sheet pairing — {plan.summary()}", expanded=True):
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Old page": p.old_index + 1 if p.old_index is not None else "—",
                    "New page": p.new_index + 1 if p.new_index is not None else "—",
                    "Sheet identity": (p.old_identity or p.new_identity).label()
                    if (p.old_identity or p.new_identity) else "",
                    "Matched by": p.method,
                    "Score": f"{p.score:.2f}",
                }
                for p in plan.pairs
            ]
        ),
        hide_index=True,
        use_container_width=True,
    )
    manual_text = st.text_area(
        "Override pairing — one `old_page,new_page` per line, 1-based. Leave blank for automatic.",
        value="", height=80,
    )

manual_pairs = None
if manual_text.strip():
    try:
        manual_pairs = [
            tuple(int(v.strip()) - 1 for v in line.split(","))
            for line in manual_text.strip().splitlines() if line.strip()
        ]
    except Exception:
        st.error("Could not parse the manual pairs. Use `old_page,new_page` per line.")

if not st.button("Run comparison", type="primary"):
    st.stop()

progress = st.progress(0.0, text="Starting…")
doc = compare_documents(
    old_path, new_path, match_mode=mode, page_pairs=manual_pairs,
    progress=lambda done, total, label: progress.progress(
        done / max(total, 1), text=f"Comparing {label} ({done}/{total})"
    ),
)
progress.empty()

prov = build_provenance(
    old_path, new_path, len(old_summaries), len(new_summaries),
    reviewer=reviewer, reference=reference, notes=notes, match_mode=mode,
)
prov.old_file.name = old_file.name
prov.new_file.name = new_file.name

# -------------------------------------------------------------- findings
st.markdown("---")

revision = doc.revision_summary()
if any(revision.values()):
    st.subheader("Revision summary")
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "": "Drawing number",
                    "Baseline (old)": revision.get("old_drawing_number")
                    or revision.get("drawing_number") or "—",
                    "Compared (new)": revision.get("drawing_number") or "—",
                },
                {
                    "": "Revision",
                    "Baseline (old)": revision.get("previous_revision") or "—",
                    "Compared (new)": revision.get("current_revision") or "—",
                },
                {
                    "": "Description",
                    "Baseline (old)": revision.get("previous_description") or "—",
                    "Compared (new)": revision.get("current_description") or "—",
                },
            ]
        ),
        hide_index=True,
        use_container_width=True,
        column_config={
            "Baseline (old)": st.column_config.TextColumn(width="large"),
            "Compared (new)": st.column_config.TextColumn(width="large"),
        },
    )
    if revision.get("title"):
        st.caption(revision["title"])

st.subheader("Findings")

sev = doc.severity_summary()
cols = st.columns(4)
for col, s in zip(cols, SEVERITY_ORDER):
    col.metric(s.value, sev.get(s.value, 0))

critical = doc.critical_changes()
if critical:
    st.error(
        f"{len(critical)} critical change(s) affect what is manufactured or purchased."
    )
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Sheet": label.split("—")[0].strip(),
                    "Zone": c.zone,
                    "Category": c.category.value,
                    "Was": c.record.old_value or "—",
                    "Is now": c.record.new_value or "—",
                }
                for label, c in critical
            ]
        ),
        hide_index=True, use_container_width=True,
    )
else:
    st.success(
        "No changes affect what is manufactured or purchased. Remaining "
        "differences are drafting or revision housekeeping."
    )

unreliable = doc.unreliable_pages()
if unreliable:
    st.warning(
        "Alignment was unreliable on: "
        + ", ".join(p.pair.label() for p in unreliable)
        + ". Differences on those sheets must be verified manually."
    )
if doc.plan.added or doc.plan.removed:
    st.info(
        f"{len(doc.plan.added)} sheet(s) added, {len(doc.plan.removed)} removed — "
        "these have no counterpart to compare."
    )

# ------------------------------------------------------------- per sheet
pages = doc.changed_pages if only_changed else doc.pages
if not pages:
    st.success("No differences detected anywhere in the set.")
else:
    st.markdown("### Sheet detail")
    labels = [
        f"{p.pair.label()} ({p.record_count})" if p.status == "compared"
        else f"{p.pair.label()} [{p.status}]"
        for p in pages
    ]
    for tab, page in zip(st.tabs(labels), pages):
        with tab:
            if page.error:
                st.error(f"This sheet failed to compare: {page.error}")
                continue
            if page.status in {"added", "removed"}:
                st.info(
                    "Sheet added in this revision — no baseline to compare."
                    if page.status == "added"
                    else "Sheet removed in this revision."
                )
                continue

            result = page.result
            if not result.alignment.reliable:
                st.warning(
                    f"Alignment unreliable ({result.alignment.good_matches} matched "
                    "features). Verify these differences manually."
                )
            # Both revisions marked in the same places, side by side, so a
            # reviewer can see what replaced what rather than only where to
            # look.
            old_col, new_col = st.columns(2)
            for column, caption, image, tag in (
                (old_col, "Baseline revision (old)", result.overlay_image, "old"),
                (new_col, "Compared revision (new)",
                 getattr(result, "new_overlay_image", None), "new"),
            ):
                if image is None:
                    continue
                with column:
                    st.image(
                        cv2.cvtColor(image, cv2.COLOR_BGR2RGB),
                        caption=caption,
                        use_container_width=True,
                    )
                    ok, buf = cv2.imencode(".png", image)
                    if ok:
                        st.download_button(
                            f"Download {tag} sheet (full resolution)",
                            data=buf.tobytes(),
                            file_name=(
                                f"{page.pair.label().replace(' ', '_')}_{tag}.png"
                            ),
                            mime="image/png",
                            key=f"dl_{tag}_{page.pair.old_index}_{page.pair.new_index}",
                            help="Opens at full resolution, where it can be zoomed — "
                            "the preview above is scaled to fit.",
                            use_container_width=True,
                        )

            with st.expander("Difference list for this sheet", expanded=True):
                st.dataframe(
                    pd.DataFrame(
                        [
                            {
                                "Severity": c.severity.value,
                                "Component": c.component.value,
                                "Significance": c.category.value,
                                # View or region, not the bare zone code — "Detail
                                # A-A" is something a reviewer can act on; "C4" on
                                # its own means opening the drawing to find out.
                                "Where": _where(c),
                                "Was": c.record.old_value or "—",
                                "Is now": c.record.new_value or "—",
                            }
                            for c in classify_records(result.records)
                        ]
                    ),
                    hide_index=True,
                    use_container_width=True,
                    height=460,
                    # The table sits full width beneath the sheets rather than
                    # squeezed beside them; "Is now" was being clipped off the
                    # right edge of a narrow column with no way to scroll to it.
                    column_config={
                        "Was": st.column_config.TextColumn(width="large"),
                        "Is now": st.column_config.TextColumn(width="large"),
                    },
                )

# ------------------------------------------------------------- downloads
st.markdown("---")
st.markdown("### Issue report")
if not reviewer:
    st.caption("Tip: fill in **Prepared by** in the sidebar so the report is attributable.")

# doc.drawing_title() is the single, validated source for this — it reads
# the title block by label first, and only falls back to sheet-pairing
# identity (also regex-guarded) if that finds nothing. Building an ad-hoc
# guess here duplicated that logic without its safeguards, which is how a
# company name ended up as the report title.
drawing_title = doc.drawing_title()

html_path = tmp_dir / "comparison_report.html"
json_path = tmp_dir / "comparison_report.json"
save_structured_report(doc, prov, html_path, drawing_title=drawing_title)
save_structured_json(doc, prov, json_path)

d1, d2 = st.columns(2)
d1.download_button(
    "Download report (HTML)", data=html_path.read_bytes(),
    file_name="drawing_comparison_report.html", mime="text/html",
    use_container_width=True,
)
d2.download_button(
    "Download data (JSON)", data=json_path.read_bytes(),
    file_name="drawing_comparison_report.json", mime="application/json",
    use_container_width=True,
)
st.caption(
    f"Report includes SHA-256 digests of both files "
    f"({prov.old_file.short_hash} / {prov.new_file.short_hash}), the settings used, "
    "and a sign-off block."
)

st.markdown("---")
st.markdown(
    "**Ramu Gopal** — CAD Automation | AI Systems Developer  \n"
    "[thetechthinker.com](https://thetechthinker.com/ramu-gopal/)"
)
