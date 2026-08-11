"""Tests for multi-page sheet matching."""

from __future__ import annotations

import pytest

from drawing_compare.page_matcher import (
    CONTENT_MATCH_THRESHOLD,
    extract_identity,
    match_pages,
)
from drawing_compare.pdf_io import PageSummary, tokenize


def make_page(index: int, body: str, title_block: str = "") -> PageSummary:
    full = f"{body} {title_block}".strip()
    return PageSummary(
        page_number=index,
        page_size_pt=(841.0, 594.0),
        text=full,
        title_block_text=title_block,
        text_tokens=tokenize(full),
        body_tokens=tokenize(body),
        title_block_tokens=tokenize(title_block),
        vector_primitive_count=500,
    )


def titled_page(index: int, sheet: int, total: int, drawing: str, body: str) -> PageSummary:
    return make_page(index, body, f"DWG NO {drawing} SHEET {sheet} OF {total} REV B")


# --------------------------------------------------------------- identity


def test_extract_identity_reads_sheet_and_drawing_number():
    page = titled_page(0, 2, 5, "12345-678", "assembly bracket weld")
    ident = extract_identity(page)
    assert ident.sheet_number == 2
    assert ident.sheet_total == 5
    assert ident.drawing_number == "12345-678"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("SHEET 3 OF 7", 3),
        ("SHEET 3/7", 3),
        ("SH. 3 OF 7", 3),
        ("SHT 3 OF 7", 3),
        ("sheet 3 of 7", 3),
    ],
)
def test_sheet_number_formats(text, expected):
    assert extract_identity(make_page(0, "", text)).sheet_number == expected


def test_identity_is_none_when_title_block_has_nothing():
    ident = extract_identity(make_page(0, "just some geometry notes"))
    assert ident.key() is None


def test_single_character_sheet_numbers_survive_tokenization():
    """Regression: tokenize() drops 1-char tokens, so identity must read raw
    text — otherwise every sheet in a set gets the same identity."""
    pages = [titled_page(i, i + 1, 3, "12345-678", f"body {i}") for i in range(3)]
    keys = {extract_identity(p).key() for p in pages}
    assert len(keys) == 3


# ---------------------------------------------------------------- matching


def test_sheet_label_matches_reordered_sheets():
    old = [titled_page(i, i + 1, 3, "A-100", f"body text {i} unique") for i in range(3)]
    new = [titled_page(0, 3, 3, "A-100", "body text 2 unique"),
           titled_page(1, 1, 3, "A-100", "body text 0 unique"),
           titled_page(2, 2, 3, "A-100", "body text 1 unique")]

    plan = match_pages(old, new, mode="sheet_label")
    mapping = {p.old_index: p.new_index for p in plan.matched}
    assert mapping == {0: 1, 1: 2, 2: 0}
    assert all(p.method == "sheet_label" for p in plan.matched)


def test_added_and_removed_sheets_are_not_force_matched():
    old = [titled_page(0, 1, 2, "A-100", "assembly bracket weld flange"),
           titled_page(1, 2, 2, "A-100", "bill of materials fasteners washer")]
    new = [titled_page(0, 1, 2, "A-100", "assembly bracket weld flange"),
           titled_page(1, 9, 2, "A-100", "inspection datum surface finish")]

    plan = match_pages(old, new, mode="auto")
    assert len(plan.matched) == 1
    assert len(plan.added) == 1 and plan.added[0].new_index == 1
    assert len(plan.removed) == 1 and plan.removed[0].old_index == 1


def test_content_mode_pairs_by_body_text():
    old = [make_page(0, "assembly bracket weld flange gusset plate"),
           make_page(1, "bill of materials fasteners washer nut bolt")]
    new = [make_page(0, "bill of materials fasteners washer nut screw"),
           make_page(1, "assembly bracket weld flange gusset rib")]

    plan = match_pages(old, new, mode="content")
    assert {p.old_index: p.new_index for p in plan.matched} == {0: 1, 1: 0}
    assert all(p.score >= CONTENT_MATCH_THRESHOLD for p in plan.matched)


def test_content_mode_ignores_shared_title_block():
    """Title blocks are near-identical across a set; including them would
    make unrelated sheets look similar enough to pair."""
    tb = "DWG NO A-100 REV B SCALE 1:1 DRAWN BY RG APPROVED MATERIAL MILD STEEL"
    old = [make_page(0, "totally distinct alpha content here", tb)]
    new = [make_page(0, "completely different beta wording instead", tb)]

    plan = match_pages(old, new, mode="content")
    assert plan.matched == []


def test_sequential_mode_pairs_by_index_regardless_of_labels():
    old = [titled_page(0, 1, 2, "A-100", "alpha"), titled_page(1, 2, 2, "A-100", "beta")]
    new = [titled_page(0, 7, 2, "B-200", "gamma"), titled_page(1, 8, 2, "B-200", "delta")]

    plan = match_pages(old, new, mode="sequential")
    assert {p.old_index: p.new_index for p in plan.matched} == {0: 0, 1: 1}


def test_uneven_page_counts_report_the_extras():
    old = [make_page(i, f"unrelated content {i}") for i in range(3)]
    new = [make_page(0, "unrelated content 0")]

    plan = match_pages(old, new, mode="auto")
    assert len(plan.matched) == 1
    assert len(plan.removed) == 2
    assert len(plan.added) == 0


def test_empty_document_yields_all_removed():
    old = [make_page(0, "some content"), make_page(1, "other content")]
    plan = match_pages(old, [], mode="auto")
    assert len(plan.removed) == 2 and not plan.matched


def test_single_page_documents_still_match():
    plan = match_pages([make_page(0, "one sheet drawing")], [make_page(0, "one sheet drawing")])
    assert len(plan.matched) == 1


def test_unknown_mode_raises():
    with pytest.raises(ValueError):
        match_pages([], [], mode="nonsense")


def test_pairs_are_ordered_by_new_document_position():
    old = [titled_page(i, i + 1, 3, "A-100", f"body {i}") for i in range(3)]
    new = [titled_page(0, 3, 3, "A-100", "body 2"),
           titled_page(1, 1, 3, "A-100", "body 0"),
           titled_page(2, 2, 3, "A-100", "body 1")]

    plan = match_pages(old, new, mode="auto")
    new_order = [p.new_index for p in plan.pairs if p.new_index is not None]
    assert new_order == sorted(new_order)


def test_fallback_drawing_number_excludes_phone_numbers():
    """Regression: the phone-number exclusion here is a safety net for when
    the primary label-anchored reader (layout.py) finds nothing. It must
    never let a ten-digit phone number outscore a real drawing number just
    because it is longer."""
    page = PageSummary(
        0, (1224.0, 792.0), text="", title_block_text="",
        text_tokens=[], title_block_tokens=["442079-FAB", "1-604-273-1068"],
    )
    assert extract_identity(page).drawing_number == "442079-FAB"


def test_fallback_does_not_reject_hyphenated_drawing_numbers():
    """The exclusion keys on digit COUNT (phone-number length), not on the
    mere presence of hyphens — otherwise ordinary drawing numbers like
    '12345-678' are excluded too."""
    page = PageSummary(
        0, (1224.0, 792.0), text="", title_block_text="",
        text_tokens=[], title_block_tokens=["DWG", "NO", "12345-678"],
    )
    assert extract_identity(page).drawing_number == "12345-678"
