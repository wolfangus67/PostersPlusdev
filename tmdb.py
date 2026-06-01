#tmdb.py
import asyncio
import colorsys
import io
import logging
from datetime import date as _date, datetime as _datetime
import httpx
import numpy as np

logger = logging.getLogger(__name__)
from PIL import Image, ImageDraw, ImageFilter

# SVG title-logo support — TMDB serves many of its highest-voted logos as SVG.
# Soft import so the service still runs (PNG-only) if cairosvg is unavailable.
try:
    import cairosvg as _cairosvg
    _HAS_CAIROSVG = True
except Exception:
    _HAS_CAIROSVG = False


def svg_logo_supported() -> bool:
    """True when SVG title logos can be rasterised (cairosvg is importable)."""
    return _HAS_CAIROSVG


def _rasterize_svg(svg_bytes: bytes, target_w: int = 1000) -> "Image.Image | None":
    """Render SVG bytes to an RGBA PIL image at target_w px wide, or None on failure."""
    if not _HAS_CAIROSVG:
        return None
    try:
        png_bytes = _cairosvg.svg2png(bytestring=svg_bytes, output_width=target_w)
        return Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    except Exception as exc:
        logger.warning(f"SVG logo rasterise failed: {exc}")
        return None

from cache import (
    get_cached_trending_snapshot,
    set_cached_trending_snapshot,
    get_cached_tmdb_poster,
    set_cached_tmdb_poster,
    get_cached_tmdb_logo,
    set_cached_tmdb_logo,
    get_cached_tmdb_metadata,
    set_cached_tmdb_metadata,
    get_cached_release_status,
    set_cached_release_status,
)

from config import (
    POSTER_WIDTH,
    POSTER_HEIGHT,
    LOGO_MAX_W_RATIO,
    LOGO_MAX_H_RATIO,
    LOGO_BOTTOM_RATIO,
    LOGO_CONTRAST_RESCUE,
    DEBUG_LOGO_SIZING,
)


# ---------------------------------------------------------------------------
# Image helpers
# ---------------------------------------------------------------------------

def normalise_poster(image: Image.Image) -> Image.Image:
    target_w, target_h = POSTER_WIDTH, POSTER_HEIGHT
    src_w, src_h = image.size
    scale = max(target_w / src_w, target_h / src_h)
    new_w = round(src_w * scale)
    new_h = round(src_h * scale)
    image = image.resize((new_w, new_h), Image.LANCZOS)
    left = round((new_w - target_w) / 2)
    top  = round((new_h - target_h) / 2)
    return image.crop((left, top, left + target_w, top + target_h))


def ensure_light_logo(logo: Image.Image,
                       lum_threshold: float = 0.2,
                       sat_threshold: float = 0.25) -> Image.Image:
    """
    If the visible pixels of a logo are too dark AND mostly achromatic (low
    saturation), force them to white so they read on dark poster backgrounds.
    Coloured logos (red titles, branded colours, etc.) are left untouched —
    only neutral black/dark-grey logos are converted.
    """
    rgba = np.array(logo.convert("RGBA"), dtype=np.float32)
    alpha = rgba[:, :, 3]
    visible = alpha > 30

    if not visible.any():
        return logo

    r = rgba[:, :, 0][visible]
    g = rgba[:, :, 1][visible]
    b = rgba[:, :, 2][visible]

    avg_lum = (0.2126 * r + 0.7152 * g + 0.0722 * b).mean() / 255.0
    if avg_lum > lum_threshold:
        return logo  # Already light enough

    # Check average saturation of visible pixels.
    # Saturation = (max - min) / max per pixel (HSV definition).
    max_c = np.maximum(np.maximum(r, g), b)
    min_c = np.minimum(np.minimum(r, g), b)
    coloured = max_c > 0
    if coloured.any():
        avg_sat = (((max_c - min_c) / np.where(coloured, max_c, 1.0)) * coloured).mean()
    else:
        avg_sat = 0.0

    if avg_sat > sat_threshold:
        return logo  # Coloured logo — preserve original hues

    # Dark, achromatic logo — force to white
    out = rgba.copy()
    out[:, :, 0][visible] = 255
    out[:, :, 1][visible] = 255
    out[:, :, 2][visible] = 255
    return Image.fromarray(out.astype(np.uint8), "RGBA")


# Experimental contrast-rescue tuning.  Lower = more conservative (only recolour
# when the logo and background colours are very close).  Set to 0 to disable.
LOGO_CONTRAST_MIN = 0.25   # normalised RGB distance (0–1) below which we recolour
# Logos whose internal colour spread exceeds this are left alone — multi-colour
# logos (Mario) or outline+fill logos (Archer) rely on their own colours for
# legibility and would be ruined by a flat recolour.
LOGO_COLOR_VARIANCE_MAX = 0.16
# When a flat logo is recoloured, default to white and only switch to black on
# genuinely light backgrounds (white reads well on most posters).
LOGO_DARK_TEXT_LUM = 0.66   # background luminance above which black is used
# In the mid-luminance band (where plain white/black are weakest) a flat logo
# may instead be recoloured to the COMPLEMENTARY hue of the background, forced
# to an extreme value for guaranteed luminance contrast.  Only used when the
# background has a clear dominant hue — greyscale backgrounds fall back to
# white/black.  Set the band to (0, 0) to disable accents entirely.
LOGO_ACCENT_LUM_BAND = (0.40, 0.66)   # bg-luminance window for accent colours
LOGO_ACCENT_MIN_SAT  = 0.25           # bg must be at least this saturated

# Logos are normalised to one overall size (the geometric mean of the Width and
# Height caps), then clamped to those caps preserving aspect ratio.  Both caps
# are HARD ceilings — the configured ratios are the true maximums.  PIVOT is the
# width:height ratio treated as "neutral" (a typical title logo is wider than
# tall); it's used only to label logo orientation in the sizing telemetry.
LOGO_ASPECT_PIVOT = 2.8    # neutral aspect (wider → "wide", narrower → "tall")
# Absolute pixel ceiling on rendered logo height — a hard stop so a tall, only
# moderately-wide logo can never dominate the poster, regardless of the Height
# ratio slider or aspect flex.  ~25 % of a 750 px poster.
LOGO_ABS_MAX_H = 150
# Single-axis fill stretch: a slim logo whose under-cap dimension would leave it
# looking lost may be stretched up to this factor toward its cap (width OR
# height, never both).  Height stays bounded by LOGO_ABS_MAX_H.
LOGO_FILL_STRETCH = 2
# The height stretch only fires when the logo's clamped height is below this
# fraction of its height cap — i.e. only genuinely short/slim logos are lifted,
# while normally-proportioned logos are left at their true aspect ratio.
LOGO_FILL_HEIGHT_TRIGGER = 0.6


def logo_centre_y(height: int, bottom_ratio: float = LOGO_BOTTOM_RATIO) -> int:
    """
    Vertical centre line that composite_logo aligns logos to.  Exposed so the
    fallback title-text renderer can sit on the exact same line, keeping logo
    and text posters visually consistent.
    """
    max_h = min(int(height * LOGO_MAX_H_RATIO), LOGO_ABS_MAX_H)
    return int(height - int(height * bottom_ratio) - max_h / 2)


def _recolor_target(bg_rgb: tuple[float, float, float],
                    bg_lum: float) -> tuple[tuple[int, int, int], str]:
    """
    Choose the colour to recolour a flat logo to, given the background under it.

    Returns (rgb, label).  In the mid-luminance band, a saturated background
    yields the complementary hue pushed to an extreme value (dark accent over a
    lighter bg, light accent over a darker bg) so contrast stays high while the
    tint ties to the poster.  Outside the band, or on greyscale backgrounds,
    falls back to white (default) or black (very light backgrounds).
    """
    r, g, b = bg_rgb[0] / 255, bg_rgb[1] / 255, bg_rgb[2] / 255
    h, s, _v = colorsys.rgb_to_hsv(r, g, b)

    lo, hi = LOGO_ACCENT_LUM_BAND
    if lo < hi and lo <= bg_lum <= hi and s >= LOGO_ACCENT_MIN_SAT:
        comp_h = (h + 0.5) % 1.0
        comp_v = 0.30 if bg_lum >= 0.50 else 0.95   # opposite side of bg luminance
        cr, cg, cb = colorsys.hsv_to_rgb(comp_h, 0.85, comp_v)
        return (int(cr * 255), int(cg * 255), int(cb * 255)), "accent"

    if bg_lum > LOGO_DARK_TEXT_LUM:
        return (20, 20, 20), "black"
    return (255, 255, 255), "white"


def _logo_color_stats(logo: Image.Image) -> tuple[tuple[float, float, float], float] | None:
    """
    Return ((mean_r, mean_g, mean_b), variance) for the logo's opaque pixels.

    variance is the mean normalised RGB distance of pixels from the mean colour
    (0–1).  Low → flat single-colour logo (safe to recolour); high → multi-colour
    or outline+fill logo whose own colours carry its legibility.
    Returns None when the logo has no opaque pixels.
    """
    rgba = np.array(logo.convert("RGBA"), dtype=np.float32)
    vis = rgba[:, :, 3] > 64
    if not vis.any():
        return None
    rgb  = rgba[:, :, :3][vis]                       # N×3
    mean = rgb.mean(axis=0)
    var  = float(np.sqrt(((rgb - mean) ** 2).sum(axis=1)).mean() / 441.673)
    return (float(mean[0]), float(mean[1]), float(mean[2])), var


def _recolor_logo_solid(logo: Image.Image, rgb: tuple[int, int, int]) -> Image.Image:
    """Force all visible logo pixels to a solid colour, preserving alpha."""
    rgba = np.array(logo.convert("RGBA"))
    vis = rgba[:, :, 3] > 30
    rgba[:, :, 0][vis] = rgb[0]
    rgba[:, :, 1][vis] = rgb[1]
    rgba[:, :, 2][vis] = rgb[2]
    return Image.fromarray(rgba, "RGBA")


# ---------------------------------------------------------------------------
# Fetch helpers
# ---------------------------------------------------------------------------

async def fetch_poster_metadata(
    client: httpx.AsyncClient,
    tmdb_id: str,
    tmdb_key: str,
    media_type: str = "movie",
    logo_language: str = "en",
) -> tuple[list[int], bool, list[dict], str | None, str, str, str | None, dict]:
    """
    Fetch (or return cached) TMDB metadata, including credits,
    production_companies, and original_language for discovery sash logic.

    Returns:
        (genre_ids, is_textless, logos, release_year, title, poster_path, backdrop_path, tmdb_data)
    """
    endpoint = "tv" if media_type in ("tv", "series") else "movie"
    metadata_cache_key = f"{endpoint}_{tmdb_id}"

    meta = get_cached_tmdb_metadata(metadata_cache_key)

    if meta:
        logger.info(f"TMDB metadata cache hit for {tmdb_id}")
        tmdb_data = {
            "credits":               meta.get("credits", {}),
            "production_companies":  meta.get("production_companies", []),
            "original_language":     meta.get("original_language"),
            "runtime":               meta.get("runtime"),
            "number_of_seasons":     meta.get("number_of_seasons"),
            "number_of_episodes":    meta.get("number_of_episodes"),
            "tmdb_status":           meta.get("tmdb_status"),
            "vote_count":            meta.get("vote_count"),
            "text_backdrop_path":    meta.get("text_backdrop_path"),
        }
        return (
            meta["genre_ids"],
            meta["is_textless"],
            meta["logos"],
            meta["release_year"],
            meta["title"],
            meta["poster_path"],
            meta.get("backdrop_path"),
            tmdb_data,
        )

    # Build include_image_language so TMDB returns:
    #   null  — language-neutral entries (TMDB's signal for textless/unspecified)
    #   en    — English (logos + fallback posters)
    #   logo_language — non-English logo candidates when requested
    # Note: null-language ≠ guaranteed text-free; TMDB uses it for both truly
    # textless art and posters where the language simply wasn't catalogued.
    _img_langs = "en,null" if logo_language == "en" else f"{logo_language},en,null"

    logger.info(f"External API Call: Requested meta from TMDB for {tmdb_id}")
    resp = await client.get(
        f"https://api.themoviedb.org/3/{endpoint}/{tmdb_id}",
        params={
            "api_key": tmdb_key,
            "append_to_response": "images,credits",
            "include_image_language": _img_langs,
        },
    )
    resp.raise_for_status()
    data = resp.json()

    title = (
        data.get("title")
        or data.get("name")
        or data.get("original_title")
        or data.get("original_name")
        or "Unknown Title"
    )

    raw_date = data.get("release_date") or data.get("first_air_date") or ""
    release_year: str | None = raw_date[:4] if len(raw_date) >= 4 else None

    images    = data.get("images", {})
    posters   = images.get("posters", [])
    logos     = images.get("logos", [])
    backdrops = images.get("backdrops", [])

    # iso_639_1 is None (JSON null) for most textless entries;
    # older TMDB records occasionally use "" (empty string) for the same thing.
    textless = [p for p in posters if p.get("iso_639_1") in (None, "")]

    if textless:
        best = max(textless, key=lambda x: x.get("vote_average", 0))
        poster_path = best["file_path"]
        is_textless = True
    else:
        poster_path = data.get("poster_path")
        is_textless = False

    if not poster_path:
        logger.warning(f"No poster image on TMDB for tmdb_id={tmdb_id} — fallback canvas will be served")
        is_textless = False  # no art, no point fetching logos
        # poster_path stays None; get_poster will generate a fallback canvas

    # Best backdrop — only consider null/unspecified language entries, which are
    # the ones TMDB marks as language-neutral (almost always textless).
    # Backdrops with an explicit language tag frequently have title text burned in,
    # so we ignore them entirely rather than risk a borked crop.
    # backdrop_path stays None if no null-language backdrop exists, which suppresses
    # the backdrop fallback path in main.py.
    backdrop_candidates = [b for b in backdrops if b.get("iso_639_1") in (None, "")]
    if backdrop_candidates:
        best_backdrop = max(backdrop_candidates, key=lambda x: x.get("vote_average", 0))
        backdrop_path: str | None = best_backdrop["file_path"]
    else:
        backdrop_path = None

    # Best TEXT-bearing (language-tagged) backdrop — the last-resort landscape
    # source for titles with no textless poster or backdrop.  Only used by the
    # text-aware crop rescue (gated behind TEXTLESS_TEXT_DETECTION) which crops
    # away the title text; never used by the default pipeline.
    _text_backdrops = [b for b in backdrops if b.get("iso_639_1") not in (None, "")]
    text_backdrop_path: str | None = (
        max(_text_backdrops, key=lambda x: x.get("vote_average", 0))["file_path"]
        if _text_backdrops else None
    )

    genre_ids            = [g["id"] for g in data.get("genres", [])]
    credits              = data.get("credits", {})
    production_companies = data.get("production_companies", [])
    original_language    = data.get("original_language")
    runtime              = data.get("runtime")
    number_of_seasons    = data.get("number_of_seasons")
    number_of_episodes   = data.get("number_of_episodes")
    tmdb_status          = data.get("status")   # e.g. "Released", "In Production", "Returning Series"
    vote_count           = data.get("vote_count")

    # If the content's original language wasn't included in the initial image
    # request (e.g. a Romanian show fetched by an English-language user), TMDB
    # won't return native-language logos.  Do a cheap supplemental /images call
    # so we can cache those logos alongside the rest.  Skipped when the original
    # language is already covered by _img_langs (en or user's logo_language).
    _covered = {logo_language, "en"}
    if (
        original_language
        and original_language not in _covered
        and not any(lg.get("iso_639_1") == original_language for lg in logos)
    ):
        try:
            logger.info(
                f"Fetching supplemental {original_language} logos for {tmdb_id}"
            )
            supp = await client.get(
                f"https://api.themoviedb.org/3/{endpoint}/{tmdb_id}/images",
                params={
                    "api_key":                tmdb_key,
                    "include_image_language": original_language,
                },
            )
            if supp.status_code == 200:
                supp_logos = supp.json().get("logos", [])
                logos = logos + supp_logos
                logger.info(
                    f"Added {len(supp_logos)} {original_language} logo(s) for {tmdb_id}"
                )
        except Exception as exc:
            logger.warning(f"Supplemental logo fetch failed for {tmdb_id}: {exc}")

    set_cached_tmdb_metadata(
        metadata_cache_key,
        title,
        release_year,
        genre_ids,
        is_textless,
        poster_path,
        logos,
        credits=credits,
        production_companies=production_companies,
        original_language=original_language,
        runtime=runtime,
        number_of_seasons=number_of_seasons,
        number_of_episodes=number_of_episodes,
        backdrop_path=backdrop_path,
        tmdb_status=tmdb_status,
        vote_count=vote_count,
        text_backdrop_path=text_backdrop_path,
    )

    tmdb_data = {
        "credits":              credits,
        "production_companies": production_companies,
        "original_language":    original_language,
        "runtime":              runtime,
        "number_of_seasons":    number_of_seasons,
        "number_of_episodes":   number_of_episodes,
        "tmdb_status":          tmdb_status,
        "vote_count":           vote_count,
        "text_backdrop_path":   text_backdrop_path,
    }

    return genre_ids, is_textless, logos, release_year, title, poster_path, backdrop_path, tmdb_data


async def fetch_poster_image(
    client: httpx.AsyncClient,
    tmdb_id: str,
    media_type: str,
    poster_path: str,
) -> Image.Image:
    """
    Fetch and cache the base poster image.

    Disk cache format is JPEG (q=92 RGB) rather than PNG:
      - ~4-5x faster decode on cache hit
      - ~5x smaller on disk
      - Imperceptible quality difference for photographic poster art
    The image is returned as RGBA so the compositing pipeline can use
    alpha_composite throughout without mode-checking.
    """
    poster_cache_key = f"{media_type}_{tmdb_id}_{poster_path.strip('/')}"
    cached_bytes = get_cached_tmdb_poster(poster_cache_key)

    if cached_bytes:
        logger.info(f"TMDB poster cache hit for {tmdb_id}")
        # Stored as JPEG RGB — convert to RGBA for the compositing pipeline
        image = Image.open(io.BytesIO(cached_bytes)).convert("RGBA")
        if image.size != (POSTER_WIDTH, POSTER_HEIGHT):
            image = normalise_poster(image)
        return image

    logger.info(f"External API Call: Requested poster from TMDB for {tmdb_id}")
    img_resp = await client.get(f"https://image.tmdb.org/t/p/w500{poster_path}")
    img_resp.raise_for_status()
    image = Image.open(io.BytesIO(img_resp.content)).convert("RGBA")
    image = normalise_poster(image)

    # Save as JPEG RGB (no alpha needed for base poster; restoring alpha on load is free)
    buf = io.BytesIO()
    image.convert("RGB").save(buf, format="JPEG", quality=92)
    set_cached_tmdb_poster(poster_cache_key, buf.getvalue())

    return image


# Bumped whenever the backdrop crop logic changes, so cached crops from the old
# algorithm are invalidated rather than served.
#   v2 = face-aware cropping
#   v3 = focus a single face when subjects are too far apart to both fit
_CROP_VERSION = "v3"


def _face_crop_left(image: Image.Image, crop_w: int) -> "int | None":
    """
    Best left-edge x for a portrait crop that keeps detected faces in frame, or
    None when no faces are found / detection is unavailable (caller then falls
    back to the saliency crop).

    If every face fits within the crop window, the window is centred on their
    combined bounding box so all stay framed.  If the faces are too far apart to
    fit, the crop focuses on the single most prominent face (largest × most
    confident) rather than splitting the difference and slicing each in half.
    """
    try:
        from face_detect import detect_faces
    except Exception:
        return None
    faces = detect_faces(image)
    if not faces:
        return None
    w = image.width
    if crop_w >= w:
        return 0

    lefts  = [cx - fw / 2.0 for cx, fw, _ in faces]
    rights = [cx + fw / 2.0 for cx, fw, _ in faces]
    extent = max(rights) - min(lefts)

    if extent <= crop_w:
        # All faces fit — centre on their bounding-box midpoint.
        target_cx = (min(lefts) + max(rights)) / 2.0
    else:
        # Too far apart to keep both — focus on the most prominent face.
        target_cx = max(faces, key=lambda f: f[2])[0]

    left = int(round(target_cx - crop_w / 2))
    return max(0, min(w - crop_w, left))


def _saliency_crop_left(image: Image.Image, crop_w: int,
                        text_penalty=None, text_weight: float = 1.8) -> int:
    """
    Find the best left-edge x-coordinate for a portrait crop of a landscape image.

    Uses three complementary saliency signals combined into a per-column profile,
    then picks the crop window with the highest score.  A mild Gaussian centre
    bias acts as a tiebreaker when the scene is uniform so the result never drifts
    to an arbitrary edge.

    Signals (all computed on a 320 px-wide thumbnail for speed):

    1. Skin-tone mask  — HSV-based detection of warm pinkish-orange hues that
       reliably indicate human (and many animated) characters.  Strong weight
       (×4) because it's the most semantically meaningful signal for movie art.

    2. Center-surround saliency  — Difference of two Gaussian blurs at different
       radii (fine ≈ 4 % of width, coarse ≈ 20 % of width).  Finds blobs that
       are locally distinct from their surroundings — faces, figures, bright
       objects — rather than just any edge or texture.  Weight ×2.

    3. Saturation  — Subjects tend to be more saturated than blurred/desaturated
       backgrounds.  Lightweight secondary signal (×0.5).

    Vertical weighting: upper 65 % of frame gets 2× weight because characters'
    faces and torsos live in the top half; floors and landscape fill the bottom.

    Centre bias: ≈10 % of peak score — gentle enough not to override clear signal
    but prevents chaotic results on uniformly textured frames.
    """
    w, h = image.size
    if crop_w >= w:
        return 0

    # --- Downsample for speed ------------------------------------------------
    SMALL_W = 320
    scale   = min(1.0, SMALL_W / w)
    sw      = max(1, int(w * scale))
    sh      = max(1, int(h * scale))
    scrop_w = max(1, int(crop_w * scale))

    small = image.resize((sw, sh), Image.LANCZOS).convert("RGB")
    rgb   = np.array(small, dtype=np.float32) / 255.0   # H × W × 3, [0,1]
    r, g, b = rgb[:,:,0], rgb[:,:,1], rgb[:,:,2]

    # --- Skin-tone mask (HSV) ------------------------------------------------
    # Compute V, S, H in numpy without scipy.
    cmax  = np.maximum(np.maximum(r, g), b)
    cmin  = np.minimum(np.minimum(r, g), b)
    delta = cmax - cmin

    v = cmax
    s = np.zeros_like(cmax)
    np.divide(delta, cmax, out=s, where=cmax > 1e-5)

    # Hue in [0, 360)
    hue = np.zeros((sh, sw), dtype=np.float32)
    m_r = (cmax == r) & (delta > 1e-5)
    m_g = (cmax == g) & (delta > 1e-5)
    m_b = (cmax == b) & (delta > 1e-5)
    hue[m_r] = (60.0 * ((g[m_r] - b[m_r]) / delta[m_r])) % 360.0
    hue[m_g] =  60.0 *  (b[m_g] - r[m_g]) / delta[m_g] + 120.0
    hue[m_b] =  60.0 *  (r[m_b] - g[m_b]) / delta[m_b] + 240.0

    # Skin: hue in [0,25]∪[335,360], moderate saturation, reasonable brightness.
    skin = (
        ((hue <= 25.0) | (hue >= 335.0)) &
        (s >= 0.15) & (s <= 0.90) &
        (v >= 0.25)
    ).astype(np.float32)

    # --- Center-surround saliency (DoG) --------------------------------------
    grey_pil  = Image.fromarray((rgb @ np.array([0.2126, 0.7152, 0.0722]) * 255).clip(0,255).astype(np.uint8))
    r_fine    = max(1, int(sw * 0.04))
    r_coarse  = max(1, int(sw * 0.20))
    fine      = np.array(grey_pil.filter(ImageFilter.GaussianBlur(radius=r_fine)),   dtype=np.float32)
    coarse    = np.array(grey_pil.filter(ImageFilter.GaussianBlur(radius=r_coarse)), dtype=np.float32)
    dog       = np.abs(fine - coarse) / 255.0   # [0, 1]

    # --- Saturation layer ----------------------------------------------------
    sat = s   # already [0, 1]

    # --- Vertical weighting --------------------------------------------------
    # Upper 65 % of rows get a 2× boost; lower 35 % stay at 1×.
    vert = np.ones(sh, dtype=np.float32)
    vert[:int(sh * 0.65)] = 2.0

    # --- Combine -------------------------------------------------------------
    saliency = (skin * 4.0 + dog * 2.0 + sat * 0.5) * vert[:, np.newaxis]

    col_sal = saliency.sum(axis=0)   # shape (sw,)

    # --- Text avoidance ------------------------------------------------------
    # Subtract a penalty proportional to per-column text density so the chosen
    # crop window dodges burned-in title text.  text_penalty is a left→right
    # profile (any length) in [0,1]; we resample it to the thumbnail width.
    if text_penalty is not None and len(text_penalty) > 1 and col_sal.max() > 0:
        prof = np.asarray(text_penalty, dtype=np.float32)
        prof_resized = np.interp(
            np.linspace(0.0, 1.0, sw, dtype=np.float32),
            np.linspace(0.0, 1.0, len(prof), dtype=np.float32),
            prof,
        )
        col_sal = col_sal - prof_resized * col_sal.max() * text_weight

    # --- Sliding-window via cumulative sum -----------------------------------
    cum         = np.concatenate([[0.0], col_sal.cumsum()])
    n_positions = sw - scrop_w + 1
    if n_positions <= 1:
        return 0

    window_scores = cum[scrop_w:scrop_w + n_positions] - cum[:n_positions]

    # --- Gaussian centre bias (10 % of peak) ---------------------------------
    centre  = (n_positions - 1) / 2.0
    sigma   = n_positions * 0.35
    xs      = np.arange(n_positions, dtype=np.float32)
    bias    = np.exp(-0.5 * ((xs - centre) / sigma) ** 2)
    sal_max = window_scores.max()
    if sal_max > 0:
        bias *= sal_max * 0.10

    best_small_left = int((window_scores + bias).argmax())

    # --- Scale back and clamp ------------------------------------------------
    left = int(round(best_small_left / scale))
    return max(0, min(w - crop_w, left))


async def fetch_backdrop_image(
    client: httpx.AsyncClient,
    tmdb_id: str,
    backdrop_path: str,
    avoid_text: bool = False,
) -> Image.Image:
    """
    Fetch, saliency-crop, and cache a TMDB backdrop as a portrait poster.

    Backdrops are 16:9 landscape; we take the full height and cut a 2:3 strip
    whose horizontal position is chosen by gradient-magnitude saliency rather
    than always defaulting to the centre.  This keeps the main subject in frame
    when cinematographers frame wide shots off-centre.

    When *avoid_text* is set (text-detection feature on), an EAST per-column text
    profile biases the crop away from burned-in title text.  Cached under the
    same JPEG scheme as regular posters (text-aware crops keyed separately).
    """
    # Cache key carries a crop-logic version so changing the crop algorithm
    # (e.g. adding face-aware cropping) invalidates previously-cached crops
    # instead of serving the old framing.  Bump _CROP_VERSION on any crop change.
    cache_key = (
        f"backdrop_{tmdb_id}_{backdrop_path.strip('/')}_{_CROP_VERSION}"
        + ("_ta" if avoid_text else "")
    )
    cached_bytes = get_cached_tmdb_poster(cache_key)

    if cached_bytes:
        logger.info(f"TMDB backdrop cache hit for {tmdb_id}")
        image = Image.open(io.BytesIO(cached_bytes)).convert("RGBA")
        if image.size != (POSTER_WIDTH, POSTER_HEIGHT):
            image = normalise_poster(image)
        return image

    # w1280 gives enough resolution to crop to a quality portrait
    logger.info(f"External API Call: Requested backdrop from TMDB for {tmdb_id}")
    img_resp = await client.get(f"https://image.tmdb.org/t/p/w1280{backdrop_path}")
    img_resp.raise_for_status()
    image = Image.open(io.BytesIO(img_resp.content)).convert("RGBA")

    # The crop runs face/text OpenCV inference (CPU + a serialising lock), so do
    # it in the thread pool — never inline on the event loop, or it would stall
    # request handling while waiting on the shared inference lock.
    image = await asyncio.get_running_loop().run_in_executor(
        None, _crop_and_normalise_backdrop, image, tmdb_id, avoid_text
    )

    buf = io.BytesIO()
    image.convert("RGB").save(buf, format="JPEG", quality=92)
    set_cached_tmdb_poster(cache_key, buf.getvalue())

    return image


def _crop_and_normalise_backdrop(image: Image.Image, tmdb_id: str,
                                 avoid_text: bool) -> Image.Image:
    """Synchronous backdrop crop (face-aware → saliency fallback) + normalise.
    Runs in the thread pool; all OpenCV inference is confined here."""
    # Optional text-density profile to steer the crop away from title text.
    _text_prof = None
    if avoid_text:
        try:
            from text_detect import text_column_profile
            _text_prof = text_column_profile(image)
        except Exception as exc:
            logger.warning(f"Backdrop text profile failed for {tmdb_id}: {exc}")

    # Crop full-height to a 2:3 strip.  Prefer a face-aware crop (robust on
    # people shots where warm/textured backgrounds fool the saliency heuristic);
    # fall back to saliency when no faces are detected.
    w, h   = image.size
    crop_w = int(h * 2 / 3)
    if crop_w < w:
        left = _face_crop_left(image, crop_w)
        if left is not None:
            logger.info(
                f"Backdrop face-aware crop for {tmdb_id}: "
                f"left={left} (centre would be {(w - crop_w) // 2}) of w={w}"
            )
        else:
            left = _saliency_crop_left(image, crop_w, text_penalty=_text_prof)
            logger.info(
                f"Backdrop saliency crop for {tmdb_id}: "
                f"left={left} (centre would be {(w - crop_w) // 2}) of w={w}"
                f"{' [text-aware]' if _text_prof is not None else ''}"
            )
        image = image.crop((left, 0, left + crop_w, h))

    return normalise_poster(image)


async def _fetch_metahub_logo(
    client: httpx.AsyncClient,
    imdb_id: str,
) -> Image.Image | None:
    """
    Fetch a title logo from the Metahub CDN (images.metahub.space).

    Metahub is the same CDN Cinemeta (Stremio's catalogue addon) uses for
    logo art.  It requires no authentication and caches aggressively
    (max-age ≈ 60 days server-side).  We use it as a final fallback when
    TMDB has no logo candidates for a given title.

    URL pattern: https://images.metahub.space/logo/medium/{imdb_id}/img
    """
    cache_key = f"metahub_logo_{imdb_id}"
    cached_bytes = get_cached_tmdb_logo(cache_key)

    if cached_bytes:
        logger.info(f"Metahub logo cache hit for {imdb_id}")
        return Image.open(io.BytesIO(cached_bytes)).convert("RGBA")

    # Try medium first (smaller payload), fall back to large — some titles only
    # have a large-size entry on Metahub and the medium URL 404s.
    resp = None
    for size in ("medium", "large", "small"):
        url = f"https://images.metahub.space/logo/{size}/{imdb_id}/img"
        logger.info(f"External API Call: Requested logo from Metahub ({size}) for {imdb_id}")
        try:
            r = await client.get(url, follow_redirects=True)
            if r.status_code == 404:
                logger.info(f"Metahub: no {size} logo for {imdb_id}")
                continue
            r.raise_for_status()
            resp = r
            break
        except httpx.HTTPStatusError as exc:
            logger.warning(f"Metahub logo fetch failed for {imdb_id} ({size}): {exc}")
        except Exception as exc:
            logger.warning(f"Metahub logo fetch error for {imdb_id} ({size}): {exc}")

    if resp is None:
        return None

    try:
        logo = Image.open(io.BytesIO(resp.content)).convert("RGBA")
    except Exception as exc:
        logger.warning(f"Metahub logo parse failed for {imdb_id}: {exc}")
        return None

    bbox = logo.getchannel("A").getbbox()
    if bbox:
        logo = logo.crop(bbox)

    buf = io.BytesIO()
    logo.save(buf, format="PNG")
    set_cached_tmdb_logo(cache_key, buf.getvalue())

    return logo


async def fetch_logo(
    client: httpx.AsyncClient,
    logos: list[dict],
    logo_language: str = "en",
    imdb_id: str | None = None,
    original_language: str | None = None,
    logo_priority: str = "native_original",
) -> Image.Image | None:
    """
    Fetch the best available logo for a title, with a Metahub CDN fallback.

    Two language-specific buckets are weighed first, in an order set by
    *logo_priority*:
      • "native"   — a logo in the requested language (logo_language).
      • "original" — a logo in the content's own original language
                     (original_language); helps foreign titles that only ship
                     a native-language logo on TMDB.
      logo_priority:
        "native_original" (default) → native, then original
        "original_native"           → original, then native
        "native_text"               → native only (skip the original-language
                                       bucket so the caller's text-title fallback
                                       renders the translated title instead)

    After those, the common fallbacks apply regardless of priority:
      → TMDB language-neutral logo (iso_639_1 null/"")
      → TMDB English logo
      → Metahub CDN logo (images.metahub.space) — requires imdb_id
      → None (caller may render the translated title as text instead).

    All results are cached locally so repeat requests never hit external APIs.
    """
    # Accept PNG always; accept SVG too when we can rasterise it (cairosvg).
    # TMDB's highest-voted logo is frequently an SVG, so excluding them would
    # silently fall back to a lower-quality raster or a text title.
    _exts = (".png", ".svg") if _HAS_CAIROSVG else (".png",)
    _cand = [lg for lg in logos if lg["file_path"].lower().endswith(_exts)]

    preferred = [lg for lg in _cand if lg.get("iso_639_1") == logo_language]
    # Original-language logos.  Empty when original_language wasn't provided,
    # when it duplicates the requested-language bucket, or when the caller opted
    # for "native_text" (prefer a text-title fallback over an original-lang logo).
    original  = (
        [lg for lg in _cand if lg.get("iso_639_1") == original_language]
        if (original_language and original_language != logo_language
            and logo_priority != "native_text")
        else []
    )
    neutral   = [lg for lg in _cand if lg.get("iso_639_1") in (None, "")]
    english   = [lg for lg in _cand if lg.get("iso_639_1") == "en"]

    # Order the two language buckets per logo_priority, then the common
    # fallbacks: language-neutral → English → (Metahub) → None.
    # Note: when logo_language == "en", preferred and english are the same bucket;
    # the "or" short-circuits so english is never tried twice.
    if logo_priority == "original_native":
        candidates = original or preferred or neutral or english
    else:  # "native_original" and "native_text" both lead with the native bucket
        candidates = preferred or original or neutral or english

    candidates = sorted(
        candidates,
        key=lambda x: x.get("vote_average", 0),
        reverse=True,
    )

    if not candidates:
        # No TMDB logo at all — try Metahub before giving up
        if imdb_id:
            return await _fetch_metahub_logo(client, imdb_id)
        return None

    logo_path = candidates[0]["file_path"]
    is_svg    = logo_path.lower().endswith(".svg")

    logo_cache_key = logo_path.strip('/').replace('/', '_')
    cached_bytes = get_cached_tmdb_logo(logo_cache_key)

    if cached_bytes:
        logger.info("TMDB logo cache hit")
        logo = Image.open(io.BytesIO(cached_bytes)).convert("RGBA")
        return logo

    # SVGs are served at "original" (the sized w500 path doesn't apply to vector);
    # rasters use w500 which is plenty for our ≤~440px rendered width.
    _size = "original" if is_svg else "w500"
    resp = await client.get(f"https://image.tmdb.org/t/p/{_size}{logo_path}")
    logger.info(f"External API Call: Requested logo from TMDB")
    resp.raise_for_status()

    if is_svg:
        logo = _rasterize_svg(resp.content)
        if logo is None:
            # Rasterise failed — fall back to Metahub, then None.
            logger.warning(f"SVG logo unusable for {imdb_id} — trying Metahub fallback")
            return await _fetch_metahub_logo(client, imdb_id) if imdb_id else None
    else:
        logo = Image.open(io.BytesIO(resp.content)).convert("RGBA")

    bbox = logo.getchannel("A").getbbox()
    if bbox:
        logo = logo.crop(bbox)

    buf = io.BytesIO()
    logo.save(buf, format="PNG")
    set_cached_tmdb_logo(logo_cache_key, buf.getvalue())

    return logo


async def fetch_trending_rank(
    client: httpx.AsyncClient,
    tmdb_id: str,
    tmdb_key: str,
    media_type: str = "movie",
) -> int | None:

    endpoint = "tv" if media_type in ("tv", "series") else "movie"

    snapshot = get_cached_trending_snapshot(endpoint)

    if snapshot is None:
        logger.info("External API Call: Refreshing TMDB trending snapshot (pages 1+2 concurrent)")

        async def _fetch_page(page: int) -> list[dict]:
            resp = await client.get(
                f"https://api.themoviedb.org/3/trending/{endpoint}/day",
                params={"api_key": tmdb_key, "page": page},
            )
            resp.raise_for_status()
            return resp.json().get("results", [])

        try:
            page1_results, page2_results = await asyncio.gather(
                _fetch_page(1),
                _fetch_page(2),
            )
        except Exception as exc:
            logger.error(f"TMDB trending fetch error: {exc}")
            return None

        rankings: dict[str, int] = {}
        for i, item in enumerate(page1_results, start=1):
            rankings[str(item["id"])] = i
        for i, item in enumerate(page2_results, start=len(page1_results) + 1):
            rankings[str(item["id"])] = i

        set_cached_trending_snapshot(endpoint, rankings)
        snapshot = rankings

    rank = snapshot.get(str(tmdb_id))

    if rank:
        logger.info(f"Trending rank for {tmdb_id}: #{rank}")

    return rank


async def fetch_release_status(
    client: httpx.AsyncClient,
    tmdb_id: str,
    tmdb_key: str,
    media_type: str,
    tmdb_status: str | None,
) -> str | None:
    """
    Determine the current release status for the info sash.

    TV shows: mapped from the TMDB ``status`` field (already fetched as part
    of poster metadata, so no extra API call is needed).

    Movies: consults ``/movie/{id}/release_dates`` to determine whether the
    film is on physical media (Physical), digital/streaming (Streaming), still
    theatrical-only (Cinema), or not yet released (Production).  Result is
    cached for 7 days via the ``release_status_cache`` table.

    Returns one of: "Physical" | "Streaming" | "Cinema" | "Production" |
                    "Returning" | "Ended" | "Cancelled" | None.
    """
    cache_key = f"{media_type}_{tmdb_id}"
    cached = get_cached_release_status(cache_key)
    if cached:
        return cached

    result: str | None = None

    if media_type in ("tv", "series"):
        # No extra API call — map the TMDB status field we already have.
        # "Ended" and "Cancelled" both mean the show has fully aired; assume
        # it's on streaming rather than showing a run-status label that says
        # nothing about where you can actually watch it.  "Cancelled" is kept
        # distinct so users know the story may be unresolved.
        _tv_map: dict[str, str] = {
            "Returning Series": "Airing",
            "In Production":    "Production",
            "Planned":          "Production",
            "Pilot":            "Production",
            "Ended":            "Streaming",  # completed run → assume available on streaming
            "Cancelled":        "Cancelled",
            "Canceled":         "Cancelled",
        }
        result = _tv_map.get(tmdb_status or "")
    else:
        # For movies already known to be pre-release, skip the API call.
        _pre_release = {"In Production", "Post Production", "Planned", "Rumored"}
        if tmdb_status in _pre_release:
            result = "Production"
        elif tmdb_status == "Cancelled":
            result = "Cancelled"
        else:
            # Fetch release dates to distinguish Physical / Streaming / Cinema.
            # TMDB release date types:
            #   3 = Theatrical   4 = Digital   5 = Physical   6 = TV (broadcast/cable)
            # Type 6 covers TV movies and specials that never had a theatrical run;
            # treat it the same as digital/streaming since those titles are now on
            # streaming platforms.  If the movie is marked "Released" by TMDB but has
            # no matching release date entries (common for older/obscure titles with
            # incomplete TMDB data), default to "Streaming" rather than "Production".
            try:
                logger.info(f"External API Call: TMDB release_dates for movie {tmdb_id}")
                resp = await client.get(
                    f"https://api.themoviedb.org/3/movie/{tmdb_id}/release_dates",
                    params={"api_key": tmdb_key},
                )
                resp.raise_for_status()
                today = _date.today()
                has_physical = has_digital = has_theatrical = False
                for entry in resp.json().get("results", []):
                    for rd in entry.get("release_dates", []):
                        rtype = rd.get("type")
                        date_str = (rd.get("release_date") or "")[:10]
                        try:
                            rdate = _date.fromisoformat(date_str)
                        except (ValueError, TypeError):
                            continue
                        if rdate > today:
                            continue
                        if rtype == 5:
                            has_physical = True
                        elif rtype in (4, 6):   # digital or TV broadcast
                            has_digital = True
                        elif rtype == 3:
                            has_theatrical = True

                if has_physical:
                    result = "Physical"
                elif has_digital:
                    result = "Streaming"
                elif has_theatrical:
                    result = "Cinema"
                elif tmdb_status == "Released":
                    # Released per TMDB but no release date records found —
                    # incomplete TMDB data rather than genuinely unreleased.
                    result = "Streaming"
                else:
                    result = "Production"
            except Exception as exc:
                logger.warning(f"fetch_release_status failed for {tmdb_id}: {exc}")
                return None

    if result:
        set_cached_release_status(cache_key, result)
    return result


# ---------------------------------------------------------------------------
# Logo rendering (onto poster)
# ---------------------------------------------------------------------------



def composite_logo(
    image: Image.Image,
    logo: Image.Image,
    *,
    max_w_ratio: float = LOGO_MAX_W_RATIO,
    max_h_ratio: float = LOGO_MAX_H_RATIO,
    bottom_ratio: float = LOGO_BOTTOM_RATIO,
) -> None:
    width, height = image.size

    max_w = int(width  * max_w_ratio)
    # Height is bounded by BOTH the ratio and an absolute pixel ceiling, so a
    # raised Height slider can't let tall logos take over the poster.
    max_h = min(int(height * max_h_ratio), LOGO_ABS_MAX_H)

    # ── Tight crop: ignore faint glow / halo / anti-alias pixels ──────────────
    # A plain getbbox() keys off ANY non-zero alpha, so baked-in soft shadows,
    # outer glows, or stray anti-aliased specks inflate the bounding box and
    # throw off the width-based normalisation below.  Threshold the alpha first
    # so only reasonably solid pixels define the box, then fall back to the full
    # alpha bbox if thresholding leaves nothing (e.g. a deliberately faint logo).
    alpha = logo.getchannel("A")
    solid = alpha.point(lambda a: 255 if a > 32 else 0)
    bbox  = solid.getbbox() or alpha.getbbox()
    if bbox:
        logo = logo.crop(bbox)

    lw, lh = logo.width, logo.height
    if lw <= 0 or lh <= 0:
        return

    # ── Normalise size by AREA, with hard caps on both axes ───────────────────
    # We target a constant geometric mean of the two caps (one overall size),
    # then clamp to the caps preserving aspect ratio.  BOTH caps are now hard
    # ceilings: the configured Width and Height ratios are the true maximums a
    # logo will ever reach.  Fill stretching may grow a slim logo UP TO a cap,
    # but never past it — so logos can't sprawl toward the borders.
    aspect = lw / lh

    # Orientation, kept for the sizing telemetry below: -1 (tall) .. +1 (wide).
    orient    = float(np.tanh(np.log(aspect / LOGO_ASPECT_PIVOT)))
    eff_max_w = max_w                       # hard width ceiling
    eff_max_h = max_h                       # hard height ceiling (already ≤ LOGO_ABS_MAX_H)

    # Overall size target comes from the BASE caps so the average logo size stays
    # consistent; the flex only relaxes the clamp for the dominant axis.
    target = (max_w * max_h) ** 0.5
    new_w  = target * (aspect ** 0.5)
    new_h  = target / (aspect ** 0.5)

    if new_w > eff_max_w:
        new_h *= eff_max_w / new_w
        new_w  = eff_max_w
    if new_h > eff_max_h:
        new_w *= eff_max_h / new_h
        new_h  = eff_max_h

    # Single-axis fill: after the aspect-preserving clamp, one dimension is
    # pinned to its cap and the other sits below it.  Stretch that under-cap
    # dimension toward its cap to give slim logos more presence.
    #
    # The HEIGHT stretch only fires for genuinely short logos (below the
    # trigger fraction of the cap) and its strength scales with HOW short the
    # logo is: one sitting right at the trigger gets ~1.0× (barely touched),
    # while a far-shorter logo ramps up toward the full LOGO_FILL_STRETCH.
    # This avoids over-stretching logos that only just qualify.
    trigger_h = eff_max_h * LOGO_FILL_HEIGHT_TRIGGER
    if new_h < trigger_h:
        t      = (trigger_h - new_h) / trigger_h          # 0 at trigger → 1 near zero
        factor = 1.0 + t * (LOGO_FILL_STRETCH - 1.0)
        new_h  = min(eff_max_h, float(LOGO_ABS_MAX_H), new_h * factor)
    elif new_w < eff_max_w:
        new_w = min(eff_max_w, new_w * LOGO_FILL_STRETCH)

    # Logo sizing telemetry — gated behind DEBUG_LOGO_SIZING (off by default).
    if DEBUG_LOGO_SIZING:
        logger.info(
            f"LOGO SIZE: src={lw}x{lh} aspect={aspect:.2f} orient={orient:+.2f} "
            f"max_h={max_h} eff_max_h={eff_max_h:.0f} → final={int(new_w)}x{int(new_h)}"
        )

    logo = logo.resize((max(1, int(new_w)), max(1, int(new_h))), Image.LANCZOS)

    # ── Position ─────────────────────────────────────────────────────────────
    # Centre every logo on a fixed vertical line rather than sharing a common
    # bottom edge.  Bottom-anchoring made short single-line logos sit low and
    # feel like they lacked presence next to tall multi-line logos.  The centre
    # line is the midline of the tallest possible logo (the height cap plus its
    # aspect flex), so the tallest logos still bottom out at the intended
    # baseline while shorter logos float up to share that same centre.
    logo_x   = round((width - logo.width) / 2)
    centre_y = logo_centre_y(height, bottom_ratio)
    logo_y   = int(centre_y - logo.height / 2)

    # ── Background-aware legibility adjustments ──────────────────────────────
    # Sample the poster region the logo will cover (pure poster, sampled before
    # the paste) and derive its mean colour + luminance.
    cx1 = max(0, logo_x)
    cy1 = max(0, logo_y)
    cx2 = min(width,  logo_x + logo.width)
    cy2 = min(height, logo_y + logo.height)
    if cx2 > cx1 and cy2 > cy1:
        bg_arr = np.array(image.crop((cx1, cy1, cx2, cy2)).convert("RGB"),
                          dtype=np.float32)
        bg_r   = float(bg_arr[:, :, 0].mean())
        bg_g   = float(bg_arr[:, :, 1].mean())
        bg_b   = float(bg_arr[:, :, 2].mean())
        bg_lum = (0.2126 * bg_r + 0.7152 * bg_g + 0.0722 * bg_b) / 255.0

        # ── Experimental: contrast rescue ────────────────────────────────────
        # If the logo's average colour sits too close to the background's, the
        # title blends in (e.g. a red logo over a warm orange poster).  Recolour
        # it to white or black for guaranteed legibility — but ONLY when the
        # colour distance is small enough to be confident it's truly unreadable,
        # so well-contrasted logos are never touched.
        recoloured = False
        if LOGO_CONTRAST_RESCUE and LOGO_CONTRAST_MIN > 0:
            stats = _logo_color_stats(logo)
            if stats is not None:
                logo_rgb, variance = stats
                dist = (((logo_rgb[0] - bg_r) ** 2 +
                         (logo_rgb[1] - bg_g) ** 2 +
                         (logo_rgb[2] - bg_b) ** 2) ** 0.5) / 441.673  # 0–1
                if dist < LOGO_CONTRAST_MIN:
                    if variance > LOGO_COLOR_VARIANCE_MAX:
                        # Multi-colour / outline+fill logo — recolouring would
                        # destroy the internal contrast it relies on. Leave it.
                        logger.info(
                            f"Logo contrast rescue SKIPPED: dist={dist:.3f} but "
                            f"variance={variance:.3f} > {LOGO_COLOR_VARIANCE_MAX} "
                            f"(multi-colour logo preserved)"
                        )
                    else:
                        target, label = _recolor_target((bg_r, bg_g, bg_b), bg_lum)
                        logo = _recolor_logo_solid(logo, target)
                        recoloured = True
                        logger.info(
                            f"Logo contrast rescue: dist={dist:.3f} < "
                            f"{LOGO_CONTRAST_MIN}, variance={variance:.3f}, "
                            f"bg_lum={bg_lum:.2f} → recoloured to {label} "
                            f"rgb{target}"
                        )

        # Existing narrow rescue: whiten dark achromatic logos on dark posters.
        if not recoloured and bg_lum < 0.40:
            logo = ensure_light_logo(logo)

    image.paste(logo, (logo_x, logo_y), logo)