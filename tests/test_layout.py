"""Tests for sheet layout: regions, views, and title-block fields."""

from __future__ import annotations

from drawing_compare.layout import (
    DRAWING_BODY,
    TITLE_BLOCK,
    analyse_sheet,
    detect_views,
    diff_view_inventory,
    extract_title_block_fields,
)
from drawing_compare.pdf_io import TextSpan

PAGE = (1224.0, 792.0)


def span(text: str, x: float, y: float, width: float | None = None) -> TextSpan:
    width = width if width is not None else max(len(text) * 5.5, 8.0)
    return TextSpan(text=text, bbox=(x, y, x + width, y + 10.0), font_size=9.0)


def title_block_lines() -> list[TextSpan]:
    return [
        span("DRAWING NO", 1000.0, 700.0),
        span("442079-FAB", 1000.0, 714.0),
        span("TITLE", 800.0, 700.0),
        span("WLD, GDR, BRG, 4 STEP", 800.0, 714.0),
        span("REVISION", 1140.0, 700.0),
        span("B", 1142.0, 714.0, 8.0),
        span("SCALE", 900.0, 740.0),
        span("1:24", 900.0, 754.0),
        span("DRAWN BY", 700.0, 700.0),
        span("CHECKED BY", 700.0, 720.0),
        span("APPROVED BY", 700.0, 740.0),
        span("SHEET 1 OF 3", 900.0, 770.0),
        span("DWG CATEGORY", 1050.0, 740.0),
        span("TEL: 1-604-273-1068 WEB: www.example.com", 1000.0, 770.0),
    ]


# ------------------------------------------------------------ views


def test_detects_detail_and_section_labels():
    lines = [
        span("DETAIL E", 300.0, 400.0),
        span("SECTION A-A", 100.0, 500.0),
        span("SECTION D-D", 700.0, 300.0),
        span("ISOMETRIC FRONT RIGHT VIEW", 400.0, 100.0),
    ]
    labels = {view.normalised() for view in detect_views(lines)}
    assert labels == {"DETAIL E", "SECTION A-A", "SECTION D-D", "ISOMETRIC FRONT RIGHT VIEW"}


def test_scale_note_attaches_to_its_view():
    views = detect_views([span("DETAIL F", 300.0, 400.0), span("SCALE 1 : 8", 300.0, 412.0)])
    assert views[0].scale == "SCALE 1 : 8"


def test_change_is_attributed_to_the_nearest_view():
    layout = analyse_sheet(
        [span("DETAIL E", 300.0, 400.0), span("SECTION A-A", 900.0, 400.0)], PAGE
    )
    assert layout.view_for((310.0, 380.0, 340.0, 392.0)) == "DETAIL E"
    assert layout.view_for((910.0, 380.0, 940.0, 392.0)) == "SECTION A-A"


def test_view_inventory_difference():
    old = analyse_sheet([span("DETAIL G", 100.0, 100.0), span("DETAIL H", 400.0, 100.0)], PAGE)
    new = analyse_sheet([span("DETAIL H", 400.0, 100.0), span("SECTION J-J", 700.0, 100.0)], PAGE)
    added, removed = diff_view_inventory(old, new)
    assert added == ["SECTION J-J"]
    assert removed == ["DETAIL G"]


# ------------------------------------------------------------ regions


def test_title_block_found_by_its_own_labels():
    layout = analyse_sheet(title_block_lines(), PAGE)
    assert any(region.name == TITLE_BLOCK for region in layout.regions)
    assert layout.region_for((1000.0, 700.0, 1080.0, 712.0)) == TITLE_BLOCK


def test_drawing_body_is_the_default_region():
    layout = analyse_sheet(title_block_lines(), PAGE)
    assert layout.region_for((200.0, 200.0, 260.0, 212.0)) == DRAWING_BODY


# ------------------------------------------------- title block fields


def test_drawing_number_is_read_from_its_label():
    """Regression: the longest digit-bearing token in the title block is the
    company phone number, so pattern-matching the region reports
    '1-604-273-1068' as the drawing number."""
    fields = extract_title_block_fields(title_block_lines())
    assert fields.drawing_number == "442079-FAB"
    assert fields.revision == "B"
    assert fields.title.startswith("WLD, GDR")


def test_phone_number_is_never_a_drawing_number():
    lines = [span("DRAWING NO", 1000.0, 700.0), span("1-604-273-1068", 1000.0, 714.0)]
    assert extract_title_block_fields(lines).drawing_number is None


def test_trailing_revision_letter_is_split_off():
    lines = [span("DRAWING NO", 1000.0, 700.0), span("442079-FAB A", 1000.0, 714.0)]
    fields = extract_title_block_fields(lines)
    assert fields.drawing_number == "442079-FAB"
    assert fields.revision == "A"


def test_doubled_render_is_collapsed():
    lines = [span("TITLE", 800.0, 700.0), span("WWLLDD,, GGDDRR", 800.0, 714.0)]
    assert extract_title_block_fields(lines).title == "WLD, GDR"


# ------------------------------------------------- revision block


def revision_row(letter: str, description: str, y: float) -> list[TextSpan]:
    """A revision row as drawn: the letter far left, the description in its
    own column well to the right of it."""
    return [
        span(letter, 49.0, y, 10.0),
        span(description, 194.0, y),
        span("2018-04-20", 524.0, y),
    ]


def test_revision_letter_and_description_read_from_the_block():
    from drawing_compare.layout import extract_revision_info

    cells = revision_row("A", "INITIAL RELEASE", 726.0)
    info = extract_revision_info(cells)
    assert info.revision == "A"
    assert info.description == "INITIAL RELEASE"


def test_shadow_rendered_revision_block_is_repaired():
    """Some templates draw title-block text twice, offset slightly, so every
    character arrives doubled — including the revision letter, which is too
    short for the general un-doubling rules to be confident about."""
    from drawing_compare.layout import extract_revision_info

    cells = revision_row("AA", "IINNIITTIIAALL RREELLEEAASSEE", 726.0)
    info = extract_revision_info(cells)
    assert info.revision == "A"
    assert info.description == "INITIAL RELEASE"


def test_description_preferred_over_dates_and_initials():
    """A revision block holds a date, an EC number and approver initials
    alongside the description. Only the description is prose."""
    from drawing_compare.layout import extract_revision_info

    cells = revision_row("B", "BAR WAS 5/8, ADDED DUAL DIMENSIONS", 726.0)
    cells.append(span("451994", 431.0, 726.0))
    cells.append(span("BE", 492.0, 726.0))
    info = extract_revision_info(cells)
    assert "BAR WAS" in info.description


def test_title_block_revision_wins_when_available():
    from drawing_compare.layout import extract_revision_info

    cells = revision_row("A", "INITIAL RELEASE", 726.0)
    assert extract_revision_info(cells, None, "C").revision == "C"
