"""
Page matching for multi-page drawing sets.

The hard part of multi-page comparison is *not* running the diff N times —
it's deciding which old page corresponds to which new page. In a real
revision, sheets get inserted, deleted, and reordered, so page 3 of Rev A
is very often not page 3 of Rev B.

Three strategies, in the order "auto" tries them:

  1. sheet_label — read the title block text ("SHEET 2 OF 5", "SH. 2/5",
     and the drawing number) and pair sheets that declare the same
     identity. Most reliable when the title block is real text (vector
     PDF exported from SolidWorks/AutoCAD).
  2. content    — pair by text-token similarity (Jaccard) of the whole
     page. Works when the title block is scanned/flattened or uses a
     format the regexes don't know. Greedy best-first assignment.
  3. sequential — page i of old vs page i of new. The dumb fallback, and
     the right answer when both PDFs are the same sheet set in the same
     order.

Whatever is left unpaired is reported as an added sheet (new only) or a
removed sheet (old only) rather than being force-matched to something
unrelated.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .pdf_io import PageSummary

# "SHEET 2 OF 5", "SHEET 2/5", "SH 2 OF 5", "SH. 2/5"
_SHEET_OF_RE = re.compile(
    r"\bSH(?:EET|T|\.)?\s*[:.]?\s*(\d{1,3})\s*(?:OF|/|-)\s*(\d{1,3})\b", re.IGNORECASE
)
# Bare "SHEET 2" with no total
_SHEET_BARE_RE = re.compile(r"\bSH(?:EET|T|\.)?\s*[:.]?\s*(\d{1,3})\b", re.IGNORECASE)
# Drawing/part numbers: 3+ chars, digits with separators, e.g. 12345-678, A-1024_B
# The digit lookahead is scoped to [A-Z0-9\-_/]* — characters the match
# itself can consume — not to the unbounded ".*". An unscoped ".*\d" only
# asks "does a digit exist anywhere later in the whole string", which any
# word before a part number satisfies; "WHITEWATER" and "INDUSTRIES" both
# wrongly qualified as drawing-number candidates purely because a real
# drawing number happened to appear later in the same title block, and
# outscored it on length once PyMuPDF's kerning joined "WHITEWATER" and
# "WEST" into one 14-character token.
_DRAWING_NO_RE = re.compile(r"\b(?=[A-Z0-9\-_/]*\d)[A-Z0-9][A-Z0-9\-_/]{4,}\b")

# Excludes phone numbers, dates, and postal codes from the drawing-number
# guess. This is a secondary path (layout.extract_title_block_fields is the
# primary, label-anchored reader) — kept as a safety net so that if the
# primary reader ever returns nothing, this one cannot silently regress to
# reporting a phone number as the drawing identity, as it did before this
# guard existed.
# Phone numbers specifically, not any digit-and-hyphen string — an ordinary
# drawing number like "12345-678" is also all digits and hyphens, so the
# exclusion has to key on what makes a phone number recognisable: ten or
# more digits in total, which a typical drawing number does not reach.
def _looks_like_phone_number(token: str) -> bool:
    digits = sum(c.isdigit() for c in token)
    return digits >= 10 and bool(re.match(r"^\+?[\d\-\s()]+$", token))


_NOT_A_DRAWING_NUMBER_DATE_OR_ZIP = re.compile(
    r"^(?:\d{4}-\d{2}-\d{2}|[A-Z]\d[A-Z]\s*\d[A-Z]\d)$", re.IGNORECASE
)


def _not_a_drawing_number(token: str) -> bool:
    return _looks_like_phone_number(token) or bool(
        _NOT_A_DRAWING_NUMBER_DATE_OR_ZIP.match(token)
    )

# Below this Jaccard score we refuse to call two pages the same sheet.
CONTENT_MATCH_THRESHOLD = 0.40


@dataclass(frozen=True)
class SheetIdentity:
    """What a page claims to be, read off its title block."""

    sheet_number: int | None = None
    sheet_total: int | None = None
    drawing_number: str | None = None

    def key(self) -> tuple | None:
        """A hashable identity, or None if the page didn't declare one."""
        if self.drawing_number and self.sheet_number is not None:
            return ("dwg_sheet", self.drawing_number, self.sheet_number)
        if self.drawing_number:
            return ("dwg", self.drawing_number)
        if self.sheet_number is not None:
            return ("sheet", self.sheet_number)
        return None

    def label(self) -> str:
        bits = []
        if self.drawing_number:
            bits.append(self.drawing_number)
        if self.sheet_number is not None:
            if self.sheet_total is not None:
                bits.append(f"sheet {self.sheet_number} of {self.sheet_total}")
            else:
                bits.append(f"sheet {self.sheet_number}")
        return " — ".join(bits)


@dataclass
class PagePair:
    """One old page matched to one new page. Either side may be None."""

    old_index: int | None
    new_index: int | None
    method: str  # "sheet_label" | "content" | "sequential" | "added" | "removed"
    score: float = 1.0
    old_identity: SheetIdentity | None = None
    new_identity: SheetIdentity | None = None

    @property
    def is_pair(self) -> bool:
        return self.old_index is not None and self.new_index is not None

    def label(self) -> str:
        ident = self.old_identity or self.new_identity
        ident_txt = ident.label() if ident else ""
        if self.old_index is None:
            base = f"Added sheet (new p.{self.new_index + 1})"
        elif self.new_index is None:
            base = f"Removed sheet (old p.{self.old_index + 1})"
        else:
            base = f"Old p.{self.old_index + 1} \u2192 New p.{self.new_index + 1}"
        return f"{base} — {ident_txt}" if ident_txt else base


@dataclass
class MatchPlan:
    pairs: list[PagePair] = field(default_factory=list)

    @property
    def matched(self) -> list[PagePair]:
        return [p for p in self.pairs if p.is_pair]

    @property
    def added(self) -> list[PagePair]:
        return [p for p in self.pairs if p.old_index is None]

    @property
    def removed(self) -> list[PagePair]:
        return [p for p in self.pairs if p.new_index is None]

    def summary(self) -> str:
        return (
            f"{len(self.matched)} sheet(s) matched, "
            f"{len(self.added)} added, {len(self.removed)} removed"
        )


def extract_identity(summary: PageSummary) -> SheetIdentity:
    """
    Read sheet number / drawing number out of a page's text.

    We look at the whole page text for the sheet number (cheap, and
    "SHEET n OF m" is unambiguous enough), but restrict drawing-number
    hunting to the title-block region — the bottom-right ~35% x 30% of
    the sheet — so we don't pick up a random callout from the body.
    """
    # Raw text, not tokens: "SHEET 1 OF 3" has single-character numbers
    # that the tokenizer drops.
    joined = summary.text.upper() or " ".join(summary.text_tokens)

    sheet_number = sheet_total = None
    m = _SHEET_OF_RE.search(joined)
    if m:
        sheet_number, sheet_total = int(m.group(1)), int(m.group(2))
    else:
        m = _SHEET_BARE_RE.search(joined)
        if m:
            sheet_number = int(m.group(1))

    drawing_number = None
    tb_text = summary.title_block_text.upper() or " ".join(summary.title_block_tokens).upper()
    candidates = [
        c for c in _DRAWING_NO_RE.findall(tb_text) if not _not_a_drawing_number(c)
    ]
    if candidates:
        # Longest candidate is the most specific — favours "12345-678-A"
        # over the "12345" that a looser pattern would also match.
        drawing_number = max(candidates, key=len)

    return SheetIdentity(
        sheet_number=sheet_number, sheet_total=sheet_total, drawing_number=drawing_number
    )


def _content_tokens(summary: PageSummary) -> set[str]:
    """Tokens to score similarity on — body text if we have it, else
    everything. Title-block text is excluded because it barely varies
    between sheets of the same drawing and would make every sheet in a
    set look like every other."""
    return set(summary.body_tokens) or set(summary.text_tokens)


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def match_pages(
    old_pages: list[PageSummary],
    new_pages: list[PageSummary],
    mode: str = "auto",
) -> MatchPlan:
    """
    Build the old-page -> new-page pairing.

    mode: "auto" | "sheet_label" | "content" | "sequential"
    """
    if mode not in {"auto", "sheet_label", "content", "sequential"}:
        raise ValueError(f"Unknown match mode: {mode!r}")

    old_ids = [extract_identity(p) for p in old_pages]
    new_ids = [extract_identity(p) for p in new_pages]

    pairs: list[PagePair] = []
    used_old: set[int] = set()
    used_new: set[int] = set()

    def commit(oi: int, ni: int, method: str, score: float) -> None:
        used_old.add(oi)
        used_new.add(ni)
        pairs.append(
            PagePair(
                old_index=oi,
                new_index=ni,
                method=method,
                score=score,
                old_identity=old_ids[oi],
                new_identity=new_ids[ni],
            )
        )

    # --- Pass 1: declared sheet identity -------------------------------
    if mode in {"auto", "sheet_label"}:
        new_by_key: dict[tuple, list[int]] = {}
        for ni, ident in enumerate(new_ids):
            k = ident.key()
            if k is not None:
                new_by_key.setdefault(k, []).append(ni)

        for oi, ident in enumerate(old_ids):
            k = ident.key()
            if k is None:
                continue
            for ni in new_by_key.get(k, []):
                if ni not in used_new:
                    commit(oi, ni, "sheet_label", 1.0)
                    break

    # --- Pass 2: content similarity ------------------------------------
    if mode in {"auto", "content"}:
        scored: list[tuple[float, int, int]] = []
        for oi, op in enumerate(old_pages):
            if oi in used_old:
                continue
            for ni, np_ in enumerate(new_pages):
                if ni in used_new:
                    continue
                s = _jaccard(_content_tokens(op), _content_tokens(np_))
                if s >= CONTENT_MATCH_THRESHOLD:
                    scored.append((s, oi, ni))
        # Greedy best-first: highest similarity claims its pair first.
        for s, oi, ni in sorted(scored, key=lambda t: -t[0]):
            if oi in used_old or ni in used_new:
                continue
            commit(oi, ni, "content", s)

    # --- Pass 3: positional fallback -----------------------------------
    # In "sequential" mode this is the whole algorithm, so it pairs
    # everything by index. In "auto" it is only a last resort, and we
    # deliberately do NOT force-match a page that declared a sheet
    # identity which found no counterpart: a sheet that says "SHEET 3 OF 5"
    # when the new set has no sheet 3 was almost certainly deleted, and
    # diffing it against whatever happens to sit at the same index would
    # produce hundreds of meaningless differences.
    if mode in {"auto", "sequential"}:
        strict = mode == "auto"
        for oi in range(len(old_pages)):
            if oi in used_old:
                continue
            if oi >= len(new_pages) or oi in used_new:
                continue
            if strict and (old_ids[oi].key() is not None or new_ids[oi].key() is not None):
                continue
            commit(oi, oi, "sequential", 0.0)

    # --- Leftovers = added / removed sheets ----------------------------
    for oi in range(len(old_pages)):
        if oi not in used_old:
            pairs.append(
                PagePair(oi, None, "removed", 0.0, old_identity=old_ids[oi])
            )
    for ni in range(len(new_pages)):
        if ni not in used_new:
            pairs.append(
                PagePair(None, ni, "added", 0.0, new_identity=new_ids[ni])
            )

    # Present in reading order of the new document, with removals last.
    pairs.sort(
        key=lambda p: (
            p.new_index if p.new_index is not None else 10_000 + (p.old_index or 0)
        )
    )
    return MatchPlan(pairs=pairs)
