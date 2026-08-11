"""
Zone mapping: given a page size and a bounding box on that page, return the
drawing zone label (e.g. "B5") the way a title-block reference grid would.

This mirrors the "Zone" column in the tool you saw. Standard drawing
borders print row letters (A, B, C, D top-to-bottom) and column numbers
(1..8, often numbered right-to-left) along the sheet edges specifically so
engineers can call out "see zone C4" in notes — we're just automating that
lookup.

Adjust ZONE_GRID in config.py if your company's title block differs.
"""

from __future__ import annotations

from .config import ZONE_BORDER_MARGIN, ZONE_GRID, ZoneGridConfig


def bbox_center(bbox: tuple[float, float, float, float]) -> tuple[float, float]:
    x0, y0, x1, y1 = bbox
    return ((x0 + x1) / 2.0, (y0 + y1) / 2.0)


def zone_label_for_point(
    x: float, y: float, page_width: float, page_height: float
) -> str:
    """
    x, y, page_width, page_height must all be in the same units (PDF points
    or pixels — just be consistent).
    """
    columns = ZONE_GRID.columns
    rows = ZONE_GRID.rows

    col_width = page_width / columns
    row_height = page_height / len(rows)

    col_index = min(int(x // col_width), columns - 1)
    row_index = min(int(y // row_height), len(rows) - 1)

    if ZONE_GRID.columns_right_to_left:
        column_number = columns - col_index
    else:
        column_number = col_index + 1

    row_letter = rows[row_index]
    return f"{row_letter}{column_number}"


def zone_label_for_bbox(
    bbox: tuple[float, float, float, float], page_width: float, page_height: float
) -> str:
    cx, cy = bbox_center(bbox)
    return zone_label_for_point(cx, cy, page_width, page_height)


def detect_zone_grid(text_spans, page_width: float, page_height: float):
    """
    Read the drawing border's own reference characters off the sheet edges.

    Standard borders print column numbers along the top and bottom edges and
    row letters along the left and right edges, precisely so changes can be
    called out by zone. Rather than assuming a grid size, we count the
    labels actually printed on this sheet.

    A border label always appears twice — once on each opposing edge, at the
    same position along that edge. Requiring that mirror is what separates
    real border references from title-block text that happens to sit near
    the sheet margin; without it a company address in the bottom-right
    corner contributes stray letters and the detected grid is nonsense.

    Returns a ZoneGridConfig, or None if the border can't be read, in which
    case the caller falls back to the configured default.
    """
    margin_x = page_width * ZONE_BORDER_MARGIN
    margin_y = page_height * ZONE_BORDER_MARGIN
    tolerance = max(page_height, page_width) * 0.01

    left: list[tuple[str, float]] = []
    right: list[tuple[str, float]] = []
    top: list[tuple[str, float]] = []
    bottom: list[tuple[str, float]] = []

    for span in text_spans:
        text = span.text.strip()
        if len(text) != 1:
            continue
        x0, y0, x1, y1 = span.bbox
        cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0

        if text.isalpha():
            if cx <= margin_x:
                left.append((text.upper(), cy))
            elif cx >= page_width - margin_x:
                right.append((text.upper(), cy))
        elif text.isdigit():
            if cy <= margin_y:
                top.append((text, cx))
            elif cy >= page_height - margin_y:
                bottom.append((text, cx))

    def mirrored(side_a, side_b) -> set[str]:
        found = set()
        for char, pos in side_a:
            for other_char, other_pos in side_b:
                if char == other_char and abs(pos - other_pos) <= tolerance:
                    found.add(char)
                    break
        return found

    rows = mirrored(left, right)
    columns = mirrored(top, bottom)

    if len(columns) < 2 or len(rows) < 2:
        return None

    numbers = sorted(int(c) for c in columns)
    if numbers != list(range(1, len(numbers) + 1)):
        return None

    letters = "".join(sorted(rows))
    if letters != "".join(chr(ord("A") + i) for i in range(len(letters))):
        return None

    return ZoneGridConfig(
        columns=len(numbers), rows=letters, columns_right_to_left=True
    )


def zone_label_for_bbox_with_grid(bbox, page_width, page_height, grid):
    """zone_label_for_bbox against an explicit grid rather than the global."""
    cx, cy = bbox_center(bbox)
    col_width = page_width / grid.columns
    row_height = page_height / len(grid.rows)
    col_index = min(int(cx // col_width), grid.columns - 1)
    row_index = min(int(cy // row_height), len(grid.rows) - 1)
    column_number = (
        grid.columns - col_index if grid.columns_right_to_left else col_index + 1
    )
    return f"{grid.rows[row_index]}{column_number}"
