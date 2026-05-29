import logging
import os
import re

import httpx

logger = logging.getLogger(__name__)
from PIL import Image, ImageDraw, ImageFont

from awards import FETCH_FAILED
from cache import set_cached_quality
from config import (
    AIOSTREAMS_AUTH,
    AIOSTREAMS_URL,
    BADGE_DIR,
    BADGE_FILES,
    BADGE_HEIGHT,
    QUALITY_LABELS,
)


# ---------------------------------------------------------------------------
# Token extraction
# ---------------------------------------------------------------------------

def _extract_tokens_from_parsed_file(parsed: dict) -> set[str]:
    tokens: set[str] = set()

    res = parsed.get("resolution", "")
    if res == "2160p":
        tokens.add("4K")
    elif res == "1080p":
        tokens.add("1080P")

    visual_tags = {t.upper() for t in parsed.get("visualTags", [])}
    if "DV" in visual_tags or "DOLBY VISION" in visual_tags or "DOVI" in visual_tags:
        tokens.add("DV")
    if "HDR10+" in visual_tags:
        tokens.add("HDR10+")
    if "HDR10" in visual_tags or "HDR" in visual_tags:
        tokens.add("HDR10")

    quality = parsed.get("quality", "").upper()
    if "REMUX" in quality:
        tokens.add("REMUX")
    elif quality == "WEB-DL":
        tokens.add("WEBDL")

    audio_tags = {t.upper() for t in parsed.get("audioTags", [])}
    if any("ATMOS" in t for t in audio_tags):
        tokens.add("ATMOS")
    if "DTS:X" in audio_tags or "DTSX" in audio_tags or "DTS-X" in audio_tags:
        tokens.add("DTSX")

    return tokens


def parse_quality(quality_param: str) -> list[str]:
    """Parse a comma-separated quality string into validated tokens."""
    if not quality_param:
        return []
    tokens = []
    for token in quality_param.split(","):
        token = token.strip()
        if token in QUALITY_LABELS:
            tokens.append(token)
        else:
            logger.warning(f"Unknown quality token ignored: {token!r}")
    return tokens


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

async def fetch_quality_from_aiostreams(
    client: httpx.AsyncClient,
    imdb_id: str,
    media_type: str = "movie",
    season: int = 1,
    episode: int = 1,
    release_date: str | None = None,
) -> "list[str] | _FetchFailed":
    """
    Returns a list of quality tokens on success (may be empty if the title
    has no streams), or ``FETCH_FAILED`` on a network / API error.

    NOTE: The caller (main.py) is responsible for checking the quality cache
    *before* calling this function and should only call it on a cache miss.
    This function no longer performs a redundant cache read; it only writes
    to the cache after a successful fetch.
    """
    if not AIOSTREAMS_URL or not AIOSTREAMS_AUTH:
        logger.info("AIOStreams URL or auth not configured — skipping quality fetch")
        return []

    if media_type in ("tv", "series"):
        aio_id   = f"{imdb_id}:{season}:{episode}"
        aio_type = "series"
    else:
        aio_id   = imdb_id
        aio_type = "movie"

    try:
        logger.info(f"External API Call: AIOStreams Quality Fetch For {imdb_id}")
        resp = await client.get(
            f"{AIOSTREAMS_URL.rstrip('/')}/api/v1/search",
            params={"type": aio_type, "id": aio_id},
            headers={"Authorization": f"Basic {AIOSTREAMS_AUTH}"},
        )

        if resp.status_code != 200:
            logger.warning(f"AIOStreams error {resp.status_code} for {imdb_id}")
            return FETCH_FAILED

        payload = resp.json()
        if not payload.get("success"):
            err = (payload.get("error") or {}).get("message", "unknown error")
            logger.warning(f"AIOStreams returned failure for {imdb_id}: {err}")
            return FETCH_FAILED

        data    = payload.get("data") or {}
        results = data.get("results", [])
        errors  = data.get("errors") or {}

        if not results:
            if errors:
                logger.warning(
                    f"AIOStreams returned no results for {imdb_id} "
                    f"with scraper errors present: {errors}"
                )
                return FETCH_FAILED

            logger.info(f"AIOStreams returned authoritative empty result for {imdb_id}")
            tokens: list[str] = []
            set_cached_quality(imdb_id, tokens, release_date)
            return tokens

        seen: set[str] = set()
        for result in results[:5]:
            seen |= _extract_tokens_from_parsed_file(result.get("parsedFile") or {})

        tokens = []
        for res in ("4K", "1080P"):
            if res in seen:
                tokens.append(res)
                break
        for source in ("REMUX", "WEBDL"):
            if source in seen:
                tokens.append(source)
                break
        for visual in ("DV", "HDR10+", "HDR10"):
            if visual in seen:
                tokens.append(visual)
                break
        for audio in ("ATMOS", "DTSX"):
            if audio in seen:
                tokens.append(audio)
                break

        logger.info(f"AIOStreams quality for {imdb_id}: {tokens}")
        set_cached_quality(imdb_id, tokens, release_date)
        return tokens

    except Exception as exc:
        logger.error(f"AIOStreams fetch error for {imdb_id}: {type(exc).__name__}: {exc}")
        return FETCH_FAILED


# ---------------------------------------------------------------------------
# Stremio addon scraper (simplified quality source)
# ---------------------------------------------------------------------------
# Users who find AIOStreams complex can point PostersPlus directly at any
# Stremio addon that supports the stream endpoint — Torrentio, Comet, etc.
# They paste the manifest URL (or install link) from their configured addon
# page; PostersPlus derives the stream base URL and calls it like a regular
# Stremio addon client.


def _normalize_scraper_url(url: str) -> str:
    """Normalise a user-pasted Stremio addon URL to a bare base URL."""
    url = url.strip()
    # stremio:// install links → https://
    if url.startswith("stremio://"):
        url = "https://" + url[10:]
    # Strip /manifest.json suffix
    if url.endswith("/manifest.json"):
        url = url[: -len("/manifest.json")]
    return url.rstrip("/")


def _tokens_from_stremio_stream(
    name: str,
    title: str,
    behavior_hints: dict | None = None,
) -> set[str]:
    """
    Extract quality tokens from a single Stremio stream's name + title fields,
    plus optional behaviorHints (Torrentio-style).

    Stremio addons embed quality in either field; we scan both to be safe.
    The name field typically looks like "Torrentio\\n4K DV" or "Comet\\n1080p".
    The title field is usually a filename like "Movie.2023.2160p.WEB-DL.Atmos.mkv".

    Torrentio also provides richer structured data in behaviorHints:
      - bingeGroup: "torrentio|4k|BluRay REMUX|HDR"  (pipe-separated quality tokens)
      - filename:   "Movie.2023.BDREMUX.2160p.HDR.mkv" (clean release filename)
    Including these fields improves detection accuracy for Torrentio responses.
    """
    binge_group = ""
    filename = ""
    if behavior_hints:
        binge_group = behavior_hints.get("bingeGroup") or ""
        filename    = behavior_hints.get("filename") or ""
    text = f"{name}\n{title}\n{binge_group}\n{filename}".upper()
    tokens: set[str] = set()

    # Resolution
    if re.search(r'\b(2160P|4K|UHD)\b', text):
        tokens.add("4K")
    elif "1080P" in text:
        tokens.add("1080P")

    # HDR — order matters: check DV before HDR10+ before HDR10
    if re.search(r'\bDV\b|DOLBY.?VISION|\bDOVI\b', text):
        tokens.add("DV")
    if "HDR10+" in text:
        tokens.add("HDR10+")
    elif re.search(r'\bHDR10\b|\bHDR\b', text):
        tokens.add("HDR10")

    # Source
    if "REMUX" in text:
        tokens.add("REMUX")
    elif re.search(r'WEB.?DL|WEBDL', text):
        tokens.add("WEBDL")

    # Audio
    if "ATMOS" in text:
        tokens.add("ATMOS")
    if re.search(r'DTS.?X\b', text):
        tokens.add("DTSX")

    return tokens


async def fetch_quality_from_scraper(
    client: httpx.AsyncClient,
    scraper_url: str,
    imdb_id: str,
    media_type: str = "movie",
    season: int = 1,
    episode: int = 1,
    release_date: str | None = None,
) -> "list[str] | _FetchFailed":
    """
    Fetch quality tokens from a user-configured Stremio addon.

    ``scraper_url`` should be the addon's manifest URL or base URL — e.g.
    ``https://torrentio.stremio.ru/{config}/manifest.json`` or the bare
    base.  Both forms are normalised before use.

    Returns a list of quality tokens on success, or ``FETCH_FAILED`` on a
    network / API error.  The caller is responsible for checking the quality
    cache before calling this function; this function only writes on success.
    """
    base = _normalize_scraper_url(scraper_url)
    if not base:
        return []

    is_series = media_type in ("tv", "series")
    if is_series:
        stream_type = "series"
        stream_id   = f"{imdb_id}:{season}:{episode}"
    else:
        stream_type = "movie"
        stream_id   = imdb_id

    url = f"{base}/stream/{stream_type}/{stream_id}.json"

    try:
        logger.info(f"External API Call: Stremio scraper quality fetch for {imdb_id} → {url}")
        resp = await client.get(url, timeout=20.0, follow_redirects=True)

        if resp.status_code != 200:
            logger.warning(
                f"Scraper returned {resp.status_code} for {imdb_id} "
                f"(url={url})"
            )
            # For series, fall back to a show-level lookup (no season/episode).
            # Some addons support this and it avoids failures when a specific
            # episode isn't indexed yet.
            if is_series:
                fallback_url = f"{base}/stream/series/{imdb_id}.json"
                logger.info(
                    f"Trying show-level series fallback for {imdb_id} → {fallback_url}"
                )
                resp = await client.get(fallback_url, timeout=20.0, follow_redirects=True)
                if resp.status_code != 200:
                    logger.warning(
                        f"Scraper series fallback also returned {resp.status_code} "
                        f"for {imdb_id}"
                    )
                    return FETCH_FAILED
            else:
                return FETCH_FAILED

        streams = resp.json().get("streams") or []

        if not streams:
            logger.info(f"Scraper returned no streams for {imdb_id} — caching empty result")
            tokens: list[str] = []
            set_cached_quality(imdb_id, tokens, release_date)
            return tokens

        # Aggregate tokens across the top 5 streams (same logic as AIOStreams).
        seen: set[str] = set()
        for stream in streams[:5]:
            seen |= _tokens_from_stremio_stream(
                stream.get("name") or "",
                stream.get("title") or stream.get("description") or "",
                stream.get("behaviorHints"),
            )

        tokens = []
        for res in ("4K", "1080P"):
            if res in seen:
                tokens.append(res)
                break
        for source in ("REMUX", "WEBDL"):
            if source in seen:
                tokens.append(source)
                break
        for visual in ("DV", "HDR10+", "HDR10"):
            if visual in seen:
                tokens.append(visual)
                break
        for audio in ("ATMOS", "DTSX"):
            if audio in seen:
                tokens.append(audio)
                break

        logger.info(f"Scraper quality for {imdb_id}: {tokens}")
        set_cached_quality(imdb_id, tokens, release_date)
        return tokens

    except Exception as exc:
        logger.error(f"Scraper fetch error for {imdb_id}: {type(exc).__name__}: {exc}")
        return FETCH_FAILED


# ---------------------------------------------------------------------------
# Badge image cache
# ---------------------------------------------------------------------------
# The top-of-poster gradient ensures the background is always dark, so we
# always use the "light" variant.  The dark variant and luminosity sampling
# are therefore removed.
#
# Badges are cached in memory as pre-resized RGBA Images, keyed by
# (token, height).  The default height is pre-warmed at import time so the
# very first request never pays the resize cost.

BadgeItem = tuple[Image.Image | None, str]

# Raw (un-resized) badge images, loaded once from disk.
_RAW_BADGES: dict[str, Image.Image] = {}

# Resized badge cache: (token, height) -> Image
_BADGE_CACHE: dict[tuple[str, int], Image.Image] = {}


def _load_raw_badge(token: str) -> Image.Image | None:
    """Load and tightly crop the raw badge PNG for *token* (light variant only)."""
    stem = BADGE_FILES.get(token)
    if not stem:
        return None

    path = os.path.join(BADGE_DIR, f"{stem}_light.png")
    if not os.path.exists(path):
        logger.warning(f"Badge file not found: {path}")
        return None

    try:
        img = Image.open(path).convert("RGBA")
        bbox = img.getbbox()
        if bbox:
            img = img.crop(bbox)
        return img
    except Exception as exc:
        logger.error(f"Badge load failed ({path}): {exc}")
        return None


def _warm_badge_cache(height: int) -> None:
    """Pre-resize all known badges at *height* and store in _BADGE_CACHE."""
    for token in BADGE_FILES:
        raw = _RAW_BADGES.get(token)
        if raw is None:
            continue
        w, h = raw.size
        new_w = max(1, round(w * height / h))
        _BADGE_CACHE[(token, height)] = raw.resize((new_w, height), Image.LANCZOS)


def _init_badge_cache() -> None:
    """Load all raw badges and pre-warm the cache at the default badge height."""
    for token in BADGE_FILES:
        img = _load_raw_badge(token)
        if img is not None:
            _RAW_BADGES[token] = img

    _warm_badge_cache(BADGE_HEIGHT)
    logger.info(f"Badge cache warmed: {len(_BADGE_CACHE)} entries at {BADGE_HEIGHT}px")


# Run at import time (cheap — just disk reads + one resize pass per badge).
_init_badge_cache()


def get_resized_badge(token: str, height: int) -> Image.Image | None:
    """
    Return a cached resized badge for *token* at *height* pixels tall.
    Resizes and caches on first miss for a new height.
    """
    key = (token, height)
    cached = _BADGE_CACHE.get(key)
    if cached is not None:
        return cached

    raw = _RAW_BADGES.get(token)
    if raw is None:
        return None

    w, h = raw.size
    new_w = max(1, round(w * height / h))
    resized = raw.resize((new_w, height), Image.LANCZOS)
    _BADGE_CACHE[key] = resized
    return resized


# ---------------------------------------------------------------------------
# Fallback font (loaded once at module level)
# ---------------------------------------------------------------------------

try:
    _FALLBACK_FONT: ImageFont.FreeTypeFont | ImageFont.ImageFont = ImageFont.truetype(os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts", "Inter-Bold.ttf"), 28)
except IOError:
    _FALLBACK_FONT = ImageFont.load_default()


# ---------------------------------------------------------------------------
# Badge rendering
# ---------------------------------------------------------------------------

def render_badges_left(
    image: Image.Image,
    items: list[BadgeItem],
    x_start: int,
    y_top: int,
    badge_height: int,
    badge_gap: int,
) -> None:
    if not items:
        return

    draw = ImageDraw.Draw(image)
    x = x_start

    for badge_img, label in items:
        if badge_img is not None:
            image.paste(badge_img, (x, y_top), badge_img)
            x += badge_img.width + badge_gap
        else:
            # Text fallback
            bb = draw.textbbox((0, 0), label, font=_FALLBACK_FONT)
            text_h = bb[3] - bb[1]
            ty = y_top + (badge_height - text_h) // 2
            draw.text((x, ty), label, font=_FALLBACK_FONT, fill=(255, 255, 255, 220))
            x += (bb[2] - bb[0]) + badge_gap


