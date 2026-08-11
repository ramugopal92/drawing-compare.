"""
Structured report writer.

Produces a controlled document rather than a printout: document-control
header, executive summary, critical findings first, per-sheet detail,
sign-off block, and an appendix recording the settings and limitations.

The ordering is the point. A reader who stops after the first screen should
already know whether this revision needs their attention.
"""

from __future__ import annotations

import base64
import html
import json
from pathlib import Path

import cv2

from .classify import (
    ChangeCategory,
    ClassifiedChange,
    Severity,
    classify_records,
    summarize_by_category,
    summarize_by_severity,
)
from .config import MAX_REPORT_ROWS_PER_SHEET
from .provenance import ReportProvenance

SEVERITY_COLOUR = {
    Severity.CRITICAL: "#A32D2D",
    Severity.MAJOR: "#BA7517",
    Severity.MINOR: "#185FA5",
    Severity.INFORMATIONAL: "#6B6A66",
}

SEVERITY_TINT = {
    Severity.CRITICAL: "#FBEAEA",
    Severity.MAJOR: "#FDF3E3",
    Severity.MINOR: "#EAF1FA",
    Severity.INFORMATIONAL: "#F1F1EF",
}

_CSS = """
:root { --ink:#1A1A18; --muted:#6B6A66; --line:#DEDDD9; --bg:#FFFFFF; --panel:#F7F7F5; }
* { box-sizing:border-box; }
body { font-family:-apple-system,'Segoe UI',Roboto,sans-serif; color:var(--ink);
       background:var(--bg); margin:0; padding:32px 40px; line-height:1.5; }
h1 { font-size:23px; margin:0 0 4px; font-weight:600; letter-spacing:-0.01em; }
h2 { font-size:16px; margin:34px 0 10px; padding-bottom:6px;
     border-bottom:1px solid var(--line); font-weight:600; }
h3 { font-size:14px; margin:20px 0 8px; font-weight:600; }
.sub { color:var(--muted); font-size:13px; margin-bottom:22px; }
table { border-collapse:collapse; width:100%; font-size:12.5px; margin:10px 0; }
th,td { border:1px solid var(--line); padding:6px 9px; text-align:left; vertical-align:top; }
th { background:var(--panel); font-weight:600; }
td.num { text-align:right; font-variant-numeric:tabular-nums; }
.mono { font-family:ui-monospace,'SF Mono',Consolas,monospace; font-size:11.5px;
        word-break:break-all; }
.control { display:grid; grid-template-columns:1fr 1fr; gap:14px; margin:16px 0 8px; }
.control table { margin:0; }
.cards { display:flex; gap:10px; margin:14px 0 6px; flex-wrap:wrap; }
.card { flex:1; min-width:120px; border:1px solid var(--line); border-radius:6px;
        padding:10px 14px; background:var(--panel); }
.card .n { font-size:26px; font-weight:600; line-height:1.1; }
.card .l { font-size:11px; color:var(--muted); text-transform:uppercase;
           letter-spacing:0.06em; margin-top:2px; }
.pill { display:inline-block; padding:1px 8px; border-radius:10px; font-size:11px;
        font-weight:600; white-space:nowrap; }
.note { border-left:3px solid #BA7517; background:#FDF3E3; padding:9px 12px;
        font-size:12.5px; margin:10px 0; }
.ok { border-left:3px solid #3D7A3D; background:#EDF5ED; padding:9px 12px;
      font-size:12.5px; margin:10px 0; }
img { max-width:100%; border:1px solid var(--line); border-radius:4px; margin:10px 0; }
.signoff td { height:44px; }
.foot { margin-top:34px; padding-top:12px; border-top:1px solid var(--line);
        font-size:11px; color:var(--muted); }
ul { margin:8px 0; padding-left:20px; font-size:12.5px; }
li { margin:3px 0; }
a { color:#185FA5; }
@media print { body { padding:0; } h2 { page-break-after:avoid; } table { page-break-inside:auto; } }
"""


def _esc(value) -> str:
    return html.escape(str(value)) if value is not None else ""


def _pill(severity: Severity) -> str:
    return (
        f'<span class="pill" style="background:{SEVERITY_TINT[severity]};'
        f'color:{SEVERITY_COLOUR[severity]}">{severity.value}</span>'
    )


def _document_control(prov: ReportProvenance, drawing_title: str | None) -> str:
    def file_rows(label: str, f) -> str:
        return f"""
        <table>
          <tr><th colspan="2" style="background:#EFEFEC">{label}</th></tr>
          <tr><th style="width:34%">File name</th><td>{_esc(f.name)}</td></tr>
          <tr><th>Pages</th><td>{_esc(f.page_count if f.page_count is not None else '-')}</td></tr>
          <tr><th>Size</th><td>{_esc(f.size_display)}</td></tr>
          <tr><th>SHA-256</th><td class="mono">{_esc(f.sha256)}</td></tr>
        </table>"""

    meta = [
        ("Report generated", prov.generated_at),
        ("Prepared by", prov.reviewer or "—"),
        ("Reference", prov.reference or "—"),
        ("Sheet pairing method", prov.match_mode),
        ("Tool version", f"drawing-compare {prov.tool_version}"),
        ("Environment", prov.platform_info),
    ]
    meta_rows = "".join(
        f"<tr><th style='width:34%'>{_esc(k)}</th><td>{_esc(v)}</td></tr>" for k, v in meta
    )
    notes = (
        f'<tr><th>Notes</th><td>{_esc(prov.notes)}</td></tr>' if prov.notes else ""
    )
    title_row = (
        f"<tr><th>Drawing</th><td>{_esc(drawing_title)}</td></tr>" if drawing_title else ""
    )
    return f"""
    <h2>Document control</h2>
    <table>{title_row}{meta_rows}{notes}</table>
    <div class="control">{file_rows('Baseline revision (old)', prov.old_file)}
    {file_rows('Compared revision (new)', prov.new_file)}</div>
    <p class="sub" style="margin-top:8px">The SHA-256 digests above identify the exact
    files compared. Re-running this comparison on files with these digests reproduces
    this report.</p>"""


def _executive_summary(all_changes: list[ClassifiedChange], plan_summary: str) -> str:
    by_sev = summarize_by_severity(all_changes)
    cards = "".join(
        f'<div class="card" style="border-left:3px solid {SEVERITY_COLOUR[sev]}">'
        f'<div class="n" style="color:{SEVERITY_COLOUR[sev]}">{by_sev.get(sev, 0)}</div>'
        f'<div class="l">{sev.value}</div></div>'
        for sev in (Severity.CRITICAL, Severity.MAJOR, Severity.MINOR, Severity.INFORMATIONAL)
    )

    by_cat = summarize_by_category(all_changes)
    cat_rows = "".join(
        f"<tr><td>{_esc(cat.value)}</td><td class='num'>{count}</td></tr>"
        for cat, count in sorted(by_cat.items(), key=lambda kv: -kv[1])
    ) or "<tr><td colspan='2'>No differences detected.</td></tr>"

    critical = by_sev.get(Severity.CRITICAL, 0)
    verdict = (
        f'<div class="note"><b>{critical} critical change(s)</b> affect what is '
        "manufactured or purchased. These are listed in full in the next section and "
        "require engineering review before release.</div>"
        if critical
        else '<div class="ok">No changes were found that affect what is manufactured '
        "or purchased. Remaining differences are drafting or revision housekeeping.</div>"
    )

    return f"""
    <h2>Executive summary</h2>
    <div class="cards">{cards}</div>
    {verdict}
    <p class="sub" style="margin:10px 0 0">Sheet pairing: {_esc(plan_summary)}</p>
    <h3>Changes by category</h3>
    <table><thead><tr><th>Category</th><th style="width:90px">Count</th></tr></thead>
    <tbody>{cat_rows}</tbody></table>"""


def _critical_section(items: list[tuple[str, ClassifiedChange]]) -> str:
    if not items:
        return ""
    rows = "".join(
        f"<tr><td>{i}</td><td>{_esc(sheet)}</td><td>{_esc(c.zone)}</td>"
        f"<td>{_esc(c.category.value)}</td><td>{_esc(c.record.old_value or '—')}</td>"
        f"<td>{_esc(c.record.new_value or '—')}</td></tr>"
        for i, (sheet, c) in enumerate(items, start=1)
    )
    return f"""
    <h2>Critical findings</h2>
    <p class="sub" style="margin-bottom:6px">Changes that alter the part as made or
    bought — materials, specifications, part numbers, quantities, dimensions and
    tolerances.</p>
    <table><thead><tr><th style="width:36px">#</th><th>Sheet</th><th style="width:56px">Zone</th>
    <th style="width:150px">Category</th><th>Was</th><th>Is now</th></tr></thead>
    <tbody>{rows}</tbody></table>"""


def _change_table(classified: list[ClassifiedChange]) -> str:
    shown = classified[:MAX_REPORT_ROWS_PER_SHEET]
    truncated = len(classified) - len(shown)
    rows: list[str] = []
    last: Severity | None = None
    for n, c in enumerate(shown, start=1):
        if c.severity is not last:
            last = c.severity
            count = sum(1 for x in classified if x.severity is c.severity)
            rows.append(
                f'<tr><td colspan="5" style="background:{SEVERITY_TINT[c.severity]};'
                f"border-left:3px solid {SEVERITY_COLOUR[c.severity]};font-weight:600;"
                f'color:{SEVERITY_COLOUR[c.severity]}">{c.severity.value} — {count} change(s)</td></tr>'
            )
        rows.append(
            f"<tr><td>{n}</td><td>{_esc(c.zone)}</td><td>{_esc(c.category.value)}</td>"
            f"<td>{_esc(c.record.old_value or '—')}</td>"
            f"<td>{_esc(c.record.new_value or '—')}</td></tr>"
        )
    if not rows:
        return "<p class='sub'>No differences detected on this sheet.</p>"
    trunc = (
        f'<div class="note">Showing {len(shown)} of {len(classified)} differences. '
        f"The remaining {truncated} are in the JSON export.</div>"
        if truncated > 0
        else ""
    )
    return (
        trunc
        + '<table><thead><tr><th style="width:36px">#</th><th style="width:56px">Zone</th>'
        '<th style="width:150px">Category</th><th>Was</th><th>Is now</th>'
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
    )


def _sheet_sections(doc_result) -> str:
    parts: list[str] = []
    for idx, page in enumerate(doc_result.pages, start=1):
        title = _esc(page.pair.label())
        parts.append(f'<h2 id="sheet-{idx}">{title}</h2>')

        if page.error:
            parts.append(f'<div class="note">This sheet failed to compare: {_esc(page.error)}</div>')
            continue
        if page.pair.old_index is None:
            parts.append('<div class="note">Sheet added in this revision — no baseline to compare.</div>')
            continue
        if page.pair.new_index is None:
            parts.append('<div class="note">Sheet removed in this revision.</div>')
            continue

        result = page.result
        parts.append(
            f'<p class="sub" style="margin:0 0 8px">Paired by {_esc(page.pair.method)}'
            f" · alignment {'reliable' if result.alignment.reliable else 'UNRELIABLE'}"
            f" ({result.alignment.good_matches} matched features)</p>"
        )
        if not result.alignment.reliable:
            parts.append(
                '<div class="note">Alignment between these sheets was unreliable. '
                "Differences below may include false positives and must be verified "
                "against the drawings.</div>"
            )
        ok, buf = cv2.imencode(".png", result.overlay_image)
        if ok:
            b64 = base64.b64encode(buf.tobytes()).decode("ascii")
            parts.append(f'<img src="data:image/png;base64,{b64}" alt="Overlay {title}"/>')
        parts.append(_change_table(classify_records(result.records)))
    return "".join(parts)


def _signoff() -> str:
    return """
    <h2>Review and disposition</h2>
    <table class="signoff">
      <thead><tr><th style="width:22%">Role</th><th style="width:26%">Name</th>
      <th style="width:26%">Signature</th><th>Date</th></tr></thead>
      <tbody>
        <tr><td>Checked by</td><td></td><td></td><td></td></tr>
        <tr><td>Approved by</td><td></td><td></td><td></td></tr>
      </tbody>
    </table>
    <table style="margin-top:10px">
      <tr><th style="width:22%">Disposition</th>
      <td>☐ Accepted &nbsp;&nbsp; ☐ Accepted with comments &nbsp;&nbsp; ☐ Rejected &nbsp;&nbsp; ☐ Further review required</td></tr>
      <tr><th>Comments</th><td style="height:60px"></td></tr>
    </table>"""


def _appendix(prov: ReportProvenance) -> str:
    settings = "".join(
        f"<tr><td>{_esc(k)}</td><td class='num mono'>{_esc(v)}</td></tr>"
        for k, v in sorted(prov.settings.items())
    )
    limits = "".join(f"<li>{_esc(item)}</li>" for item in prov.limitations)
    return f"""
    <h2>Appendix A — Comparison parameters</h2>
    <p class="sub" style="margin-bottom:6px">Recorded so this result can be reproduced
    and so two reports can be judged comparable.</p>
    <table><thead><tr><th>Parameter</th><th style="width:130px">Value</th></tr></thead>
    <tbody>{settings}</tbody></table>
    <h2>Appendix B — Scope and limitations</h2>
    <ul>{limits}</ul>"""


def save_structured_report(doc_result, prov: ReportProvenance, path: str | Path,
                           drawing_title: str | None = None) -> None:
    """Write the full structured HTML report."""
    all_changes: list[ClassifiedChange] = []
    critical: list[tuple[str, ClassifiedChange]] = []
    for page in doc_result.pages:
        if not page.result:
            continue
        for c in classify_records(page.result.records):
            all_changes.append(c)
            if c.severity is Severity.CRITICAL:
                critical.append((page.pair.label(), c))

    heading = drawing_title or f"{prov.old_file.name} → {prov.new_file.name}"
    body = f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<title>Drawing Revision Comparison Report</title><style>{_CSS}</style></head><body>
<h1>Drawing Revision Comparison Report</h1>
<div class="sub">{_esc(heading)} · generated {_esc(prov.generated_at)}</div>
{_document_control(prov, drawing_title)}
{_executive_summary(all_changes, doc_result.plan.summary())}
{_critical_section(critical)}
<h2 style="margin-top:36px">Sheet detail</h2>
{_sheet_sections(doc_result)}
{_signoff()}
{_appendix(prov)}
<div class="foot">Generated by drawing-compare {_esc(prov.tool_version)}.
This report supports engineering review and does not replace it.</div>
</body></html>"""
    Path(path).write_text(body, encoding="utf-8")


def save_structured_json(doc_result, prov: ReportProvenance, path: str | Path) -> None:
    """Machine-readable twin of the HTML report."""
    sheets = []
    for page in doc_result.pages:
        entry = {
            "label": page.pair.label(),
            "old_page_index": page.pair.old_index,
            "new_page_index": page.pair.new_index,
            "status": page.status,
            "match_method": page.pair.method,
            "error": page.error,
            "changes": [],
        }
        if page.result:
            entry["alignment_reliable"] = page.result.alignment.reliable
            entry["alignment_matches"] = page.result.alignment.good_matches
            for c in classify_records(page.result.records):
                entry["changes"].append(
                    {
                        "severity": c.severity.value,
                        "category": c.category.value,
                        "zone": c.zone,
                        "change_type": c.record.change_type.value,
                        "old_value": c.record.old_value,
                        "new_value": c.record.new_value,
                        "confidence": round(c.record.confidence, 3),
                        "rationale": c.rationale,
                        "bbox": [round(v, 2) for v in c.record.bbox],
                    }
                )
        sheets.append(entry)

    all_changes = [
        c
        for page in doc_result.pages
        if page.result
        for c in classify_records(page.result.records)
    ]
    payload = {
        "schema": "drawing-compare/report/1",
        "provenance": prov.as_dict(),
        "summary": {
            "total_changes": len(all_changes),
            "by_severity": {k.value: v for k, v in summarize_by_severity(all_changes).items()},
            "by_category": {k.value: v for k, v in summarize_by_category(all_changes).items()},
            "sheet_pairing": doc_result.plan.summary(),
        },
        "sheets": sheets,
    }
    Path(path).write_text(json.dumps(payload, indent=2), encoding="utf-8")
