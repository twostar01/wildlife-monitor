"""
image_quality.py — Score animal crop images for display quality
"""

import cv2
import numpy as np
from pathlib import Path
from typing import Optional
import logging

log = logging.getLogger("wildlife_processor")

# Weight each quality component (must sum to 1.0)
WEIGHTS = {
    "sharpness":  0.40,
    "brightness": 0.20,
    "contrast":   0.20,
    "size":       0.20,
}

# Min crop size to bother scoring (pixels, width × height)
MIN_AREA = 32 * 32


def score_image(image_path: str) -> Optional[dict]:
    """
    Score a crop image for display quality.
    Returns a dict with individual metrics and an overall 0-100 score,
    or None if the image can't be read.
    """
    img = cv2.imread(image_path)
    if img is None:
        return None

    h, w = img.shape[:2]
    area = w * h

    if area < MIN_AREA:
        return {
            "quality_score": 0.0,
            "sharpness": 0.0,
            "brightness": 0.0,
            "contrast": 0.0,
            "pixel_area": area,
            "width": w,
            "height": h,
        }

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    # ── Sharpness: Laplacian variance ──────────────────────────────────────────
    lap_var = cv2.Laplacian(gray, cv2.CV_64F).var()
    # Normalise: 0 = completely blurry, 500+ = very sharp
    sharpness_norm = float(min(lap_var / 500.0, 1.0))

    # ── Brightness: penalise under/over-exposed images ─────────────────────────
    mean_brightness = float(np.mean(gray))
    # Ideal range 60–190. Score peaks at 128, falls to 0 at 0 or 255.
    brightness_norm = float(1.0 - abs(mean_brightness - 128.0) / 128.0)
    brightness_norm = max(0.0, brightness_norm)

    # ── Contrast: standard deviation of pixel values ───────────────────────────
    std_dev = float(np.std(gray))
    # Normalise: 0 = flat, 80+ = high contrast
    contrast_norm = float(min(std_dev / 80.0, 1.0))

    # ── Size: reward larger crops ──────────────────────────────────────────────
    # Full HD frame ~= 2 million pixels, anything > 200k is "large"
    size_norm = float(min(area / 200_000.0, 1.0))

    # ── Combined score ─────────────────────────────────────────────────────────
    score = (
        WEIGHTS["sharpness"]  * sharpness_norm +
        WEIGHTS["brightness"] * brightness_norm +
        WEIGHTS["contrast"]   * contrast_norm +
        WEIGHTS["size"]       * size_norm
    ) * 100.0

    return {
        "quality_score": round(score, 2),
        "sharpness":     round(sharpness_norm * 100, 2),
        "brightness":    round(brightness_norm * 100, 2),
        "contrast":      round(contrast_norm * 100, 2),
        "pixel_area":    area,
        "width":         w,
        "height":        h,
    }


def score_images_batch(image_paths: list[str]) -> list[Optional[dict]]:
    return [score_image(p) for p in image_paths]


def rank_crops_by_quality(image_paths: list[str]) -> list[tuple[str, float]]:
    """Return [(path, score), ...] sorted by score descending."""
    scored = []
    for p in image_paths:
        result = score_image(p)
        scored.append((p, result["quality_score"] if result else 0.0))
    return sorted(scored, key=lambda x: -x[1])
