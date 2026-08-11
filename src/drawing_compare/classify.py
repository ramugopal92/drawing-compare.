"""
Engineering classification of raw differences.

The diff engine answers "what bytes changed". This module answers the
question an engineer actually asks: **what kind of change is this, and does
it matter?**

A flat list mixing "copyright 2016 became 2023" with "fastener material
changed from SST 304 to SST 316" is unusable, because the two demand
completely different responses — one is administrative housekeeping, the
other stops production until purchasing is told. Both look identical to a
text differ.

So every DiffRecord is routed to a ChangeCategory by what its content looks
like, and each category carries a fixed Severity. The report then leads with
CRITICAL, and the reader can stop reading whenever they like.

The classifier is pattern-based rather than positional on purpose: BOM
tables, notes, and title blocks sit in different places on every company's
template, but "ASTM F593C" looks like a material spec everywhere.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from .diff_engine import ChangeType, DiffRecord


class Severity(str, Enum):
    CRITICAL = "Critical"        # changes the part that gets made or bought
    MAJOR = "Major"              # changes how it is made or inspected
    MINOR = "Minor"              # presentation, layout, drafting
    INFORMATIONAL = "Info"       # revision housekeeping


class ChangeCategory(str, Enum):
    BOM_ITEM = "Bill of materials"
    BOM_STRUCTURE = "Parts list structure"
    DEFAULT_TOLERANCE = "General tolerance block"
    PART_SUBSTITUTION = "Part substitution"
    MATERIAL_SPEC = "Material / specification"
    QUANTITY = "Quantity"
    DIMENSION = "Dimension"
    TOLERANCE = "Tolerance"
    SURFACE_FINISH = "Surface finish"
    NOTE = "Drawing note"
    ANNOTATION = "Annotation / label"
    GEOMETRY = "Geometry"
    TITLE_BLOCK = "Title block / revision"
    UNCLASSIFIED = "Unclassified"


SEVERITY_OF: dict[ChangeCategory, Severity] = {
    ChangeCategory.BOM_ITEM: Severity.CRITICAL,
    ChangeCategory.BOM_STRUCTURE: Severity.MAJOR,
    ChangeCategory.DEFAULT_TOLERANCE: Severity.MAJOR,
    ChangeCategory.PART_SUBSTITUTION: Severity.CRITICAL,
    ChangeCategory.MATERIAL_SPEC: Severity.CRITICAL,
    ChangeCategory.QUANTITY: Severity.CRITICAL,
    ChangeCategory.DIMENSION: Severity.CRITICAL,
    ChangeCategory.TOLERANCE: Severity.CRITICAL,
    ChangeCategory.SURFACE_FINISH: Severity.MAJOR,
    ChangeCategory.NOTE: Severity.MAJOR,
    ChangeCategory.ANNOTATION: Severity.MINOR,
    ChangeCategory.GEOMETRY: Severity.MAJOR,
    ChangeCategory.TITLE_BLOCK: Severity.INFORMATIONAL,
    ChangeCategory.UNCLASSIFIED: Severity.MINOR,
}

SEVERITY_ORDER = {
    Severity.CRITICAL: 0,
    Severity.MAJOR: 1,
    Severity.MINOR: 2,
    Severity.INFORMATIONAL: 3,
}

# --- content patterns -------------------------------------------------

# Materials and standards: ASTM/ASME/ISO/DIN designations, stainless grades.
_MATERIAL_RE = re.compile(
    r"\b(?:SST|SS\s*\d{3}|CS|ALUM(?:INUM|INIUM)?|BRASS|BRONZE|NYLON|PTFE|"
    r"UNS\s*[A-Z]?\d+|ASTM\s*[A-Z]?\d+[A-Z]*|AISI\s*\d+|TYPE\s*\d{3}|"
    r"GR(?:ADE)?\.?\s*\d+|A36|A32[0-9]|30[46]L?|316L?|304L?)\b",
    re.IGNORECASE,
)
_STANDARD_RE = re.compile(
    r"\b(?:ASME|ANSI|ISO|DIN|JIS|BS|EN|SAE|MIL)[\s\-]?[A-Z]?[\d.]+[A-Z\d.\-]*\b",
    re.IGNORECASE,
)
# A bare standard designation with the prefix stripped off, e.g. "B18.21.1".
# The prefix is usually common to both sides, so it cancels out of the
# changed-fragment set and only the number survives.
# Must carry a letter prefix (B18.21.1) or at least two dots (16.5.1) —
# without that it also matches a bare metric value like 1717.52, and every
# dual dimension added to a drawing gets reported as a specification change.
_STANDARD_CODE_RE = re.compile(
    r"^(?:[A-Z]{1,3}\d+(?:\.\d+)+[A-Z]?|\d+\.\d+\.\d+[A-Z]?)$"
)

# Text that a PDF has rendered twice, offset by a fraction of a point, comes
# back with every character doubled ("DDUUAALL"). Collapsing it makes the
# row readable and stops the doubled digits looking like a new part number.
def _undouble(text: str) -> str:
    def fix(word: str) -> str:
        if len(word) < 4 or len(word) % 2:
            return word
        if all(word[i] == word[i + 1] for i in range(0, len(word), 2)):
            return word[::2]
        return word

    words = text.split(" ")
    fixed = [fix(w) for w in words]
    # Only accept the collapse if it actually applied to most of the line,
    # so a genuine "AA" or "1122" is left alone.
    changed = sum(1 for a, b in zip(words, fixed) if a != b)
    return " ".join(fixed) if changed >= max(1, len(words) // 2) else text

# Part / drawing numbers: 5+ chars, digit-bearing, optionally hyphenated.
_PART_NO_RE = re.compile(r"\b(?=[A-Z0-9]*\d)[A-Z0-9]{3,}(?:[\-/][A-Z0-9]+)*\b")

# Dimensions: decimal inches, fractions, bracketed metric, degrees, diameters.
_DIMENSION_RE = re.compile(
    r"(?:\[\s*\d+(?:\.\d+)?\s*\]"          # [3518]
    r"|\b\d+\s+\d+/\d+\b"                  # 138 1/2
    r"|\b\d+/\d+\b"                        # 1/2
    r"|\b\d+\.\d+\b"                       # 25.40
    r"|\d+(?:\.\d+)?\s*°"                  # 23.2 degrees
    r"|[⌀ØΦ]\s*\d+"                        # diameter
    r"|\bR\d+(?:\.\d+)?\b)"                # radius
)
_TOLERANCE_RE = re.compile(r"(?:±|\+/-|\+\s*-|\bTOL(?:ERANCE)?\b)", re.IGNORECASE)
# Ra must be followed by a value: bare "ra" appears inside ordinary words
# once a PDF splits a line into character fragments ("F ra se rw oo d").
_FINISH_RE = re.compile(
    r"\bRa\s*\d|\bRMS\s*\d|\bRz\s*\d|µin|MICROINCH|SURFACE\s+FINISH", re.IGNORECASE
)

# --- bill of materials -------------------------------------------------
# A parts-list row is recognised by its structure (item no. / part no. /
# quantity / description) or by naming a component. Establishing that a
# line belongs to the BOM has to happen before any value-level rule runs:
# "5/8" inside "FLAT WASHER, TYPE A, SERIES N, 5/8" is a fastener size, not
# a drawing dimension, and classifying it as one sends the reader hunting
# the sheet for a dimension that was never there.
_BOM_STRUCTURE_RE = re.compile(
    r"^\s*\d{1,3}\s+[A-Z0-9][A-Z0-9\-/]{3,}\s+\d{1,4}\s+\S", re.IGNORECASE
)
_BOM_QTY_DESC_RE = re.compile(r"^\s*\d{1,4}\s+[A-Z]", re.IGNORECASE)
_COMPONENT_RE = re.compile(
    r"\b(?:WASHER|NUT|BOLT|SCREW|CAP\s*SCREW|RIVET|PIN|ROD|STUD|SPACER|"
    r"BUSHING|BEARING|GASKET|SEAL|O-RING|CLIP|CLAMP|BRACKET|PLATE|SHIM|"
    r"WLD|WELDMENT|ASSY|ASSEMBLY|STEP|TUBE|ANGLE|CHANNEL|BEAM|GDR|GIRDER|"
    r"ITEMS?\s+LIST|PARTS?\s+LIST|BILL\s+OF\s+MATERIAL)\b",
    re.IGNORECASE,
)

# Records produced by the structured parts-list comparator, which already
# know their item number and column.
# The decimal-place notation of a general tolerance block: .X, .XX, .XXX
_DECIMAL_PLACES_RE = re.compile(r"\.X{1,4}\b|\bDECIMALS\b|\bFRACTIONS\b|\bANGLES\b")

_BOM_RECORD_RE = re.compile(r"^BOM item (\d+) ([a-z ]+):", re.IGNORECASE)

# Quantity: a bare small integer, or an explicit QTY column.
_QTY_RE = re.compile(r"\bQTY\b|\bQUANTITY\b", re.IGNORECASE)
_BARE_INT_RE = re.compile(r"^\d{1,4}$")

# Title-block housekeeping: dates, initials, copyright, addresses, rev rows.
_DATE_RE = re.compile(r"\b(?:19|20)\d{2}[-/]\d{1,2}[-/]\d{1,2}\b|\b\d{1,2}/\d{1,2}/\d{2,4}\b")
_ADMIN_RE = re.compile(
    r"\b(?:COPYRIGHT|©|ALL RIGHTS RESERVED|PROPRIETARY|CONFIDENTIAL|"
    r"TEL:|WEB:|www\.|DRAWN BY|CHECKED BY|APPROVED BY|DESIGNED BY|"
    r"SHEET\s+\d|SCALE|PROJECTION|THIRD ANGLE|FIRST ANGLE|REVISION|"
    r"EC-ID|APVD|MOD BY|DO NOT SCALE|UNLESS OTHERWISE SPECIFIED)\b",
    re.IGNORECASE,
)
_ADDRESS_RE = re.compile(
    r"\b(?:STREET|ST\.|ROAD|RD\.|AVENUE|AVE\.|WAY|PLACE|PL\b|DRIVE|SUITE|#\d+|"
    r"CANADA|USA|INDIA|[A-Z]\d[A-Z]\s*\d[A-Z]\d)\b",
    re.IGNORECASE,
)

# Notes: numbered instructions and imperative drafting language.
_NOTE_RE = re.compile(
    r"^\s*(?:NOTES?:|\d+\.\s)|"
    r"\b(?:SEE\s+(?:DRAWING|DWG|SHEET)|FIELD\s+DRILL|DEBURR|TYPICAL|TYP\b|"
    r"BREAK\s+ALL|REMOVE\s+BURRS|WELD|PAINT|PLATE|HEAT\s+TREAT|"
    r"APPLICABLE|REFERENCE ONLY)\b",
    re.IGNORECASE,
)

# View and detail labels.
_VIEW_RE = re.compile(
    r"\b(?:SECTION|DETAIL|VIEW|ISOMETRIC|SCALE\s*\d+\s*:\s*\d+|"
    r"BROKEN-OUT|ENLARGED)\b",
    re.IGNORECASE,
)


@dataclass
class ClassifiedChange:
    """A DiffRecord plus the engineering meaning we inferred for it."""

    record: DiffRecord
    category: ChangeCategory
    severity: Severity
    rationale: str
    display_old: str | None = None
    display_new: str | None = None

    @property
    def zone(self) -> str:
        return self.record.zone

    def describe(self) -> str:
        """One line an engineer can read without opening the drawing."""
        old = (self.display_old or self.record.old_value or "").strip()
        new = (self.display_new or self.record.new_value or "").strip()
        if old and new:
            return f"{old} \u2192 {new}"
        if new:
            return f"added: {new}"
        if old:
            return f"removed: {old}"
        return self.record.change_type.value


def _is_bom_row(text: str) -> bool:
    """True if this line reads like a parts-list entry."""
    if not text:
        return False
    if _BOM_STRUCTURE_RE.match(text):
        return True
    if _COMPONENT_RE.search(text) and (
        _BOM_QTY_DESC_RE.match(text) or text.count(",") >= 2
    ):
        return True
    return bool(_COMPONENT_RE.search(text) and _STANDARD_RE.search(text))


def _classify_bom_change(
    record: DiffRecord, old: str, new: str, edited: set[str], edited_text: str
) -> ClassifiedChange:
    """
    Sub-classify a change inside a parts-list row.

    The row is already known to be BOM, so the question is only which
    column moved: the part number, the quantity, the material or spec, or
    the description itself.
    """
    old_parts = {p for p in _PART_NO_RE.findall(old.upper()) if any(c.isdigit() for c in p)}
    new_parts = {p for p in _PART_NO_RE.findall(new.upper()) if any(c.isdigit() for c in p)}
    swapped = {p for p in (old_parts ^ new_parts) if len(p) >= 5 and "." not in p}

    old_std = _STANDARD_RE.findall(old)
    new_std = _STANDARD_RE.findall(new)
    if (old_std or new_std) and old_std != new_std:
        return _make(
            record,
            ChangeCategory.MATERIAL_SPEC,
            f"parts-list specification changed ({'/'.join(old_std + new_std)[:60]})",
        )

    if _MATERIAL_RE.search(edited_text) or any(_STANDARD_CODE_RE.match(t) for t in edited):
        return _make(record, ChangeCategory.MATERIAL_SPEC, "parts-list material/grade changed")

    if swapped:
        return _make(
            record,
            ChangeCategory.PART_SUBSTITUTION,
            f"parts-list part number changed ({', '.join(sorted(swapped))[:60]})",
        )

    old_qty = _BOM_QTY_DESC_RE.match(old)
    new_qty = _BOM_QTY_DESC_RE.match(new)
    if old_qty and new_qty and old.split()[0] != new.split()[0]:
        return _make(record, ChangeCategory.QUANTITY, "parts-list quantity changed")

    return _make(record, ChangeCategory.BOM_ITEM, "parts-list entry changed")


def _tokens(*values: str | None) -> str:
    return " ".join(v for v in values if v)


def _changed_fragments(old: str, new: str) -> tuple[set[str], set[str]]:
    """Words present in one side but not the other — the actual edit."""
    a = set(re.findall(r"[A-Za-z0-9±°⌀./\-\[\]#]+", old.upper()))
    b = set(re.findall(r"[A-Za-z0-9±°⌀./\-\[\]#]+", new.upper()))
    return a - b, b - a


def classify_record(record: DiffRecord) -> ClassifiedChange:
    """
    Decide what kind of engineering change a raw difference represents.

    Order matters: the first rule that fires wins, and the rules are
    arranged most-specific first. Material and part-number changes are
    tested before the generic note and title-block rules, because a BOM row
    contains enough English to look like prose otherwise.
    """
    old = _undouble((record.old_value or "").strip())
    new = _undouble((record.new_value or "").strip())
    both = _tokens(old, new)

    def tag(category: ChangeCategory, rationale: str) -> ClassifiedChange:
        return _make(record, category, rationale, display_old=old or None, display_new=new or None)

    if record.change_type in {
        ChangeType.GEOMETRY_ADDED,
        ChangeType.GEOMETRY_REMOVED,
        ChangeType.GEOMETRY_CHANGED,
    }:
        return tag(ChangeCategory.GEOMETRY, "vector geometry difference")

    removed_frag, added_frag = _changed_fragments(old, new)
    edited = removed_frag | added_frag
    edited_text = " ".join(sorted(edited))

    # Region is decisive evidence, so it is consulted before any content
    # rule. A tolerance printed in the title block is the sheet's *default*
    # tolerance; the identical text on a dimension applies to one feature.
    # They are indistinguishable as strings and mean entirely different
    # things, so classifying on text alone reports the general tolerance
    # block as though a dimension had been retoleranced.
    if record.region == "title_block":
        if _TOLERANCE_RE.search(both) or _DECIMAL_PLACES_RE.search(both):
            return tag(
                ChangeCategory.DEFAULT_TOLERANCE,
                "general tolerance block in the title block",
            )
        return tag(ChangeCategory.TITLE_BLOCK, "title block content")
    if record.region == "revision_table":
        return tag(ChangeCategory.TITLE_BLOCK, "revision table content")

    if (record.old_value or "").lower().startswith("parts list"):
        return tag(ChangeCategory.BOM_STRUCTURE, "parts-list column structure changed")

    # Records emitted by the structured parts-list comparator name their
    # own column, so they are classified from that rather than re-parsed.
    bom_field = _BOM_RECORD_RE.match(old) or _BOM_RECORD_RE.match(new)
    if bom_field:
        column = bom_field.group(2).lower()
        category = {
            "part number": ChangeCategory.PART_SUBSTITUTION,
            "quantity": ChangeCategory.QUANTITY,
            "specification": ChangeCategory.MATERIAL_SPEC,
            "material": ChangeCategory.MATERIAL_SPEC,
        }.get(column, ChangeCategory.BOM_ITEM)
        return _make(
            record,
            category,
            f"parts-list item {bom_field.group(1)}, {column} column",
            display_old=old or None,
            display_new=new or None,
        )

    # Establish BOM context first. Everything inside a parts-list row is a
    # BOM change of some kind, never a drawing dimension.
    if _is_bom_row(old) or _is_bom_row(new):
        bom = _classify_bom_change(record, old, new, edited, edited_text)
        bom.display_old, bom.display_new = old or None, new or None
        return bom

    # --- what actually changed inside the line ------------------------
    if _TOLERANCE_RE.search(edited_text) or (
        _TOLERANCE_RE.search(both) and _DIMENSION_RE.search(edited_text)
    ):
        return tag(ChangeCategory.TOLERANCE, "tolerance notation changed")

    if _FINISH_RE.search(both):
        return tag(ChangeCategory.SURFACE_FINISH, "surface finish callout")

    # Standards usually share their prefix across revisions, so compare the
    # full strings rather than only the differing tokens.
    old_std = _STANDARD_RE.findall(old)
    new_std = _STANDARD_RE.findall(new)
    if (old_std or new_std) and old_std != new_std:
        return tag(ChangeCategory.MATERIAL_SPEC, f"specification standard changed ({'/'.join(old_std + new_std)[:60]})")
    if any(_STANDARD_CODE_RE.match(t) for t in edited):
        return tag(ChangeCategory.MATERIAL_SPEC, "specification code changed")

    if _MATERIAL_RE.search(edited_text):
        return tag(ChangeCategory.MATERIAL_SPEC, f"material/grade changed ({edited_text[:60]})")

    if _DATE_RE.search(edited_text) or _ADDRESS_RE.search(edited_text) or _ADMIN_RE.search(both):
        return tag(ChangeCategory.TITLE_BLOCK, "title block or revision housekeeping")

    if _NOTE_RE.search(old) or _NOTE_RE.search(new):
        return tag(ChangeCategory.NOTE, "drawing note text changed")

    if _VIEW_RE.search(both):
        return tag(ChangeCategory.ANNOTATION, "view or detail label")

    # A part number that appears on exactly one side is a substitution.
    old_parts = {p for p in _PART_NO_RE.findall(old.upper()) if any(c.isdigit() for c in p)}
    new_parts = {p for p in _PART_NO_RE.findall(new.upper()) if any(c.isdigit() for c in p)}
    swapped = (old_parts - new_parts) | (new_parts - old_parts)
    long_swapped = {p for p in swapped if len(p) >= 5}
    if long_swapped and not _DIMENSION_RE.search(edited_text):
        return tag(ChangeCategory.PART_SUBSTITUTION, f"part number changed ({', '.join(sorted(long_swapped))[:60]})")

    if _DIMENSION_RE.search(edited_text):
        return tag(ChangeCategory.DIMENSION, "dimension value changed")

    if _QTY_RE.search(both) or (_BARE_INT_RE.match(old) and _BARE_INT_RE.match(new)):
        return tag(ChangeCategory.QUANTITY, "quantity changed")

    # Short alphanumeric tokens in a drawing are revision letters, drafter
    # initials, and approval marks — the title block's own bookkeeping.
    if old and new and len(old) <= 4 and len(new) <= 4:
        return tag(ChangeCategory.TITLE_BLOCK, "revision letter or initials")
    if (old or new) and len(old or new) <= 3:
        return tag(ChangeCategory.TITLE_BLOCK, "revision letter or initials")

    return tag(ChangeCategory.UNCLASSIFIED, "no matching pattern")


def _make(
    record: DiffRecord,
    category: ChangeCategory,
    rationale: str,
    display_old: str | None = None,
    display_new: str | None = None,
) -> ClassifiedChange:
    return ClassifiedChange(
        record=record,
        category=category,
        severity=SEVERITY_OF[category],
        rationale=rationale,
        display_old=display_old,
        display_new=display_new,
    )


def _aggregate_tolerance_block(
    classified: list[ClassifiedChange],
) -> list[ClassifiedChange]:
    """
    Collapse the general tolerance block into one row.

    Reformatting the block — adding metric equivalents, say — rewrites every
    line in it, and the text differ splits those across the reflow, so half
    the rows read as fragments. It is one drafting decision and belongs on
    one line, with the individual values kept in the detail.
    """
    block = [c for c in classified if c.category is ChangeCategory.DEFAULT_TOLERANCE]
    if len(block) < 3:
        return classified

    others = [c for c in classified if c.category is not ChangeCategory.DEFAULT_TOLERANCE]
    pairs = [
        f"{(c.display_old or c.record.old_value or '—')} \u2192 "
        f"{(c.display_new or c.record.new_value or '—')}"
        for c in block
        if (c.record.old_value or c.record.new_value)
    ]
    summary = DiffRecord(
        zone=block[0].zone,
        change_type=block[0].record.change_type,
        bbox=block[0].record.bbox,
        old_value="general tolerance block",
        new_value=f"revised in {len(block)} place(s): " + "; ".join(pairs[:6]),
        confidence=0.8,
        source=block[0].record.source,
        match_basis="title-block tolerance aggregation",
        region="title_block",
    )
    others.append(
        ClassifiedChange(
            record=summary,
            category=ChangeCategory.DEFAULT_TOLERANCE,
            severity=SEVERITY_OF[ChangeCategory.DEFAULT_TOLERANCE],
            rationale=f"{len(block)} tolerance-block lines aggregated",
        )
    )
    return others


def classify_records(records: list[DiffRecord]) -> list[ClassifiedChange]:
    """Classify and sort: most severe first, then grouped by category."""
    classified = _aggregate_tolerance_block([classify_record(r) for r in records])
    classified.sort(
        key=lambda c: (SEVERITY_ORDER[c.severity], c.category.value, c.zone)
    )
    return classified


def summarize_by_category(
    classified: list[ClassifiedChange],
) -> dict[ChangeCategory, int]:
    counts: dict[ChangeCategory, int] = {}
    for c in classified:
        counts[c.category] = counts.get(c.category, 0) + 1
    return counts


def summarize_by_severity(classified: list[ClassifiedChange]) -> dict[Severity, int]:
    counts: dict[Severity, int] = {}
    for c in classified:
        counts[c.severity] = counts.get(c.severity, 0) + 1
    return counts
