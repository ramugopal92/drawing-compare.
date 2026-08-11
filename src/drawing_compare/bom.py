"""
Bill of materials extraction and comparison.

A parts list is a table, and comparing it as free text throws away the one
thing that makes the comparison reliable: the item number. Rows 8, 9 and 10
of a fastener list read almost identically ("FLAT WASHER, TYPE A, SERIES N,
1/4" versus "... 3/4"), so a similarity matcher pairs the wrong ones and a
positional matcher breaks the moment a description wraps onto a second
line. Either way the reviewer is told a row was deleted and an unrelated
row added, and has to reconstruct the actual substitution by eye.

Anchoring on the item number removes the ambiguity entirely. Item 10 in the
old revision is item 10 in the new one, and the only question left is which
column changed.

Extraction works on geometry rather than on a template: cells belonging to
one row share a baseline, and columns are recovered by sorting those cells
left to right. That holds across drawing standards without configuration,
because it is how parts lists are drawn rather than how any one company
formats them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .config import (
    BOM_BASELINE_TOLERANCE_PT,
    BOM_COLUMN_TOLERANCE_PT,
    BOM_DESCRIPTION_INDENT_PT,
    BOM_MIN_ROWS,
    TEXT_FUZZY_MATCH_THRESHOLD,
)
from .diff_engine import ChangeType, DiffRecord
from .pdf_io import TextSpan
from .zones import zone_label_for_bbox

_ITEM_CELL_RE = re.compile(r"^\d{1,3}$")
# A whole row arriving as one line: item, part number, quantity, then the
# description. Different PDF text extractors split table rows differently —
# some return each cell separately, some merge a row into a single line —
# so the extractor has to recognise both shapes or it silently finds no
# parts list at all on half the drawings it is given.
_ROW_LINE_RE = re.compile(
    r"^(\d{1,3})\s+((?=[A-Z0-9\-/]*\d)[A-Z0-9][A-Z0-9\-/]{2,})\s+(\d{1,4})\s+(.+)$",
    re.IGNORECASE | re.DOTALL,
)
_PART_CELL_RE = re.compile(r"^(?=[A-Z0-9\-/]*\d)[A-Z0-9][A-Z0-9\-/]{2,}$", re.IGNORECASE)
_QTY_PREFIX_RE = re.compile(r"^(\d{1,4})\s+(.*)$", re.DOTALL)
_STANDARD_CELL_RE = re.compile(
    r"\b(?:ASME|ANSI|ISO|DIN|JIS|BS|EN|SAE|MIL|ASTM)\b", re.IGNORECASE
)
_MATERIAL_CELL_RE = re.compile(
    r"\b(?:SST|SS|CS|UNS|ASTM|AISI|TYPE\s*\d{3}|ALUM|BRASS|BRONZE|NYLON|PTFE|"
    r"STEEL|PLASTIC|RUBBER)\b",
    re.IGNORECASE,
)

# Maximum cell width for something to be an item-number cell. Item columns
# are narrow; a wide cell holding "10" is prose, not a table cell.
_ITEM_CELL_MAX_WIDTH_PT = 40.0


@dataclass
class BomRow:
    """One parts-list entry, decomposed into its columns."""

    item: str
    bbox: tuple[float, float, float, float]
    cell_boxes: list[tuple[float, float, float, float]] = field(default_factory=list)
    part_number: str | None = None
    quantity: str | None = None
    description: str = ""
    specification: str | None = None
    material: str | None = None
    cells: list[str] = field(default_factory=list)

    def fields(self) -> dict[str, str | None]:
        return {
            "part number": self.part_number,
            "quantity": self.quantity,
            "description": self.description or None,
            "specification": self.specification,
            "material": self.material,
        }

    def summary(self) -> str:
        bits = [f"item {self.item}"]
        if self.part_number:
            bits.append(self.part_number)
        if self.quantity:
            bits.append(f"qty {self.quantity}")
        return " · ".join(bits)


def _union(boxes) -> tuple[float, float, float, float]:
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


def extract_bom_rows(lines: list[TextSpan]) -> tuple[list[BomRow], set[int]]:
    """
    Find parts-list rows among the page's text lines.

    Two shapes are recognised, because PDF text extractors disagree about
    table rows: some emit each cell as its own line, others merge a whole
    row into one. Handling only the first silently finds no parts list at
    all on drawings produced by the second, and the rows then fall through
    to the general text diff where they are mistaken for dimensions.

    Returns the rows and the indices of every line consumed by them.
    """
    rows, consumed = _extract_from_cells(lines)
    if len(rows) < BOM_MIN_ROWS:
        rows, consumed = _extract_from_row_lines(lines)
    if len(rows) < BOM_MIN_ROWS:
        return [], set()

    rows.sort(key=lambda r: r.bbox[1])
    _attach_wrapped_lines(rows, lines, consumed)
    for row in rows:
        _parse_columns(row)
    return rows, consumed


def _extract_from_cells(lines: list[TextSpan]) -> tuple[list[BomRow], set[int]]:
    """Shape 1: the item number is its own cell, columns to its right."""
    candidates: list[tuple[int, TextSpan]] = [
        (i, line)
        for i, line in enumerate(lines)
        if _ITEM_CELL_RE.match(line.text.strip())
        and (line.bbox[2] - line.bbox[0]) <= _ITEM_CELL_MAX_WIDTH_PT
    ]
    if len(candidates) < BOM_MIN_ROWS:
        return [], set()

    # A real item column is vertically aligned. Grouping candidates by their
    # left edge and keeping the largest group discards the stray integers in
    # title blocks, tolerance tables and view labels, which would otherwise
    # be read as parts-list rows in completely wrong places.
    by_column: dict[int, list[tuple[int, TextSpan]]] = {}
    for index, line in candidates:
        key = int(round(line.bbox[0] / BOM_COLUMN_TOLERANCE_PT))
        by_column.setdefault(key, []).append((index, line))
    column = max(by_column.values(), key=len)
    if len(column) < BOM_MIN_ROWS:
        return [], set()

    rows: list[BomRow] = []
    consumed: set[int] = set()
    for index, item_cell in column:
        baseline = item_cell.bbox[1]
        same_row = [
            (j, line)
            for j, line in enumerate(lines)
            if j != index
            and abs(line.bbox[1] - baseline) <= BOM_BASELINE_TOLERANCE_PT
            and line.bbox[0] > item_cell.bbox[2]
        ]
        if len(same_row) < 2:
            continue
        same_row.sort(key=lambda pair: pair[1].bbox[0])
        texts = [line.text.strip() for _, line in same_row]
        if not _PART_CELL_RE.match(texts[0]):
            continue

        rows.append(
            BomRow(
                item=item_cell.text.strip(),
                bbox=_union([item_cell.bbox] + [line.bbox for _, line in same_row]),
                cell_boxes=[line.bbox for _, line in same_row],
                cells=texts,
            )
        )
        consumed.add(index)
        consumed.update(j for j, _ in same_row)
    return rows, consumed


def _extract_from_row_lines(lines: list[TextSpan]) -> tuple[list[BomRow], set[int]]:
    """Shape 2: the whole row arrives as one line beginning with the item
    number, with any trailing columns as separate cells to its right."""
    rows: list[BomRow] = []
    consumed: set[int] = set()

    anchors: list[tuple[int, TextSpan, re.Match]] = []
    for i, line in enumerate(lines):
        match = _ROW_LINE_RE.match(line.text.strip())
        if match:
            anchors.append((i, line, match))
    if len(anchors) < BOM_MIN_ROWS:
        return [], set()

    by_column: dict[int, list[tuple[int, TextSpan, re.Match]]] = {}
    for index, line, match in anchors:
        key = int(round(line.bbox[0] / BOM_COLUMN_TOLERANCE_PT))
        by_column.setdefault(key, []).append((index, line, match))
    column = max(by_column.values(), key=len)
    if len(column) < BOM_MIN_ROWS:
        return [], set()

    for index, line, match in column:
        item, part, quantity, remainder = match.groups()
        trailing = [
            (j, other)
            for j, other in enumerate(lines)
            if j != index
            and abs(other.bbox[1] - line.bbox[1]) <= BOM_BASELINE_TOLERANCE_PT
            and other.bbox[0] > line.bbox[2]
        ]
        trailing.sort(key=lambda pair: pair[1].bbox[0])

        row = BomRow(
            item=item,
            bbox=_union([line.bbox] + [other.bbox for _, other in trailing]),
            cell_boxes=[line.bbox] + [other.bbox for _, other in trailing],
            cells=[part, f"{quantity} {remainder}".strip()]
            + [other.text.strip() for _, other in trailing],
        )
        rows.append(row)
        consumed.add(index)
        consumed.update(j for j, _ in trailing)
    return rows, consumed


def _attach_wrapped_lines(
    rows: list[BomRow], lines: list[TextSpan], consumed: set[int]
) -> None:
    """
    Fold continuation lines into the row above them.

    A long description wraps onto its own line with no item number of its
    own; left unattached it reads as text appearing from nowhere. The line
    must start in the description column and fall in the vertical gap
    before the next row, so unrelated text that merely shares a band of the
    sheet is not absorbed.
    """
    if not rows:
        return

    description_lefts = [row.cell_boxes[1][0] for row in rows if len(row.cell_boxes) > 1]
    if not description_lefts:
        return
    description_lefts.sort()
    column_left = description_lefts[len(description_lefts) // 2]
    lower = column_left - BOM_COLUMN_TOLERANCE_PT
    upper = column_left + BOM_DESCRIPTION_INDENT_PT

    # A multi-line description is centred on its row, so continuation lines
    # sit both above and below the baseline. Assigning each to the nearest
    # row rather than to the row above keeps the halves of a wrapped
    # description with the item they describe.
    spacing = _median_row_spacing(rows)
    for i, line in enumerate(lines):
        if i in consumed:
            continue
        if not (lower <= line.bbox[0] <= upper):
            continue
        nearest = min(rows, key=lambda r: abs(r.bbox[1] - line.bbox[1]))
        if abs(nearest.bbox[1] - line.bbox[1]) > spacing:
            continue
        nearest.cells.append(line.text.strip())
        nearest.bbox = _union([nearest.bbox, line.bbox])
        consumed.add(i)


def _median_row_spacing(rows: list[BomRow]) -> float:
    """Typical vertical pitch of the table, used as the attachment radius."""
    tops = sorted(row.bbox[1] for row in rows)
    gaps = [b - a for a, b in zip(tops, tops[1:]) if b > a]
    if not gaps:
        return 20.0
    gaps.sort()
    return max(gaps[len(gaps) // 2] * 0.75, 6.0)


def _parse_columns(row: BomRow) -> None:
    """Assign each cell of a row to a column by what it looks like."""
    cells = [c for c in row.cells if c]
    if not cells:
        return

    row.part_number = cells[0]
    remainder = cells[1:]

    description_parts: list[str] = []
    for cell in remainder:
        if _STANDARD_CELL_RE.search(cell) and row.specification is None and len(cell) < 40:
            row.specification = cell
        elif _MATERIAL_CELL_RE.search(cell) and len(cell) < 60 and row.material is None:
            row.material = cell
        else:
            description_parts.append(cell)

    if description_parts:
        first = description_parts[0]
        match = _QTY_PREFIX_RE.match(first)
        if match:
            row.quantity = match.group(1)
            description_parts[0] = match.group(2).strip()
        elif _ITEM_CELL_RE.match(first) and len(description_parts) > 1:
            row.quantity = first
            description_parts = description_parts[1:]

    row.description = " ".join(p for p in description_parts if p).strip()


def diff_bom(
    old_lines: list[TextSpan],
    new_lines: list[TextSpan],
    page_size: tuple[float, float],
) -> tuple[list[DiffRecord], set[int], set[int]]:
    """
    Compare two parts lists, matched on item number.

    Returns the differences plus the line indices consumed on each side, so
    the general text diff can skip them.
    """
    old_rows, old_used = extract_bom_rows(old_lines)
    new_rows, new_used = extract_bom_rows(new_lines)
    if not old_rows and not new_rows:
        return [], set(), set()

    old_by_item = {row.item: row for row in old_rows}
    new_by_item = {row.item: row for row in new_rows}
    records: list[DiffRecord] = []

    for item in sorted(set(old_by_item) | set(new_by_item), key=lambda s: int(s)):
        old_row = old_by_item.get(item)
        new_row = new_by_item.get(item)

        if old_row and not new_row:
            records.append(
                DiffRecord(
                    zone=zone_label_for_bbox(old_row.bbox, *page_size),
                    change_type=ChangeType.TEXT_REMOVED,
                    bbox=old_row.bbox,
                    old_value=f"BOM {old_row.summary()} — {old_row.description}",
                )
            )
            continue
        if new_row and not old_row:
            records.append(
                DiffRecord(
                    zone=zone_label_for_bbox(new_row.bbox, *page_size),
                    change_type=ChangeType.TEXT_ADDED,
                    bbox=new_row.bbox,
                    new_value=f"BOM {new_row.summary()} — {new_row.description}",
                )
            )
            continue

        old_fields = old_row.fields()
        new_fields = new_row.fields()
        for name in old_fields:
            before, after = old_fields[name], new_fields[name]
            if (before or "") == (after or ""):
                continue
            records.append(
                DiffRecord(
                    zone=zone_label_for_bbox(old_row.bbox, *page_size),
                    change_type=ChangeType.TEXT_CHANGED,
                    bbox=old_row.bbox,
                    old_value=f"BOM item {item} {name}: {before or '—'}",
                    new_value=f"{after or '—'}",
                    confidence=1.0,
                )
            )

    return records, old_used, new_used
