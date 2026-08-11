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
    # Geometry rows are suppressed in the full pipeline by default (a PDF
    # cannot say what a shape changed from or to), so this exercises the
    # geometry pass directly.
    # Several primitives per side, not one: an isolated primitive is treated
    # as noise (a hatch fragment, a rounding artifact) and deliberately not
    # reported. Real edits move a cluster of geometry together.
    old_page = make_page(primitives=[
        VectorPrimitive(kind="line", bbox=(0, 0, 100, 0)),
        VectorPrimitive(kind="line", bbox=(0, 2, 100, 2)),
        VectorPrimitive(kind="line", bbox=(0, 4, 100, 4)),
    ])
    new_page = make_page(primitives=[
        VectorPrimitive(kind="line", bbox=(0, 200, 100, 200)),
        VectorPrimitive(kind="line", bbox=(0, 202, 100, 202)),
        VectorPrimitive(kind="line", bbox=(0, 204, 100, 204)),
    ])

    from drawing_compare.diff_engine import diff_geometry

    records = diff_geometry(old_page, new_page, IDENTITY_ALIGNMENT)
    types = {r.change_type for r in records}
    assert ChangeType.GEOMETRY_REMOVED in types
    assert ChangeType.GEOMETRY_ADDED in types


# --- text reconstruction --------------------------------------------------


def test_glyph_split_text_is_rejoined_without_spaces():
    """Some exports emit rotated or kerned text one glyph at a time. Joining
    those with spaces yields '5 2 4 . 5 4', which then fails to match its
    unchanged counterpart and is reported as a change that never happened."""
    from drawing_compare.diff_engine import _group_spans_into_lines
    from drawing_compare.pdf_io import TextSpan

    glyphs = [
        TextSpan(text=ch, bbox=(100.0 + i * 4.0, 50.0, 104.0 + i * 4.0, 58.0), font_size=8.0)
        for i, ch in enumerate("524.54")
    ]
    lines = _group_spans_into_lines(glyphs)
    assert len(lines) == 1
    assert lines[0].text == "524.54"


def test_genuine_word_spacing_is_preserved():
    from drawing_compare.diff_engine import _group_spans_into_lines
    from drawing_compare.pdf_io import TextSpan

    spans = [
        TextSpan(text="PIPE,", bbox=(100.0, 50.0, 120.0, 58.0), font_size=8.0),
        TextSpan(text="NPS", bbox=(123.0, 50.0, 138.0, 58.0), font_size=8.0),
        TextSpan(text="2", bbox=(141.0, 50.0, 146.0, 58.0), font_size=8.0),
    ]
    assert _group_spans_into_lines(spans)[0].text == "PIPE, NPS 2"
