"""
End-to-end pipeline: load two PDF pages, align, diff, report.

    from drawing_compare.pipeline import compare_drawings
    result = compare_drawings("old.pdf", "new.pdf")
    result.to_html("report.html")
    result.to_json("report.json")
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .alignment import AlignmentResult, compute_alignment
from .diff_engine import DiffRecord, diff_pages
from .pdf_io import PageData, load_pdf_page
from .report import save_html, save_json, render_overlay


@dataclass
class CompareResult:
    old_page: PageData
    new_page: PageData
    alignment: AlignmentResult
    records: list[DiffRecord]
    overlay_image: np.ndarray

    def to_json(self, path: str | Path) -> None:
        save_json(
            self.records,
            path,
            meta={
                "alignment_reliable": self.alignment.reliable,
                "alignment_matches": self.alignment.good_matches,
                "old_vector_primitive_count": len(self.old_page.vector_primitives),
                "new_vector_primitive_count": len(self.new_page.vector_primitives),
            },
        )

    def to_html(self, path: str | Path) -> None:
        save_html(
            self.records,
            self.overlay_image,
            path,
            meta={
                "Alignment": "reliable" if self.alignment.reliable else "UNRELIABLE (check manually)",
                "Alignment matches": self.alignment.good_matches,
                "Differences found": len(self.records),
            },
        )

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for rec in self.records:
            counts[rec.change_type.value] = counts.get(rec.change_type.value, 0) + 1
        return counts


def compare_drawings(
    old_pdf: str | Path,
    new_pdf: str | Path,
    old_page_index: int = 0,
    new_page_index: int = 0,
) -> CompareResult:
    old_page = load_pdf_page(old_pdf, old_page_index)
    new_page = load_pdf_page(new_pdf, new_page_index)

    if not old_page.has_vector_content() or not new_page.has_vector_content():
        # Not a hard failure — diff_text/diff_geometry will just have less
        # to work with. Vector-empty pages will mostly surface as text
        # diffs from any OCR you layer on top (see ocr_ensemble.py) plus
        # sparse/no geometry diffs. Surfaced here so callers can warn users.
        pass

    alignment = compute_alignment(old_page.raster_image, new_page.raster_image)
    records = diff_pages(old_page, new_page, alignment)
    overlay = render_overlay(old_page.raster_image, records, dpi=old_page.render_dpi)

    return CompareResult(
        old_page=old_page,
        new_page=new_page,
        alignment=alignment,
        records=records,
        overlay_image=overlay,
    )
