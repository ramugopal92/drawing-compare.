import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from drawing_compare.config import ZONE_GRID
from drawing_compare.zones import zone_label_for_point, zone_label_for_bbox


def test_top_left_is_the_highest_column_when_right_to_left():
    # Columns are numbered right-to-left, so the leftmost column carries the
    # highest number. Asserted against the configured grid rather than a
    # hard-coded 8, because the default grid is a fallback: real sheets have
    # their grid read off the border by zones.detect_zone_grid().
    label = zone_label_for_point(x=1, y=1, page_width=800, page_height=400)
    assert label == f"A{ZONE_GRID.columns}"


def test_bottom_right_is_d1_when_right_to_left():
    label = zone_label_for_point(x=799, y=399, page_width=800, page_height=400)
    assert label == "D1"


def test_center_point_matches_bbox_center():
    bbox = (380.0, 180.0, 420.0, 220.0)  # center at (400, 200)
    label_bbox = zone_label_for_bbox(bbox, page_width=800, page_height=400)
    label_point = zone_label_for_point(400, 200, page_width=800, page_height=400)
    assert label_bbox == label_point
