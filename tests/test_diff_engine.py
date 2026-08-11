import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from drawing_compare.alignment import AlignmentResult
from drawing_compare.diff_engine import ChangeType, diff_pages
from drawing_compare.pdf_io import PageData, TextSpan, VectorPrimitive

IDENTITY_ALIGNMENT = AlignmentResult(homography=np.eye(3), good_matches=999, reliable=True)


def make_page(text_spans=None, primitives=None):
    return PageData(
        page_number=0,
        page_size_pt=(800.0, 400.0),
        text_spans=text_spans or [],
        vector_primitives=primitives or [],
        raster_image=np.zeros((400, 800, 3), dtype=np.uint8),  # not used at dpi=72 scale here
        render_dpi=72,  # keep pixel space == point space for simple test math
    )


def test_added_text_is_detected():
    old_page = make_page(text_spans=[])
    new_page = make_page(text_spans=[TextSpan(text="25.4", bbox=(10, 10, 30, 20), font_size=8)])

    records = diff_pages(old_page, new_page, IDENTITY_ALIGNMENT)
    added = [r for r in records if r.change_type == ChangeType.TEXT_ADDED]
    assert len(added) == 1
    assert added[0].new_value == "25.4"


def test_removed_text_is_detected():
    old_page = make_page(text_spans=[TextSpan(text="25.4", bbox=(10, 10, 30, 20), font_size=8)])
    new_page = make_page(text_spans=[])

    records = diff_pages(old_page, new_page, IDENTITY_ALIGNMENT)
    removed = [r for r in records if r.change_type == ChangeType.TEXT_REMOVED]
    assert len(removed) == 1
    assert removed[0].old_value == "25.4"


def test_dimension_value_change_is_detected():
    old_page = make_page(text_spans=[TextSpan(text="25.4", bbox=(10, 10, 30, 20), font_size=8)])
    new_page = make_page(text_spans=[TextSpan(text="24.8", bbox=(10, 10, 30, 20), font_size=8)])

    records = diff_pages(old_page, new_page, IDENTITY_ALIGNMENT)
    changed = [r for r in records if r.change_type == ChangeType.TEXT_CHANGED]
    assert len(changed) == 1
    assert changed[0].old_value == "25.4"
    assert changed[0].new_value == "24.8"


def test_unchanged_text_produces_no_record():
    old_page = make_page(text_spans=[TextSpan(text="M6x1.0", bbox=(10, 10, 40, 20), font_size=8)])
    new_page = make_page(text_spans=[TextSpan(text="M6x1.0", bbox=(10, 10, 40, 20), font_size=8)])

    records = diff_pages(old_page, new_page, IDENTITY_ALIGNMENT)
    assert records == []


def test_geometry_added_and_removed():
    old_page = make_page(primitives=[VectorPrimitive(kind="line", bbox=(0, 0, 100, 0))])
    new_page = make_page(primitives=[VectorPrimitive(kind="line", bbox=(0, 200, 100, 200))])

    records = diff_pages(old_page, new_page, IDENTITY_ALIGNMENT)
    types = {r.change_type for r in records}
    assert ChangeType.GEOMETRY_REMOVED in types
    assert ChangeType.GEOMETRY_ADDED in types
