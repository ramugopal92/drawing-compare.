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

from .config import ZONE_GRID


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
