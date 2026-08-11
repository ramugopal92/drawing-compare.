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
    columns: int = 8          # numbered, typically right-to-left on the sheet
    rows: str = "ABCD"        # lettered, top-to-bottom
    columns_right_to_left: bool = True


ZONE_GRID = ZoneGridConfig()

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

# A cluster made of fewer primitives than this is usually noise (a hatch
# fragment, a rounding artifact) rather than a real design change.
GEOMETRY_MIN_CLUSTER_PRIMITIVES = 2

# Reports list at most this many rows per sheet; the rest are summarized.
# A report an engineer cannot scroll through is a report nobody reads.
MAX_REPORT_ROWS_PER_SHEET = 300

# Text/OCR diff: two text spans are considered a positional match if their
# centers are within this many PDF points (1/72 inch) after alignment.
TEXT_POSITION_TOLERANCE_PT = 12.0

# Text/OCR diff: minimum fuzzy-match ratio (0-100, rapidfuzz) to call two
# text strings "the same" (small OCR noise allowed) vs. "changed".
TEXT_FUZZY_MATCH_THRESHOLD = 85

# OCR ensemble: minimum agreement (fraction of engines) before we accept a
# token without flagging it as low-confidence.
OCR_MIN_ENGINE_AGREEMENT = 0.5
