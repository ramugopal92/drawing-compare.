"""Tests for engineering drawing component recognition."""

from __future__ import annotations

import pytest

from drawing_compare.taxonomy import Component, classify_component, component_of_pair


@pytest.mark.parametrize(
    "text,expected",
    [
        # --- view labels ---
        ("SECTION A-A", Component.SECTION_VIEW),
        ("SECTION D-D", Component.SECTION_VIEW),
        ("DETAIL F", Component.DETAIL_VIEW),
        ("DETAIL A-A", Component.DETAIL_VIEW),
        ("ISOMETRIC FRONT RIGHT VIEW", Component.ISOMETRIC_VIEW),
        ("FRONT VIEW", Component.VIEW_LABEL),
        ("SCALE 1 : 8", Component.VIEW_LABEL),
        # --- dimensions, by kind ---
        ("41 15/16", Component.DIMENSION_LINEAR),
        ("138 1/2", Component.DIMENSION_LINEAR),
        ("101.60", Component.DIMENSION_LINEAR),
        ("\u00d83/4 THRU ALL", Component.DIMENSION_DIAMETER),
        ("R3", Component.DIMENSION_RADIUS),
        ("78.4\u00b0", Component.DIMENSION_ANGLE),
        ("2 x 45\u00b0", Component.DIMENSION_CHAMFER),
        ("1/4-20 UNC-2B", Component.DIMENSION_THREAD),
        ("M8 x 1.25", Component.DIMENSION_THREAD),
        # --- annotation ---
        ("\u00b1 .06 [1.5]", Component.TOLERANCE),
        ("Ra 3.2", Component.SURFACE_FINISH),
        ("WRAP CORNERS", Component.WELD_SYMBOL),
        ("FILLET WELD ALL AROUND", Component.WELD_SYMBOL),
        ("4-7 1/2", Component.WELD_SYMBOL),
        ("TRUE POSITION", Component.GDT_FRAME),
        ("DATUM A", Component.DATUM),
        # --- tables and blocks ---
        ("WELD CONVERSION", Component.WELD_TABLE),
        ("MM", Component.WELD_TABLE),
        ("1. SEE 322451 FOR GENERAL PROCESSING NOTES.", Component.GENERAL_NOTES),
        ("THRU ALL", Component.GENERAL_NOTES),
    ],
)
def test_component_recognised_from_callout_text(text, expected):
    assert classify_component(text) is expected


def test_chamfer_wins_over_angle():
    """A chamfer contains an angle, so the more specific reading must be
    tested first or every chamfer is reported as an angular dimension."""
    assert classify_component("2 x 45\u00b0") is Component.DIMENSION_CHAMFER


def test_region_disambiguates_identical_text():
    """The same text means different things in different parts of a sheet."""
    assert classify_component("INITIAL RELEASE", "revision_table") is Component.REVISION_TABLE
    assert classify_component("2", "parts_list") is Component.PARTS_LIST


def test_balloons_only_in_the_drawing_body():
    assert classify_component("B", "drawing_body") is Component.REVISION_BALLOON
    assert classify_component("12", "drawing_body") is Component.ITEM_BALLOON
    # Inside a table the same characters are table content, not a balloon.
    assert classify_component("12", "parts_list") is Component.PARTS_LIST


def test_pair_takes_the_more_specific_side():
    """A dimension that gains a diameter symbol is a diameter dimension,
    even though the old side read as a bare fraction."""
    assert component_of_pair("5/8", "\u00d83/4") is Component.DIMENSION_DIAMETER
    assert component_of_pair("41 15/16", "41 1/2") is Component.DIMENSION_LINEAR


def test_pair_handles_one_sided_changes():
    assert component_of_pair(None, "SECTION D-D") is Component.SECTION_VIEW
    assert component_of_pair("DETAIL G", None) is Component.DETAIL_VIEW


def test_unrecognised_text_is_not_forced_into_a_component():
    assert classify_component("COND CD") is Component.UNCLASSIFIED
