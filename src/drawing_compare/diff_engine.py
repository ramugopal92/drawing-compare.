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
from .config import (
    GEOMETRY_CLUSTER_GAP_PT,
    GEOMETRY_MATCH_TOLERANCE_PT,
    GEOMETRY_MIN_CLUSTER_PRIMITIVES,
    TEXT_FUZZY_MATCH_THRESHOLD,
    TEXT_POSITION_TOLERANCE_PT,
)
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
    """
    Intersection over union, with degenerate boxes padded.

    A horizontal or vertical line has a zero-area bounding box, so raw IoU
    returns 0.0 even for a line compared against an identical copy of
    itself. Padding by half the match tolerance gives every primitive a
    real area so the ratio means something. Kept for reporting confidence;
    matching itself uses _corner_distance.
    """
    pad = GEOMETRY_MATCH_TOLERANCE_PT / 2.0
    ax0, ay0, ax1, ay1 = a[0] - pad, a[1] - pad, a[2] + pad, a[3] + pad
    bx0, by0, bx1, by1 = b[0] - pad, b[1] - pad, b[2] + pad, b[3] + pad
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    inter = (ix1 - ix0) * (iy1 - iy0)
    area_a = (ax1 - ax0) * (ay1 - ay0)
    area_b = (bx1 - bx0) * (by1 - by0)
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def _bbox_center_distance(a, b) -> float:
    ax = (a[0] + a[2]) / 2.0
    ay = (a[1] + a[3]) / 2.0
    bx = (b[0] + b[2]) / 2.0
    by = (b[1] + b[3]) / 2.0
    return ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5


def _corner_distance(a, b) -> float:
    """Largest distance between corresponding bbox corners. Works for
    zero-area boxes (lines) exactly as well as it does for rects."""
    return max(abs(a[i] - b[i]) for i in range(4))


def _quantize(bbox, tol: float) -> tuple[int, int, int, int]:
    return tuple(int(round(v / tol)) for v in bbox)


def _match_primitives(
    old_prims: list[VectorPrimitive], new_prims: list[VectorPrimitive]
) -> tuple[list[tuple[int, int]], list[int], list[int]]:
    """
    Pair up primitives between two pages.

    Returns (pairs, unmatched_old_indices, unmatched_new_indices).

    Two passes, both keyed off a spatial hash so this is O(n) rather than
    the O(n*n) all-pairs scan it replaces — the difference between seconds
    and many minutes on a sheet with 40k primitives:

      1. Exact pass: quantize each bbox to the match tolerance and bucket
         by (kind, quantized bbox). Geometry that didn't change at all —
         the overwhelming majority on any real revision — matches here
         immediately.
      2. Neighbourhood pass: leftovers are bucketed into a coarse grid and
         compared only against primitives in the same or adjacent cells,
         accepting the closest within tolerance.
    """
    tol = GEOMETRY_MATCH_TOLERANCE_PT
    pairs: list[tuple[int, int]] = []
    used_new: set[int] = set()
    matched_old: set[int] = set()

    exact: dict[tuple, list[int]] = {}
    for j, p in enumerate(new_prims):
        exact.setdefault((p.kind,) + _quantize(p.bbox, tol), []).append(j)

    for i, p in enumerate(old_prims):
        bucket = exact.get((p.kind,) + _quantize(p.bbox, tol))
        if not bucket:
            continue
        for j in bucket:
            if j not in used_new:
                used_new.add(j)
                matched_old.add(i)
                pairs.append((i, j))
                break

    cell = max(tol * 4.0, 4.0)
    grid: dict[tuple, list[int]] = {}
    for j, p in enumerate(new_prims):
        if j in used_new:
            continue
        cx = (p.bbox[0] + p.bbox[2]) / 2.0
        cy = (p.bbox[1] + p.bbox[3]) / 2.0
        grid.setdefault((int(cx // cell), int(cy // cell)), []).append(j)

    for i, p in enumerate(old_prims):
        if i in matched_old:
            continue
        cx = (p.bbox[0] + p.bbox[2]) / 2.0
        cy = (p.bbox[1] + p.bbox[3]) / 2.0
        gx, gy = int(cx // cell), int(cy // cell)
        best_j, best_d = -1, tol
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for j in grid.get((gx + dx, gy + dy), ()):
                    if j in used_new or new_prims[j].kind != p.kind:
                        continue
                    d = _corner_distance(p.bbox, new_prims[j].bbox)
                    if d <= best_d:
                        best_j, best_d = j, d
        if best_j >= 0:
            used_new.add(best_j)
            matched_old.add(i)
            pairs.append((i, best_j))

    unmatched_old = [i for i in range(len(old_prims)) if i not in matched_old]
    unmatched_new = [j for j in range(len(new_prims)) if j not in used_new]
    return pairs, unmatched_old, unmatched_new


def _cluster_bboxes(
    bboxes: list[tuple[float, float, float, float]], gap: float
) -> list[list[int]]:
    """
    Group bboxes that sit within `gap` points of each other into clusters.

    Grid-bucketed flood fill, so it stays linear. One design change moves
    an outline, its dimension lines, and its leaders together; reporting
    those as 40 separate rows is what makes a difference list unreadable.
    """
    if not bboxes:
        return []
    cell = max(gap, 1.0)
    grid: dict[tuple, list[int]] = {}
    for i, b in enumerate(bboxes):
        cx = (b[0] + b[2]) / 2.0
        cy = (b[1] + b[3]) / 2.0
        grid.setdefault((int(cx // cell), int(cy // cell)), []).append(i)

    seen: set[int] = set()
    clusters: list[list[int]] = []
    for start in range(len(bboxes)):
        if start in seen:
            continue
        stack, group = [start], []
        seen.add(start)
        while stack:
            i = stack.pop()
            group.append(i)
            b = bboxes[i]
            cx = (b[0] + b[2]) / 2.0
            cy = (b[1] + b[3]) / 2.0
            gx, gy = int(cx // cell), int(cy // cell)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for j in grid.get((gx + dx, gy + dy), ()):
                        if j in seen:
                            continue
                        if _bbox_gap(b, bboxes[j]) <= gap:
                            seen.add(j)
                            stack.append(j)
        clusters.append(group)
    return clusters


def _bbox_gap(a, b) -> float:
    """Edge-to-edge distance between two boxes; 0 if they touch or overlap."""
    dx = max(0.0, max(a[0] - b[2], b[0] - a[2]))
    dy = max(0.0, max(a[1] - b[3], b[1] - a[3]))
    return (dx * dx + dy * dy) ** 0.5


def _union_bbox(bboxes):
    return (
        min(b[0] for b in bboxes),
        min(b[1] for b in bboxes),
        max(b[2] for b in bboxes),
        max(b[3] for b in bboxes),
    )


def diff_geometry(
    old_page: PageData,
    new_page: PageData,
    alignment: AlignmentResult,
) -> list[DiffRecord]:
    """
    Compare vector geometry between two pages.

    Unmatched primitives are clustered into engineering-level changes
    before being reported: one row per edit, annotated with how many
    primitives it covers, rather than one row per line segment.
    """
    old_prims = old_page.vector_primitives
    new_prims = [
        VectorPrimitive(
            kind=p.kind,
            bbox=_aligned_bbox(p.bbox, alignment, old_page.render_dpi),
            stroke_width=p.stroke_width,
        )
        for p in new_page.vector_primitives
    ]

    pairs, unmatched_old, unmatched_new = _match_primitives(old_prims, new_prims)
    page_size = old_page.page_size_pt
    records: list[DiffRecord] = []

    for i, j in pairs:
        op, np_ = old_prims[i], new_prims[j]
        if abs(np_.stroke_width - op.stroke_width) > 0.25:
            records.append(
                DiffRecord(
                    zone=zone_label_for_bbox(op.bbox, *page_size),
                    change_type=ChangeType.GEOMETRY_CHANGED,
                    bbox=op.bbox,
                    old_value=f"line weight {op.stroke_width:.2f}",
                    new_value=f"line weight {np_.stroke_width:.2f}",
                    confidence=_bbox_iou(op.bbox, np_.bbox),
                )
            )

    records.extend(
        _cluster_records(
            [old_prims[i] for i in unmatched_old],
            ChangeType.GEOMETRY_REMOVED,
            page_size,
        )
    )
    records.extend(
        _cluster_records(
            [new_prims[j] for j in unmatched_new],
            ChangeType.GEOMETRY_ADDED,
            page_size,
        )
    )
    return records


def _cluster_records(
    prims: list[VectorPrimitive],
    change_type: ChangeType,
    page_size: tuple[float, float],
) -> list[DiffRecord]:
    """Turn a list of unmatched primitives into one record per cluster."""
    if not prims:
        return []

    clusters = _cluster_bboxes([p.bbox for p in prims], GEOMETRY_CLUSTER_GAP_PT)
    verb = "removed" if change_type is ChangeType.GEOMETRY_REMOVED else "added"
    records: list[DiffRecord] = []

    for group in clusters:
        if len(group) < GEOMETRY_MIN_CLUSTER_PRIMITIVES:
            continue
        boxes = [prims[i].bbox for i in group]
        bbox = _union_bbox(boxes)
        kinds: dict[str, int] = {}
        for i in group:
            kinds[prims[i].kind] = kinds.get(prims[i].kind, 0) + 1
        summary = ", ".join(
            f"{count} {kind}{'s' if count > 1 else ''}"
            for kind, count in sorted(kinds.items(), key=lambda kv: -kv[1])
        )
        width = bbox[2] - bbox[0]
        height = bbox[3] - bbox[1]
        desc = f"{summary} ({width:.0f} x {height:.0f} pt region)"
        records.append(
            DiffRecord(
                zone=zone_label_for_bbox(bbox, *page_size),
                change_type=change_type,
                bbox=bbox,
                old_value=desc if verb == "removed" else None,
                new_value=desc if verb == "added" else None,
                confidence=1.0,
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
