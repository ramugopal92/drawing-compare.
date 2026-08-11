"""
Report generation: turns a list of DiffRecord into
  - an overlay PNG (old drawing raster + colored boxes per change type)
  - a self-contained HTML report (difference table + embedded overlay image)
  - a JSON export (for feeding into other tools / a future SolidWorks
    add-in / CI pipelines that gate on "no unexpected geometry changes")
"""

from __future__ import annotations

import base64
import json
from dataclasses import asdict
from pathlib import Path

import cv2
import numpy as np

from .alignment import pdf_points_to_pixels
from .classify import Severity, classify_records, summarize_by_severity
from .config import MAX_REPORT_ROWS_PER_SHEET
from .diff_engine import ChangeType, DiffRecord

_COLORS = {
    ChangeType.GEOMETRY_ADDED: (0, 200, 0),      # green, BGR
    ChangeType.GEOMETRY_REMOVED: (0, 0, 220),    # red
    ChangeType.GEOMETRY_CHANGED: (0, 165, 255),  # orange
    ChangeType.TEXT_ADDED: (200, 200, 0),        # teal-ish
    ChangeType.TEXT_REMOVED: (150, 0, 150),      # purple
    ChangeType.TEXT_CHANGED: (255, 140, 0),      # blue-ish orange
}


def render_overlay(
    old_raster: np.ndarray, records: list[DiffRecord], dpi: int
) -> np.ndarray:
    overlay = old_raster.copy()
    for rec in records:
        bbox_px = pdf_points_to_pixels(rec.bbox, dpi)
        x0, y0, x1, y1 = (int(round(v)) for v in bbox_px)
        color = _COLORS.get(rec.change_type, (255, 255, 255))
        cv2.rectangle(overlay, (x0, y0), (x1, y1), color, 2)
    return overlay


def records_to_dicts(records: list[DiffRecord]) -> list[dict]:
    out = []
    for rec in records:
        d = asdict(rec)
        d["change_type"] = rec.change_type.value
        out.append(d)
    return out


def save_json(records: list[DiffRecord], path: str | Path, meta: dict | None = None) -> None:
    payload = {
        "meta": meta or {},
        "difference_count": len(records),
        "differences": records_to_dicts(records),
    }
    Path(path).write_text(json.dumps(payload, indent=2))


_HTML_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Drawing Comparison Report</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, sans-serif; margin: 24px; color: #1a1a1a; }}
  h1 {{ font-size: 20px; }}
  .meta {{ color: #555; margin-bottom: 16px; font-size: 13px; }}
  table {{ border-collapse: collapse; width: 100%; margin-top: 16px; }}
  th, td {{ border: 1px solid #ddd; padding: 6px 10px; font-size: 13px; text-align: left; }}
  th {{ background: #f4f4f4; }}
  tr:nth-child(even) {{ background: #fafafa; }}
  .swatch {{ display: inline-block; width: 12px; height: 12px; margin-right: 6px; border-radius: 2px; vertical-align: middle; }}
  img {{ max-width: 100%; border: 1px solid #ccc; margin-top: 12px; }}
  .summary {{ display: flex; gap: 24px; margin: 16px 0; }}
  .summary div {{ background: #f4f4f4; padding: 8px 14px; border-radius: 6px; font-size: 13px; }}
</style>
</head>
<body>
<h1>Drawing Comparison Report</h1>
<div class="meta">{meta_line}</div>

<div class="summary">
{summary_html}
</div>

<img src="data:image/png;base64,{overlay_b64}" alt="overlay" />

<table>
<thead>
<tr><th>#</th><th>Zone</th><th>Type</th><th>Old Value</th><th>New Value</th><th>Confidence</th></tr>
</thead>
<tbody>
{rows_html}
</tbody>
</table>
</body>
</html>
"""

_ROW_COLORS_HEX = {
    ChangeType.GEOMETRY_ADDED: "#00c800",
    ChangeType.GEOMETRY_REMOVED: "#dc0000",
    ChangeType.GEOMETRY_CHANGED: "#ffa500",
    ChangeType.TEXT_ADDED: "#00c8c8",
    ChangeType.TEXT_REMOVED: "#960096",
    ChangeType.TEXT_CHANGED: "#ff8c00",
}


def save_html(
    records: list[DiffRecord],
    overlay_image: np.ndarray,
    path: str | Path,
    meta: dict | None = None,
) -> None:
    meta = meta or {}
    meta_line = " | ".join(f"{k}: {v}" for k, v in meta.items())

    counts: dict[str, int] = {}
    for rec in records:
        counts[rec.change_type.value] = counts.get(rec.change_type.value, 0) + 1
    summary_html = "\n".join(
        f'<div><b>{count}</b> {ctype}</div>' for ctype, count in sorted(counts.items())
    )

    rows = []
    for i, rec in enumerate(records, start=1):
        color = _ROW_COLORS_HEX.get(rec.change_type, "#999")
        rows.append(
            "<tr>"
            f"<td>{i}</td>"
            f"<td>{rec.zone}</td>"
            f'<td><span class="swatch" style="background:{color}"></span>{rec.change_type.value}</td>'
            f"<td>{_escape(rec.old_value)}</td>"
            f"<td>{_escape(rec.new_value)}</td>"
            f"<td>{rec.confidence:.2f}</td>"
            "</tr>"
        )
    rows_html = "\n".join(rows) if rows else "<tr><td colspan='6'>No differences detected.</td></tr>"

    ok, buf = cv2.imencode(".png", overlay_image)
    overlay_b64 = base64.b64encode(buf.tobytes()).decode("ascii") if ok else ""

    html = _HTML_TEMPLATE.format(
        meta_line=meta_line,
        summary_html=summary_html,
        overlay_b64=overlay_b64,
        rows_html=rows_html,
    )
    Path(path).write_text(html, encoding="utf-8")


# --------------------------------------------------------------------------
# Multi-page reports
# --------------------------------------------------------------------------

_MULTIPAGE_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Drawing Set Comparison Report</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, sans-serif; margin: 24px; color: #1a1a1a; }}
  h1 {{ font-size: 22px; margin-bottom: 4px; }}
  h2 {{ font-size: 17px; margin-top: 36px; padding-top: 14px; border-top: 2px solid #e5e5e5; }}
  .meta {{ color: #555; margin-bottom: 16px; font-size: 13px; }}
  table {{ border-collapse: collapse; width: 100%; margin-top: 12px; }}
  th, td {{ border: 1px solid #ddd; padding: 6px 10px; font-size: 13px; text-align: left; }}
  th {{ background: #f4f4f4; }}
  tr:nth-child(even) {{ background: #fafafa; }}
  .swatch {{ display: inline-block; width: 12px; height: 12px; margin-right: 6px;
             border-radius: 2px; vertical-align: middle; }}
  img {{ max-width: 100%; border: 1px solid #ccc; margin-top: 12px; }}
  .summary {{ display: flex; gap: 16px; margin: 16px 0; flex-wrap: wrap; }}
  .summary div {{ background: #f4f4f4; padding: 8px 14px; border-radius: 6px; font-size: 13px; }}
  .badge {{ display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 12px;
            margin-left: 8px; vertical-align: middle; }}
  .b-added {{ background: #d8f5d8; color: #14601a; }}
  .b-removed {{ background: #fadada; color: #8a1010; }}
  .b-clean {{ background: #e8e8e8; color: #555; }}
  .b-error {{ background: #ffe4b5; color: #7a4b00; }}
  .warn {{ background: #fff6e0; border-left: 4px solid #e0a800; padding: 8px 12px;
           font-size: 13px; margin: 10px 0; }}
  .toc a {{ display: block; padding: 3px 0; font-size: 13px; color: #0a58ca;
            text-decoration: none; }}
  .toc a:hover {{ text-decoration: underline; }}
</style>
</head>
<body>
<h1>Drawing Set Comparison Report</h1>
<div class="meta">{meta_line}</div>

<div class="summary">
{summary_html}
</div>

<h2 style="border:none; margin-top:20px;">Sheets</h2>
<div class="toc">
{toc_html}
</div>

{pages_html}
</body>
</html>
"""


def _summary_counts(records: list[DiffRecord]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for rec in records:
        counts[rec.change_type.value] = counts.get(rec.change_type.value, 0) + 1
    return counts


_SEVERITY_HEX = {
    Severity.CRITICAL: "#A32D2D",
    Severity.MAJOR: "#BA7517",
    Severity.MINOR: "#185FA5",
    Severity.INFORMATIONAL: "#5F5E5A",
}


def _diff_table_html(records: list[DiffRecord]) -> str:
    """
    Difference table, ordered by engineering severity rather than by
    detection order.

    A flat list forces the reader to work out for themselves that a
    fastener material change matters more than a copyright year. Sorting
    by severity and labelling the category means they can stop reading
    once the criticals are handled.
    """
    classified = classify_records(records)
    shown = classified[:MAX_REPORT_ROWS_PER_SHEET]
    truncated = len(classified) - len(shown)
    rows = []
    last_severity = None
    index = 0
    for change in shown:
        if change.severity is not last_severity:
            last_severity = change.severity
            colour = _SEVERITY_HEX[change.severity]
            count = sum(1 for c in classified if c.severity is change.severity)
            rows.append(
                f'<tr><td colspan="6" style="background:#f4f4f4;font-weight:600;'
                f'border-left:4px solid {colour}">{change.severity.value} '
                f"— {count} change(s)</td></tr>"
            )
        index += 1
        rec = change.record
        color = _ROW_COLORS_HEX.get(rec.change_type, "#999")
        rows.append(
            "<tr>"
            f"<td>{index}</td>"
            f"<td>{rec.zone}</td>"
            f'<td><span class="swatch" style="background:{color}"></span>'
            f"{change.category.value}<br><small style='color:#777'>"
            f"{_escape(rec.change_type.value)}</small></td>"
            f"<td>{_escape(rec.old_value)}</td>"
            f"<td>{_escape(rec.new_value)}</td>"
            f"<td>{rec.confidence:.2f}</td>"
            "</tr>"
        )
    if not rows:
        return ""
    note = (
        f'<div class="warn">Showing the first {len(shown)} of {len(records)} '
        f"differences on this sheet. {truncated} more are in the JSON export. "
        "A list this long usually means alignment drifted or the two sheets "
        "are not the same drawing — check the overlay before working through "
        "it.</div>"
        if truncated > 0
        else ""
    )
    return (
        note
        + "<table><thead><tr><th>#</th><th>Zone</th><th>Category</th><th>Old Value</th>"
        "<th>New Value</th><th>Confidence</th></tr></thead><tbody>"
        + "\n".join(rows)
        + "</tbody></table>"
    )


def save_multipage_html(doc_result, path: str | Path) -> None:
    """
    Write one self-contained HTML report covering every sheet in the set.

    Takes a pipeline.DocumentCompareResult (untyped here to keep report.py
    free of a circular import back into pipeline.py).
    """
    meta_line = (
        f"Old: {doc_result.old_pdf.name} | New: {doc_result.new_pdf.name} | "
        f"{doc_result.plan.summary()} | {doc_result.total_records} difference(s) total"
    )

    counts = doc_result.summary()
    summary_html = "\n".join(
        f"<div><b>{count}</b> {ctype}</div>" for ctype, count in sorted(counts.items())
    ) or "<div>No differences detected across the set.</div>"

    toc_bits: list[str] = []
    page_bits: list[str] = []

    for idx, page in enumerate(doc_result.pages, start=1):
        anchor = f"sheet-{idx}"
        title = _escape(page.pair.label())

        if page.status == "added":
            badge = '<span class="badge b-added">sheet added</span>'
        elif page.status == "removed":
            badge = '<span class="badge b-removed">sheet removed</span>'
        elif page.status == "error":
            badge = '<span class="badge b-error">error</span>'
        elif page.record_count == 0:
            badge = '<span class="badge b-clean">no changes</span>'
        else:
            badge = f'<span class="badge b-removed">{page.record_count} change(s)</span>'

        toc_bits.append(f'<a href="#{anchor}">{title} {badge}</a>')

        body: list[str] = [f'<h2 id="{anchor}">{title} {badge}</h2>']

        if page.pair.method:
            body.append(
                f'<div class="meta">Matched by: {page.pair.method}'
                + (f" (score {page.pair.score:.2f})" if page.pair.score else "")
                + "</div>"
            )

        if page.error:
            body.append(f'<div class="warn">This sheet failed to compare: {_escape(page.error)}</div>')
        elif page.status == "added":
            body.append(
                '<div class="warn">This sheet exists only in the new PDF — '
                "it has no counterpart to diff against.</div>"
            )
        elif page.status == "removed":
            body.append(
                '<div class="warn">This sheet exists only in the old PDF — '
                "it was deleted in the new revision.</div>"
            )
        elif page.result is not None:
            if not page.result.alignment.reliable:
                body.append(
                    '<div class="warn">Alignment was unreliable on this sheet '
                    f"({page.result.alignment.good_matches} matched features) — "
                    "differences may include false positives. Verify manually.</div>"
                )
            ok, buf = cv2.imencode(".png", page.result.overlay_image)
            if ok:
                b64 = base64.b64encode(buf.tobytes()).decode("ascii")
                body.append(f'<img src="data:image/png;base64,{b64}" alt="overlay {title}" />')
            table = _diff_table_html(page.result.records)
            body.append(table if table else "<p><i>No differences detected on this sheet.</i></p>")

        page_bits.append("\n".join(body))

    html = _MULTIPAGE_TEMPLATE.format(
        meta_line=meta_line,
        summary_html=summary_html,
        toc_html="\n".join(toc_bits),
        pages_html="\n".join(page_bits),
    )
    Path(path).write_text(html, encoding="utf-8")


def save_multipage_json(doc_result, path: str | Path) -> None:
    """JSON export of a whole-document comparison, one entry per sheet."""
    pages = []
    for page in doc_result.pages:
        entry = {
            "old_page_index": page.pair.old_index,
            "new_page_index": page.pair.new_index,
            "label": page.pair.label(),
            "status": page.status,
            "match_method": page.pair.method,
            "match_score": page.pair.score,
            "difference_count": page.record_count,
            "error": page.error,
        }
        if page.result is not None:
            entry["alignment_reliable"] = page.result.alignment.reliable
            entry["alignment_matches"] = page.result.alignment.good_matches
            entry["differences"] = records_to_dicts(page.result.records)
        pages.append(entry)

    payload = {
        "meta": {
            "old_pdf": doc_result.old_pdf.name,
            "new_pdf": doc_result.new_pdf.name,
            "sheets_matched": len(doc_result.plan.matched),
            "sheets_added": len(doc_result.plan.added),
            "sheets_removed": len(doc_result.plan.removed),
        },
        "difference_count": doc_result.total_records,
        "summary": doc_result.summary(),
        "pages": pages,
    }
    Path(path).write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _escape(value) -> str:
    if value is None:
        return ""
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
