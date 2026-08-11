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
_STANDARD_CODE_RE = re.compile(r"^[A-Z]{0,3}\d+\.\d+(?:\.\d+)*[A-Z]?$")

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

    @property
    def zone(self) -> str:
        return self.record.zone

    def describe(self) -> str:
        """One line an engineer can read without opening the drawing."""
        old = (self.record.old_value or "").strip()
        new = (self.record.new_value or "").strip()
        if old and new:
            return f"{old} \u2192 {new}"
        if new:
            return f"added: {new}"
        if old:
            return f"removed: {old}"
        return self.record.change_type.value


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

    if record.change_type in {
        ChangeType.GEOMETRY_ADDED,
        ChangeType.GEOMETRY_REMOVED,
        ChangeType.GEOMETRY_CHANGED,
    }:
        return _make(record, ChangeCategory.GEOMETRY, "vector geometry difference")

    removed_frag, added_frag = _changed_fragments(old, new)
    edited = removed_frag | added_frag
    edited_text = " ".join(sorted(edited))

    # --- what actually changed inside the line ------------------------
    if _TOLERANCE_RE.search(edited_text) or (
        _TOLERANCE_RE.search(both) and _DIMENSION_RE.search(edited_text)
    ):
        return _make(record, ChangeCategory.TOLERANCE, "tolerance notation changed")

    if _FINISH_RE.search(both):
        return _make(record, ChangeCategory.SURFACE_FINISH, "surface finish callout")

    # Standards usually share their prefix across revisions, so compare the
    # full strings rather than only the differing tokens.
    old_std = _STANDARD_RE.findall(old)
    new_std = _STANDARD_RE.findall(new)
    if (old_std or new_std) and old_std != new_std:
        return _make(
            record,
            ChangeCategory.MATERIAL_SPEC,
            f"specification standard changed ({'/'.join(old_std + new_std)[:60]})",
        )
    if any(_STANDARD_CODE_RE.match(t) for t in edited):
        return _make(record, ChangeCategory.MATERIAL_SPEC, "specification code changed")

    if _MATERIAL_RE.search(edited_text):
        return _make(
            record,
            ChangeCategory.MATERIAL_SPEC,
            f"material/grade changed ({edited_text[:60]})",
        )

    if _DATE_RE.search(edited_text) or _ADDRESS_RE.search(edited_text) or _ADMIN_RE.search(both):
        return _make(record, ChangeCategory.TITLE_BLOCK, "title block or revision housekeeping")

    if _NOTE_RE.search(old) or _NOTE_RE.search(new):
        return _make(record, ChangeCategory.NOTE, "drawing note text changed")

    if _VIEW_RE.search(both):
        return _make(record, ChangeCategory.ANNOTATION, "view or detail label")

    # A part number that appears on exactly one side is a substitution.
    old_parts = {p for p in _PART_NO_RE.findall(old.upper()) if any(c.isdigit() for c in p)}
    new_parts = {p for p in _PART_NO_RE.findall(new.upper()) if any(c.isdigit() for c in p)}
    swapped = (old_parts - new_parts) | (new_parts - old_parts)
    long_swapped = {p for p in swapped if len(p) >= 5}
    if long_swapped and not _DIMENSION_RE.search(edited_text):
        return _make(
            record,
            ChangeCategory.PART_SUBSTITUTION,
            f"part number changed ({', '.join(sorted(long_swapped))[:60]})",
        )

    if _DIMENSION_RE.search(edited_text):
        return _make(record, ChangeCategory.DIMENSION, "dimension value changed")

    if _QTY_RE.search(both) or (_BARE_INT_RE.match(old) and _BARE_INT_RE.match(new)):
        return _make(record, ChangeCategory.QUANTITY, "quantity changed")

    # Short alphanumeric tokens in a drawing are revision letters, drafter
    # initials, and approval marks — the title block's own bookkeeping.
    if old and new and len(old) <= 4 and len(new) <= 4:
        return _make(record, ChangeCategory.TITLE_BLOCK, "revision letter or initials")
    if (old or new) and len(old or new) <= 3:
        return _make(record, ChangeCategory.TITLE_BLOCK, "revision letter or initials")

    return _make(record, ChangeCategory.UNCLASSIFIED, "no matching pattern")


def _make(record: DiffRecord, category: ChangeCategory, rationale: str) -> ClassifiedChange:
    return ClassifiedChange(
        record=record,
        category=category,
        severity=SEVERITY_OF[category],
        rationale=rationale,
    )


def classify_records(records: list[DiffRecord]) -> list[ClassifiedChange]:
    """Classify and sort: most severe first, then grouped by category."""
    classified = [classify_record(r) for r in records]
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
