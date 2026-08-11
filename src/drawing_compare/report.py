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


def _escape(value) -> str:
    if value is None:
        return ""
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
