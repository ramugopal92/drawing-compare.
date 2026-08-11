"""
End-to-end pipeline: load PDF pages, align, diff, report.

Single page:

    from drawing_compare.pipeline import compare_drawings
    result = compare_drawings("old.pdf", "new.pdf")
    result.to_html("report.html")
    result.to_json("report.json")

Whole document (multi-page sets, sheets matched automatically):

    from drawing_compare.pipeline import compare_documents
    doc = compare_documents("old.pdf", "new.pdf")
    print(doc.plan.summary())
    doc.to_html("report.html")
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from .alignment import AlignmentResult, compute_alignment
from .diff_engine import DiffRecord, diff_pages
from .classify import classify_records, summarize_by_severity
from .page_matcher import MatchPlan, PagePair, match_pages
from .pdf_io import PageData, load_pdf_page, scan_pdf_pages
from .report import (
    render_overlay,
    save_html,
    save_json,
    save_multipage_html,
    save_multipage_json,
)


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


# --------------------------------------------------------------------------
# Multi-page / whole-document comparison
# --------------------------------------------------------------------------


@dataclass
class PageComparison:
    """One matched sheet pair, plus its diff. `result` is None for a sheet
    that was added or removed outright — there is nothing to diff against."""

    pair: PagePair
    result: CompareResult | None = None
    error: str | None = None

    @property
    def record_count(self) -> int:
        return len(self.result.records) if self.result else 0

    @property
    def status(self) -> str:
        if self.error:
            return "error"
        if self.pair.old_index is None:
            return "added"
        if self.pair.new_index is None:
            return "removed"
        return "compared"


@dataclass
class DocumentCompareResult:
    old_pdf: Path
    new_pdf: Path
    plan: MatchPlan
    pages: list[PageComparison] = field(default_factory=list)

    @property
    def total_records(self) -> int:
        return sum(p.record_count for p in self.pages)

    @property
    def changed_pages(self) -> list[PageComparison]:
        return [p for p in self.pages if p.record_count > 0 or p.status in {"added", "removed"}]

    def summary(self) -> dict[str, int]:
        """Change-type counts rolled up across every compared sheet."""
        counts: dict[str, int] = {}
        for page in self.pages:
            if not page.result:
                continue
            for change_type, count in page.result.summary().items():
                counts[change_type] = counts.get(change_type, 0) + count
        return counts

    def severity_summary(self) -> dict[str, int]:
        """Change counts by engineering severity across the whole set."""
        records = [r for p in self.pages if p.result for r in p.result.records]
        return {k.value: v for k, v in summarize_by_severity(classify_records(records)).items()}

    def critical_changes(self):
        """The changes that alter what gets made or bought."""
        out = []
        for page in self.pages:
            if not page.result:
                continue
            for c in classify_records(page.result.records):
                if c.severity.value == "Critical":
                    out.append((page.pair.label(), c))
        return out

    def unreliable_pages(self) -> list[PageComparison]:
        return [
            p for p in self.pages if p.result and not p.result.alignment.reliable
        ]

    def to_html(self, path: str | Path) -> None:
        save_multipage_html(self, path)

    def to_json(self, path: str | Path) -> None:
        save_multipage_json(self, path)


def compare_documents(
    old_pdf: str | Path,
    new_pdf: str | Path,
    match_mode: str = "auto",
    page_pairs: list[tuple[int, int]] | None = None,
    keep_page_data: bool = False,
    progress: Callable[[int, int, str], None] | None = None,
) -> DocumentCompareResult:
    """
    Compare two multi-page drawing PDFs.

    match_mode:     how to pair sheets — "auto", "sheet_label", "content",
                    or "sequential". Ignored when `page_pairs` is given.
    page_pairs:     explicit 0-based (old_index, new_index) pairs, for when
                    the user has overridden the automatic matching in the UI.
    keep_page_data: keep each page's raster + primitives on the result.
                    Off by default — a 40-sheet set at 300 DPI is several
                    GB of pixels if you hold it all. Overlays are kept
                    either way, since the report needs them.
    progress:       optional callback(done, total, label) for UI progress
                    bars.
    """
    old_pdf, new_pdf = Path(old_pdf), Path(new_pdf)

    old_summaries = scan_pdf_pages(old_pdf)
    new_summaries = scan_pdf_pages(new_pdf)

    if page_pairs is not None:
        plan = MatchPlan(
            pairs=[
                PagePair(old_index=oi, new_index=ni, method="manual", score=1.0)
                for oi, ni in page_pairs
            ]
        )
    else:
        plan = match_pages(old_summaries, new_summaries, mode=match_mode)

    comparisons: list[PageComparison] = []
    total = len(plan.pairs)

    for done, pair in enumerate(plan.pairs, start=1):
        if progress:
            progress(done, total, pair.label())

        if not pair.is_pair:
            # Added or removed sheet — record it, nothing to diff.
            comparisons.append(PageComparison(pair=pair))
            continue

        try:
            result = compare_drawings(
                old_pdf,
                new_pdf,
                old_page_index=pair.old_index,
                new_page_index=pair.new_index,
            )
            if not keep_page_data:
                # Drop the heavy per-page arrays; the overlay is already
                # rendered and the diff records are self-contained.
                result.old_page.raster_image = None
                result.new_page.raster_image = None
                result.old_page.vector_primitives = []
                result.new_page.vector_primitives = []
            comparisons.append(PageComparison(pair=pair, result=result))
        except Exception as exc:  # one bad sheet shouldn't sink the set
            comparisons.append(PageComparison(pair=pair, error=f"{type(exc).__name__}: {exc}"))

    return DocumentCompareResult(
        old_pdf=old_pdf, new_pdf=new_pdf, plan=plan, pages=comparisons
    )
