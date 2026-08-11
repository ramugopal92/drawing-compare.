"""
Core diff engine.

Two stages:
  1. Geometry diff — match vector primitives between old/new (after
     alignment) by bounding-box IoU. Unmatched old primitives are
     "Geometry Removed", unmatched new ones are "Geometry Added", matched
     pairs whose stroke width / kind changed are "Geometry Changed".
  2. Text diff — match text spans by position + fuzzy text similarity.
     Same idea: unmatched old = "Text Removed", unmatched new = "Text
     Added", matched-but-different = "Text Changed" (this is where
     dimension value changes like 25.4 -> 24.8 show up).

Both stages produce the same DiffRecord shape so the report/UI layer
doesn't need to know which stage produced a given row — mirrors the unified
"Difference List" (Zone / Type) you saw in the reference tool.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from rapidfuzz import fuzz

from .alignment import AlignmentResult, transform_bbox
from .config import GEOMETRY_MATCH_IOU, TEXT_FUZZY_MATCH_THRESHOLD, TEXT_POSITION_TOLERANCE_PT
from .pdf_io import PageData, TextSpan, VectorPrimitive
from .zones import zone_label_for_bbox


class ChangeType(str, Enum):
    GEOMETRY_ADDED = "Geometry Added"
    GEOMETRY_REMOVED = "Geometry Removed"
    GEOMETRY_CHANGED = "Geometry Changed"
    TEXT_ADDED = "Text Added"
    TEXT_REMOVED = "Text Removed"
    TEXT_CHANGED = "Text Changed"


@dataclass
class DiffRecord:
    zone: str
    change_type: ChangeType
    bbox: tuple[float, float, float, float]  # in OLD page's coordinate space, PDF points
    old_value: str | None = None
    new_value: str | None = None
    confidence: float = 1.0


def _bbox_iou(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    inter = (ix1 - ix0) * (iy1 - iy0)
    area_a = max(ax1 - ax0, 1e-6) * max(ay1 - ay0, 1e-6)
    area_b = max(bx1 - bx0, 1e-6) * max(by1 - by0, 1e-6)
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def _bbox_center_distance(a, b) -> float:
    ax = (a[0] + a[2]) / 2.0
    ay = (a[1] + a[3]) / 2.0
    bx = (b[0] + b[2]) / 2.0
    by = (b[1] + b[3]) / 2.0
    return ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5


def diff_geometry(
    old_page: PageData,
    new_page: PageData,
    alignment: AlignmentResult,
) -> list[DiffRecord]:
    old_prims = old_page.vector_primitives
    new_prims = [
        VectorPrimitive(
            kind=p.kind,
            bbox=_aligned_bbox(p.bbox, alignment, old_page.render_dpi),
            stroke_width=p.stroke_width,
        )
        for p in new_page.vector_primitives
    ]

    matched_new_idx: set[int] = set()
    records: list[DiffRecord] = []

    for old_p in old_prims:
        best_idx, best_iou = -1, 0.0
        for i, new_p in enumerate(new_prims):
            if i in matched_new_idx:
                continue
            if new_p.kind != old_p.kind:
                continue
            iou = _bbox_iou(old_p.bbox, new_p.bbox)
            if iou > best_iou:
                best_idx, best_iou = i, iou

        if best_idx == -1 or best_iou < GEOMETRY_MATCH_IOU:
            records.append(
                DiffRecord(
                    zone=zone_label_for_bbox(old_p.bbox, *old_page.page_size_pt),
                    change_type=ChangeType.GEOMETRY_REMOVED,
                    bbox=old_p.bbox,
                    old_value=old_p.kind,
                    new_value=None,
                )
            )
            continue

        matched_new_idx.add(best_idx)
        new_p = new_prims[best_idx]
        if abs(new_p.stroke_width - old_p.stroke_width) > 0.25:
            records.append(
                DiffRecord(
                    zone=zone_label_for_bbox(old_p.bbox, *old_page.page_size_pt),
                    change_type=ChangeType.GEOMETRY_CHANGED,
                    bbox=old_p.bbox,
                    old_value=f"width={old_p.stroke_width:.2f}",
                    new_value=f"width={new_p.stroke_width:.2f}",
                    confidence=best_iou,
                )
            )

    for i, new_p in enumerate(new_prims):
        if i in matched_new_idx:
            continue
        records.append(
            DiffRecord(
                zone=zone_label_for_bbox(new_p.bbox, *old_page.page_size_pt),
                change_type=ChangeType.GEOMETRY_ADDED,
                bbox=new_p.bbox,
                old_value=None,
                new_value=new_p.kind,
            )
        )

    return records


def diff_text(
    old_page: PageData,
    new_page: PageData,
    alignment: AlignmentResult,
) -> list[DiffRecord]:
    old_spans = old_page.text_spans
    new_spans = [
        TextSpan(
            text=s.text,
            bbox=_aligned_bbox(s.bbox, alignment, old_page.render_dpi),
            font_size=s.font_size,
        )
        for s in new_page.text_spans
    ]

    matched_new_idx: set[int] = set()
    records: list[DiffRecord] = []

    for old_s in old_spans:
        best_idx, best_dist = -1, float("inf")
        for i, new_s in enumerate(new_spans):
            if i in matched_new_idx:
                continue
            dist = _bbox_center_distance(old_s.bbox, new_s.bbox)
            if dist < best_dist:
                best_idx, best_dist = i, dist

        if best_idx == -1 or best_dist > TEXT_POSITION_TOLERANCE_PT:
            records.append(
                DiffRecord(
                    zone=zone_label_for_bbox(old_s.bbox, *old_page.page_size_pt),
                    change_type=ChangeType.TEXT_REMOVED,
                    bbox=old_s.bbox,
                    old_value=old_s.text,
                    new_value=None,
                )
            )
            continue

        matched_new_idx.add(best_idx)
        new_s = new_spans[best_idx]
        similarity = fuzz.ratio(old_s.text, new_s.text)
        if similarity < TEXT_FUZZY_MATCH_THRESHOLD:
            records.append(
                DiffRecord(
                    zone=zone_label_for_bbox(old_s.bbox, *old_page.page_size_pt),
                    change_type=ChangeType.TEXT_CHANGED,
                    bbox=old_s.bbox,
                    old_value=old_s.text,
                    new_value=new_s.text,
                    confidence=similarity / 100.0,
                )
            )

    for i, new_s in enumerate(new_spans):
        if i in matched_new_idx:
            continue
        records.append(
            DiffRecord(
                zone=zone_label_for_bbox(new_s.bbox, *old_page.page_size_pt),
                change_type=ChangeType.TEXT_ADDED,
                bbox=new_s.bbox,
                old_value=None,
                new_value=new_s.text,
            )
        )

    return records


def _aligned_bbox(bbox, alignment: AlignmentResult, dpi: int):
    """
    Vector/text bboxes are in PDF points; the homography was computed on
    pixel-space raster images. Convert -> transform -> convert back.
    """
    from .alignment import pdf_points_to_pixels, pixels_to_pdf_points

    bbox_px = pdf_points_to_pixels(bbox, dpi)
    transformed_px = transform_bbox(alignment.homography, bbox_px)
    return pixels_to_pdf_points(transformed_px, dpi)


def diff_pages(
    old_page: PageData, new_page: PageData, alignment: AlignmentResult
) -> list[DiffRecord]:
    records = []
    records.extend(diff_geometry(old_page, new_page, alignment))
    records.extend(diff_text(old_page, new_page, alignment))
    return records
