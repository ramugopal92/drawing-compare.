"""
Central configuration: zone grid layout, matching thresholds, rendering DPI.

Tune these against your actual title-block/border standard. Most western
mechanical drawings use either:
  - ISO 8-column x 4- or 6-row grid (columns numbered right-to-left: 8..1,
    rows lettered top-to-bottom: A..D or A..F)
  - ANSI zoning, similar idea but conventions vary by company

The screenshot you shared used zones like "C1", "B5", "D5" — an
8-column x 4-row (A-D) grid — so that's the default here.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ZoneGridConfig:
    columns: int = 4          # numbered, typically right-to-left on the sheet
    rows: str = "ABCD"        # lettered, top-to-bottom
    columns_right_to_left: bool = True


# Default grid. Overridden per sheet by zones.detect_zone_grid() when the
# border reference characters can be read off the sheet edges, so a set
# that uses a different standard is handled without editing this file.
ZONE_GRID = ZoneGridConfig()

# Zone detection: how far into the sheet (as a fraction of width/height) to
# look for the border reference characters printed along the edges.
ZONE_BORDER_MARGIN = 0.06

# Geometry diff: primitives lying inside a text bounding box are glyph
# outlines, not drawing geometry. Some CAD exports emit certain fonts as
# filled vector paths, which otherwise floods the geometry diff with
# hundreds of "changes" every time a note is reworded. Text bounding boxes
# are inflated by this many points before the test.
TEXT_MASK_PADDING_PT = 1.5

# Rendering resolution when we rasterize a PDF page (for OCR fallback,
# alignment feature matching, and the overlay report image).
RENDER_DPI = 300

# Hard ceiling on rasterized page area. An A1 sheet at 300 DPI is ~70
# megapixels — 209 MB per raster, three of which are live during a
# comparison. ORB feature detection on an image that size is also the
# single slowest step in the pipeline. Sheets larger than this are
# rendered at a reduced DPI instead; alignment quality is unaffected
# because ORB finds plenty of features at 150 DPI, and the vector layer
# (not the raster) is what actually gets diffed.
MAX_RASTER_PIXELS = 12_000_000
MIN_RENDER_DPI = 100

# Alignment: minimum number of good feature matches before we trust a
# homography; below this we fall back to identity (no alignment).
MIN_ALIGNMENT_MATCHES = 15

# Geometry diff: two vector primitives are "the same object" if their
# bounding-box corners agree within this many PDF points after alignment.
# Corner distance is used rather than bounding-box IoU because a horizontal
# or vertical line has a zero-area bounding box, which makes IoU
# structurally 0 even against an identical copy of itself — and most lines
# in an engineering drawing are axis-aligned.
GEOMETRY_MATCH_TOLERANCE_PT = 1.0

# Geometry diff: unmatched primitives closer together than this are treated
# as one engineering change rather than N separate rows. Moving one feature
# shifts its outline, its dimension lines, and its leaders together; an
# engineer wants to review that as a single edit.
GEOMETRY_CLUSTER_GAP_PT = 10.0

# Two clusters this close, one removed and one added, are treated as the
# same feature edited in place rather than as an unrelated deletion plus an
# unrelated addition. Real revisions almost never delete something and add
# an unrelated thing of the same size in the same spot.
CLUSTER_PAIR_MAX_DISTANCE_PT = 60.0

# ...and only if their primitive counts are within this ratio of each other.
CLUSTER_PAIR_COUNT_RATIO = 0.5

# A leftover removed line and added line this close together are the same
# line of text edited in place, however different the wording. Without this
# a part number change reads as an unexplained deletion next to an
# unexplained addition, and the engineer has to pair them up by eye.
TEXT_PAIR_MAX_DISTANCE_PT = 30.0

# Lines further apart than TEXT_PAIR_MAX_DISTANCE_PT can still be the same
# line edited, provided the wording is recognisably similar. A parts-list
# row that gains a parenthetical ("... 5/8 (0.656 ID X 1.25 OD)") wraps
# differently and its centre shifts well past the positional limit, so
# without this the row reads as a deletion beside an unrelated addition.
TEXT_PAIR_FALLBACK_DISTANCE_PT = 140.0
TEXT_PAIR_FALLBACK_SIMILARITY = 55

# Text rendered as vector outlines (some CAD exports do this for certain
# fonts) produces clusters of hundreds of tiny paths inside a few square
# points. No real design feature has that density, so clusters above this
# many primitives per square point are discarded as glyph outlines.
GEOMETRY_MAX_CLUSTER_DENSITY = 1.0

# --- bill of materials -------------------------------------------------
# Cells of one parts-list row share a baseline to within this many points.
BOM_BASELINE_TOLERANCE_PT = 3.0

# Fewer rows than this and it is not a table — a couple of stray integers
# beside some text is prose, and treating it as a parts list produces
# nonsense columns.
BOM_MIN_ROWS = 3

# Cells of one column line up vertically to within this many points.
BOM_COLUMN_TOLERANCE_PT = 6.0

# How far a wrapped continuation line may be indented past the start of the
# description column and still belong to that row.
BOM_DESCRIPTION_INDENT_PT = 30.0

# Dual dimensioning adds a metric equivalent beneath every imperial
# dimension on the sheet. That is one drafting decision, not N hundred
# changes, and listing each one buries the revision it was made alongside.
# Below this count they are listed individually; at or above it they are
# collapsed into a single row.
DUAL_DIMENSION_AGGREGATE_THRESHOLD = 8

# Fragments closer together than this fraction of the font height were not
# separated on the page — they are one word split into glyphs by the
# exporter, and joining them with a space produces "5 2 4 . 5 4".
GLYPH_JOIN_GAP_RATIO = 0.10

# Line-weight-only changes are usually a plotting difference between the two
# exports, not a design change, and they can easily outnumber real changes
# by 10:1. Off by default; when disabled they are still counted and reported
# as a single sheet-level note.
REPORT_LINE_WEIGHT_CHANGES = False

# A cluster made of fewer primitives than this is usually noise (a hatch
# fragment, a rounding artifact) rather than a real design change.
GEOMETRY_MIN_CLUSTER_PRIMITIVES = 2

# Reports list at most this many rows per sheet; the rest are summarized.
# A report an engineer cannot scroll through is a report nobody reads.
MAX_REPORT_ROWS_PER_SHEET = 300

# Text/OCR diff: two text spans are considered a positional match if their
# centers are within this many PDF points (1/72 inch) after alignment.
TEXT_POSITION_TOLERANCE_PT = 20.0

# Text/OCR diff: minimum fuzzy-match ratio (0-100, rapidfuzz) to call two
# text strings "the same" (small OCR noise allowed) vs. "changed".
TEXT_FUZZY_MATCH_THRESHOLD = 85

# OCR ensemble: minimum agreement (fraction of engines) before we accept a
# token without flagging it as low-confidence.
OCR_MIN_ENGINE_AGREEMENT = 0.5
