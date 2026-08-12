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
from .report import render_new_overlay, render_overlay, save_html, save_json
from .structured_report import save_structured_json, save_structured_report


@dataclass
class CompareResult:
    old_page: PageData
    new_page: PageData
    alignment: AlignmentResult
    records: list[DiffRecord]
    overlay_image: np.ndarray
    # The same change boxes drawn on the new sheet, so a reviewer can put
    # the two revisions side by side and see what replaced what.
    new_overlay_image: np.ndarray | None = None

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
    new_overlay = render_new_overlay(
        new_page.raster_image, records, alignment, dpi=new_page.render_dpi
    )

    return CompareResult(
        old_page=old_page,
        new_page=new_page,
        alignment=alignment,
        records=records,
        overlay_image=overlay,
        new_overlay_image=new_overlay,
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
    views_added: list[str] = field(default_factory=list)
    views_removed: list[str] = field(default_factory=list)

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
    old_page_count: int = 0
    new_page_count: int = 0
    match_mode: str = "auto"
    title_block: object | None = None
    old_title_block: object | None = None

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

    def to_html(self, path: str | Path, provenance=None, drawing_title: str | None = None) -> None:
        """
        Write the structured HTML report.

        Provenance is built here if the caller didn't supply one, so a
        report always carries file digests and settings even when produced
        from a bare script.
        """
        save_structured_report(
            self, provenance or self.build_provenance(), path, drawing_title=drawing_title
        )

    def to_json(self, path: str | Path, provenance=None) -> None:
        save_structured_json(self, provenance or self.build_provenance(), path)

    def build_provenance(self, **kwargs):
        from .provenance import build_provenance

        return build_provenance(
            self.old_pdf,
            self.new_pdf,
            old_pages=self.old_page_count,
            new_pages=self.new_page_count,
            match_mode=self.match_mode,
            **kwargs,
        )

    def drawing_title(self) -> str | None:
        """
        Drawing identity for the report heading.

        Read from the title block by label — DRAWING NO, TITLE, REVISION —
        rather than by pattern-matching the title-block area, which picks up
        the company phone number as often as the drawing number.
        """
        if self.title_block is not None:
            described = self.title_block.describe()
            if described:
                return described
        for pair in self.plan.pairs:
            identity = pair.old_identity or pair.new_identity
            if identity and identity.label():
                return identity.label()
        return None

    def revision_summary(self) -> dict[str, str | None]:
        """
        The header an engineer expects on a comparison: which drawing, from
        which revision to which, and what the revision block says changed.

        The description is taken from the revision table's own new row —
        the drafter's own words — rather than being synthesised from the
        detected differences, so the two can be read against each other.
        """
        from .classify import classify_records

        from .classify import _REVISION_DESCRIPTION_RE

        # The revision block holds a date, initials, an EC number and the
        # description. Only the description is prose, so it is selected by
        # wording rather than by being the longest field — otherwise a date
        # or an approver's initials wins.
        description = None
        for page in self.pages:
            if not page.result:
                continue
            for change in classify_records(page.result.records):
                in_revision_block = (
                    change.record.region == "revision_table"
                    or change.component.value == "Revision table"
                )
                candidate = change.display_new or change.record.new_value
                if not candidate:
                    continue
                looks_like_description = _REVISION_DESCRIPTION_RE.search(candidate)
                if not (in_revision_block or looks_like_description):
                    continue
                if not looks_like_description and not any(
                    ch.isalpha() for ch in candidate
                ):
                    continue
                if looks_like_description and (
                    description is None
                    or not _REVISION_DESCRIPTION_RE.search(description)
                    or len(candidate) > len(description)
                ):
                    description = candidate
        return {
            "drawing_number": getattr(self.title_block, "drawing_number", None),
            "title": getattr(self.title_block, "title", None),
            "previous_revision": getattr(self.old_title_block, "revision", None),
            "current_revision": getattr(self.title_block, "revision", None),
            "revision_description": description,
        }

    def view_changes(self) -> tuple[list[str], list[str]]:
        """Views added and removed across the whole set."""
        added: list[str] = []
        removed: list[str] = []
        for page in self.pages:
            added.extend(page.views_added)
            removed.extend(page.views_removed)
        return sorted(set(added)), sorted(set(removed))


def _view_inventory_change(result: CompareResult) -> tuple[list[str], list[str]]:
    """
    Which drawing views were added or removed on this sheet.

    Once views are recovered as objects this is a set difference — "Section
    D-D added", "Detail G removed" — rather than something a reader has to
    infer from scattered geometry differences.
    """
    from .diff_engine import group_text_lines
    from .layout import analyse_sheet, diff_view_inventory

    old_layout = analyse_sheet(group_text_lines(result.old_page), result.old_page.page_size_pt)
    new_layout = analyse_sheet(group_text_lines(result.new_page), result.new_page.page_size_pt)
    return diff_view_inventory(old_layout, new_layout)


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
            views_added, views_removed = _view_inventory_change(result)
            if not keep_page_data:
                # Drop the heavy per-page arrays; the overlay is already
                # rendered and the diff records are self-contained.
                result.old_page.raster_image = None
                result.new_page.raster_image = None
                result.old_page.vector_primitives = []
                result.new_page.vector_primitives = []
            comparisons.append(
                PageComparison(
                    pair=pair,
                    result=result,
                    views_added=views_added,
                    views_removed=views_removed,
                )
            )
        except Exception as exc:  # one bad sheet shouldn't sink the set
            comparisons.append(PageComparison(pair=pair, error=f"{type(exc).__name__}: {exc}"))

    def read_title_block(pdf_path):
        """Title-block fields from a document's first sheet.

        Cells, not grouped lines: grouping merges a title-block label with
        the value beside it, leaving the label-anchored lookup nothing to
        anchor to. Read for BOTH revisions so the report can state which
        revision went to which."""
        from .diff_engine import group_text_cells, group_text_lines
        from .layout import analyse_sheet, extract_title_block_fields
        from .pdf_io import load_pdf_page

        first = load_pdf_page(pdf_path, 0)
        lines = group_text_lines(first)
        return extract_title_block_fields(
            group_text_cells(first),
            analyse_sheet(lines, first.page_size_pt),
            lines=lines,
        )

    try:
        title_block = read_title_block(new_pdf)
    except Exception:
        title_block = None
    try:
        old_title_block = read_title_block(old_pdf)
    except Exception:
        old_title_block = None

    return DocumentCompareResult(
        old_pdf=old_pdf,
        new_pdf=new_pdf,
        plan=plan,
        pages=comparisons,
        old_page_count=len(old_summaries),
        new_page_count=len(new_summaries),
        match_mode=match_mode,
        title_block=title_block,
        old_title_block=old_title_block,
    )
