"""
The components of an engineering drawing.

A drawing is not a page of text — it is a set of well-defined components,
each governed by its own conventions and each meaning something different
when it changes. A number inside a feature control frame is a geometric
tolerance; the same number in a parts list is a quantity; in the title block
it is a sheet number. Reporting all three as "text changed" throws away the
only thing that makes a difference reviewable.

This module names those components once, so the classifier, the region
detector and the report all agree on what they are. It follows the component
breakdown an engineer would use when checking a drawing:

  Frame components   title block, revision table, parts list, weld table,
                     general notes, general tolerance block, sheet border
  Graphical content  views (principal, section, detail, isometric), and
                     their labels
  Annotation         dimensions (linear, diameter, radius, angle, chamfer,
                     thread), tolerances, surface finish, weld symbols,
                     GD&T feature control frames, datums, item balloons,
                     revision balloons
"""

from __future__ import annotations

import re
from enum import Enum


class Component(str, Enum):
    """Every part of a drawing this tool can recognise and name."""

    # --- frame / documentation components -----------------------------
    TITLE_BLOCK = "Title block"
    REVISION_TABLE = "Revision table"
    PARTS_LIST = "Parts list (BOM)"
    WELD_TABLE = "Weld table"
    GENERAL_NOTES = "General notes"
    GENERAL_TOLERANCE = "General tolerance block"
    SHEET_BORDER = "Sheet border / zone grid"

    # --- graphical components ------------------------------------------
    VIEW = "View"
    SECTION_VIEW = "Section view"
    DETAIL_VIEW = "Detail view"
    ISOMETRIC_VIEW = "Isometric view"
    VIEW_LABEL = "View label"

    # --- annotation components -----------------------------------------
    DIMENSION_LINEAR = "Linear dimension"
    DIMENSION_DIAMETER = "Diameter dimension"
    DIMENSION_RADIUS = "Radius dimension"
    DIMENSION_ANGLE = "Angular dimension"
    DIMENSION_CHAMFER = "Chamfer"
    DIMENSION_THREAD = "Thread callout"
    TOLERANCE = "Tolerance"
    GDT_FRAME = "GD&T feature control frame"
    DATUM = "Datum"
    WELD_SYMBOL = "Weld symbol"
    SURFACE_FINISH = "Surface finish"
    ITEM_BALLOON = "Item balloon"
    REVISION_BALLOON = "Revision balloon"

    UNCLASSIFIED = "Unclassified"


# Which components are dimensional callouts — used wherever "is this a
# dimension?" needs answering without listing the five kinds each time.
DIMENSION_COMPONENTS = frozenset(
    {
        Component.DIMENSION_LINEAR,
        Component.DIMENSION_DIAMETER,
        Component.DIMENSION_RADIUS,
        Component.DIMENSION_ANGLE,
        Component.DIMENSION_CHAMFER,
        Component.DIMENSION_THREAD,
    }
)

VIEW_COMPONENTS = frozenset(
    {
        Component.VIEW,
        Component.SECTION_VIEW,
        Component.DETAIL_VIEW,
        Component.ISOMETRIC_VIEW,
    }
)

TABLE_COMPONENTS = frozenset(
    {
        Component.PARTS_LIST,
        Component.WELD_TABLE,
        Component.REVISION_TABLE,
    }
)


# ---------------------------------------------------------------------
# Recognition patterns
# ---------------------------------------------------------------------
# Each pattern identifies a component from the *text* of a callout, so it
# works regardless of which PDF library extracted it and regardless of where
# on the sheet the callout sits. Geometry is better evidence when it is
# available, but it varies between extractors; wording does not.

# Diameter: the ⌀ symbol, or its common OCR/encoding substitutes.
DIAMETER_RE = re.compile(r"[\u00d8\u2300\u03c6\u0444]\s*[\d./]|(?:\bDIA\b)", re.IGNORECASE)

# Radius: R followed by a value, not part of a word.
RADIUS_RE = re.compile(r"\bR\s*\d+(?:\.\d+)?(?:/\d+)?\b")

# Angle: a value followed by the degree sign.
ANGLE_RE = re.compile(r"\d+(?:\.\d+)?\s*[\u00b0]")

# Chamfer: "2 x 45°" and its variants.
CHAMFER_RE = re.compile(r"\d+(?:\.\d+)?\s*[xX\u00d7]\s*\d+(?:\.\d+)?\s*[\u00b0]")

# Threads: unified, metric, and pipe thread callouts.
THREAD_RE = re.compile(
    r"\b\d+/\d+\s*-\s*\d+\s*UN[CRF]?\b"
    r"|\bM\d+(?:\.\d+)?\s*[xX]\s*\d+(?:\.\d+)?\b"
    r"|\b\d+-\d+\s*UN[CRF]\b"
    r"|\bNPT\b|\bBSP[TP]?\b",
    re.IGNORECASE,
)

# Linear: whole numbers, decimals, and imperial fractions such as "41 15/16".
LINEAR_RE = re.compile(r"^\s*\d+(?:\s+\d+/\d+)?\s*$|^\s*\d+/\d+\s*$|^\s*\d+\.\d+\s*$")

# Tolerance notation.
TOLERANCE_RE = re.compile(r"[\u00b1]|\+\s*/\s*-|\bTOL(?:ERANCE)?\b", re.IGNORECASE)

# GD&T: a feature control frame's characteristic symbols, plus the ASCII
# spellings that PDF text layers often carry instead.
GDT_RE = re.compile(
    r"[\u2312\u232d\u232e\u2330\u2331\u2332\u2333\u2334\u2335\u2336\u2337"
    r"\u23e4\u25b1\u25ce\u2225\u27c2\u2220]"
    r"|\bGD&T\b|\bTRUE\s+POSITION\b|\bPERPENDICULARIT|\bCONCENTRICIT"
    r"|\bCYLINDRICIT|\bPARALLELISM\b|\bFLATNESS\b|\bCIRCULARIT|\bRUNOUT\b"
    r"|\bPROFILE\s+OF\b|\bMMC\b|\bLMC\b",
    re.IGNORECASE,
)

# Datum: a boxed reference letter, or an explicit datum callout.
DATUM_RE = re.compile(r"\bDATUM\s+[A-Z]\b|\[[A-Z]\]|\b-[A-Z]-\b", re.IGNORECASE)

# Weld symbols: fillet/groove sizes and weld process callouts. A weld size
# like "1/4" is indistinguishable from a fraction dimension by value alone,
# so the accompanying weld vocabulary is what identifies it.
WELD_RE = re.compile(
    r"\bFILLET\b|\bGROOVE\b|\bWELD\b|\bCJP\b|\bPJP\b|\bTYP\.?\s*WELD\b"
    r"|\bWRAP\s+CORNERS?\b|\bALL\s+AROUND\b|\bFIELD\s+WELD\b"
    r"|\bGTAW\b|\bGMAW\b|\bSMAW\b|\bFCAW\b",
    re.IGNORECASE,
)

# A weld size range: fillet leg plus length ("4-7 1/2"), or the metric
# equivalent pair in a conversion table ("100-190"). Written as a range with
# a hyphen, which distinguishes it from an ordinary dimension.
WELD_SIZE_RE = re.compile(r"^\s*\d+(?:\.\d+)?(?:\s+\d+/\d+)?\s*-\s*\d+(?:\.\d+)?(?:\s+\d+/\d+)?\s*$")

# Unit headers of a conversion table.
UNIT_HEADER_RE = re.compile(r"^\s*(?:IN|MM|INCH(?:ES)?|MILLIMET(?:ER|RE)S?)\s*$", re.IGNORECASE)

# Machining and feature notes attached to a dimension rather than standing
# alone — "THRU ALL", "WRAP CORNERS", "CUTOUT", "TYP".
FEATURE_NOTE_RE = re.compile(
    r"\bTHRU(?:\s+ALL)?\b|\bWRAP\s+CORNERS?\b|\bCUTOUT\b|\bC'?BORE\b"
    r"|\bC'?SINK\b|\bCOUNTERBORE\b|\bCOUNTERSINK\b|\bTAP(?:PED)?\b"
    r"|\bREAM(?:ED)?\b|\bTYP\b|\bNOTE\s+\d+\b|\bPIPE\s+FACE\b",
    re.IGNORECASE,
)

# Weld conversion table, which some standards require alongside dual units.
WELD_TABLE_RE = re.compile(
    r"\bWELD\s+(?:SIZE\s+)?CONVERSION\b|\bWELD\s+TABLE\b"
    r"|\bSEE\s+TABLE\s+FOR\s+WELD\b",
    re.IGNORECASE,
)

# Surface finish: a roughness value, not a bare "Ra".
SURFACE_FINISH_RE = re.compile(
    r"\bRa\s*\d|\bRMS\s*\d|\bRz\s*\d|\u00b5in|MICROINCH|SURFACE\s+FINISH",
    re.IGNORECASE,
)

# View labels.
SECTION_LABEL_RE = re.compile(r"^\s*SECTION\s+[A-Z]{1,2}\s*-\s*[A-Z]{1,2}\s*$", re.IGNORECASE)
DETAIL_LABEL_RE = re.compile(r"^\s*DETAIL\s+[A-Z]{1,2}(?:-[A-Z]{1,2})?\s*$", re.IGNORECASE)
ISOMETRIC_LABEL_RE = re.compile(
    r"^\s*(?:[A-Z ]*\s)?(?:ISOMETRIC|EXPLODED|AUXILIARY|ENLARGED|BROKEN-OUT)"
    r"[A-Z ]*VIEW\s*$",
    re.IGNORECASE,
)
NAMED_VIEW_RE = re.compile(
    r"^\s*(?:FRONT|TOP|BOTTOM|LEFT|RIGHT|REAR|BACK|PLAN|ELEVATION)\s+VIEW\s*$",
    re.IGNORECASE,
)
SCALE_NOTE_RE = re.compile(r"^\s*SCALE\s*\d+\s*:\s*\d+\s*$", re.IGNORECASE)

# Item balloon: a bare item number, optionally with a quantity multiplier
# ("12X 1"). These reference the parts list from the drawing body.
ITEM_BALLOON_RE = re.compile(r"^\s*(?:\d{1,3}\s*[xX]\s*)?\d{1,3}\s*$")

# Revision balloon: a lone revision letter marking what a revision changed.
REVISION_BALLOON_RE = re.compile(r"^\s*[A-Z]\d?\s*$")

# General notes.
GENERAL_NOTES_RE = re.compile(
    r"^\s*NOTES?\s*:?\s*$|^\s*\d+\.\s+\S|"
    r"\b(?:SEE\s+(?:DRAWING|DWG|SHEET)|FIELD\s+DRILL|DEBURR|TYPICAL|TYP\b"
    r"|BREAK\s+ALL|REMOVE\s+BURRS|HEAT\s+TREAT|UNLESS\s+OTHERWISE)\b",
    re.IGNORECASE,
)


def classify_component(text: str, region: str | None = None) -> Component:
    """
    Name the drawing component a piece of callout text belongs to.

    Region is used as context where it is known — the same "1/4" is a weld
    size beside a weld symbol, a fraction dimension in a view, and a stock
    thickness in a parts list — but the decision never *depends* on region,
    because region detection relies on geometry that differs between PDF
    extractors. Wording is the reliable signal; region refines it.
    """
    if not text:
        return Component.UNCLASSIFIED
    value = text.strip()

    # --- structural context first, where it is unambiguous -------------
    if region == "revision_table":
        return Component.REVISION_TABLE
    if region == "parts_list":
        return Component.PARTS_LIST

    # --- tables and blocks ---------------------------------------------
    if WELD_TABLE_RE.search(value) or UNIT_HEADER_RE.match(value):
        return Component.WELD_TABLE

    # --- view labels ----------------------------------------------------
    if SECTION_LABEL_RE.match(value):
        return Component.SECTION_VIEW
    if DETAIL_LABEL_RE.match(value):
        return Component.DETAIL_VIEW
    if ISOMETRIC_LABEL_RE.match(value):
        return Component.ISOMETRIC_VIEW
    if NAMED_VIEW_RE.match(value) or SCALE_NOTE_RE.match(value):
        return Component.VIEW_LABEL

    # --- annotation, most specific first --------------------------------
    # Chamfer before angle: "2 x 45°" contains an angle but is a chamfer.
    if CHAMFER_RE.search(value):
        return Component.DIMENSION_CHAMFER
    if GDT_RE.search(value):
        return Component.GDT_FRAME
    if DATUM_RE.search(value):
        return Component.DATUM
    if SURFACE_FINISH_RE.search(value):
        return Component.SURFACE_FINISH
    if WELD_RE.search(value):
        return Component.WELD_SYMBOL
    if THREAD_RE.search(value):
        return Component.DIMENSION_THREAD
    if DIAMETER_RE.search(value):
        return Component.DIMENSION_DIAMETER
    if RADIUS_RE.search(value):
        return Component.DIMENSION_RADIUS
    if ANGLE_RE.search(value):
        return Component.DIMENSION_ANGLE
    if TOLERANCE_RE.search(value):
        return Component.TOLERANCE

    if WELD_SIZE_RE.match(value):
        return Component.WELD_SYMBOL

    if FEATURE_NOTE_RE.search(value):
        return Component.GENERAL_NOTES

    if GENERAL_NOTES_RE.search(value):
        return Component.GENERAL_NOTES

    # A lone revision letter in the drawing body is a revision balloon —
    # the marker a drafter places beside whatever that revision changed.
    if REVISION_BALLOON_RE.match(value) and region in (None, "drawing_body"):
        return Component.REVISION_BALLOON

    if ITEM_BALLOON_RE.match(value) and region in (None, "drawing_body"):
        return Component.ITEM_BALLOON

    if region == "title_block":
        return Component.TITLE_BLOCK

    if LINEAR_RE.match(value):
        return Component.DIMENSION_LINEAR

    return Component.UNCLASSIFIED


def component_of_pair(
    old_value: str | None, new_value: str | None, region: str | None = None
) -> Component:
    """
    Name the component a change affects, from both sides of the change.

    The side that carries a recognisable symbol wins: a dimension that gains
    a diameter symbol is a diameter dimension, even though the old side read
    as a bare number.
    """
    old_component = classify_component(old_value or "", region)
    new_component = classify_component(new_value or "", region)

    if old_component is new_component:
        return old_component
    if old_component is Component.UNCLASSIFIED:
        return new_component
    if new_component is Component.UNCLASSIFIED:
        return old_component
    # A more specific reading on either side is the better answer than the
    # generic linear-dimension fallback.
    if old_component is Component.DIMENSION_LINEAR:
        return new_component
    if new_component is Component.DIMENSION_LINEAR:
        return old_component
    return new_component
