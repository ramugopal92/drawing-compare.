"""Tests for parts-list extraction and item-anchored comparison."""

from __future__ import annotations

from drawing_compare.bom import diff_bom, extract_bom_rows
from drawing_compare.pdf_io import TextSpan

PAGE = (1224.0, 792.0)
ROW_PITCH = 18.0


def cell(text: str, x: float, y: float, width: float | None = None) -> TextSpan:
    width = width if width is not None else max(len(text) * 5.0, 8.0)
    return TextSpan(text=text, bbox=(x, y, x + width, y + 10.0), font_size=9.0)


def bom_row(item: str, part: str, qty_desc: str, spec: str, material: str, y: float):
    """One parts-list row laid out as real drawings lay them out: separate
    cells sharing a baseline."""
    return [
        cell(item, 623.0, y, 14.0),
        cell(part, 657.0, y, 46.0),
        cell(qty_desc, 708.0, y),
        cell(spec, 1010.0, y, 80.0),
        cell(material, 1100.0, y, 90.0),
    ]


def standard_table(part_10: str = "266635", desc_10: str = "4 FLAT WASHER, TYPE A, SERIES N, 5/8",
                   spec: str = "ASME B18.22.1") -> list[TextSpan]:
    lines: list[TextSpan] = []
    lines += bom_row("10", part_10, desc_10, spec, "SST / UNS S30400", 100.0)
    lines += bom_row("9", "266636", "16 FLAT WASHER, TYPE A, SERIES N, 3/4", spec,
                     "SST / UNS S30400", 100.0 + ROW_PITCH)
    lines += bom_row("8", "266629", "184 FLAT WASHER, TYPE A, SERIES N, 1/4", spec,
                     "SST / UNS S30400", 100.0 + 2 * ROW_PITCH)
    lines += bom_row("7", "372453", "40 CAP SCREW, HEX SOCKET", "ASME B18.3",
                     "SST / ASTM F879", 100.0 + 3 * ROW_PITCH)
    return lines


# ------------------------------------------------------------ extraction


def test_extracts_every_row_of_the_table():
    rows, consumed, _ = extract_bom_rows(standard_table())
    assert [r.item for r in rows] == ["10", "9", "8", "7"]
    assert consumed


def test_parses_columns_into_fields():
    rows, _, _ = extract_bom_rows(standard_table())
    row = next(r for r in rows if r.item == "10")
    assert row.part_number == "266635"
    assert row.quantity == "4"
    assert "FLAT WASHER" in row.description
    assert row.specification == "ASME B18.22.1"
    assert row.material == "SST / UNS S30400"


def test_ignores_stray_integers_outside_the_item_column():
    """Title blocks and tolerance tables are full of bare numbers. They sit
    in a different column, so they must not be read as parts-list rows."""
    lines = standard_table()
    lines += [cell("13", 120.0, 400.0, 12.0), cell("SCALE 1:35", 150.0, 400.0)]
    lines += [cell("67", 120.0, 420.0, 12.0), cell("SHEET 1 OF 2", 150.0, 420.0)]
    rows, _, _ = extract_bom_rows(lines)
    assert [r.item for r in rows] == ["10", "9", "8", "7"]


def test_rejects_a_table_that_is_too_small_to_be_one():
    lines = bom_row("1", "123456", "2 BOLT", "ASME B18.2.1", "SST", 100.0)
    rows, consumed, _ = extract_bom_rows(lines)
    assert rows == [] and consumed == set()


def test_row_needs_a_part_number_beside_the_item_number():
    lines: list[TextSpan] = []
    for i, y in enumerate((100.0, 118.0, 136.0)):
        lines += [cell(str(i + 1), 623.0, y, 14.0), cell("SOME PROSE HERE", 657.0, y),
                  cell("MORE PROSE", 900.0, y)]
    rows, _, _ = extract_bom_rows(lines)
    assert rows == []


def test_wrapped_description_attaches_to_its_own_row():
    """A long description wraps and straddles its row's baseline, so the
    continuation must go to the nearest row, not the row above."""
    lines = standard_table()
    lines.append(cell("(0.656 ID X 1.25 OD X 0.1 THK)", 729.0, 100.0 - 4.0))
    lines.append(cell("EXTRA FOR ROW 9", 729.0, 100.0 + ROW_PITCH + 4.0))
    rows, _, _ = extract_bom_rows(lines)
    row_10 = next(r for r in rows if r.item == "10")
    row_9 = next(r for r in rows if r.item == "9")
    assert "0.656 ID" in row_10.description
    assert "EXTRA FOR ROW 9" in row_9.description
    assert "EXTRA FOR ROW 9" not in row_10.description


# ------------------------------------------------------------ comparison


def test_part_substitution_is_anchored_on_the_item_number():
    old = standard_table()
    new = standard_table(part_10="266673")
    records, used_old, used_new = diff_bom(old, new, PAGE)
    changed = [r for r in records if "part number" in (r.old_value or "")]
    assert len(changed) == 1
    assert "266635" in changed[0].old_value
    assert changed[0].new_value == "266673"
    assert used_old and used_new


def test_similar_neighbouring_rows_are_not_cross_matched():
    """Items 8, 9 and 10 read almost identically. Only item 10 changed, so
    exactly one row may be reported."""
    records, _, _ = diff_bom(standard_table(), standard_table(part_10="266673"), PAGE)
    items = {r.old_value.split()[2] for r in records if r.old_value}
    assert items == {"10"}


def test_specification_change_reported_per_item():
    records, _, _ = diff_bom(
        standard_table(), standard_table(spec="ASME B18.21.1"), PAGE
    )
    specs = [r for r in records if "specification" in (r.old_value or "")]
    assert len(specs) == 3  # items 10, 9 and 8 share the spec; item 7 differs
    assert all(r.new_value == "ASME B18.21.1" for r in specs)


def test_identical_tables_produce_no_differences():
    records, _, _ = diff_bom(standard_table(), standard_table(), PAGE)
    assert records == []


def test_added_and_removed_rows_are_reported():
    old = standard_table()
    new = standard_table() + bom_row(
        "11", "265135", "112 LOCK NUT", "ASME B18.16.6", "SST", 100.0 + 4 * ROW_PITCH
    )
    records, _, _ = diff_bom(old, new, PAGE)
    assert any("item 11" in (r.new_value or "") for r in records)

    records, _, _ = diff_bom(new, old, PAGE)
    assert any("item 11" in (r.old_value or "") for r in records)


def test_no_table_present_is_handled():
    records, used_old, used_new = diff_bom([], [], PAGE)
    assert records == [] and used_old == set() and used_new == set()


# ------------------------------------------- merged-row extraction shape


def merged_row(item, part, qty, desc, spec, material, y):
    """The other shape: PDF extractors that merge a table row into one line
    return item, part, qty and description together, with trailing columns
    still separate."""
    return [
        cell(f"{item} {part} {qty} {desc}", 623.0, y, 260.0),
        cell(spec, 1010.0, y, 80.0),
        cell(material, 1100.0, y, 90.0),
    ]


def merged_table(part_1: str = "321795", desc_1: str = "BAR, ROUND, 5/8") -> list[TextSpan]:
    lines: list[TextSpan] = []
    lines += merged_row("3", part_1, "2", desc_1, "ASTM A29", "STL / ASTM A108", 100.0)
    lines += merged_row("2", part_1, "18", desc_1, "ASTM A29", "STL / ASTM A108",
                        100.0 + ROW_PITCH)
    lines += merged_row("1", part_1, "12", desc_1, "ASTM A29", "STL / ASTM A108",
                        100.0 + 2 * ROW_PITCH)
    lines += merged_row("4", "305726", "8", "D-RING, 1 1/2", "-", "-",
                        100.0 + 3 * ROW_PITCH)
    return lines


def test_merged_row_layout_is_extracted():
    """Regression: bom.py was built against an extractor that returns each
    cell separately. Another returns a whole row as one line, and the parts
    list was silently not found — its rows fell through to the text diff and
    were mistaken for dimension changes."""
    rows, consumed, _ = extract_bom_rows(merged_table())
    assert [r.item for r in rows] == ["3", "2", "1", "4"]
    assert consumed


def test_merged_row_columns_are_parsed():
    rows, _, _ = extract_bom_rows(merged_table())
    row = next(r for r in rows if r.item == "3")
    assert row.part_number == "321795"
    assert row.quantity == "2"
    assert "BAR, ROUND, 5/8" in row.description
    assert row.specification == "ASTM A29"


def test_merged_row_part_substitution_is_detected():
    records, _, _ = diff_bom(
        merged_table(), merged_table(part_1="370170", desc_1="BAR, ROUND, 3/4"), PAGE
    )
    parts = [r for r in records if "part number" in (r.old_value or "")]
    assert len(parts) == 3
    assert all(r.new_value == "370170" for r in parts)
