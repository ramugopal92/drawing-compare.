"""
Sheet layout analysis — what part of the drawing a change sits in.

A zone reference like "C4" is a grid coordinate. It tells a checker where to
point a finger, but not what they are looking at. Engineers do not say "the
change in C4"; they say "the dimension in Detail A-A changed" or "the
tolerance block was revised". Those are different kinds of statement about
different parts of the sheet, and until the tool can tell them apart it
cannot report a change the way a person would describe it.

Two levels of structure are recovered here, both from the text layer:

  1. Regions — title block, revision table, drawing body. This is what
     separates the sheet's *default* tolerance block from a tolerance on an
     actual dimension. They look identical as text and mean entirely
     different things.

  2. Views — "DETAIL A", "SECTION D-D", "ISOMETRIC FRONT RIGHT VIEW". Every
     view on a drawing labels itself, in text, with coordinates. Finding
     those labels and assigning each an extent lets every difference be
     attributed to the view it falls inside.

Both are found from evidence on the sheet rather than from fixed page
fractions. A title block located by assuming "bottom-right 30%" works on the
drawings it was written against and silently mislabels everything on a
company whose template differs; a title block located by finding the words
DRAWING NO and REVISION works wherever those words are printed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .pdf_io import TextSpan

# --- regions -----------------------------------------------------------

TITLE_BLOCK = "title_block"
REVISION_TABLE = "revision_table"
PARTS_LIST = "parts_list"
DRAWING_BODY = "drawing_body"

# Labels printed inside a title block on essentially every drawing standard.
_TITLE_BLOCK_LABELS = re.compile(
    r"\b(?:DRAWING\s*NO|DWG\s*NO|DRAWING\s*NUMBER|PART\s*NO|SHEET\s+\d+\s+OF|"
    r"SCALE|DRAWN\s*BY|CHECKED\s*BY|APPROVED\s*BY|DESIGNED\s*BY|"
    r"DWG\s*CATEGORY|THIRD\s*ANGLE|FIRST\s*ANGLE|PROJECTION|"
    r"UNLESS\s+OTHERWISE\s+SPECIFIED|TOLERANCES\s+ARE|DO\s+NOT\s+SCALE)\b",
    re.IGNORECASE,
)

_REVISION_TABLE_LABELS = re.compile(
    r"\b(?:REV\.?\s+DESCRIPTION|EC-?ID|APVD|MOD\s*BY|REVISION\s+HISTORY|"
    r"ZONE\s+REV)\b",
    re.IGNORECASE,
)

_PARTS_LIST_LABELS = re.compile(
    r"\b(?:ITEMS?\s+LIST|PARTS?\s+LIST|BILL\s+OF\s+MATERIALS?|B\.?O\.?M\.?)\b",
    re.IGNORECASE,
)

# --- views -------------------------------------------------------------

_VIEW_LABEL_RE = re.compile(
    r"^(?:"
    r"(?P<detail>DETAIL\s+[A-Z]{1,2}(?:-[A-Z]{1,2})?)"
    r"|(?P<section>SECTION\s+[A-Z]{1,2}\s*-\s*[A-Z]{1,2})"
    r"|(?P<view>(?:[A-Z ]+\s)?(?:ISOMETRIC|EXPLODED|AUXILIARY|ENLARGED|"
    r"BROKEN-OUT)[A-Z ]*VIEW)"
    r"|(?P<named>(?:FRONT|TOP|BOTTOM|LEFT|RIGHT|REAR|BACK|PLAN|ELEVATION)"
    r"\s+VIEW)"
    r")\b",
    re.IGNORECASE,
)

# A view label sits directly above or below its own view, never far away.
VIEW_LABEL_MAX_DISTANCE_PT = 400.0


@dataclass(frozen=True)
class Region:
    """A named area of the sheet, with the evidence that identified it."""

    name: str
    bbox: tuple[float, float, float, float]
    evidence: str

    def contains(self, bbox: tuple[float, float, float, float]) -> bool:
        cx = (bbox[0] + bbox[2]) / 2.0
        cy = (bbox[1] + bbox[3]) / 2.0
        return self.bbox[0] <= cx <= self.bbox[2] and self.bbox[1] <= cy <= self.bbox[3]


@dataclass
class View:
    """One drawing view, identified by its own printed label."""

    label: str
    kind: str  # detail | section | isometric | named
    anchor: tuple[float, float]  # centre of the label itself
    scale: str | None = None
    members: list[tuple[float, float, float, float]] = field(default_factory=list)

    def normalised(self) -> str:
        """Label reduced to a comparable form, e.g. 'DETAIL A'."""
        return re.sub(r"\s+", " ", self.label.strip().upper())


@dataclass
class SheetLayout:
    """Everything known about how one sheet is laid out."""

    page_size: tuple[float, float]
    regions: list[Region] = field(default_factory=list)
    views: list[View] = field(default_factory=list)

    def region_for(self, bbox: tuple[float, float, float, float]) -> str:
        """Most specific region containing this box, else the drawing body.

        Regions are tested smallest-first so a parts list or revision table
        drawn inside the title-block corner wins over the title block."""
        for region in sorted(
            self.regions,
            key=lambda r: (r.bbox[2] - r.bbox[0]) * (r.bbox[3] - r.bbox[1]),
        ):
            if region.contains(bbox):
                return region.name
        return DRAWING_BODY

    def view_for(self, bbox: tuple[float, float, float, float]) -> str | None:
        """
        Nearest view label to this box, if any is close enough.

        A label gives a point, not a boundary, so nearest-anchor assignment
        is an approximation: a change sitting between two views may be
        attributed to either. It is right far more often than a grid
        reference is useful, and being wrong about which detail is a smaller
        error than saying nothing at all.
        """
        if not self.views:
            return None
        cx = (bbox[0] + bbox[2]) / 2.0
        cy = (bbox[1] + bbox[3]) / 2.0
        best, best_distance = None, VIEW_LABEL_MAX_DISTANCE_PT
        for view in self.views:
            distance = ((cx - view.anchor[0]) ** 2 + (cy - view.anchor[1]) ** 2) ** 0.5
            if distance < best_distance:
                best, best_distance = view, distance
        return best.normalised() if best else None


def _union(boxes) -> tuple[float, float, float, float]:
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


def _cluster_by_proximity(spans: list[TextSpan], radius: float) -> list[list[TextSpan]]:
    """Group label hits that belong to the same block of the sheet."""
    remaining = list(spans)
    clusters: list[list[TextSpan]] = []
    while remaining:
        seed = remaining.pop()
        group = [seed]
        changed = True
        while changed:
            changed = False
            for span in list(remaining):
                for member in group:
                    if _gap(span.bbox, member.bbox) <= radius:
                        group.append(span)
                        remaining.remove(span)
                        changed = True
                        break
        clusters.append(group)
    return clusters


def _gap(a, b) -> float:
    dx = max(0.0, max(a[0] - b[2], b[0] - a[2]))
    dy = max(0.0, max(a[1] - b[3], b[1] - a[3]))
    return (dx * dx + dy * dy) ** 0.5


def detect_regions(
    lines: list[TextSpan], page_size: tuple[float, float]
) -> list[Region]:
    """
    Locate the title block, revision table, and parts list by their labels.

    Each is found by the words printed inside it, then given an extent that
    covers the cluster of those words plus the text sitting with them. No
    page fractions are assumed, so a company whose title block runs along
    the bottom edge rather than the bottom-right corner is handled without
    configuration.
    """
    width, height = page_size
    radius = max(width, height) * 0.06
    regions: list[Region] = []

    for name, pattern in (
        (PARTS_LIST, _PARTS_LIST_LABELS),
        (REVISION_TABLE, _REVISION_TABLE_LABELS),
        (TITLE_BLOCK, _TITLE_BLOCK_LABELS),
    ):
        hits = [line for line in lines if pattern.search(line.text)]
        if not hits:
            continue
        clusters = _cluster_by_proximity(hits, radius)
        biggest = max(clusters, key=len)
        if len(biggest) < 2 and name is TITLE_BLOCK:
            continue
        seed = _union([span.bbox for span in biggest])
        # Grow the region to take in the values printed beside the labels.
        neighbours = [
            line.bbox
            for line in lines
            if _gap(line.bbox, seed) <= radius * 0.5
        ]
        bbox = _union([seed] + neighbours) if neighbours else seed
        regions.append(
            Region(
                name=name,
                bbox=bbox,
                evidence=f"{len(biggest)} label(s) matched, e.g. {biggest[0].text[:40]!r}",
            )
        )

    return regions


def detect_views(lines: list[TextSpan]) -> list[View]:
    """
    Find the drawing's own view labels.

    Every view on a drawing announces itself — DETAIL A, SECTION D-D,
    ISOMETRIC FRONT RIGHT VIEW — in text, with coordinates. Reading those
    is far more reliable than trying to infer view boundaries from geometry,
    and it yields the name a person would actually use.
    """
    views: list[View] = []
    for index, line in enumerate(lines):
        text = line.text.strip()
        match = _VIEW_LABEL_RE.match(text)
        if not match:
            continue
        kind = next(k for k, v in match.groupdict().items() if v)
        label = match.group(kind)

        # A scale note printed under the label belongs to that view.
        scale = None
        scale_match = re.search(r"SCALE\s*\d+\s*:\s*\d+", text, re.IGNORECASE)
        if scale_match:
            scale = scale_match.group(0)
        else:
            for other in lines[index + 1 : index + 3]:
                if re.match(r"^SCALE\s*\d+\s*:\s*\d+$", other.text.strip(), re.IGNORECASE):
                    if abs(other.bbox[0] - line.bbox[0]) < 60:
                        scale = other.text.strip()
                        break

        views.append(
            View(
                label=label,
                kind=kind,
                anchor=(
                    (line.bbox[0] + line.bbox[2]) / 2.0,
                    (line.bbox[1] + line.bbox[3]) / 2.0,
                ),
                scale=scale,
            )
        )

    # Two labels for the same view (some templates repeat them) collapse.
    unique: dict[str, View] = {}
    for view in views:
        unique.setdefault(view.normalised(), view)
    return list(unique.values())


def analyse_sheet(
    lines: list[TextSpan], page_size: tuple[float, float]
) -> SheetLayout:
    """Full layout analysis for one sheet."""
    return SheetLayout(
        page_size=page_size,
        regions=detect_regions(lines, page_size),
        views=detect_views(lines),
    )


def diff_view_inventory(
    old_layout: SheetLayout, new_layout: SheetLayout
) -> tuple[list[str], list[str]]:
    """
    Which views were added and which were removed.

    Once views are objects this is a set difference rather than an inference
    from scattered line segments — "Section D-D added", "Detail G removed"
    falls straight out.
    """
    old_labels = {view.normalised() for view in old_layout.views}
    new_labels = {view.normalised() for view in new_layout.views}
    return sorted(new_labels - old_labels), sorted(old_labels - new_labels)


# --- title block fields -------------------------------------------------

# Label printed beside each value in the title block. The value sits to the
# right of the label, or directly beneath it, depending on the template.
_FIELD_LABELS: dict[str, tuple[str, ...]] = {
    "drawing_number": ("DRAWING NO", "DWG NO", "DRAWING NUMBER", "PART NO"),
    "title": ("TITLE",),
    "revision": ("REVISION", "REV"),
    "scale": ("SCALE",),
    "sheet": ("SHEET",),
    "size": ("SIZE",),
    "category": ("DWG CATEGORY", "CATEGORY"),
}

# Values that are never a drawing number, however much they look like one.
_NOT_A_DRAWING_NUMBER = re.compile(
    r"^(?:\+?\d[\d\-\s()]{7,}"          # phone numbers
    r"|\d{4}-\d{2}-\d{2}"               # dates
    r"|[A-Z]\d[A-Z]\s*\d[A-Z]\d"        # postal codes
    r"|(?:ASME|ASTM|ANSI|ISO|DIN|MSS|SAE|MIL)\b.*"  # standards
    r"|WWW\..*|.*\.COM.*)$",
    re.IGNORECASE,
)


def _undouble_text(text: str) -> str:
    """Collapse text that the exporter drew twice, character by character.

    Some templates render title-block text with a shadow copy offset by a
    fraction of a point, which arrives as "WWLDLD" rather than "WLD"."""
    def fix(word: str) -> str:
        if len(word) < 4 or len(word) % 2:
            return word
        return word[::2] if all(word[i] == word[i + 1] for i in range(0, len(word), 2)) else word

    # The shadow copy can interleave across word boundaries, giving
    # "WWLDLD,G,GDDRR" — so try the whole string as one doubled sequence
    # before falling back to fixing word by word.
    stripped = text.replace(" ", "")
    if len(stripped) >= 8 and len(stripped) % 2 == 0:
        if all(stripped[i] == stripped[i + 1] for i in range(0, len(stripped), 2)):
            return _respace(stripped[::2])

    words = text.split(" ")
    fixed = [fix(w) for w in words]
    changed = sum(1 for a, b in zip(words, fixed) if a != b)
    return " ".join(fixed) if changed >= max(1, len(words) // 2) else text


def _respace(text: str) -> str:
    """Re-insert the spaces lost when un-interleaving a doubled string."""
    return re.sub(r",(?=\S)", ", ", text)


@dataclass
class TitleBlockFields:
    """Values read from the title block, each beside its own label."""

    drawing_number: str | None = None
    title: str | None = None
    revision: str | None = None
    scale: str | None = None
    sheet: str | None = None
    size: str | None = None
    category: str | None = None

    def describe(self) -> str:
        bits = [b for b in (self.drawing_number, self.title) if b]
        if self.revision:
            bits.append(f"Rev {self.revision}")
        return " — ".join(bits)


def _value_beside(
    label: TextSpan, lines: list[TextSpan], max_gap: float = 200.0
) -> str | None:
    """
    The value belonging to a title-block label.

    Templates place the value either to the right of its label on the same
    baseline, or directly underneath it. Both are tried, nearest first,
    which is far more dependable than pattern-matching the whole title-block
    region — the region also contains a phone number, a postal code, and
    several standards designations, any of which can out-score the real
    drawing number.
    """
    height = max(label.bbox[3] - label.bbox[1], 6.0)
    label_centre_y = (label.bbox[1] + label.bbox[3]) / 2.0

    def is_a_label(text: str) -> bool:
        cleaned = re.sub(r"\s+", " ", text.strip().upper())
        return any(cleaned in names for names in _FIELD_LABELS.values())

    # Directly beneath the label is the commonest arrangement — the label
    # captions a boxed cell — so it is tried first.
    below = [
        line
        for line in lines
        if line is not label
        and 0 < line.bbox[1] - label.bbox[3] <= height * 2.6
        and line.bbox[0] < label.bbox[2] + 40
        and line.bbox[2] > label.bbox[0] - 10
        and not is_a_label(line.text)
    ]
    if below:
        return min(below, key=lambda l: l.bbox[1]).text.strip()

    # Otherwise the value sits to the right on the same baseline. Another
    # label to the right is the next cell's caption, not this cell's value.
    to_right = [
        line
        for line in lines
        if line is not label
        and abs(((line.bbox[1] + line.bbox[3]) / 2.0) - label_centre_y) <= height * 0.7
        and 0 <= line.bbox[0] - label.bbox[2] <= max_gap
        and not is_a_label(line.text)
    ]
    if to_right:
        return min(to_right, key=lambda l: l.bbox[0]).text.strip()
    return None


def extract_title_block_fields(
    lines: list[TextSpan], layout: SheetLayout | None = None
) -> TitleBlockFields:
    """
    Read the title block by its own labels.

    Anchoring each value to the label printed beside it is what stops a
    company phone number being reported as the drawing number: the phone
    number is longer and equally digit-bearing, so any "longest plausible
    token wins" rule picks it every time.
    """
    region = None
    if layout is not None:
        region = next((r for r in layout.regions if r.name == TITLE_BLOCK), None)

    candidates = lines
    if region is not None:
        candidates = [line for line in lines if region.contains(line.bbox)] or lines

    fields = TitleBlockFields()
    for attribute, labels in _FIELD_LABELS.items():
        for line in candidates:
            text = re.sub(r"\s+", " ", line.text.strip().upper())
            if text not in labels:
                continue
            value = _value_beside(line, candidates)
            if not value:
                continue
            value = _undouble_text(value)
            if attribute == "drawing_number":
                if _NOT_A_DRAWING_NUMBER.match(value):
                    continue
                # Templates that place the revision cell hard against the
                # drawing-number cell yield "442079-FAB A"; the trailing
                # single letter is the revision, not part of the number.
                trailing = re.match(r"^(\S+)\s+([A-Z]\d?)$", value)
                if trailing:
                    value = trailing.group(1)
                    if fields.revision is None:
                        fields.revision = trailing.group(2)
            if attribute == "revision" and len(value) > 4:
                continue
            setattr(fields, attribute, value)
            break

    return fields
