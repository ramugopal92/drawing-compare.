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

import re

from dataclasses import dataclass
from enum import Enum

from rapidfuzz import fuzz

from .alignment import AlignmentResult, transform_bbox
from .config import (
    CLUSTER_PAIR_COUNT_RATIO,
    CLUSTER_PAIR_MAX_DISTANCE_PT,
    GEOMETRY_CLUSTER_GAP_PT,
    GEOMETRY_MATCH_TOLERANCE_PT,
    GEOMETRY_MIN_CLUSTER_PRIMITIVES,
    DUAL_DIMENSION_AGGREGATE_THRESHOLD,
    GLYPH_JOIN_GAP_RATIO,
    GEOMETRY_MAX_CLUSTER_DENSITY,
    TEXT_MASK_PADDING_PT,
    REPORT_LINE_WEIGHT_CHANGES,
    TEXT_PAIR_FALLBACK_DISTANCE_PT,
    TEXT_PAIR_FALLBACK_SIMILARITY,
    TEXT_PAIR_MAX_DISTANCE_PT,
    TEXT_FUZZY_MATCH_THRESHOLD,
    TEXT_POSITION_TOLERANCE_PT,
)
from .pdf_io import PageData, TextSpan, VectorPrimitive
from .zones import (
    detect_zone_grid,
    zone_label_for_bbox,
    zone_label_for_bbox_with_grid,
)


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


def _build_text_mask(text_spans: list[TextSpan], cell: float = 24.0) -> dict:
    """Grid index of text bounding boxes, for fast containment tests."""
    index: dict[tuple, list[tuple]] = {}
    pad = TEXT_MASK_PADDING_PT
    for span in text_spans:
        x0, y0, x1, y1 = span.bbox
        box = (x0 - pad, y0 - pad, x1 + pad, y1 + pad)
        for gx in range(int(box[0] // cell), int(box[2] // cell) + 1):
            for gy in range(int(box[1] // cell), int(box[3] // cell) + 1):
                index.setdefault((gx, gy), []).append(box)
    return {"cell": cell, "index": index}


def _inside_text(bbox, mask: dict) -> bool:
    """True if this primitive lies entirely within some text bounding box.

    Certain CAD exports render text as filled vector outlines rather than
    as font glyphs. Those outlines land in get_drawings() as hundreds of
    tiny paths, so rewording one note reads as a large geometry change.
    Anything wholly contained in a text box is lettering, not drawing
    geometry, and belongs to the text diff instead.
    """
    cell = mask["cell"]
    index = mask["index"]
    gx, gy = int(bbox[0] // cell), int(bbox[1] // cell)
    for box in index.get((gx, gy), ()):
        if (
            bbox[0] >= box[0]
            and bbox[1] >= box[1]
            and bbox[2] <= box[2]
            and bbox[3] <= box[3]
        ):
            return True
    return False


def diff_geometry(
    old_page: PageData,
    new_page: PageData,
    alignment: AlignmentResult,
) -> list[DiffRecord]:
    """
    Compare vector geometry between two pages.

    Reports engineering-level changes, not primitive-level ones:
      - unmatched primitives are clustered by proximity
      - a removed cluster sitting next to an added cluster of similar size
        is one feature that was edited, reported as a single row
      - line-weight-only differences are collapsed into one note per sheet
        unless REPORT_LINE_WEIGHT_CHANGES is enabled
    """
    old_mask = _build_text_mask(old_page.text_spans)
    new_mask = _build_text_mask(new_page.text_spans)

    old_prims = [p for p in old_page.vector_primitives if not _inside_text(p.bbox, old_mask)]
    new_prims = [
        VectorPrimitive(
            kind=p.kind,
            bbox=_aligned_bbox(p.bbox, alignment, old_page.render_dpi),
            stroke_width=p.stroke_width,
        )
        for p in new_page.vector_primitives
        if not _inside_text(p.bbox, new_mask)
    ]

    pairs, unmatched_old, unmatched_new = _match_primitives(old_prims, new_prims)
    page_size = old_page.page_size_pt
    records: list[DiffRecord] = []

    weight_changes: list[tuple[VectorPrimitive, VectorPrimitive]] = []
    for i, j in pairs:
        op, np_ = old_prims[i], new_prims[j]
        if abs(np_.stroke_width - op.stroke_width) > 0.25:
            weight_changes.append((op, np_))

    if weight_changes:
        if REPORT_LINE_WEIGHT_CHANGES:
            for op, np_ in weight_changes:
                records.append(
                    DiffRecord(
                        zone=zone_label_for_bbox(op.bbox, *page_size),
                        change_type=ChangeType.GEOMETRY_CHANGED,
                        bbox=op.bbox,
                        old_value=f"line weight {op.stroke_width:.2f}",
                        new_value=f"line weight {np_.stroke_width:.2f}",
                    )
                )
        else:
            boxes = [op.bbox for op, _ in weight_changes]
            transitions: dict[tuple[str, str], int] = {}
            for op, np_ in weight_changes:
                key = (f"{op.stroke_width:.2f}", f"{np_.stroke_width:.2f}")
                transitions[key] = transitions.get(key, 0) + 1
            detail = "; ".join(
                f"{a} to {b} on {n} primitives"
                for (a, b), n in sorted(transitions.items(), key=lambda kv: -kv[1])
            )
            records.append(
                DiffRecord(
                    zone="sheet",
                    change_type=ChangeType.GEOMETRY_CHANGED,
                    bbox=_union_bbox(boxes),
                    old_value="line weights differ",
                    new_value=f"{len(weight_changes)} primitives ({detail})",
                    confidence=0.3,
                )
            )

    removed = _build_clusters([old_prims[i] for i in unmatched_old])
    added = _build_clusters([new_prims[j] for j in unmatched_new])
    paired, removed_only, added_only = _pair_clusters(removed, added)

    for r, a in paired:
        offset = _bbox_center_distance(r["bbox"], a["bbox"])
        moved = f", shifted {offset:.0f} pt" if offset >= 1.0 else ""
        records.append(
            DiffRecord(
                zone=zone_label_for_bbox(r["bbox"], *page_size),
                change_type=ChangeType.GEOMETRY_CHANGED,
                bbox=_union_bbox([r["bbox"], a["bbox"]]),
                old_value=f"{r['summary']} ({r['size']})",
                new_value=f"{a['summary']} ({a['size']}){moved}",
            )
        )

    for c in removed_only:
        records.append(
            DiffRecord(
                zone=zone_label_for_bbox(c["bbox"], *page_size),
                change_type=ChangeType.GEOMETRY_REMOVED,
                bbox=c["bbox"],
                old_value=f"{c['summary']} ({c['size']})",
            )
        )
    for c in added_only:
        records.append(
            DiffRecord(
                zone=zone_label_for_bbox(c["bbox"], *page_size),
                change_type=ChangeType.GEOMETRY_ADDED,
                bbox=c["bbox"],
                new_value=f"{c['summary']} ({c['size']})",
            )
        )
    return records


def _build_clusters(prims: list[VectorPrimitive]) -> list[dict]:
    """Group unmatched primitives into clusters described in plain terms."""
    if not prims:
        return []
    clusters: list[dict] = []
    for group in _cluster_bboxes([p.bbox for p in prims], GEOMETRY_CLUSTER_GAP_PT):
        if len(group) < GEOMETRY_MIN_CLUSTER_PRIMITIVES:
            continue
        bbox = _union_bbox([prims[i].bbox for i in group])
        area = max(bbox[2] - bbox[0], 1.0) * max(bbox[3] - bbox[1], 1.0)
        if len(group) / area > GEOMETRY_MAX_CLUSTER_DENSITY:
            continue
        kinds: dict[str, int] = {}
        for i in group:
            kinds[prims[i].kind] = kinds.get(prims[i].kind, 0) + 1
        summary = ", ".join(
            f"{n} {k}{'s' if n > 1 else ''}"
            for k, n in sorted(kinds.items(), key=lambda kv: -kv[1])
        )
        clusters.append(
            {
                "bbox": bbox,
                "count": len(group),
                "summary": summary,
                "size": f"{bbox[2] - bbox[0]:.0f} x {bbox[3] - bbox[1]:.0f} pt",
            }
        )
    return clusters


def _pair_clusters(
    removed: list[dict], added: list[dict]
) -> tuple[list[tuple[dict, dict]], list[dict], list[dict]]:
    """
    Match removed clusters to added clusters that are probably the same
    feature edited in place.

    Without this, moving one bracket produces "26 lines removed in D6" and
    "26 lines added in D6" as two unrelated rows, and the engineer has to
    work out for themselves that they are the same edit. Candidates are
    scored by centre distance and accepted closest-first, provided their
    primitive counts are comparable.
    """
    candidates: list[tuple[float, int, int]] = []
    for i, r in enumerate(removed):
        for j, a in enumerate(added):
            dist = _bbox_center_distance(r["bbox"], a["bbox"])
            if dist > CLUSTER_PAIR_MAX_DISTANCE_PT:
                continue
            lo, hi = sorted((r["count"], a["count"]))
            if hi == 0 or lo / hi < CLUSTER_PAIR_COUNT_RATIO:
                continue
            candidates.append((dist, i, j))

    used_r: set[int] = set()
    used_a: set[int] = set()
    paired: list[tuple[dict, dict]] = []
    for _, i, j in sorted(candidates, key=lambda t: t[0]):
        if i in used_r or j in used_a:
            continue
        used_r.add(i)
        used_a.add(j)
        paired.append((removed[i], added[j]))

    return (
        paired,
        [c for i, c in enumerate(removed) if i not in used_r],
        [c for j, c in enumerate(added) if j not in used_a],
    )


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


def _group_spans_into_lines(spans: list[TextSpan]) -> list[TextSpan]:
    """
    Merge word-level spans into whole text lines.

    Diffing individual words is what produced the cascade failure: insert
    one word into a note and every following word shifts position, so each
    one is reported as changed. "SEE 322451" becoming "SEE DRAWING 322451"
    generated ten rows of nonsense (322451 -> DRAWING, FOR -> 322451, ...).
    At line level it is one row that reads the way an engineer would
    describe it.

    Spans are grouped when they share a baseline (within half the font
    height) and sit close enough horizontally to be the same line of text.
    """
    if not spans:
        return []

    ordered = sorted(spans, key=lambda s: (round(s.bbox[1], 1), s.bbox[0]))
    lines: list[list[TextSpan]] = []
    current: list[TextSpan] = [ordered[0]]

    for span in ordered[1:]:
        prev = current[-1]
        height = max(prev.bbox[3] - prev.bbox[1], 1.0)
        same_baseline = abs(span.bbox[1] - prev.bbox[1]) <= height * 0.6
        gap = span.bbox[0] - prev.bbox[2]
        adjacent = -height <= gap <= height * 2.5
        if same_baseline and adjacent:
            current.append(span)
        else:
            lines.append(current)
            current = [span]
    lines.append(current)

    merged: list[TextSpan] = []
    for group in lines:
        # Join with a space only where the fragments were actually separated
        # on the page. Some exports emit rotated or kerned text one glyph at
        # a time, and joining those with spaces yields "5 2 4 . 5 4" instead
        # of "524.54" — which then fails to match its counterpart and is
        # reported as a change that never happened.
        pieces: list[str] = []
        for position, span in enumerate(group):
            if position:
                previous = group[position - 1]
                height = max(previous.bbox[3] - previous.bbox[1], 1.0)
                gap = span.bbox[0] - previous.bbox[2]
                # Measured on real exports, glyph-level splits sit at a gap
                # ratio of ~0.00 and genuine word spaces at ~0.28, so the
                # threshold has plenty of clearance either side.
                if gap > height * GLYPH_JOIN_GAP_RATIO:
                    pieces.append(" ")
            pieces.append(span.text)
        text = "".join(pieces).strip()
        if not text:
            continue
        merged.append(
            TextSpan(
                text=text,
                bbox=_union_bbox([s.bbox for s in group]),
                font_size=max(s.font_size for s in group),
            )
        )
    return merged


def diff_text(
    old_page: PageData,
    new_page: PageData,
    alignment: AlignmentResult,
    skip_old: set[int] | None = None,
    skip_new: set[int] | None = None,
) -> list[DiffRecord]:
    """
    Compare text between two pages, one row per changed line.

    Lines are matched by position first, then by content among anything
    left over — so a note that moved down the sheet is reported as one
    change rather than as a deletion plus an unrelated addition.
    """
    old_lines = _group_spans_into_lines(old_page.text_spans)
    new_lines = _group_spans_into_lines(
        [
            TextSpan(
                text=s.text,
                bbox=_aligned_bbox(s.bbox, alignment, old_page.render_dpi),
                font_size=s.font_size,
            )
            for s in new_page.text_spans
        ]
    )

    page_size = old_page.page_size_pt
    records: list[DiffRecord] = []
    # Lines already handled by a structured comparator (the parts list) are
    # marked used so the same change is not reported twice, once as a table
    # field and once as loose text.
    used_new: set[int] = set(skip_new or ())
    matched_old: set[int] = set(skip_old or ())

    identical: dict[str, list[int]] = {}
    for i, line in enumerate(new_lines):
        if i in used_new:
            continue
        identical.setdefault(line.text, []).append(i)

    for i, old_line in enumerate(old_lines):
        for j in identical.get(old_line.text, []):
            if j in used_new:
                continue
            if _bbox_center_distance(old_line.bbox, new_lines[j].bbox) <= TEXT_POSITION_TOLERANCE_PT:
                used_new.add(j)
                matched_old.add(i)
                break

    for i, old_line in enumerate(old_lines):
        if i in matched_old:
            continue
        best_j, best_score = -1, 0.0
        for j, new_line in enumerate(new_lines):
            if j in used_new:
                continue
            if _bbox_center_distance(old_line.bbox, new_line.bbox) > TEXT_POSITION_TOLERANCE_PT:
                continue
            score = fuzz.ratio(old_line.text, new_line.text)
            if score > best_score:
                best_j, best_score = j, score
        if best_j >= 0:
            used_new.add(best_j)
            matched_old.add(i)
            if best_score < TEXT_FUZZY_MATCH_THRESHOLD:
                records.append(
                    DiffRecord(
                        zone=zone_label_for_bbox(old_line.bbox, *page_size),
                        change_type=ChangeType.TEXT_CHANGED,
                        bbox=old_line.bbox,
                        old_value=old_line.text,
                        new_value=new_lines[best_j].text,
                        confidence=best_score / 100.0,
                    )
                )

    for i, old_line in enumerate(old_lines):
        if i in matched_old:
            continue
        best_j, best_score = -1, 0.0
        for j, new_line in enumerate(new_lines):
            if j in used_new:
                continue
            score = fuzz.ratio(old_line.text, new_line.text)
            if score > best_score:
                best_j, best_score = j, score
        if best_j >= 0 and best_score >= TEXT_FUZZY_MATCH_THRESHOLD:
            used_new.add(best_j)
            matched_old.add(i)
            moved = _bbox_center_distance(old_line.bbox, new_lines[best_j].bbox)
            if moved > TEXT_POSITION_TOLERANCE_PT:
                records.append(
                    DiffRecord(
                        zone=zone_label_for_bbox(old_line.bbox, *page_size),
                        change_type=ChangeType.TEXT_CHANGED,
                        bbox=old_line.bbox,
                        old_value=old_line.text,
                        new_value=f"{new_lines[best_j].text} (moved {moved:.0f} pt)",
                        confidence=best_score / 100.0,
                    )
                )

    leftover_old = [i for i in range(len(old_lines)) if i not in matched_old]
    leftover_new = [j for j in range(len(new_lines)) if j not in used_new]
    candidates = []
    for i in leftover_old:
        for j in leftover_new:
            d = _bbox_center_distance(old_lines[i].bbox, new_lines[j].bbox)
            if d <= TEXT_PAIR_MAX_DISTANCE_PT:
                candidates.append((d, i, j))
            elif d <= TEXT_PAIR_FALLBACK_DISTANCE_PT:
                score = fuzz.ratio(old_lines[i].text, new_lines[j].text)
                if score >= TEXT_PAIR_FALLBACK_SIMILARITY:
                    # Rank behind every positional match, best wording first.
                    candidates.append((TEXT_PAIR_MAX_DISTANCE_PT + (100 - score), i, j))
    for _, i, j in sorted(candidates, key=lambda t: t[0]):
        if i in matched_old or j in used_new:
            continue
        matched_old.add(i)
        used_new.add(j)
        records.append(
            DiffRecord(
                zone=zone_label_for_bbox(old_lines[i].bbox, *page_size),
                change_type=ChangeType.TEXT_CHANGED,
                bbox=old_lines[i].bbox,
                old_value=old_lines[i].text,
                new_value=new_lines[j].text,
                confidence=fuzz.ratio(old_lines[i].text, new_lines[j].text) / 100.0,
            )
        )

    for i, old_line in enumerate(old_lines):
        if i not in matched_old:
            records.append(
                DiffRecord(
                    zone=zone_label_for_bbox(old_line.bbox, *page_size),
                    change_type=ChangeType.TEXT_REMOVED,
                    bbox=old_line.bbox,
                    old_value=old_line.text,
                )
            )
    added_records: list[DiffRecord] = []
    for j, new_line in enumerate(new_lines):
        if j not in used_new:
            added_records.append(
                DiffRecord(
                    zone=zone_label_for_bbox(new_line.bbox, *page_size),
                    change_type=ChangeType.TEXT_ADDED,
                    bbox=new_line.bbox,
                    new_value=new_line.text,
                )
            )

    records.extend(_collapse_dual_dimensions(added_records, old_lines, page_size))
    return records


_METRIC_VALUE_RE = re.compile(r"^\d{1,5}(?:\.\d{1,2})?$")


def _collapse_dual_dimensions(
    added: list[DiffRecord],
    old_lines: list[TextSpan],
    page_size: tuple[float, float],
) -> list[DiffRecord]:
    """
    Fold "added metric equivalent" lines into a single row.

    When a drawing is dual-dimensioned, every imperial dimension gains a
    bare metric value directly beneath it. Each one is a real addition, but
    together they are one drafting decision — and listed individually they
    outnumber the actual design change by fifty to one.

    A line qualifies only if it is a bare number sitting just below an
    existing dimension that did not itself change, so a genuinely new
    dimension elsewhere on the sheet is still reported on its own.
    """
    candidates: list[DiffRecord] = []
    others: list[DiffRecord] = []

    for record in added:
        text = (record.new_value or "").strip()
        if not _METRIC_VALUE_RE.match(text):
            others.append(record)
            continue
        x0, y0, x1, _ = record.bbox
        above = any(
            line.bbox[0] < x1
            and line.bbox[2] > x0
            and 0 < y0 - line.bbox[1] <= 22.0
            for line in old_lines
        )
        (candidates if above else others).append(record)

    if len(candidates) < DUAL_DIMENSION_AGGREGATE_THRESHOLD:
        return added

    boxes = [r.bbox for r in candidates]
    others.append(
        DiffRecord(
            zone="sheet",
            change_type=ChangeType.TEXT_ADDED,
            bbox=_union_bbox(boxes),
            new_value=(
                f"dual dimensioning: metric equivalents added beneath "
                f"{len(candidates)} dimensions"
            ),
            confidence=0.5,
        )
    )
    return others


def _aligned_bbox(bbox, alignment: AlignmentResult, dpi: int):
    """
    Vector/text bboxes are in PDF points; the homography was computed on
    pixel-space raster images. Convert -> transform -> convert back.
    """
    from .alignment import pdf_points_to_pixels, pixels_to_pdf_points

    bbox_px = pdf_points_to_pixels(bbox, dpi)
    transformed_px = transform_bbox(alignment.homography, bbox_px)
    return pixels_to_pdf_points(transformed_px, dpi)


def group_text_lines(page: PageData) -> list[TextSpan]:
    """Public accessor for the line grouping, shared with the BOM pass."""
    return _group_spans_into_lines(page.text_spans)


def diff_pages(
    old_page: PageData, new_page: PageData, alignment: AlignmentResult
) -> list[DiffRecord]:
    """
    Full comparison of one aligned sheet pair.

    Runs structured comparators before general ones. The parts list is a
    table with a primary key (the item number), so comparing it as a table
    is both more accurate and more informative than letting the text differ
    guess at which row is which; whatever it consumes is withheld from the
    text pass so nothing is reported twice.
    """
    from .bom import diff_bom  # imported here to keep module import acyclic

    records: list[DiffRecord] = []
    records.extend(diff_geometry(old_page, new_page, alignment))

    old_lines = group_text_lines(old_page)
    new_lines = group_text_lines(new_page)
    bom_records, used_old, used_new = diff_bom(
        old_lines, new_lines, old_page.page_size_pt
    )
    records.extend(bom_records)
    records.extend(
        diff_text(old_page, new_page, alignment, skip_old=used_old, skip_new=used_new)
    )
    return records
