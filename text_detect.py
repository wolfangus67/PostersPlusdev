"""
text_detect.py — burned-in title-text detection for mislabelled posters.

TMDB often tags a poster as language-neutral ("textless") when it actually has
the title burned in.  PostersPlus would then composite its own logo on top,
producing a double title.  This module flags such posters so the caller can
skip its own overlay.

It uses the EAST scene-text detector (cv2.dnn) rather than classic morphology:
EAST is trained to find *text* specifically, so — unlike edge/contour heuristics
— it ignores faces, objects and busy art that otherwise cause false positives
(which here are harmful: a false positive drops the title overlay entirely).

The ~96 MB EAST model is NOT bundled.  It is downloaded once, on first use, into
the cache volume — so only operators who enable TEXTLESS_TEXT_DETECTION pay the
cost, and the repo/image stay lean.  If OpenCV is missing or the model can't be
fetched, detection soft-disables (always returns False).
"""
from __future__ import annotations

import logging
import os
import threading
import urllib.request

import numpy as np

logger = logging.getLogger(__name__)

try:
    import cv2 as _cv2
    _HAS_CV2 = True
except Exception:
    _HAS_CV2 = False

# Frozen EAST graph.  Override the URL or local path via env if you'd rather
# pre-place the model (e.g. bake it into a custom image) than download it.
_EAST_URL  = os.environ.get(
    "EAST_MODEL_URL",
    "https://github.com/oyyd/frozen_east_text_detection.pb/raw/master/frozen_east_text_detection.pb",
)
# Prefer a model baked into the image (docker build --build-arg BAKE_EAST_MODEL=true)
# so it survives cache-volume wipes and never re-downloads; otherwise fall back to
# a one-time download into the cache volume.
_BAKED_EAST = "/app/models/frozen_east_text_detection.pb"
_EAST_PATH = os.environ.get("EAST_MODEL_PATH") or (
    _BAKED_EAST if os.path.exists(_BAKED_EAST) else "/app/cache/frozen_east_text_detection.pb"
)
_EAST_LAYERS = ["feature_fusion/Conv_7/Sigmoid", "feature_fusion/concat_3"]

# EAST input resolution (each dim must be a multiple of 32).  Inference cost
# scales ~linearly with pixel count, so a smaller input is a direct speed lever.
# Default 256x512 runs ~1.5x faster than the 320x640 reference at ~99.3% decision
# agreement on a real poster sample, and (unlike 192x384) resolves thin, widely
# letter-spaced serif titles.  Drop to 192x384 for ~2.7x speed if detection cost
# becomes a problem, or raise to 320x640 to match the reference exactly.  min_boxes
# is expressed at the 320x640 reference and auto-scaled to the actual score-map
# below, so the configured threshold keeps its meaning at any size.
def _mult32(v: int, default: int) -> int:
    try:
        v = int(v)
    except (TypeError, ValueError):
        return default
    return max(32, (v // 32) * 32)

_EAST_W = _mult32(os.environ.get("EAST_INPUT_WIDTH"),  256)
_EAST_H = _mult32(os.environ.get("EAST_INPUT_HEIGHT"), 512)
_EAST_REF_CELLS = (640 // 4) * (320 // 4)   # 12800 cells at the 320x640 reference

# Fraction of poster height to skip from the TOP before counting text activations.
# Titles can sit anywhere — top, middle or bottom — so we scan almost the whole
# poster; the small default margin still ignores studio/network bugs at the very
# edge.  (Was effectively 0.30, a bottom-70%-only scan that completely missed
# top-titled posters like "SANITKA 2" / "AMBER THE AMBULANCE".)
try:
    _SCAN_TOP = float(os.environ.get("TEXTLESS_SCAN_TOP", "0.08"))
except (TypeError, ValueError):
    _SCAN_TOP = 0.08
_SCAN_TOP = max(0.0, min(0.9, _SCAN_TOP))

# Cache-key token: detection results change with resolution and scan region, so
# callers append this to their result/composite cache keys to auto-invalidate.
DETECT_RES_SIG = f"{_EAST_W}x{_EAST_H}t{int(round(_SCAN_TOP * 100))}"

_net = None
_net_lock = threading.Lock()
# cv2.dnn.Net is NOT thread-safe.  poster_has_burned_in_text runs in the thread
# pool (run_in_executor), so a burst of concurrent poster requests would call
# net.forward() on the shared model from many threads at once — corrupting the
# score map (→ false positives) and risking stalls.  Serialise all inference.
_infer_lock = threading.Lock()
_load_failed = False


def text_detection_available() -> bool:
    """True when OpenCV is importable (the model is fetched lazily on first use)."""
    return _HAS_CV2


def _ensure_model():
    """Download (once) + load the EAST model. Thread-safe; soft-fails to None."""
    global _net, _load_failed
    if _net is not None or _load_failed or not _HAS_CV2:
        return _net
    with _net_lock:
        if _net is not None or _load_failed:
            return _net
        try:
            if not os.path.exists(_EAST_PATH) or os.path.getsize(_EAST_PATH) < 1_000_000:
                logger.info(f"Downloading EAST text-detection model (one-time) → {_EAST_PATH}")
                os.makedirs(os.path.dirname(_EAST_PATH), exist_ok=True)
                tmp = _EAST_PATH + ".part"
                urllib.request.urlretrieve(_EAST_URL, tmp)
                os.replace(tmp, _EAST_PATH)
                logger.info("EAST model downloaded")
            _net = _cv2.dnn.readNet(_EAST_PATH)
            logger.info("EAST text-detection model loaded")
        except Exception as exc:
            logger.warning(f"EAST model unavailable — text detection disabled: {exc}")
            _load_failed = True
    return _net


def warm_model() -> bool:
    """Eagerly ensure the model is ready (call at startup when the feature is on)."""
    return _ensure_model() is not None


def poster_has_burned_in_text(
    image,
    *,
    min_boxes: int = 32,
    conf: float = 0.5,
    lower_region: float = _SCAN_TOP,
    debug: bool = False,
) -> bool:
    """
    Return True if *image* (a PIL RGB/RGBA image) likely has burned-in title
    text.  Counts EAST text-region activations above *conf* in the
    *lower_region*..1.0 band of the poster (skipping only a small top margin, so
    top-, middle- and bottom-titled posters are all covered) and flags the poster
    when at least *min_boxes* fire.  Returns False if OpenCV/model are unavailable.
    """
    net = _ensure_model()
    if net is None:
        return False
    try:
        # Keep the array RGB and let blobFromImage(swapRB=False) consume it as-is.
        # The EAST mean (123.68, 116.78, 103.94) is in R,G,B order, so RGB-in /
        # no-swap is bit-identical to the canonical BGR-in / swapRB=True pipeline
        # while skipping a full-frame channel-reverse + copy (~14 ms/scan).
        img = np.asarray(image.convert("RGB"))
        H0, W0 = img.shape[:2]
        if H0 == 0 or W0 == 0:
            return False

        newW, newH = _EAST_W, _EAST_H  # multiples of 32
        blob = _cv2.dnn.blobFromImage(
            _cv2.resize(img, (newW, newH)), 1.0, (newW, newH),
            (123.68, 116.78, 103.94), swapRB=False, crop=False,
        )
        with _infer_lock:
            net.setInput(blob)
            scores, _geo = net.forward(_EAST_LAYERS)

        sc = scores[0, 0]                       # (rows, cols) confidence map
        rows, cols = sc.shape
        cutoff_row = int(rows * lower_region)
        hits = int((sc[cutoff_row:] >= conf).sum())

        # min_boxes is referenced to the 320x640 map; scale it to this map's cell
        # count so the threshold means the same thing at any input resolution.
        eff_min_boxes = max(1, round(min_boxes * (rows * cols) / _EAST_REF_CELLS))
        detected = hits >= eff_min_boxes
        if debug:
            logger.info(f"text_detect (EAST {newW}x{newH}): hits={hits} "
                        f"(>= {eff_min_boxes}?) → {'TEXT' if detected else 'clear'}")
        return detected
    except Exception as exc:
        logger.warning(f"text_detect error: {exc}")
        return False


def text_column_profile(image, conf: float = 0.5):
    """
    Return a per-column text-density profile (left→right, normalised 0..1) from
    the EAST score map, or None if unavailable.  Used to bias a portrait crop of
    a landscape backdrop *away* from the columns that contain title text.
    """
    net = _ensure_model()
    if net is None:
        return None
    try:
        # RGB-in / swapRB=False — identical result to BGR-in / swapRB=True (the
        # mean is R,G,B order), minus the redundant channel-reverse + copy.
        img = np.asarray(image.convert("RGB"))
        newW, newH = _EAST_W, _EAST_H
        blob = _cv2.dnn.blobFromImage(
            _cv2.resize(img, (newW, newH)), 1.0, (newW, newH),
            (123.68, 116.78, 103.94), swapRB=False, crop=False,
        )
        with _infer_lock:
            net.setInput(blob)
            scores, _geo = net.forward(_EAST_LAYERS)
        sc = scores[0, 0]                                   # (rows, cols)
        col = (sc >= conf).sum(axis=0).astype(np.float32)   # per-column hit count
        m = col.max()
        if m > 0:
            col /= m
        return col
    except Exception as exc:
        logger.warning(f"text_column_profile error: {exc}")
        return None


if __name__ == "__main__":
    import sys
    from PIL import Image
    logging.basicConfig(level=logging.INFO)
    for path in sys.argv[1:]:
        try:
            res = poster_has_burned_in_text(Image.open(path), debug=True)
            print(f"{path}: {'HAS TEXT' if res else 'clear'}")
        except Exception as e:
            print(f"{path}: error {e}")
