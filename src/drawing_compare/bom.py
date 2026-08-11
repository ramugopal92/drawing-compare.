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


# Parts-list column headers, mapped to the field they name. Recognising the
# header is what separates a description from a cut length: without it the
# extractor guesses from content, and "PIPE, NPS 2, SCH 40  46 7/16" reads as
# one description — so adding a metric length column makes every row look
# like its description changed.
_HEADER_ALIASES = {
    "item": ("ITEM", "ITEM NO", "NO", "FIND", "FIND NO", "BALLOON"),
    "part_number": ("ID", "PART NO", "PART NUMBER", "DWG NO", "IDENTIFYING NO"),
    "quantity": ("QTY", "QUANTITY", "REQD", "QTY REQD"),
    "description": ("DESCRIPTION", "NOMENCLATURE", "TITLE"),
    "specification": ("GEN. SPEC.", "GEN SPEC", "SPEC", "SPECIFICATION", "STANDARD"),
    "material": ("MATERIAL", "MATL", "MAT'L"),
}

# Column headers naming a length. These vary between revisions of the same
# drawing ("CUT LENGTH" becoming "LENGTH (IN)" plus "LENGTH (MM)"), which is
# a table-structure change, not a change to any item.
_LENGTH_HEADER_RE = re.compile(
    r"\b(?:CUT\s*LENGTH|LENGTH|LG|STOCK\s*LENGTH)\b", re.IGNORECASE
)
_UNIT_IN_HEADER_RE = re.compile(r"\((?:IN|INCH|INCHES)\)|\bIN\b", re.IGNORECASE)
_UNIT_MM_HEADER_RE = re.compile(r"\((?:MM|MILLIMET(?:ER|RE)S?)\)|\bMM\b", re.IGNORECASE)


@dataclass
class BomHeader:
    """The parts-list header row, and what each column means."""

    columns: list[tuple[str, tuple[float, float]]] = field(default_factory=list)
    raw: list[str] = field(default_factory=list)
    bbox: tuple[float, float, float, float] | None = None

    @property
    def field_names(self) -> list[str]:
        return [name for name, _ in self.columns]

    def field_at(self, x_centre: float) -> str | None:
        """Which column a cell at this horizontal position belongs to."""
        best, best_distance = None, None
        for name, (left, right) in self.columns:
            if left <= x_centre <= right:
                return name
            distance = min(abs(x_centre - left), abs(x_centre - right))
            if best_distance is None or distance < best_distance:
                best, best_distance = name, distance
        if best_distance is not None and best_distance <= BOM_COLUMN_TOLERANCE_PT * 4:
            return best
        return None


def _classify_header_cell(text: str, seen: set[str]) -> str | None:
    """Map one header cell to a field name."""
    cleaned = re.sub(r"\s+", " ", text.strip().upper())
    if not cleaned:
        return None
    for name, aliases in _HEADER_ALIASES.items():
        if cleaned in aliases and name not in seen:
            return name
    if _LENGTH_HEADER_RE.search(cleaned):
        if _UNIT_MM_HEADER_RE.search(cleaned):
            return "length_mm"
        if _UNIT_IN_HEADER_RE.search(cleaned):
            return "length_in"
        return "length_mm" if "length_in" in seen else "length_in"
    for name, aliases in _HEADER_ALIASES.items():
        if name in seen:
            continue
        if any(alias in cleaned for alias in aliases):
            return name
    return None


def _header_band(lines: list[TextSpan], rows_bbox) -> list[TextSpan]:
    """
    Cells belonging to the header row, plus any wrapped or qualifier lines.

    The header's own cells share a baseline; column titles that wrap ("CUT"
    above "LENGTH") and unit qualifiers ("(IN)", "(MM)") sit a few points
    off it. Anchoring on the shared baseline first, then pulling in the
    strays directly above and below, keeps neighbouring drawing text out of
    the header — text that would otherwise be read as column names.
    """
    if rows_bbox is None:
        return []

    nearby = [
        line
        for line in lines
        if rows_bbox[0] - 40 <= line.bbox[0] <= rows_bbox[2] + 60
        and rows_bbox[1] - 44 <= line.bbox[1] <= rows_bbox[3] + 44
        and not (rows_bbox[1] <= line.bbox[1] <= rows_bbox[3])
    ]
    if len(nearby) < 3:
        return []

    # The header baseline is the one carrying the most short, title-like
    # cells: a header cell is a column name, not a sentence.
    by_baseline: dict[int, list[TextSpan]] = {}
    for line in nearby:
        if len(line.text) > 30:
            continue
        key = int(round(line.bbox[1] / BOM_BASELINE_TOLERANCE_PT))
        by_baseline.setdefault(key, []).append(line)
    if not by_baseline:
        return []

    # Score each candidate baseline by how many of its cells actually name a
    # column. Counting cells alone picks whichever line happens to have the
    # most words — a drawing note beside the table wins every time.
    def header_score(group: list[TextSpan]) -> tuple[int, int]:
        seen: set[str] = set()
        hits = 0
        for span in sorted(group, key=lambda s: s.bbox[0]):
            name = _classify_header_cell(span.text, seen)
            if name:
                seen.add(name)
                hits += 1
        return hits, len(group)

    key, band = max(by_baseline.items(), key=lambda item: header_score(item[1]))
    if header_score(band)[0] < 3:
        return []

    baseline = sum(line.bbox[1] for line in band) / len(band)
    for line in nearby:
        if line in band or len(line.text) > 20:
            continue
        # Wrapped titles and unit qualifiers sit within a line height of the
        # header baseline; anything further away belongs to the drawing.
        if abs(line.bbox[1] - baseline) <= 14.0:
            band.append(line)
    return band


def _split_repeated(text: str) -> list[str]:
    """
    Split a header cell that actually holds two adjacent columns.

    Extractors merge neighbouring header cells when they sit close together,
    so 'LENGTH (IN) LENGTH (MM)' arrives as one string. Splitting on the
    repeated column word recovers both — without it the two length columns
    collapse into one and every length value lands in the wrong field.
    """
    words = text.split()
    if len(words) == 2 and words[0] == words[1]:
        return words

    # A word repeating later in the cell marks where the second column
    # title begins.
    for position in range(1, len(words)):
        if words[position] == words[0] and len(words[0]) > 2:
            return [" ".join(words[:position]), " ".join(words[position:])]
    return [text]


def _merge_header_cells(
    band: list[TextSpan], column_gap: float
) -> list[tuple[str, tuple[float, float]]]:
    """Group header fragments into columns by horizontal overlap."""
    fragments: list[tuple[str, float, float]] = []
    for span in band:
        pieces = _split_repeated(span.text.strip())
        if len(pieces) == 2:
            mid = (span.bbox[0] + span.bbox[2]) / 2.0
            fragments.append((pieces[0], span.bbox[0], mid))
            fragments.append((pieces[1], mid, span.bbox[2]))
        elif span.text.strip():
            fragments.append((span.text.strip(), span.bbox[0], span.bbox[2]))

    fragments.sort(key=lambda f: f[1])
    columns: list[tuple[list[str], float, float]] = []
    for text, left, right in fragments:
        for column in columns:
            # Overlapping horizontally means the fragments are stacked in
            # the same column, e.g. "CUT" above "LENGTH".
            if left < column[2] + column_gap and right > column[1] - column_gap:
                column[0].append(text)
                columns[columns.index(column)] = (
                    column[0], min(column[1], left), max(column[2], right)
                )
                break
        else:
            columns.append(([text], left, right))

    return [(" ".join(parts), (left, right)) for parts, left, right in columns]


def detect_header(lines: list[TextSpan], rows_bbox) -> BomHeader | None:
    """
    Find the parts-list header and map each column to a field.

    Reading the header gives every cell a meaning taken from the drawing
    itself. Without it the extractor has to guess from content, and a cut
    length sitting at the end of a row is indistinguishable from part of the
    description — so adding a metric length column makes every row look like
    its description changed.
    """
    band = _header_band(lines, rows_bbox)
    if len(band) < 3:
        return None

    merged = _merge_header_cells(band, BOM_COLUMN_TOLERANCE_PT)
    header = BomHeader()
    seen: set[str] = set()
    for text, (left, right) in sorted(merged, key=lambda item: item[1][0]):
        header.raw.append(text)
        name = _classify_header_cell(text, seen)
        if name is None:
            continue
        seen.add(name)
        header.columns.append(
            (name, (left - BOM_COLUMN_TOLERANCE_PT, right + BOM_COLUMN_TOLERANCE_PT))
        )

    if len(header.columns) < 3:
        return None
    header.bbox = _union([span.bbox for span in band])
    return header


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
    length_in: str | None = None
    length_mm: str | None = None
    cells: list[str] = field(default_factory=list)

    def fields(self) -> dict[str, str | None]:
        return {
            "part number": self.part_number,
            "quantity": self.quantity,
            "description": self.description or None,
            "specification": self.specification,
            "material": self.material,
            "length": self.length_in,
            "length (mm)": self.length_mm,
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


def extract_bom_rows(
    lines: list[TextSpan],
) -> tuple[list[BomRow], set[int], BomHeader | None]:
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
        return [], set(), None

    rows.sort(key=lambda r: r.bbox[1])
    _attach_wrapped_lines(rows, lines, consumed)

    header = detect_header(lines, _union([row.bbox for row in rows]))
    if header is not None:
        _fit_columns_to_rows(header, rows)
    for row in rows:
        if header is not None:
            _parse_columns_with_header(row, header)
        else:
            _parse_columns(row)
    return rows, consumed, header


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


def _fit_columns_to_rows(header: BomHeader, rows: list[BomRow]) -> None:
    """
    Snap header column bounds onto where the data actually sits.

    A header title is not always centred over its column, and merged header
    cells give only an estimated split point. The cells in the rows below
    are the ground truth for where a column begins and ends, so each header
    column is widened to the cell cluster nearest it and the boundaries
    between adjacent columns are set midway between clusters.
    """
    clusters: list[tuple[float, float]] = []
    for row in rows:
        for bbox in row.cell_boxes:
            for index, (left, right) in enumerate(clusters):
                if bbox[0] <= right + BOM_COLUMN_TOLERANCE_PT and bbox[2] >= left - BOM_COLUMN_TOLERANCE_PT:
                    clusters[index] = (min(left, bbox[0]), max(right, bbox[2]))
                    break
            else:
                clusters.append((bbox[0], bbox[2]))
    if not clusters:
        return
    clusters.sort()

    fitted: list[tuple[str, tuple[float, float]]] = []
    for name, (left, right) in header.columns:
        centre = (left + right) / 2.0
        best = min(clusters, key=lambda c: abs((c[0] + c[1]) / 2.0 - centre))
        fitted.append((name, best))
    fitted.sort(key=lambda item: item[1][0])

    # Split any boundary two columns still share.
    adjusted: list[tuple[str, tuple[float, float]]] = []
    for index, (name, (left, right)) in enumerate(fitted):
        if index and left < adjusted[-1][1][1]:
            midpoint = (adjusted[-1][1][0] + right) / 2.0
            previous_name, (previous_left, _) = adjusted[-1]
            adjusted[-1] = (previous_name, (previous_left, midpoint))
            left = midpoint
        adjusted.append((name, (left, right)))
    header.columns = adjusted


def _parse_columns_with_header(row: BomRow, header: BomHeader) -> None:
    """
    Assign each cell to the column it physically sits under.

    This is what stops a cut length being read as part of the description.
    Position against the header is authoritative; content patterns are only
    a fallback for rows whose cells could not be placed.
    """
    placed: dict[str, list[str]] = {}
    leftovers: list[str] = []

    for text, bbox in zip(row.cells, row.cell_boxes):
        if not text:
            continue
        name = header.field_at((bbox[0] + bbox[2]) / 2.0)
        if name is None or name == "item":
            leftovers.append(text)
            continue
        placed.setdefault(name, []).append(text)

    # A merged leading cell holds several columns at once — split it back
    # out using the part-number and quantity prefixes the row began with.
    if "part_number" not in placed and leftovers:
        merged = leftovers[0]
        match = re.match(r"^(\S+)\s+(\d{1,4})\s+(.*)$", merged, re.DOTALL)
        if match:
            placed.setdefault("part_number", []).append(match.group(1))
            placed.setdefault("quantity", []).append(match.group(2))
            remainder = match.group(3).strip()
            if remainder:
                placed.setdefault("description", []).append(remainder)
            leftovers = leftovers[1:]

    row.part_number = " ".join(placed.get("part_number", [])) or None
    row.quantity = " ".join(placed.get("quantity", [])) or None
    row.specification = " ".join(placed.get("specification", [])) or None
    row.material = " ".join(placed.get("material", [])) or None
    row.length_in = " ".join(placed.get("length_in", [])) or None
    row.length_mm = " ".join(placed.get("length_mm", [])) or None

    description = placed.get("description", []) + leftovers
    row.description = " ".join(part for part in description if part).strip()

    # A description that still carries a trailing length means the row's
    # cells were merged before they reached us; recover it by position.
    if row.length_in is None and row.description:
        trailing = re.search(r"\s((?:\d+\s+)?\d+(?:/\d+)?)$", row.description)
        if trailing and header.field_at(row.bbox[2] - 5) in {"length_in", "length_mm"}:
            row.length_in = trailing.group(1)
            row.description = row.description[: trailing.start()].strip()


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
    old_spans: list[TextSpan],
    new_spans: list[TextSpan],
    page_size: tuple[float, float],
    old_lines: list[TextSpan] | None = None,
    new_lines: list[TextSpan] | None = None,
) -> tuple[list[DiffRecord], set[int], set[int]]:
    """
    Compare two parts lists, matched on item number.

    A change to the table's *structure* — a column added, renamed, or
    removed — is reported once, as the single drafting decision it was.
    Without that, adding a metric length column makes every row in the table
    look like its description changed, and the one item that genuinely
    changed is lost among a dozen that did not.

    Returns the differences plus the line indices consumed on each side, so
    the general text diff can skip them.
    """
    old_rows, _, old_header = extract_bom_rows(old_spans)
    new_rows, _, new_header = extract_bom_rows(new_spans)
    if not old_rows and not new_rows:
        return [], set(), set()

    # Rows are found in span space; the text diff works in line space, so
    # the lines covered by the table are marked used by geometry rather
    # than by index.
    old_used = _lines_within(old_lines, old_rows)
    new_used = _lines_within(new_lines, new_rows)

    records: list[DiffRecord] = []
    table_bbox = _union([row.bbox for row in (old_rows or new_rows)])

    structural_fields = _structure_change(old_header, new_header)
    if structural_fields:
        added, removed = structural_fields
        parts = []
        if added:
            parts.append("column(s) added: " + ", ".join(added))
        if removed:
            parts.append("column(s) removed: " + ", ".join(removed))
        records.append(
            DiffRecord(
                zone=zone_label_for_bbox(table_bbox, *page_size),
                change_type=ChangeType.TEXT_CHANGED,
                bbox=table_bbox,
                old_value=(
                    f"parts list: {len(old_header.columns)} columns"
                    if old_header
                    else "parts list structure"
                ),
                new_value=(
                    f"{len(new_header.columns)} columns — " + "; ".join(parts)
                    if new_header
                    else "; ".join(parts)
                ),
                source="table",
                match_basis="parts-list header comparison",
                region="parts_list",
            )
        )

    old_by_item = {row.item: row for row in old_rows}
    new_by_item = {row.item: row for row in new_rows}

    # Fields present in one table only are structural, already reported
    # above, and must not be re-reported once per item.
    # A column that exists on one side only was reported once, above, as a
    # structural change. Reporting it again per item turns one drafting
    # decision into a row for every part in the list, which is exactly what
    # buries the item that genuinely changed.
    skip: set[str] = set()
    if structural_fields:
        added, removed = structural_fields
        skip = {_field_label(name) for name in added + removed}

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
                    source="table",
                    match_basis="parts-list item number",
                    region="parts_list",
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
                    source="table",
                    match_basis="parts-list item number",
                    region="parts_list",
                )
            )
            continue

        old_fields = old_row.fields()
        new_fields = new_row.fields()
        for name in old_fields:
            if name in skip:
                continue
            before, after = old_fields[name], new_fields[name]
            if _same_value(before, after):
                continue
            # A field that exists only on the new side because its column is
            # new was covered by the structural record.
            if before is None and name.lower() in skip:
                continue
            records.append(
                DiffRecord(
                    zone=zone_label_for_bbox(old_row.bbox, *page_size),
                    change_type=ChangeType.TEXT_CHANGED,
                    bbox=old_row.bbox,
                    old_value=f"BOM item {item} {name}: {before or '—'}",
                    new_value=f"{after or '—'}",
                    confidence=1.0,
                    source="table",
                    match_basis="parts-list item number",
                    region="parts_list",
                )
            )

    return records, old_used, new_used


_FIELD_LABELS = {
    "part_number": "part number",
    "quantity": "quantity",
    "description": "description",
    "specification": "specification",
    "material": "material",
    "length_in": "length",
    "length_mm": "length (mm)",
}


def _field_label(name: str) -> str:
    """Internal column name to the wording used in a report row."""
    return _FIELD_LABELS.get(name, name.replace("_", " "))


def _lines_within(lines: list[TextSpan] | None, rows: list[BomRow]) -> set[int]:
    """Indices of grouped lines that fall inside the parts-list table.

    The table is located in span space, so the general text diff is told
    which of its own lines to skip by intersecting with the row boxes —
    otherwise every parts-list row is reported twice, once as a table field
    and once as loose text."""
    if not lines or not rows:
        return set()
    table = _union([row.bbox for row in rows])
    used: set[int] = set()
    for index, line in enumerate(lines):
        cx = (line.bbox[0] + line.bbox[2]) / 2.0
        cy = (line.bbox[1] + line.bbox[3]) / 2.0
        if table[0] <= cx <= table[2] and table[1] <= cy <= table[3]:
            used.add(index)
    return used


def _same_value(before: str | None, after: str | None) -> bool:
    """Compare two cell values, ignoring whitespace and case."""
    left = re.sub(r"\s+", " ", (before or "")).strip().upper()
    right = re.sub(r"\s+", " ", (after or "")).strip().upper()
    return left == right


def _structure_change(
    old_header: BomHeader | None, new_header: BomHeader | None
) -> tuple[list[str], list[str]] | None:
    """Which columns were added or removed between the two tables."""
    if old_header is None or new_header is None:
        return None
    old_fields = set(old_header.field_names)
    new_fields = set(new_header.field_names)
    added = sorted(new_fields - old_fields)
    removed = sorted(old_fields - new_fields)
    if not added and not removed:
        return None
    return added, removed
