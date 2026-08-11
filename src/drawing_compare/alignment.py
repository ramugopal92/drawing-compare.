"""
Alignment between the "old" and "new" rasterized pages.

Even when both drawings are the same nominal sheet size, small offsets creep
in (different plot margins, a slightly different export DPI, a shifted
title block). We compute a homography from the new page's raster image onto
the old page's, using ORB feature matching, and reuse that same
transform to correct vector-primitive and text-span coordinates before
diffing — not just the raster image.

If not enough good matches are found we fall back to identity (no
correction) rather than guessing, and flag that in the result so the report
can warn the user alignment may be unreliable.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from .config import MIN_ALIGNMENT_MATCHES


@dataclass
class AlignmentResult:
    homography: np.ndarray  # 3x3, maps NEW page coords -> OLD page coords
    good_matches: int
    reliable: bool


def compute_alignment(old_image: np.ndarray, new_image: np.ndarray) -> AlignmentResult:
    old_gray = cv2.cvtColor(old_image, cv2.COLOR_BGR2GRAY)
    new_gray = cv2.cvtColor(new_image, cv2.COLOR_BGR2GRAY)

    orb = cv2.ORB_create(nfeatures=4000)
    kp1, des1 = orb.detectAndCompute(old_gray, None)
    kp2, des2 = orb.detectAndCompute(new_gray, None)

    if des1 is None or des2 is None or len(kp1) < 4 or len(kp2) < 4:
        return AlignmentResult(homography=np.eye(3), good_matches=0, reliable=False)

    matcher = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=False)
    raw_matches = matcher.knnMatch(des2, des1, k=2)  # new -> old

    good = []
    for pair in raw_matches:
        if len(pair) != 2:
            continue
        m, n = pair
        if m.distance < 0.75 * n.distance:  # Lowe's ratio test
            good.append(m)

    if len(good) < MIN_ALIGNMENT_MATCHES:
        return AlignmentResult(
            homography=np.eye(3), good_matches=len(good), reliable=False
        )

    src_pts = np.float32([kp2[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
    dst_pts = np.float32([kp1[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)

    homography, mask = cv2.findHomography(src_pts, dst_pts, cv2.RANSAC, 5.0)
    if homography is None:
        return AlignmentResult(
            homography=np.eye(3), good_matches=len(good), reliable=False
        )

    inliers = int(mask.sum()) if mask is not None else 0
    return AlignmentResult(
        homography=homography,
        good_matches=inliers,
        reliable=inliers >= MIN_ALIGNMENT_MATCHES,
    )


def transform_point(homography: np.ndarray, x: float, y: float) -> tuple[float, float]:
    """Apply a homography (computed on pixel coords) to a single point."""
    pt = np.array([x, y, 1.0])
    result = homography @ pt
    result /= result[2]
    return float(result[0]), float(result[1])


def transform_bbox(
    homography: np.ndarray, bbox: tuple[float, float, float, float]
) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = bbox
    corners = [(x0, y0), (x1, y0), (x0, y1), (x1, y1)]
    transformed = [transform_point(homography, x, y) for x, y in corners]
    xs = [p[0] for p in transformed]
    ys = [p[1] for p in transformed]
    return (min(xs), min(ys), max(xs), max(ys))


def pdf_points_to_pixels(
    bbox_pt: tuple[float, float, float, float], dpi: int
) -> tuple[float, float, float, float]:
    """Convert a bbox in PDF points (1/72 in) to pixel coords at the given DPI."""
    scale = dpi / 72.0
    x0, y0, x1, y1 = bbox_pt
    return (x0 * scale, y0 * scale, x1 * scale, y1 * scale)


def pixels_to_pdf_points(
    bbox_px: tuple[float, float, float, float], dpi: int
) -> tuple[float, float, float, float]:
    scale = 72.0 / dpi
    x0, y0, x1, y1 = bbox_px
    return (x0 * scale, y0 * scale, x1 * scale, y1 * scale)
