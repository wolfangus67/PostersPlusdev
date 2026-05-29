#tmdb.py
import asyncio
import io
import logging
from datetime import date as _date, datetime as _datetime
import httpx
import numpy as np

logger = logging.getLogger(__name__)
from PIL import Image

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


def ensure_light_logo(logo: Image.Image, threshold: float = 0.2) -> Image.Image:
    """
    If the visible pixels of *logo* are too dark, force them all to white.
    Uses numpy for vectorised luminance calculation — avoids materialising
    a Python list of per-pixel tuples.
    """
    rgba = np.array(logo.convert("RGBA"), dtype=np.float32)   # H×W×4
    alpha = rgba[:, :, 3]
    visible_mask = alpha > 30                                  # boolean H×W

    if not visible_mask.any():
        return logo

    r = rgba[:, :, 0][visible_mask]
    g = rgba[:, :, 1][visible_mask]
    b = rgba[:, :, 2][visible_mask]
    avg_lum = (0.2126 * r + 0.7152 * g + 0.0722 * b).mean() / 255.0

    if avg_lum > threshold:
        return logo

    # Force visible pixels to white, preserve alpha channel
    out = rgba.copy()
    out[:, :, 0][visible_mask] = 255
    out[:, :, 1][visible_mask] = 255
    out[:, :, 2][visible_mask] = 255
    return Image.fromarray(out.astype(np.uint8), "RGBA")


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

    genre_ids            = [g["id"] for g in data.get("genres", [])]
    credits              = data.get("credits", {})
    production_companies = data.get("production_companies", [])
    original_language    = data.get("original_language")
    runtime              = data.get("runtime")
    number_of_seasons    = data.get("number_of_seasons")
    number_of_episodes   = data.get("number_of_episodes")
    tmdb_status          = data.get("status")   # e.g. "Released", "In Production", "Returning Series"

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
    )

    tmdb_data = {
        "credits":              credits,
        "production_companies": production_companies,
        "original_language":    original_language,
        "runtime":              runtime,
        "number_of_seasons":    number_of_seasons,
        "number_of_episodes":   number_of_episodes,
        "tmdb_status":          tmdb_status,
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


def _saliency_crop_left(image: Image.Image, crop_w: int) -> int:
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
    from PIL import ImageFilter

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
) -> Image.Image:
    """
    Fetch, saliency-crop, and cache a TMDB backdrop as a portrait poster.

    Backdrops are 16:9 landscape; we take the full height and cut a 2:3 strip
    whose horizontal position is chosen by gradient-magnitude saliency rather
    than always defaulting to the centre.  This keeps the main subject in frame
    when cinematographers frame wide shots off-centre.
    Cached under the same JPEG scheme as regular posters.
    """
    cache_key = f"backdrop_{tmdb_id}_{backdrop_path.strip('/')}"
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

    # Saliency-aware crop: keep full height, cut the most active 2:3 strip.
    w, h   = image.size
    crop_w = int(h * 2 / 3)
    if crop_w < w:
        left = _saliency_crop_left(image, crop_w)
        logger.info(
            f"Backdrop saliency crop for {tmdb_id}: "
            f"left={left} (centre would be {(w - crop_w) // 2}) of w={w}"
        )
        image = image.crop((left, 0, left + crop_w, h))

    image = normalise_poster(image)

    buf = io.BytesIO()
    image.convert("RGB").save(buf, format="JPEG", quality=92)
    set_cached_tmdb_poster(cache_key, buf.getvalue())

    return image


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

    logo = ensure_light_logo(logo)

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
    skip_native: bool = False,
) -> Image.Image | None:
    """
    Fetch the best available logo for a title, with a Metahub CDN fallback.

    Resolution order:
      1. TMDB logo in the requested language (logo_language).
      2. TMDB logo in the content's original language (original_language) —
         helps foreign titles that only have a native-language logo on TMDB.
         Skipped when skip_native=True (caller prefers a text-title fallback).
      3. TMDB language-neutral logo (iso_639_1 is null/"").
      4. TMDB English logo.
      5. Metahub CDN logo (images.metahub.space) — requires imdb_id.
      6. None — no logo available; the caller may render the translated title
         as text instead.

    All results are cached locally so repeat requests never hit external APIs.
    """
    _png = [lg for lg in logos if lg["file_path"].endswith(".png")]

    preferred = [lg for lg in _png if lg.get("iso_639_1") == logo_language]
    # Native: original-language logos, skipped when it duplicates preferred
    # (same bucket), when original_language wasn't provided, or when the caller
    # has opted for a text-title fallback over a native-language logo.
    native    = (
        [lg for lg in _png if lg.get("iso_639_1") == original_language]
        if (original_language and original_language != logo_language and not skip_native)
        else []
    )
    neutral   = [lg for lg in _png if lg.get("iso_639_1") in (None, "")]
    english   = [lg for lg in _png if lg.get("iso_639_1") == "en"]

    # Resolution order: requested language → content's original language →
    # language-neutral → English → Metahub CDN → None.
    # Note: when logo_language == "en", preferred and english are the same bucket;
    # the "or" short-circuits so english is never tried twice.
    candidates = preferred or native or neutral or english

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

    logo_cache_key = logo_path.strip('/').replace('/', '_')
    cached_bytes = get_cached_tmdb_logo(logo_cache_key)

    if cached_bytes:
        logger.info("TMDB logo cache hit")
        logo = Image.open(io.BytesIO(cached_bytes)).convert("RGBA")
        return logo

    resp = await client.get(f"https://image.tmdb.org/t/p/w500{logo_path}")
    logger.info(f"External API Call: Requested logo from TMDB")
    resp.raise_for_status()

    logo = Image.open(io.BytesIO(resp.content)).convert("RGBA")

    bbox = logo.getchannel("A").getbbox()
    if bbox:
        logo = logo.crop(bbox)

    logo = ensure_light_logo(logo)

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
    max_h = int(height * max_h_ratio)

    logo.thumbnail((max_w, max_h), Image.LANCZOS)

    alpha_bbox = logo.getchannel("A").getbbox()
    if alpha_bbox:
        logo = logo.crop(alpha_bbox)

    logo_x = round((width - logo.width) / 2)
    logo_y = height - int(height * bottom_ratio) - logo.height

    image.paste(logo, (logo_x, logo_y), logo)