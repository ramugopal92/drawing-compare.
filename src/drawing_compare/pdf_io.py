"""
PDF loading, page rasterization, and vector primitive extraction.

This is the module that makes the whole project "vector-first" instead of
"pixel-first" like the original notebook. We pull:

  - vector drawing primitives (lines, curves, rects) via PyMuPDF's
    `page.get_drawings()`
  - text spans with bounding boxes via `page.get_text("dict")`
  - a rasterized image of the page, still needed for: OCR fallback on
    scanned drawings, the alignment step, and the human-facing overlay
    report image.

If a page has essentially no vector content (e.g. it's a scanned drawing
that was flattened to an image), `page_is_vector()` returns False and the
rest of the pipeline automatically falls back to OCR + pixel diffing only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import fitz  # PyMuPDF
import numpy as np
from PIL import Image

from .config import MAX_RASTER_PIXELS, MIN_RENDER_DPI, RENDER_DPI


@dataclass
class TextSpan:
    text: str
    bbox: tuple[float, float, float, float]  # (x0, y0, x1, y1) in PDF points
    font_size: float


@dataclass
class VectorPrimitive:
    kind: str  # "line" | "curve" | "rect" | "quad" | "other"
    bbox: tuple[float, float, float, float]
    stroke_width: float = 0.0


@dataclass
class PageData:
    page_number: int
    page_size_pt: tuple[float, float]  # (width, height)
    text_spans: list[TextSpan] = field(default_factory=list)
    vector_primitives: list[VectorPrimitive] = field(default_factory=list)
    raster_image: np.ndarray | None = None  # BGR, uint8, for cv2 use
    render_dpi: int = RENDER_DPI

    def has_vector_content(self, min_primitives: int = 20) -> bool:
        return len(self.vector_primitives) >= min_primitives


@dataclass
class PageSummary:
    """
    Lightweight description of a page, used to decide *which* pages to
    compare before paying to rasterize any of them.

    Scanning a 40-sheet set this way costs text extraction only — no
    300-DPI pixmaps, no OpenCV — which is what makes multi-page matching
    affordable.
    """

    page_number: int  # 0-based
    page_size_pt: tuple[float, float]
    # Raw text is kept alongside tokens because sheet-number regexes need
    # single characters ("SHEET 1 OF 3") that the tokenizer deliberately
    # discards as noise for similarity scoring.
    text: str = ""
    title_block_text: str = ""
    text_tokens: list[str] = field(default_factory=list)
    body_tokens: list[str] = field(default_factory=list)
    title_block_tokens: list[str] = field(default_factory=list)
    vector_primitive_count: int = 0

    def has_vector_content(self, min_primitives: int = 20) -> bool:
        return self.vector_primitive_count >= min_primitives


_TOKEN_RE = re.compile(r"[A-Za-z0-9.\-_/]+")

# Title block is conventionally the bottom-right corner of the sheet.
TITLE_BLOCK_FRACTION_X = 0.65  # left edge of the region, as a fraction of width
TITLE_BLOCK_FRACTION_Y = 0.70  # top edge of the region, as a fraction of height


def tokenize(text: str) -> list[str]:
    """Uppercased tokens of length > 1 — shared by matching and scanning."""
    return [t for t in _TOKEN_RE.findall(text.upper()) if len(t) > 1]


def scan_pdf_pages(pdf_path: str | Path) -> list[PageSummary]:
    """
    Read every page's text and vector-density without rasterizing.

    Returns one PageSummary per page, in document order.
    """
    pdf_path = Path(pdf_path)
    summaries: list[PageSummary] = []
    with fitz.open(pdf_path) as doc:
        for index, page in enumerate(doc):
            width, height = page.rect.width, page.rect.height
            spans = _extract_text_spans(page)

            tokens: list[str] = []
            body_tokens: list[str] = []
            tb_tokens: list[str] = []
            text_parts: list[str] = []
            tb_parts: list[str] = []
            tb_x = width * TITLE_BLOCK_FRACTION_X
            tb_y = height * TITLE_BLOCK_FRACTION_Y
            for span in spans:
                span_tokens = tokenize(span.text)
                tokens.extend(span_tokens)
                text_parts.append(span.text)
                x0, y0, _, _ = span.bbox
                if x0 >= tb_x and y0 >= tb_y:
                    tb_tokens.extend(span_tokens)
                    tb_parts.append(span.text)
                else:
                    # Body tokens only, for similarity scoring: title blocks
                    # are nearly identical across every sheet in a set, so
                    # including them makes unrelated sheets look alike.
                    body_tokens.extend(span_tokens)

            try:
                vector_count = sum(len(p.get("items", [])) for p in page.get_drawings())
            except Exception:
                vector_count = 0

            summaries.append(
                PageSummary(
                    page_number=index,
                    page_size_pt=(width, height),
                    text=" ".join(text_parts),
                    title_block_text=" ".join(tb_parts),
                    text_tokens=tokens,
                    body_tokens=body_tokens,
                    title_block_tokens=tb_tokens,
                    vector_primitive_count=vector_count,
                )
            )
    return summaries


def load_pdf_page(pdf_path: str | Path, page_index: int = 0) -> PageData:
    """
    Load a single page from a PDF and extract everything downstream stages
    need: vector primitives, text spans, and a rasterized image.

    page_index is 0-based.
    """
    pdf_path = Path(pdf_path)
    doc = fitz.open(pdf_path)
    if page_index >= len(doc):
        raise IndexError(
            f"{pdf_path.name} has {len(doc)} page(s); page_index={page_index} is out of range."
        )
    page = doc[page_index]

    text_spans = _extract_text_spans(page)
    vector_primitives = _extract_vector_primitives(page)
    page_size = (page.rect.width, page.rect.height)
    dpi = effective_dpi(page_size)
    raster_image = _rasterize_page(page, dpi=dpi)

    return PageData(
        page_number=page_index,
        page_size_pt=page_size,
        text_spans=text_spans,
        vector_primitives=vector_primitives,
        raster_image=raster_image,
        render_dpi=dpi,
    )


def _extract_text_spans(page: "fitz.Page") -> list[TextSpan]:
    spans: list[TextSpan] = []
    raw = page.get_text("dict")
    for block in raw.get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                text = span.get("text", "").strip()
                if not text:
                    continue
                spans.append(
                    TextSpan(
                        text=text,
                        bbox=tuple(span["bbox"]),
                        font_size=span.get("size", 0.0),
                    )
                )
    return spans


def _extract_vector_primitives(page: "fitz.Page") -> list[VectorPrimitive]:
    """
    PyMuPDF's get_drawings() returns a list of "paths", each with a list of
    drawing "items" (lines, curves, rects). We flatten these into simple
    bounding-box primitives for diffing. This intentionally throws away
    fine detail (exact curve control points) in favor of something robust
    enough to diff — good enough to say "something changed near (x, y)"
    and let the geometry-diff stage classify it.
    """
    primitives: list[VectorPrimitive] = []
    try:
        drawings = page.get_drawings()
    except Exception:
        return primitives

    for path in drawings:
        stroke_width = path.get("width") or 0.0
        for item in path.get("items", []):
            op = item[0]
            bbox = _bbox_from_item(item)
            if bbox is None:
                continue
            kind = {
                "l": "line",
                "c": "curve",
                "re": "rect",
                "qu": "quad",
            }.get(op, "other")
            primitives.append(
                VectorPrimitive(kind=kind, bbox=bbox, stroke_width=stroke_width)
            )
    return primitives


def _bbox_from_item(item) -> tuple[float, float, float, float] | None:
    """Compute a bounding box for a single PyMuPDF drawing item."""
    op = item[0]
    points: list[tuple[float, float]] = []

    if op == "l":  # line: (op, p1, p2)
        points = [tuple(item[1]), tuple(item[2])]
    elif op == "re":  # rect: (op, Rect)
        r = item[1]
        points = [(r.x0, r.y0), (r.x1, r.y1)]
    elif op == "c":  # bezier curve: (op, p1, p2, p3, p4)
        points = [tuple(p) for p in item[1:5]]
    elif op == "qu":  # quad: (op, Quad)
        q = item[1]
        points = [tuple(q.ul), tuple(q.ur), tuple(q.ll), tuple(q.lr)]
    else:
        return None

    if not points:
        return None

    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (min(xs), min(ys), max(xs), max(ys))


def effective_dpi(page_size_pt: tuple[float, float], dpi: int = RENDER_DPI) -> int:
    """
    Reduce DPI for large sheets so the raster stays under MAX_RASTER_PIXELS.

    Returns the requested DPI unchanged for normal-sized sheets.
    """
    width_pt, height_pt = page_size_pt
    pixels = (width_pt / 72.0 * dpi) * (height_pt / 72.0 * dpi)
    if pixels <= MAX_RASTER_PIXELS:
        return dpi
    scale = (MAX_RASTER_PIXELS / pixels) ** 0.5
    return max(MIN_RENDER_DPI, int(dpi * scale))


def _rasterize_page(page: "fitz.Page", dpi: int = RENDER_DPI) -> np.ndarray:
    zoom = dpi / 72.0
    matrix = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=matrix, alpha=False)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    arr = np.array(img)  # RGB
    return arr[:, :, ::-1].copy()  # convert to BGR for OpenCV consistency


def pdf_page_count(pdf_path: str | Path) -> int:
    with fitz.open(Path(pdf_path)) as doc:
        return len(doc)
