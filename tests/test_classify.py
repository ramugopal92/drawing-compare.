"""Tests for classifying raw differences into engineering categories."""

from __future__ import annotations

import pytest

from drawing_compare.classify import (
    ChangeCategory,
    Severity,
    classify_record,
    classify_records,
    summarize_by_severity,
)
from drawing_compare.diff_engine import ChangeType, DiffRecord

BBOX = (100.0, 100.0, 200.0, 112.0)


def text_change(old: str | None, new: str | None, zone: str = "C2") -> DiffRecord:
    if old and new:
        kind = ChangeType.TEXT_CHANGED
    elif new:
        kind = ChangeType.TEXT_ADDED
    else:
        kind = ChangeType.TEXT_REMOVED
    return DiffRecord(zone=zone, change_type=kind, bbox=BBOX, old_value=old, new_value=new)


def category_of(old, new, zone="C2") -> ChangeCategory:
    return classify_record(text_change(old, new, zone)).category


# --------------------------------------------------------- critical class


def test_fastener_material_change_is_critical():
    """The motivating case: a grade change nobody may miss."""
    result = classify_record(text_change("SST / UNS S30400 TYPE 304", "SST / UNS S31600 TYPE 316"))
    assert result.category is ChangeCategory.MATERIAL_SPEC
    assert result.severity is Severity.CRITICAL


def test_standard_designation_change_is_material_spec():
    assert category_of("ASME B18.22.1", "ASME B18.21.1") is ChangeCategory.MATERIAL_SPEC


def test_structured_bom_record_uses_its_own_column_name():
    record = DiffRecord(
        zone="C2", change_type=ChangeType.TEXT_CHANGED, bbox=BBOX,
        old_value="BOM item 10 part number: 266635", new_value="266673",
    )
    result = classify_record(record)
    assert result.category is ChangeCategory.PART_SUBSTITUTION
    assert result.severity is Severity.CRITICAL
    assert "item 10" in result.rationale


@pytest.mark.parametrize(
    "column,expected",
    [
        ("part number", ChangeCategory.PART_SUBSTITUTION),
        ("quantity", ChangeCategory.QUANTITY),
        ("specification", ChangeCategory.MATERIAL_SPEC),
        ("material", ChangeCategory.MATERIAL_SPEC),
        ("description", ChangeCategory.BOM_ITEM),
    ],
)
def test_every_bom_column_maps_to_a_category(column, expected):
    record = DiffRecord(
        zone="C2", change_type=ChangeType.TEXT_CHANGED, bbox=BBOX,
        old_value=f"BOM item 4 {column}: something", new_value="something else",
    )
    assert classify_record(record).category is expected


def test_fastener_size_in_a_parts_list_is_not_a_dimension():
    """Regression: '5/8' inside a washer description matched the fraction
    pattern and was reported as a drawing dimension, sending the reviewer
    hunting for a dimension that never existed."""
    result = classify_record(
        text_change("4 FLAT WASHER, TYPE A, SERIES N, 5/8 ASME B18.22.1 SST / UNS S30400", None)
    )
    assert result.category is not ChangeCategory.DIMENSION
    assert result.severity is Severity.CRITICAL


def test_real_dimension_change_is_a_dimension():
    assert category_of("138 1/2 [3518]", "140 1/4 [3563]") is ChangeCategory.DIMENSION


def test_tolerance_change_is_its_own_category():
    assert category_of(".XX ± .06 [1.5]", ".XX ± .03 [0.8]") is ChangeCategory.TOLERANCE


# ------------------------------------------------- informational and noise


@pytest.mark.parametrize(
    "old,new",
    [
        ("COPYRIGHT©2016 BY ACME LTD.", "COPYRIGHT©2023 BY ACME LTD."),
        ("2019-07-22", "2023-11-30"),
        ("6700 McMillan Way, Richmond, B.C.", "6651 Fraserwood Pl, Richmond, B.C."),
        ("B", "C"),
    ],
)
def test_housekeeping_is_informational(old, new):
    result = classify_record(text_change(old, new, zone="D1"))
    assert result.severity is Severity.INFORMATIONAL


def test_address_is_not_mistaken_for_surface_finish():
    """Regression: a split-character address ('F ra se rw oo d') matched a
    bare 'Ra' and was classified as a surface finish callout."""
    assert (
        category_of("6700 M c M illa n W a y", "6651 F ra se rw oo d Pl")
        is not ChangeCategory.SURFACE_FINISH
    )


def test_real_surface_finish_still_detected():
    assert category_of("Ra 3.2", "Ra 1.6") is ChangeCategory.SURFACE_FINISH


def test_doubled_render_is_collapsed_for_display():
    """Some exports draw title-block text twice, so every character arrives
    doubled. The reader should see the real words."""
    result = classify_record(
        text_change("DDUUAALL DDIIMMEENNSSIIOONNSS", "FFLLAATT WWAASSHHEERR")
    )
    assert "DUAL DIMENSIONS" in result.describe()
    assert "FLAT WASHER" in result.describe()


def test_drawing_note_is_major():
    result = classify_record(
        text_change("1. SEE 322451 FOR GENERAL PROCESSING NOTES.",
                    "1. SEE DRAWING 322451 FOR GENERAL PROCESSING NOTES.", zone="A1")
    )
    assert result.category is ChangeCategory.NOTE
    assert result.severity is Severity.MAJOR


def test_geometry_records_are_never_reclassified():
    record = DiffRecord(
        zone="B3", change_type=ChangeType.GEOMETRY_CHANGED, bbox=BBOX,
        old_value="18 lines (2 x 20 pt)", new_value="18 lines (2 x 20 pt), shifted 2 pt",
    )
    assert classify_record(record).category is ChangeCategory.GEOMETRY


# ------------------------------------------------------------- ordering


def test_records_sort_most_severe_first():
    records = [
        text_change("COPYRIGHT©2016", "COPYRIGHT©2023", zone="D1"),
        text_change("ASME B18.22.1", "ASME B18.21.1"),
        text_change("1. SEE 322451 FOR NOTES.", "1. SEE DRAWING 322451 FOR NOTES.", zone="A1"),
    ]
    ordered = classify_records(records)
    assert [c.severity for c in ordered] == [
        Severity.CRITICAL, Severity.MAJOR, Severity.INFORMATIONAL
    ]


def test_severity_summary_counts_every_change():
    records = [
        text_change("ASME B18.22.1", "ASME B18.21.1"),
        text_change("COPYRIGHT©2016", "COPYRIGHT©2023", zone="D1"),
    ]
    counts = summarize_by_severity(classify_records(records))
    assert sum(counts.values()) == 2
    assert counts[Severity.CRITICAL] == 1


def test_describe_reads_as_a_change_sentence():
    assert classify_record(text_change("266635", "266673")).describe() == "266635 → 266673"
    assert classify_record(text_change(None, "NEW NOTE")).describe() == "added: NEW NOTE"
    assert classify_record(text_change("OLD NOTE", None)).describe() == "removed: OLD NOTE"
