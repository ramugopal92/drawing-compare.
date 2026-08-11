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

# Alignment: minimum number of good feature matches before we trust a
# homography; below this we fall back to identity (no alignment).
MIN_ALIGNMENT_MATCHES = 15

# Geometry diff: two vector primitives are considered "the same object,
# possibly moved slightly" if their bounding boxes overlap by at least this
# IoU (intersection over union) after alignment.
GEOMETRY_MATCH_IOU = 0.55

# Text/OCR diff: two text spans are considered a positional match if their
# centers are within this many PDF points (1/72 inch) after alignment.
TEXT_POSITION_TOLERANCE_PT = 12.0

# Text/OCR diff: minimum fuzzy-match ratio (0-100, rapidfuzz) to call two
# text strings "the same" (small OCR noise allowed) vs. "changed".
TEXT_FUZZY_MATCH_THRESHOLD = 85

# OCR ensemble: minimum agreement (fraction of engines) before we accept a
# token without flagging it as low-confidence.
OCR_MIN_ENGINE_AGREEMENT = 0.5
