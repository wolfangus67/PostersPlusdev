#ratings.py
import logging
import math
import httpx
import numpy as np

logger = logging.getLogger(__name__)
from PIL import Image, ImageDraw, ImageFilter, ImageFont

try:
    import cairo as _cairo
    _HAS_CAIRO = True
except ImportError:
    _HAS_CAIRO = False
    logger.warning("pycairo not available — shape edges will use PIL (no antialiasing)")

from awards import FETCH_FAILED, _FetchFailed, _RateLimited
from config import (
    MOVIE_WEIGHTS,
    TV_WEIGHTS,
    GENRE_MAP,
    GENRE_PRIORITY,
    SCORE_NORMALISERS,
    SCORE_GLOW_THRESHOLD,
    SCORE_GLOW_BLUR,
    SCORE_GLOW_ALPHA,
)


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

async def fetch_rating(
    client: httpx.AsyncClient,
    imdb_id: str,
    mdblist_key: str,
    genre_ids: list[int],
    media_type: str = "movie",
    *,
    movie_weights: dict | None = None,
    tv_weights: dict | None = None,
) -> "tuple[dict | str, str, str | None, list[dict], int | None] | _FetchFailed | _RateLimited":
    """
    Returns ``(ratings_dict, genre, release_date, keywords, age_rating)`` on
    success, or ``FETCH_FAILED`` on a network / API error.
    """

    genre = "Unknown"
    for gid in GENRE_PRIORITY:
        if gid in genre_ids:
            genre = GENRE_MAP[gid]
            break

    mdb_type = "show" if media_type in ("tv", "series") else "movie"

    try:
        logger.info(f"External API Call: Requested ratings+keywords from MDBlist for {imdb_id}")
        resp = await client.get(
            f"https://api.mdblist.com/imdb/{mdb_type}/{imdb_id}",
            params={"apikey": mdblist_key, "append_to_response": "keyword"},
            timeout=10.0,
        )
    except Exception as exc:
        logger.error(f"MDblist request error for {imdb_id}: {type(exc).__name__}: {exc}")
        return FETCH_FAILED

    if resp.status_code == 429:
        retry_after: float | None = None
        raw = resp.headers.get("retry-after")
        if raw:
            try:
                # Most APIs send Retry-After as an integer seconds value.
                # HTTP-date format also exists but is uncommon for JSON APIs;
                # we don't try to parse it — caller will fall back to default.
                parsed = float(raw)
                if parsed > 0:
                    retry_after = parsed
            except ValueError:
                pass
        logger.warning(
            f"MDblist rate-limited for {imdb_id} (retry-after={retry_after})"
        )
        return _RateLimited(retry_after)

    if resp.status_code == 404:
        logger.info(f"MDblist 404 for {imdb_id} — title not found, returning empty result")
        return {}, genre, None, [], None

    if resp.status_code != 200:
        logger.warning(f"MDblist error {resp.status_code} for {imdb_id}")
        return FETCH_FAILED

    data         = resp.json()
    release_date = data.get("released")
    keywords: list[dict] = data.get("keywords") or []

    age_rating: int | None = data.get("age_rating") or None
    if age_rating is not None:
        try:
            age_rating = int(age_rating)
        except (ValueError, TypeError):
            age_rating = None

    ratings_dict: dict[str, float] = {}
    for r in data.get("ratings", []):
        source = (r.get("source") or "").lower()
        value  = r.get("value")
        if source in SCORE_NORMALISERS and value is not None:
            ratings_dict[source] = value

    return ratings_dict, genre, release_date, keywords, age_rating


# ---------------------------------------------------------------------------
# Score colour
# ---------------------------------------------------------------------------

def _score_color(score: int) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    if score < 50:
        return (255, 80, 80), (160, 40, 40)
    elif score < 70:
        return (255, 210, 90), (200, 150, 40)
    elif score < 85:
        return (120, 255, 160), (40, 170, 90)
    else:
        return (190, 140, 255), (186, 85, 211)


def _score_color_alt(score: int) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    """Six-band alternative: dark red → red → dark amber → yellow → dark green → bright green."""
    if score < 17:    # dark red
        return (180, 30,  30),  (120, 15,  15)
    elif score < 34:  # red
        return (255, 70,  70),  (200, 45,  45)
    elif score < 50:  # dark amber
        return (200, 130, 20),  (150, 90,  10)
    elif score < 67:  # yellow
        return (255, 215, 60),  (210, 165, 30)
    elif score < 84:  # dark green
        return (50,  160, 80),  (25,  110, 50)
    else:             # bright green
        return (110, 245, 150), (60,  190, 100)


def _score_color_metal(score: int) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    """Four-band metal palette mirroring the quality-tier badge colours: grey → bronze → silver → gold."""
    if score < 50:    # grey
        return (140, 140, 148), (90,  90,  98)
    elif score < 70:  # bronze
        return (210, 120,  50), (150, 80,  25)
    elif score < 85:  # silver
        return (218, 224, 240), (155, 165, 195)
    else:             # gold
        return (255, 210,  60), (200, 150,  25)


def _cairo_pill_mask(w: int, h: int, radius: int) -> Image.Image:
    """
    Return an antialiased greyscale pill mask (PIL 'L' mode) for use as an
    alpha mask when compositing solid-colour or gradient fills.

    Uses cairo's vector rasteriser (ANTIALIAS_BEST) when available so edges
    are smooth at any size.  Falls back to a plain PIL rounded_rectangle when
    pycairo is not installed — identical to the previous behaviour.
    """
    if _HAS_CAIRO:
        r = min(radius, w / 2, h / 2)
        surface = _cairo.ImageSurface(_cairo.FORMAT_A8, w, h)
        ctx = _cairo.Context(surface)
        ctx.set_antialias(_cairo.ANTIALIAS_BEST)
        ctx.set_source_rgba(1.0, 1.0, 1.0, 1.0)
        # Rounded-rectangle path built from four arcs
        ctx.new_sub_path()
        ctx.arc(w - r, r,     r, -math.pi / 2,  0.0)
        ctx.arc(w - r, h - r, r,  0.0,           math.pi / 2)
        ctx.arc(r,     h - r, r,  math.pi / 2,   math.pi)
        ctx.arc(r,     r,     r,  math.pi,        3 * math.pi / 2)
        ctx.close_path()
        ctx.fill()
        surface.flush()
        stride = surface.get_stride()
        arr = np.frombuffer(bytes(surface.get_data()), dtype=np.uint8).reshape((h, stride))[:, :w].copy()
        return Image.fromarray(arr, "L")
    else:
        mask = Image.new("L", (w, h), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            [(0, 0), (w - 1, h - 1)], radius=radius, fill=255
        )
        return mask


def _soften(rgb: tuple[int, int, int], amount: float = 0.9) -> tuple[int, int, int]:
    r, g, b = rgb
    return (
        int(r * amount + 255 * (1 - amount)),
        int(g * amount + 255 * (1 - amount)),
        int(b * amount + 255 * (1 - amount)),
    )


# ---------------------------------------------------------------------------
# Score bar  (horizontal)
# ---------------------------------------------------------------------------

def draw_score_bar(
    image: Image.Image,
    score: int | str,
    *,
    bottom_margin: int = 30,
    side_margin: int = 70,
    glow_threshold: int = SCORE_GLOW_THRESHOLD,
    glow_blur: int = SCORE_GLOW_BLUR,
    glow_alpha: int = SCORE_GLOW_ALPHA,
    color_mode: int = 0,
) -> None:
    if score is None:
        return
    if isinstance(score, str):
        try:
            score = int(score)
        except ValueError:
            return
    score = max(0, min(int(score), 100))
    W, H = image.size
    bar_h  = max(8, round(H * 0.012))
    x0, x1 = side_margin, W - side_margin
    y1, y0  = H - bottom_margin, H - bottom_margin - bar_h
    bar_w   = x1 - x0
    fill_w  = int(bar_w * (score / 100))
    radius  = min(bar_h // 2, 8)

    # ── Track (background pill) ───────────────────────────────────────────
    # Drawn before the early-return so score=0 still shows an empty track
    # rather than no bar at all (which would be visually indistinguishable
    # from "no rating available").
    track_mask = _cairo_pill_mask(bar_w, bar_h, radius)
    track_mask = track_mask.point(lambda v: v * 45 // 255)   # scale to fill alpha
    track_strip = Image.new("RGBA", (bar_w, bar_h), (255, 255, 255, 0))
    track_strip.putalpha(track_mask)
    track = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    track.paste(track_strip, (x0, y0))
    image.alpha_composite(track)

    if fill_w <= 0:
        return

    _color_fn = {1: _score_color_alt, 2: _score_color_metal}.get(color_mode, _score_color)
    left_color, right_color = _color_fn(score)
    left_color  = _soften(left_color,  0.90)
    right_color = _soften(right_color, 0.90)

    # ── Filled segment — numpy gradient, no Python pixel loop ────────────
    # Build an (bar_h × fill_w) RGB array by interpolating left→right colour.
    t = np.linspace(0, 1, fill_w, dtype=np.float32)               # (fill_w,)
    r_ch = (left_color[0] * (1 - t) + right_color[0] * t).astype(np.uint8)
    g_ch = (left_color[1] * (1 - t) + right_color[1] * t).astype(np.uint8)
    b_ch = (left_color[2] * (1 - t) + right_color[2] * t).astype(np.uint8)
    a_ch = np.full(fill_w, 220, dtype=np.uint8)

    # Stack into RGBA (fill_w, 4), then broadcast to (bar_h, fill_w, 4)
    row  = np.stack([r_ch, g_ch, b_ch, a_ch], axis=1)             # (fill_w, 4)
    grad_arr = np.broadcast_to(row, (bar_h, fill_w, 4)).copy()    # (bar_h, fill_w, 4)
    grad = Image.fromarray(grad_arr, "RGBA")

    # Rounded left/right mask — cairo-antialiased pill, right end cropped flat
    # when score < 99 so the cut-off aligns cleanly with the track edge.
    if score >= 99:
        mask_img = _cairo_pill_mask(fill_w, bar_h, radius)
    else:
        mask_w   = fill_w + radius       # extend right so the right cap is hidden by crop
        full_msk = _cairo_pill_mask(mask_w, bar_h, radius)
        mask_img = full_msk.crop((0, 0, fill_w, bar_h))

    fill_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    fill_layer.paste(grad, (x0, y0), mask_img)
    image.alpha_composite(fill_layer)

    # ── Highlight sliver ─────────────────────────────────────────────────
    hl = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(hl).line(
        [(x0 + radius, y0 + 1), (x0 + fill_w - 1, y0 + 1)],
        fill=(255, 255, 255, 60),
        width=1,
    )
    image.alpha_composite(hl)

    # ── Glow ─────────────────────────────────────────────────────────────
    if score >= glow_threshold:
        expand = glow_blur * 2
        # The glow is a thin strip at the bottom of the poster.  Render + blur it
        # on just its (padded) bounding box rather than a full-poster-size layer —
        # GaussianBlur cost scales with area, so this is ~50× less work for a
        # pixel-identical result.  pad gives the blur kernel room so its soft tail
        # isn't clipped; clamping to the canvas mirrors the old full-layer bounds.
        rx0, ry0 = x0 - expand,          y0 - expand
        rx1, ry1 = x0 + fill_w + expand, y1 + expand
        pad = glow_blur * 3 + 2
        cx0, cy0 = max(0, rx0 - pad), max(0, ry0 - pad)
        cx1, cy1 = min(W, rx1 + pad), min(H, ry1 + pad)
        glow = Image.new("RGBA", (cx1 - cx0, cy1 - cy0), (0, 0, 0, 0))
        ImageDraw.Draw(glow).rounded_rectangle(
            [(rx0 - cx0, ry0 - cy0), (rx1 - cx0, ry1 - cy0)],
            radius=radius + expand,
            fill=(255, 255, 255, glow_alpha),
        )
        glow = glow.filter(ImageFilter.GaussianBlur(glow_blur))
        image.alpha_composite(glow, dest=(cx0, cy0))


# ---------------------------------------------------------------------------
# Score bar  (vertical pip)
# ---------------------------------------------------------------------------

def _draw_solid_pip(
    image: Image.Image,
    *,
    x: float,
    y_center: int,
    width: int,
    height: int,
    color: tuple[int, int, int],
) -> None:
    """Draw a single solid-colour cairo-antialiased pill pip onto *image*.

    Shared primitive used by score-driven pips (where the caller computes
    the colour from the score palette).
    """
    y0     = int(y_center - height / 2)
    radius = max(1, width // 2)

    pip_mask  = _cairo_pill_mask(width, height, radius)
    pip_strip = Image.new("RGBA", (width, height), (*color, 0))
    pip_strip.putalpha(pip_mask)
    pip_layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    pip_layer.paste(pip_strip, (int(x), y0))
    image.alpha_composite(pip_layer)


def draw_score_bar_vertical(
    image: Image.Image,
    score: int | str,
    *,
    x: float,
    y_center: int,
    height: int = 36,
    width: int = 4,
    color_mode: int = 0,
) -> None:
    if score is None:
        return
    if isinstance(score, str):
        try:
            score = int(score)
        except ValueError:
            return

    score = max(0, min(int(score), 100))
    _color_fn = {1: _score_color_alt, 2: _score_color_metal}.get(color_mode, _score_color)
    left_color, _right_color = _color_fn(score)
    _draw_solid_pip(image, x=x, y_center=y_center, width=width, height=height, color=left_color)


# ---------------------------------------------------------------------------
# Frosted bar (rating_display_mode == 4)
# ---------------------------------------------------------------------------

def draw_frosted_bar(
    image: Image.Image,
    left_text: str,
    center_text: str,
    right_text: str,
    bar_height_ratio: float = 0.090,
    font_size_ratio: float = 0.40,
    frost_opacity: float = 0.75,
    bottom_inset: float = 0.0,
    style: str = "frosted",
    score: int | str | None = None,
    fill_color: tuple[int, int, int] | None = None,
) -> Image.Image:
    """Full-width frosted glass or dark-body strip near the bottom of the poster.

    style="frosted"        — plain frosted glass body, dark text.
    style="silver"         — dark body, solid silver accent stripe, silver text.
    style="gold"           — dark body, solid gold accent stripe, silver text.
    style="rating_black"   — dark body, rating progress bar (fill_color drives colour).
    style="rating_frosted" — frosted body, dark semi-transparent rating bar for contrast.
    fill_color pre-resolved accent colour for rating_black (ignored for rating_frosted).
    """
    import os, colorsys as _cs

    width, height = image.size
    bar_h = max(24, int(height * bar_height_ratio))
    bar_y = height - bar_h - int(height * bottom_inset)

    # ── Font ─────────────────────────────────────────────────────────────────
    font_size = max(10, int(bar_h * font_size_ratio))
    font_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "fonts", "Inter-Bold.ttf"
    )
    try:
        font = ImageFont.truetype(font_path, font_size)
    except IOError:
        font = ImageFont.load_default()

    _REF   = "Agypq0★·"
    _ref_b = ImageDraw.Draw(Image.new("RGBA", (1, 1))).textbbox((0, 0), _REF, font=font)
    # Pure optical centering — no base nudge; the stripe branches add their own
    # downward compensation to account for the accent bar stealing top space.
    text_y = (bar_h - (_ref_b[3] - _ref_b[1])) // 2 - _ref_b[1]

    _SILVER = (210, 210, 218)
    _GOLD   = (212, 175, 55)
    # Solid accent styles use a thin stripe; rating bar modes use a larger one.
    _accent_stripe = max(2, int(bar_h * 0.06))
    _rating_stripe = max(3, int(bar_h * 0.10))
    stripe = _accent_stripe  # overridden per branch below
    _lift          = max(1, int(bar_h * 0.025))   # small upward correction for non-plain styles
    _stripe_nudge  = max(1, int(bar_h * 0.05)) + _rating_stripe // 2 - _lift

    def _score_pct() -> int:
        try:    return max(0, min(int(score), 100))   # type: ignore[arg-type]
        except: return 0

    def _build_frosted_base() -> tuple[Image.Image, float, float, float]:
        """Returns (bar_img, raw_h, raw_s, raw_v) — HSV before lightening."""
        blur_r = max(6, int(bar_h * 0.45))
        cy = max(0, bar_y); ch = min(bar_h, height - cy)
        reg = image.crop((0, cy, width, cy + ch))
        blr = reg.filter(ImageFilter.GaussianBlur(radius=blur_r))
        th  = blr.resize((8, 8), Image.LANCZOS).convert("RGB")
        ar  = np.array(th, dtype=np.float32)
        dr, dg, db = ar[:,:,0].mean(), ar[:,:,1].mean(), ar[:,:,2].mean()
        _h2, _s2, _v2 = _cs.rgb_to_hsv(dr/255, dg/255, db/255)
        tr, tg, tb = _cs.hsv_to_rgb(_h2, min(1.0, _s2*1.2), _v2*0.4+0.60)
        r, g, b = int(tr*255*0.6+255*0.4), int(tg*255*0.6+255*0.4), int(tb*255*0.6+255*0.4)
        base  = blr.resize((width, bar_h), Image.LANCZOS).convert("RGBA")
        frost = Image.new("RGBA", (width, bar_h), (r, g, b, int(frost_opacity*255)))
        return Image.alpha_composite(base, frost), _h2, _s2, _v2

    if style == "pure_black":
        ink = (*_SILVER, 248)
        arr = np.zeros((bar_h, width, 4), dtype=np.uint8)
        arr[:, :, :3] = 12;  arr[:, :, 3] = int(frost_opacity * 255)
        bar_img = Image.fromarray(arr, "RGBA")
        text_y += max(1, int(bar_h * 0.05)) + _accent_stripe // 2 - _lift

    elif style in ("silver", "gold"):
        stripe = _accent_stripe
        accent = _GOLD if style == "gold" else _SILVER
        ink    = (*_SILVER, 248)
        arr    = np.zeros((bar_h, width, 4), dtype=np.uint8)
        arr[:, :, :3] = 12;  arr[:, :, 3] = int(frost_opacity * 255)
        arr[:stripe, :, 0] = accent[0]; arr[:stripe, :, 1] = accent[1]
        arr[:stripe, :, 2] = accent[2]; arr[:stripe, :, 3] = 240
        bar_img = Image.fromarray(arr, "RGBA")
        text_y += max(1, int(bar_h * 0.05)) + stripe // 2 - _lift

    elif style == "rating_black":
        stripe = _rating_stripe
        fc  = fill_color or _SILVER
        dim = tuple(max(0, int(c * 0.20)) for c in fc)
        ink = (*_SILVER, 248)
        arr = np.zeros((bar_h, width, 4), dtype=np.uint8)
        arr[:, :, :3] = 12;  arr[:, :, 3] = int(frost_opacity * 255)
        # Unfilled
        arr[:stripe, :, 0] = dim[0]; arr[:stripe, :, 1] = dim[1]
        arr[:stripe, :, 2] = dim[2]; arr[:stripe, :, 3] = 240
        # Filled
        fw = int(width * _score_pct() / 100)
        if fw > 0:
            arr[:stripe, :fw, 0] = fc[0]; arr[:stripe, :fw, 1] = fc[1]
            arr[:stripe, :fw, 2] = fc[2]; arr[:stripe, :fw, 3] = 240
        bar_img = Image.fromarray(arr, "RGBA")
        text_y += _stripe_nudge

    elif style == "rating_frosted":
        stripe = _rating_stripe
        ink = (15, 15, 15, 248)
        bar_img, _, _, _ = _build_frosted_base()
        if fill_color is not None:
            # Explicit colour chosen — use it directly.
            fill_col = fill_color
            dim_col  = tuple(max(0, int(c * 0.12)) for c in fill_col)
        else:
            # Colour Sample: derive a contrasting fill from the bar's own tint.
            # The frosted tint's effective value ≈ _v2*0.4+0.60; if the bar is
            # light go darker, if dark go brighter — always staying hue-matched.
            bar_img2, _h2, _s2, _v2 = _build_frosted_base()
            bar_img = bar_img2  # rebuild with HSV data
            _tint_v = _v2 * 0.4 + 0.60
            if _tint_v > 0.70:  # light bar → dark fill
                _fv = max(0.15, _v2 * 0.30)
            else:                # dark bar → bright fill
                _fv = min(1.0, _v2 * 0.40 + 0.70)
            fr2, fg2, fb2 = _cs.hsv_to_rgb(_h2, min(1.0, _s2 * 1.6), _fv)
            fill_col = (int(fr2 * 255), int(fg2 * 255), int(fb2 * 255))
            dim_col  = tuple(max(0, int(c * 0.12)) for c in fill_col)
        fw = int(width * _score_pct() / 100)
        sa = np.zeros((stripe, width, 4), dtype=np.uint8)
        sa[:, :, 0] = dim_col[0]; sa[:, :, 1] = dim_col[1]
        sa[:, :, 2] = dim_col[2]; sa[:, :, 3] = 90
        if fw > 0:
            sa[:, :fw, 0] = fill_col[0]; sa[:, :fw, 1] = fill_col[1]
            sa[:, :fw, 2] = fill_col[2]; sa[:, :fw, 3] = 230
        bar_img.alpha_composite(Image.fromarray(sa, "RGBA"), (0, 0))
        text_y += _stripe_nudge

    else:  # plain frosted — small nudge down, no stripe compensation needed
        ink = (15, 15, 15, 248)
        bar_img, _, _, _ = _build_frosted_base()
        text_y += max(1, int(bar_h * 0.03))

    txt_layer = Image.new("RGBA", (width, bar_h), (0, 0, 0, 0))
    td        = ImageDraw.Draw(txt_layer)
    h_pad     = max(20, int(width * 0.055))

    if center_text:
        cw = int(td.textlength(center_text, font=font))
        td.text(((width - cw) // 2, text_y), center_text, font=font, fill=ink)
    if left_text:
        td.text((h_pad, text_y), left_text, font=font, fill=ink)
    if right_text:
        rw = int(td.textlength(right_text, font=font))
        td.text((width - h_pad - rw, text_y), right_text, font=font, fill=ink)

    bar_final = Image.alpha_composite(bar_img, txt_layer)
    result    = image.copy()
    result.alpha_composite(bar_final, (0, bar_y))
    return result


# Weighted score
# ---------------------------------------------------------------------------

def calculate_weighted_score(
    ratings: dict,
    weights: dict,
) -> int | str:

    total_weight = 0.0
    weighted_sum = 0.0

    for source, value in ratings.items():
        if source not in weights:
            continue

        weight = weights[source]

        if weight == 0:
            continue

        normaliser = SCORE_NORMALISERS.get(source)
        if not normaliser:
            logger.warning(f"No normaliser for source '{source}' — skipping")
            continue

        weighted_sum += normaliser(value) * weight
        total_weight += weight

    if total_weight == 0:
        return "N/A"

    return round(weighted_sum / total_weight)