"""
OCR ensemble: run more than one OCR engine over a region and merge results,
targeting the exact "OCR noise introduces false token candidates" limitation
called out in the original notebook's own findings.

Currently wired up: Tesseract (via pytesseract) and EasyOCR. Both are
optional at runtime — if a package/binary isn't installed, that engine is
skipped rather than crashing the pipeline (useful while you're still
setting up your environment, or deliberately running "light" without
EasyOCR's model download).

Adding PaddleOCR later is a matter of writing one more `_run_paddleocr()`
function with the same return shape and adding it to `ENGINES`.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from rapidfuzz import fuzz

from .config import OCR_MIN_ENGINE_AGREEMENT


@dataclass
class OcrToken:
    text: str
    bbox_px: tuple[float, float, float, float]  # in the coordinate space of the crop passed in
    engine: str
    confidence: float  # 0-100


@dataclass
class ConsensusToken:
    text: str
    bbox_px: tuple[float, float, float, float]
    engines_agreeing: list[str]
    mean_confidence: float
    low_confidence: bool


def _run_tesseract(image: np.ndarray) -> list[OcrToken]:
    try:
        import pytesseract
    except ImportError:
        return []

    try:
        data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT)
    except Exception:
        # Most likely the tesseract binary isn't installed/on PATH.
        return []

    tokens: list[OcrToken] = []
    n = len(data.get("text", []))
    for i in range(n):
        text = data["text"][i].strip()
        if not text:
            continue
        conf_raw = data["conf"][i]
        try:
            conf = float(conf_raw)
        except (TypeError, ValueError):
            conf = -1.0
        if conf < 0:
            continue
        x, y, w, h = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
        tokens.append(
            OcrToken(
                text=text,
                bbox_px=(x, y, x + w, y + h),
                engine="tesseract",
                confidence=conf,
            )
        )
    return tokens


_EASYOCR_READER = None


def _get_easyocr_reader():
    global _EASYOCR_READER
    if _EASYOCR_READER is None:
        try:
            import easyocr
        except ImportError:
            return None
        _EASYOCR_READER = easyocr.Reader(["en"], gpu=False, verbose=False)
    return _EASYOCR_READER


def _run_easyocr(image: np.ndarray) -> list[OcrToken]:
    reader = _get_easyocr_reader()
    if reader is None:
        return []

    try:
        results = reader.readtext(image)
    except Exception:
        return []

    tokens: list[OcrToken] = []
    for bbox_points, text, conf in results:
        text = text.strip()
        if not text:
            continue
        xs = [p[0] for p in bbox_points]
        ys = [p[1] for p in bbox_points]
        tokens.append(
            OcrToken(
                text=text,
                bbox_px=(min(xs), min(ys), max(xs), max(ys)),
                engine="easyocr",
                confidence=float(conf) * 100.0,
            )
        )
    return tokens


ENGINES = {
    "tesseract": _run_tesseract,
    "easyocr": _run_easyocr,
}


def run_ocr_ensemble(image: np.ndarray, engines: list[str] | None = None) -> list[ConsensusToken]:
    """
    Run each requested engine over `image` (a numpy array crop, BGR or gray)
    and merge overlapping detections into consensus tokens.

    engines: subset of ENGINES.keys(); defaults to all available.
    """
    engines = engines or list(ENGINES.keys())
    all_tokens: list[OcrToken] = []
    engines_run = []
    for name in engines:
        fn = ENGINES.get(name)
        if fn is None:
            continue
        tokens = fn(image)
        if tokens:
            engines_run.append(name)
        all_tokens.extend(tokens)

    if not all_tokens:
        return []

    return _merge_tokens(all_tokens, num_engines_run=max(len(engines_run), 1))


def _iou(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    inter = (ix1 - ix0) * (iy1 - iy0)
    area_a = (ax1 - ax0) * (ay1 - ay0)
    area_b = (bx1 - bx0) * (by1 - by0)
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def _merge_tokens(tokens: list[OcrToken], num_engines_run: int) -> list[ConsensusToken]:
    """
    Greedy clustering: group tokens whose boxes overlap significantly and
    whose text is a close fuzzy match, then take the highest-confidence
    text as the cluster's representative.
    """
    clusters: list[list[OcrToken]] = []
    for tok in tokens:
        placed = False
        for cluster in clusters:
            rep = cluster[0]
            if _iou(rep.bbox_px, tok.bbox_px) > 0.3 and fuzz.ratio(rep.text, tok.text) > 70:
                cluster.append(tok)
                placed = True
                break
        if not placed:
            clusters.append([tok])

    consensus: list[ConsensusToken] = []
    for cluster in clusters:
        best = max(cluster, key=lambda t: t.confidence)
        engines_agreeing = sorted({t.engine for t in cluster})
        mean_conf = sum(t.confidence for t in cluster) / len(cluster)
        agreement_fraction = len(engines_agreeing) / num_engines_run
        consensus.append(
            ConsensusToken(
                text=best.text,
                bbox_px=best.bbox_px,
                engines_agreeing=engines_agreeing,
                mean_confidence=mean_conf,
                low_confidence=agreement_fraction < OCR_MIN_ENGINE_AGREEMENT,
            )
        )
    return consensus
