#main.py
import asyncio
import hashlib
import hmac
import io
import logging
import os
import re
import httpx
import numpy as np
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, HTMLResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageDraw, ImageFont

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    force=True,
)
# Pull uvicorn's loggers into our root handler so all output shares the same format.
for _uv_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
    _uv_logger = logging.getLogger(_uv_name)
    _uv_logger.handlers = []
    _uv_logger.propagate = True


class _TruncateUrlFilter(logging.Filter):
    """
    Redact API keys and truncate long URL paths in log records.

    Two responsibilities:
      1. For uvicorn.access records, truncate the request path so long URLs
         don't fill the log.
      2. For ALL records, redact every common API-key query parameter pattern
         in both record.msg and record.args.  This catches keys that slip
         through when an httpx exception is logged (its __str__ includes the
         full upstream URL with our outbound api_key=) as well as anything
         else that might inadvertently include a key.
    """
    _MAX = 80
    # Match query params we hold (tmdb_key, mdblist_key, access_key) AND the
    # upstream parameter names we forward keys under (api_key, apikey).
    _KEY_RE = re.compile(
        r'((?:tmdb_key|mdblist_key|access_key|api_key|apikey)=)[^&\s\'\"]*',
        re.IGNORECASE,
    )

    @classmethod
    def _redact(cls, value):
        if isinstance(value, str):
            return cls._KEY_RE.sub(r'\1***', value)
        return value

    def filter(self, record: logging.LogRecord) -> bool:
        # uvicorn.access records: args = (client_addr, method, path, http_version, status_code, ...)
        if (
            record.name == "uvicorn.access"
            and isinstance(record.args, tuple)
            and len(record.args) >= 3
        ):
            path = record.args[2]
            if isinstance(path, str):
                path = self._KEY_RE.sub(r'\1***', path)
                if len(path) > self._MAX:
                    path = path[: self._MAX] + "…"
                record.args = (record.args[0], record.args[1], path) + record.args[3:]

        # Generic redaction for every other record (application logs).
        # We redact in msg and args so the formatted output is safe regardless
        # of whether the record uses % substitution or pre-formatted strings.
        if isinstance(record.msg, str):
            record.msg = self._redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(self._redact(a) for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: self._redact(v) for k, v in record.args.items()}

        # Tracebacks (logger.exception / exc_info=True) are formatted lazily
        # by the handler.  Pre-format and redact exc_text here so the
        # downstream formatter uses our sanitised copy rather than re-rendering.
        if record.exc_info and not record.exc_text:
            import traceback
            record.exc_text = self._redact(
                "".join(traceback.format_exception(*record.exc_info))
            )
        elif record.exc_text:
            record.exc_text = self._redact(record.exc_text)

        return True


# Attach to the root handler, not the root logger — propagation calls
# callHandlers() directly on parent loggers, skipping their logger-level filters.
_url_filter = _TruncateUrlFilter()
for _handler in logging.getLogger().handlers:
    _handler.addFilter(_url_filter)

# httpx logs every outbound HTTP request at INFO level, including full URLs with
# API keys in query strings.  Raise its level to WARNING so those lines are never
# written to the log — our own try/except blocks capture errors explicitly.
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Request coalescing
# ---------------------------------------------------------------------------
# Maps final_cache_key -> Future[bytes] for in-flight renders.
# When multiple requests arrive simultaneously for the same uncached poster
# (common during a burst from AIOMetadata loading a library), only the first
# runs the full pipeline; the rest await its Future and get the result for free.
# This dict is per-worker-process — cross-process deduplication would require
# a shared store like Redis, but intra-process coalescing handles the common
# burst pattern well enough at this scale.
_render_inflight: dict[str, "asyncio.Future[bytes]"] = {}

# ---------------------------------------------------------------------------
# Background quality fetching
# ---------------------------------------------------------------------------
# Quality data (AIOStreams / scrapers) is fetched in the background so poster
# responses are never blocked by a slow scraper call.  The poster is served
# immediately without quality badges on a cache miss; the next request for the
# same title will find the quality cached and render badges normally.
#
# _quality_bg_inflight: tracks imdb_ids with an active background fetch so
#   scroll bursts don't launch duplicate fetches for the same title.
# _quality_bg_semaphore: caps concurrent AIOStreams calls so a large burst
#   doesn't hammer the scrapers with hundreds of simultaneous requests.

_quality_bg_inflight: set[str] = set()
_quality_bg_semaphore: "asyncio.Semaphore | None" = None   # created inside event loop

# ---------------------------------------------------------------------------
# Rating fetch deduplication
# ---------------------------------------------------------------------------
# Prevents concurrent requests for the same imdb_id (different raw_params /
# final_cache_key) from triggering duplicate MDBlist API calls.  The most
# common burst: AIOMetadata requests many posters simultaneously; several
# share an uncached title with different user-config hashes so render
# coalescing alone doesn't protect them.
#
# _rating_fetch_inflight: maps imdb_id -> asyncio.Event that fires once the
#   first fetch completes.  Subsequent requests wait, then re-read the DB.
# _rating_backoff: maps imdb_id -> loop-time after which a new attempt is
#   allowed.  Network failures use an escalating ladder (30s/2m/8m/1h);
#   rate-limit responses use Retry-After or 1h flat.

_rating_fetch_inflight:         dict[str, asyncio.Event] = {}
_rating_backoff:                dict[str, float]          = {}  # imdb_id -> retry-after (loop time)
_rating_fail_count:             dict[str, int]            = {}  # imdb_id -> consecutive network-failure count (for escalating back-off)
_mdblist_semaphore:             "asyncio.Semaphore | None" = None  # caps concurrent MDBlist HTTP calls; created inside event loop
# Global rate-limit cooldown: when MDBlist sends a 429, all MDBlist requests are
# paused until this timestamp (event-loop time).  This prevents the queue of
# waiting titles from each hitting 429 individually and burning per-title backoffs
# for what is really a single key-level throttle window.
_mdblist_global_cooldown_until: float = 0.0


async def _background_quality_fetch(
    imdb_id: str,
    media_type: str,
    season: int,
    episode: int,
    release_date: str | None,
) -> None:
    """Fetch quality tokens from the configured quality source and cache them.  Never raises."""
    global _quality_bg_semaphore
    if _quality_bg_semaphore is None:
        _quality_bg_semaphore = asyncio.Semaphore(_cfg.QUALITY_BG_CONCURRENCY)
    try:
        async with _quality_bg_semaphore:
            if _HTTP_CLIENT is None:
                return
            if _cfg.QUALITY_SOURCE == "scraper" and _cfg.SCRAPER_URL:
                await _with_retry(
                    fetch_quality_from_scraper,
                    _HTTP_CLIENT, _cfg.SCRAPER_URL, imdb_id, media_type, season, episode, release_date,
                )
            else:
                await _with_retry(
                    fetch_quality_from_aiostreams,
                    _HTTP_CLIENT, imdb_id, media_type, season, episode, release_date,
                )
            logger.info(f"Background quality fetch complete for {imdb_id}")
    except Exception as exc:
        logger.warning(f"Background quality fetch failed for {imdb_id}: {exc}")
    finally:
        _quality_bg_inflight.discard(imdb_id)

# Local imports
from age_badge import draw_quality_age_badge, draw_tier_bar
from awards import FETCH_FAILED, _RateLimited, draw_award_badge, draw_award_sash, parse_mdblist_awards
from cache import (
    get_cached_quality,
    get_cached_rating,
    get_cached_final_poster,
    set_cached_final_poster,
    init_db,
    is_digital_release,
    set_cached_rating,
    delete_cached_tmdb_metadata,
    prune_caches,
)
from digital_release import digital_release_poll_loop
import config as _cfg
from discovery import (
    ALL_PRIORITY_SLOTS,
    FESTIVAL_KEYWORDS,
    DiscoveryMeta,
    extract_discovery_meta,
    pick_sash,
)
from quality import (
    BadgeItem,
    fetch_quality_from_aiostreams,
    fetch_quality_from_scraper,
    get_resized_badge,
    parse_quality,
    render_badges_left,
)
from ratings import calculate_weighted_score, draw_score_bar, fetch_rating, draw_score_bar_vertical, draw_compact_label
from tmdb import composite_logo, fetch_logo, fetch_poster_metadata, fetch_poster_image, fetch_backdrop_image, fetch_trending_rank, fetch_release_status

# ---------------------------------------------------------------------------
# Persistent HTTP client
# ---------------------------------------------------------------------------
# One client for the lifetime of the process. httpx keeps TCP connections
# alive in its connection pool, so repeated requests to the same host
# (TMDB, MDblist, AIOStreams) reuse the existing socket rather than paying
# TLS + TCP handshake overhead on every poster request.
#
# Timeouts are split:
#   connect=5s  — fail fast when a host is unreachable
#   read=12s    — allow slow responses from external APIs
#   pool=5s     — don't block forever waiting for a pool slot

_HTTP_CLIENT: httpx.AsyncClient | None = None

def _make_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(connect=5.0, read=12.0, write=5.0, pool=5.0),
        limits=httpx.Limits(
            max_connections=40,
            max_keepalive_connections=20,
            keepalive_expiry=30,
        ),
        headers={
            "Accept-Encoding": "identity",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        },
        http2=False,   # most poster APIs don't support h2; skip the negotiation
    )


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------

_TMDB_ID_RE  = re.compile(r'^\d{1,10}$')
_IMDB_ID_RE  = re.compile(r'^tt\d{1,10}$')
_VALID_TYPES = frozenset({"movie", "tv", "series"})


def _check_tmdb_id(val: str) -> None:
    if not _TMDB_ID_RE.match(val):
        raise HTTPException(status_code=400, detail="Invalid tmdb_id")


def _check_imdb_id(val: str) -> None:
    if not _IMDB_ID_RE.match(val):
        raise HTTPException(status_code=400, detail="Invalid imdb_id")


def _check_type(val: str) -> None:
    if val not in _VALID_TYPES:
        raise HTTPException(status_code=400, detail="Invalid type")


# ---------------------------------------------------------------------------
# Key resolution helpers
# ---------------------------------------------------------------------------

def _resolve_tmdb_key(query_key: str) -> str | None:
    if query_key:
        return query_key
    if _cfg.SERVER_TMDB_KEY:
        return _cfg.SERVER_TMDB_KEY
    return None


def _resolve_mdblist_key(query_key: str) -> str | None:
    if query_key:
        return query_key
    if _cfg.SERVER_MDBLIST_KEY:
        return _cfg.SERVER_MDBLIST_KEY
    return None


# ---------------------------------------------------------------------------
# Per-request configuration
# ---------------------------------------------------------------------------

@dataclass
class RequestConfig:
    """
    Holds all user-tuneable config values for a single request.
    Defaults come from the global config module; query params override them.
    """
    show_award_sash:     bool = field(default_factory=lambda: _cfg.SHOW_AWARD_SASH)
    badge_display_mode:  int  = field(default_factory=lambda: _cfg.BADGE_DISPLAY_MODE)
    rating_display_mode: int  = field(default_factory=lambda: _cfg.SHOW_RATING_DISPLAY_MODE)

    accent_bar_font_size_ratio:    float = field(default_factory=lambda: _cfg.ACCENT_BAR_MODE_FONT_SIZE_RATIO)
    # Score Bar mode label suffix: 0 = Year (legacy default), 1 = Info sash, 2 = Year + Info sash
    accent_bar_append_mode:        int   = 0
    # Score Bar position knob — distance from poster bottom edge as fraction of height.
    # Default matches the legacy hardcoded 30px on a 500x750 poster.
    accent_bar_bottom_ratio:       float = 0.04
    numeric_score_font_size_ratio: float = field(default_factory=lambda: _cfg.NUMERIC_SCORE_MODE_FONT_SIZE_RATIO)
    # Clean mode (mode 2) numeric format.  When True, the rating is divided by
    # 10 and shown to one decimal (87 → "8.7", 100 → "10.0").  Default keeps
    # the legacy 0-100 integer form.
    score_out_of_10: bool = False
    accent_bar_y_offset:           float = field(default_factory=lambda: _cfg.ACCENT_BAR_MODE_FONT_Y_OFFSET)
    numeric_score_y_offset:        float = field(default_factory=lambda: _cfg.NUMERIC_SCORE_MODE_FONT_Y_OFFSET)
    score_glow_threshold:          int   = field(default_factory=lambda: _cfg.SCORE_GLOW_THRESHOLD)
    score_glow_blur:               int   = field(default_factory=lambda: _cfg.SCORE_GLOW_BLUR)
    score_glow_alpha:              int   = field(default_factory=lambda: _cfg.SCORE_GLOW_ALPHA)
    minimalist_mode_font_size_ratio:  float = field(default_factory=lambda: _cfg.MINIMALIST_MODE_FONT_SIZE_RATIO)
    minimalist_mode_font_x_offset: float = field(default_factory=lambda: _cfg.MINIMALIST_MODE_FONT_X_OFFSET)
    minimalist_mode_font_y_offset: float = field(default_factory=lambda: _cfg.MINIMALIST_MODE_FONT_Y_OFFSET)

    # Compact mode (rating_display_mode == 4) — "all info in one strip"
    # Year is OFF by default; the smaller line lets the font run a bit larger
    # (~0.066 vs Minimalist's 0.055).  Flip show_year on if you'd rather
    # include the year — you'll likely want to drop the font ratio back to ~0.055.
    compact_font_size_ratio: float = 0.066
    compact_y_offset:        float = 0.90
    compact_show_year:       bool  = False

    logo_max_w_ratio:  float = field(default_factory=lambda: _cfg.LOGO_MAX_W_RATIO)
    logo_max_h_ratio:  float = field(default_factory=lambda: _cfg.LOGO_MAX_H_RATIO)
    logo_bottom_ratio: float = field(default_factory=lambda: _cfg.LOGO_BOTTOM_RATIO)

    badge_height:    int   = field(default_factory=lambda: _cfg.BADGE_HEIGHT)
    badge_gap:       int   = field(default_factory=lambda: _cfg.BADGE_GAP)
    badge_anchor_x:  float = field(default_factory=lambda: _cfg.BADGE_ANCHOR_X_RATIO)
    badge_anchor_y:  float = field(default_factory=lambda: _cfg.BADGE_ANCHOR_Y_RATIO)

    movie_weights: dict | None = None
    tv_weights:    dict | None = None

    logo_language: str = field(default_factory=lambda: _cfg.DEFAULT_LOGO_LANGUAGE)
    # When True (default): fall through to the content's original-language logo
    # when no preferred-language logo exists (e.g. "La Cena" for a Spanish film).
    # When False: skip native-language logos and let the text-title fallback render
    # the translated title instead (e.g. "The Dinner").
    logo_native_fallback: bool = True
    sash_priority: list[str] = field(default_factory=lambda: list(_cfg.SASH_PRIORITY))
    muted: bool = False
    textless: bool = False
    score_color_mode: int = 2
    top_gradient:    str = "high"   # off | low | medium | high — strength of the top vignette
    bottom_gradient: str = "high"   # off | low | medium | high — strength of the bottom vignette
    sash_badge: bool = False   # True → badge style instead of diagonal sash
    sash_badge_x:    float = 0.62   # badge left-edge as fraction of poster width (flush right with the corner)
    sash_badge_y:    float = 0.04   # badge top-edge  as fraction of poster height
    sash_badge_size: float = 1.0    # uniform scale of badge dimensions (1.0 = default footprint)
    sash_length_ratio: float = 1.15  # diagonal sash length as fraction of poster width
    sash_height_ratio: float = 0.12  # diagonal sash height (thickness) as fraction of poster width
    wait_for_quality: bool = False  # block response until quality is fetched (for poster-warm workflows)


def _parse_bool(val: str | None, default: bool) -> bool:
    if val is None:
        return default
    return val.strip().lower() not in ("0", "false", "no")


def _parse_weights(raw: str | None, sources: list[str]) -> dict | None:
    if not raw:
        return None
    out = {}
    try:
        for part in raw.split(","):
            part = part.strip()
            if ":" not in part:
                continue
            key, val = part.split(":", 1)
            key = key.strip().lower()
            if key in sources:
                out[key] = max(0.0, min(1.0, float(val)))
    except Exception:
        return None
    return out if out else None


def _parse_sash_priority(raw: str | None) -> list[str]:
    if not raw:
        return list(_cfg.SASH_PRIORITY)
    tokens = [s.strip() for s in raw.split(",") if s.strip()]
    # Tokens prefixed with "-" are explicit exclusions
    excluded  = {t[1:] for t in tokens if t.startswith("-") and t[1:] in ALL_PRIORITY_SLOTS}
    active    = [t      for t in tokens if not t.startswith("-") and t in ALL_PRIORITY_SLOTS]
    if not active and not excluded:
        return list(_cfg.SASH_PRIORITY)
    # Append any default slots that weren't explicitly listed or excluded
    active_set = set(active)
    for slot in _cfg.SASH_PRIORITY:
        if slot not in active_set and slot not in excluded:
            active.append(slot)
    return active


def build_request_config(params: dict) -> RequestConfig:
    """Build a RequestConfig from raw query-param strings.

    All numeric overrides are clamped to a sensible range so a malicious or
    careless caller can't pass values that would melt a worker (e.g.
    score_glow_blur=99999 turning into a Gaussian kernel of that radius, or
    badge_height=99999 triggering a multi-GB image resize).  Bounds are
    deliberately a little more generous than the configurator sliders so
    power users can push past UI limits without bypassing safety.
    """
    cfg = RequestConfig()

    def _b(key, default): return _parse_bool(params.get(key), default)

    def _f(key, default, lo: float, hi: float):
        """Float param with hard clamp to [lo, hi]; invalid → default."""
        try:
            return max(lo, min(hi, float(params[key]))) if key in params else default
        except (ValueError, TypeError):
            return default

    def _i(key, default, lo: int, hi: int):
        """Int param with hard clamp to [lo, hi]; invalid → default."""
        try:
            return max(lo, min(hi, int(params[key]))) if key in params else default
        except (ValueError, TypeError):
            return default

    cfg.show_award_sash         = _b("show_award_sash",        cfg.show_award_sash)
    cfg.muted                   = _b("muted",                  cfg.muted)
    cfg.score_out_of_10         = _b("score_out_of_10",        cfg.score_out_of_10)
    cfg.textless                = _b("textless",               cfg.textless)
    # top_gradient accepts off / low / medium / high.  Legacy boolean values
    # (true / false) from pre-v1.0.4 URLs map to high / off respectively so
    # cached configurator links keep working.
    _tg_raw = (params.get("top_gradient") or "").strip().lower()
    if _tg_raw in _TOP_GRADIENT_LEVELS:
        cfg.top_gradient = _tg_raw
    elif _tg_raw in ("true", "1", "yes"):
        cfg.top_gradient = "high"
    elif _tg_raw in ("false", "0", "no"):
        cfg.top_gradient = "off"
    # else: leave RequestConfig default ("high")

    # bottom_gradient — same four-level enum as top.  Brand-new param so no
    # legacy boolean form to honour; unknown values fall through to the
    # RequestConfig default ("high") which matches the legacy behaviour.
    _bg_raw = (params.get("bottom_gradient") or "").strip().lower()
    if _bg_raw in _BOTTOM_GRADIENT_LEVELS:
        cfg.bottom_gradient = _bg_raw
    cfg.sash_badge              = _b("sash_badge",             cfg.sash_badge)
    # Position ratios — full poster span so users can put the badge anywhere
    cfg.sash_badge_x            = _f("sash_badge_x",           cfg.sash_badge_x,           0.0, 1.0)
    cfg.sash_badge_y            = _f("sash_badge_y",           cfg.sash_badge_y,           0.0, 1.0)
    # Capped at 1.5× — beyond that the badge would auto-displace via the
    # in-renderer clamp at the default x position, which is confusing UX.
    cfg.sash_badge_size         = _f("sash_badge_size",        cfg.sash_badge_size,        0.5, 1.5)
    cfg.sash_length_ratio       = _f("sash_length_ratio",      cfg.sash_length_ratio,      0.8, 1.5)
    cfg.sash_height_ratio       = _f("sash_height_ratio",      cfg.sash_height_ratio,      0.06, 0.20)
    cfg.wait_for_quality        = _b("wait_for_quality",        cfg.wait_for_quality)
    cfg.score_color_mode        = _i("score_color_mode",       cfg.score_color_mode,       0,   2)
    cfg.badge_display_mode      = _i("badge_display_mode",     cfg.badge_display_mode,     0,   4)
    cfg.rating_display_mode     = _i("rating_display_mode",    cfg.rating_display_mode,    0,   4)

    if "show_quality_badges" in params and "badge_display_mode" not in params:
        if _parse_bool(params.get("show_quality_badges"), True):
            cfg.badge_display_mode = 1
        else:
            cfg.badge_display_mode = 0

    # Font-size ratios are multiplied by the poster width — anything above ~0.3
    # would overflow the poster; we cap at 0.5 to leave headroom for experimentation.
    cfg.accent_bar_font_size_ratio    = _f("accent_bar_font_size_ratio",    cfg.accent_bar_font_size_ratio,    0.0, 0.5)
    cfg.accent_bar_append_mode        = _i("accent_bar_append_mode",        cfg.accent_bar_append_mode,        0,   2)
    cfg.accent_bar_bottom_ratio       = _f("accent_bar_bottom_ratio",       cfg.accent_bar_bottom_ratio,       0.0, 0.5)
    cfg.numeric_score_font_size_ratio = _f("numeric_score_font_size_ratio", cfg.numeric_score_font_size_ratio, 0.0, 0.5)
    cfg.accent_bar_y_offset           = _f("accent_bar_y_offset",           cfg.accent_bar_y_offset,           0.0, 1.0)
    cfg.numeric_score_y_offset        = _f("numeric_score_y_offset",        cfg.numeric_score_y_offset,        0.0, 1.0)
    cfg.score_glow_threshold          = _i("score_glow_threshold",          cfg.score_glow_threshold,          0,   100)
    # Glow blur is a Gaussian kernel radius — cost is O(r²) per pixel, so anything
    # above ~50 starts measurably slowing the render.  Hard cap at 50.
    cfg.score_glow_blur               = _i("score_glow_blur",               cfg.score_glow_blur,               0,   50)
    cfg.score_glow_alpha              = _i("score_glow_alpha",              cfg.score_glow_alpha,              0,   255)
    cfg.minimalist_mode_font_size_ratio = _f("minimalist_mode_font_size_ratio", cfg.minimalist_mode_font_size_ratio, 0.0, 0.5)
    cfg.minimalist_mode_font_x_offset = _f("minimalist_mode_font_x_offset", cfg.minimalist_mode_font_x_offset, 0.0, 1.0)
    cfg.minimalist_mode_font_y_offset = _f("minimalist_mode_font_y_offset", cfg.minimalist_mode_font_y_offset, 0.0, 1.0)

    cfg.compact_font_size_ratio = _f("compact_font_size_ratio", cfg.compact_font_size_ratio, 0.0, 0.5)
    cfg.compact_y_offset        = _f("compact_y_offset",        cfg.compact_y_offset,        0.0, 1.0)
    cfg.compact_show_year       = _b("compact_show_year",       cfg.compact_show_year)

    cfg.logo_max_w_ratio  = _f("logo_max_w_ratio",  cfg.logo_max_w_ratio,  0.0, 1.5)
    cfg.logo_max_h_ratio  = _f("logo_max_h_ratio",  cfg.logo_max_h_ratio,  0.0, 1.0)
    cfg.logo_bottom_ratio = _f("logo_bottom_ratio", cfg.logo_bottom_ratio, 0.0, 1.0)

    # badge_height in pixels — generous enough to cover any reasonable customisation
    # but well below the size that would cost real memory on resize.
    cfg.badge_height   = _i("badge_height",   cfg.badge_height,   1,   200)
    cfg.badge_gap      = _i("badge_gap",      cfg.badge_gap,      0,   100)
    cfg.badge_anchor_x = _f("badge_anchor_x", cfg.badge_anchor_x, 0.0, 1.0)
    cfg.badge_anchor_y = _f("badge_anchor_y", cfg.badge_anchor_y, 0.0, 1.0)

    all_sources = list(_cfg.MOVIE_WEIGHTS.keys())
    cfg.movie_weights = _parse_weights(params.get("movie_weights"), all_sources)

    tv_sources = list(_cfg.TV_WEIGHTS.keys())
    cfg.tv_weights = _parse_weights(params.get("tv_weights"), tv_sources)

    cfg.logo_language        = (params.get("logo_language", cfg.logo_language).strip().lower())
    cfg.logo_native_fallback = _b("logo_native_fallback", cfg.logo_native_fallback)
    cfg.sash_priority        = _parse_sash_priority(params.get("sash_priority"))

    return cfg


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _resolved(value):
    return value


async def _with_retry(coro_fn, *args, **kwargs):
    """Call coro_fn(*args, **kwargs) and retry once if FETCH_FAILED is returned."""
    result = await coro_fn(*args, **kwargs)
    if result is FETCH_FAILED:
        result = await coro_fn(*args, **kwargs)
    return result


def _text_center(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    cx: float,
    cy: float,
) -> tuple[float, float]:
    bbox = draw.textbbox((0, 0), text, font=font)
    bbox_width = bbox[2] - bbox[0]
    ascent, descent = font.getmetrics()
    x = cx - bbox_width / 2 - bbox[0]
    optical_adjust = int(ascent * 0.22)
    y = cy - (ascent + descent) / 2 - descent + optical_adjust
    return x, y


# ---------------------------------------------------------------------------
# Poster composition
# ---------------------------------------------------------------------------

# Top-vignette strength.  Each entry maps a level name to
# (top_height_ratio, top_max_alpha).  None means "don't draw the gradient
# at all".  The "high" preset matches the legacy always-on behaviour so
# existing URLs / cached posters render identically when top_gradient is
# omitted.  Tweak the values here to retune any preset.
_TOP_GRADIENT_LEVELS: dict[str, tuple[float, int] | None] = {
    "off":    None,
    "low":    (0.20, 150),
    "medium": (0.25, 190),
    "high":   (0.40, 220),
}

# Bottom-vignette strength.  Same shape as the top gradient — (height_ratio,
# max_alpha).  Defaults to "high" which matches the legacy alpha-255 / 50%-
# height fade.  The previous auto-softening for Minimalist/Compact rating
# modes is dropped now that users can pick the level themselves; if you
# liked the softer look on those modes, set bottom_vignette=medium.
_BOTTOM_GRADIENT_LEVELS: dict[str, tuple[float, int] | None] = {
    "off":    None,
    "low":    (0.30, 180),
    "medium": (0.40, 220),
    "high":   (0.50, 255),
}
# Easing exponent shared across all bottom-gradient presets — controls the
# curve shape (1.0 = linear; >1 starts darker at the bottom and fades faster
# at the top).  Decoupled from strength so retuning one doesn't affect the
# other.
_BOTTOM_GRADIENT_CURVE = 1.2

# Genre-specific tint multipliers (R, G, B) for the fallback canvas.
# Applied to a dark base luminance of 10–18, so the dominant channel peaks
# around 30–55 at canvas midpoint — atmospheric rather than vivid.
# Names must match GENRE_MAP values exactly.
_GENRE_TINT: dict[str, tuple[float, float, float]] = {
    "Horror":      (3.2, 0.3, 0.3),   # deep blood red
    "Thriller":    (0.4, 2.2, 0.5),   # dark hunter green
    "Mystery":     (1.0, 0.3, 3.0),   # deep indigo
    "Sci-Fi":      (0.3, 1.2, 3.2),   # cold cyan-blue
    "Fantasy":     (1.6, 0.3, 3.0),   # purple-violet
    "Action":      (3.0, 0.8, 0.3),   # orange-red
    "Adventure":   (2.6, 1.5, 0.3),   # warm amber
    "Animation":   (0.4, 0.8, 3.2),   # electric blue
    "Comedy":      (2.6, 2.4, 0.3),   # golden yellow
    "Crime":       (2.4, 0.2, 0.2),   # dark crimson
    "Documentary": (0.3, 2.2, 2.4),   # teal
    "Drama":       (0.3, 0.3, 2.6),   # deep blue
    "Family":      (2.6, 1.2, 0.3),   # warm orange
    "History":     (2.2, 1.1, 0.3),   # sepia
    "Music":       (2.8, 0.3, 2.2),   # magenta
    "Romance":     (3.0, 0.3, 0.9),   # rose
    "War":         (0.9, 1.6, 0.3),   # olive green
    "Western":     (2.8, 1.1, 0.2),   # burnt sienna
    "Kids":        (0.3, 1.1, 3.0),   # bright blue
    "Reality":     (2.4, 0.8, 0.3),   # orange
    "Soap":        (2.6, 0.3, 0.9),   # rose-pink
    "Talk":        (0.3, 1.6, 2.4),   # teal-blue
    "News":        (0.3, 0.5, 2.6),   # steel blue
}
_FALLBACK_DEFAULT_TINT = (1.0, 1.0, 1.4)   # neutral cool blue


def _make_fallback_canvas(genre_ids: list[int] | None = None) -> Image.Image:
    """
    Dark gradient canvas served when a title has no poster art on TMDB.

    Applies a genre-derived colour tint so the canvas feels atmospheric rather
    than generically dark.  The base luminance is 10–18 (very dark) so even the
    dominant channel stays below ~55 — readable against white text overlays.
    """
    # Resolve genre → tint by walking GENRE_PRIORITY so higher-priority genres
    # win when a title belongs to multiple genres (same order as the score label).
    tint = _FALLBACK_DEFAULT_TINT
    if genre_ids:
        gid_set = set(genre_ids)
        for gid in _cfg.GENRE_PRIORITY:
            if gid in gid_set:
                name = _cfg.GENRE_MAP.get(gid)
                if name and name in _GENRE_TINT:
                    tint = _GENRE_TINT[name]
                    break

    r_mult, g_mult, b_mult = tint
    W, H = _cfg.POSTER_WIDTH, _cfg.POSTER_HEIGHT
    t    = np.linspace(0, np.pi, H, dtype=np.float32)
    # sin curve: peaks at midheight (~18), dark at top/bottom (~10)
    v    = (10 + 8 * np.sin(t)).astype(np.float32)
    arr  = np.zeros((H, W, 4), dtype=np.uint8)
    # Clamp BEFORE casting to uint8 — casting first would wrap mod-256 on
    # any value above 255, silently inverting colour for high-multiplier tints.
    arr[:, :, 0] = np.minimum(255, v * r_mult).astype(np.uint8)[:, np.newaxis]
    arr[:, :, 1] = np.minimum(255, v * g_mult).astype(np.uint8)[:, np.newaxis]
    arr[:, :, 2] = np.minimum(255, v * b_mult).astype(np.uint8)[:, np.newaxis]
    arr[:, :, 3] = 255
    return Image.fromarray(arr, "RGBA")


def build_poster(
    image: Image.Image,
    score: int | str,
    genre: str,
    cfg: RequestConfig,
    logo: Image.Image | None = None,
    fallback_title: str | None = None,
    discovery_meta: DiscoveryMeta | None = None,
    quality_tokens: list[str] | None = None,
    release_year: str | None = None,
    age_rating: int | None = None,
    no_poster: bool = False,
) -> Image.Image:

    width, height = image.size
    draw = ImageDraw.Draw(image)

    # --- TOP GRADIENT (vectorised) ---
    # Darkens the top of the poster so the age-rating numeral and quality
    # badges stay legible over bright art.  Strength is one of four presets
    # (off / low / medium / high) — see _TOP_GRADIENT_LEVELS for the
    # (height_ratio, max_alpha) tuple each level uses.  Unknown level is
    # treated as "high" rather than skipped so a typo in a URL doesn't
    # silently disable the vignette.
    _tg_preset = _TOP_GRADIENT_LEVELS.get(cfg.top_gradient, _TOP_GRADIENT_LEVELS["high"])
    if _tg_preset is not None:
        top_height_ratio, top_max_alpha = _tg_preset
        top_height = int(height * top_height_ratio)
        t_top = np.linspace(0, 1, top_height, dtype=np.float32)
        eased_top = ((1 - t_top) * top_max_alpha).astype(np.uint8)
        top_array = np.broadcast_to(eased_top[:, np.newaxis], (top_height, width)).copy()
        top_overlay = Image.fromarray(top_array, mode="L")
        top_tinted = Image.new("RGBA", (width, top_height), (0, 0, 0, 0))
        top_tinted.putalpha(top_overlay)
        image.paste(top_tinted, (0, 0), mask=top_tinted)

    # --- BOTTOM GRADIENT (vectorised) ---
    # Strength is one of four presets (off / low / medium / high) — see
    # _BOTTOM_GRADIENT_LEVELS for the (height_ratio, max_alpha) tuple each
    # level uses.  The previous auto-softening for Minimalist / Compact modes
    # is dropped now that the user can pick the level themselves; if you'd
    # like the lighter fade those modes used to get for free, pick "medium".
    # Unknown level falls back to "high" so a typo can't accidentally turn
    # the fade off entirely (which would break label legibility).
    _bg_preset = _BOTTOM_GRADIENT_LEVELS.get(cfg.bottom_gradient, _BOTTOM_GRADIENT_LEVELS["high"])
    if _bg_preset is not None:
        bottom_height_ratio, bottom_max_alpha = _bg_preset
        bottom_height = int(height * bottom_height_ratio)
        bottom_start  = height - bottom_height
        t_bot         = np.linspace(0, 1, bottom_height, dtype=np.float32)
        eased_bot     = ((1 - (1 - t_bot) ** _BOTTOM_GRADIENT_CURVE) * bottom_max_alpha).astype(np.uint8)
        bottom_array  = np.broadcast_to(eased_bot[:, np.newaxis], (bottom_height, width)).copy()
        bottom_overlay = Image.fromarray(bottom_array, mode="L")
        bottom_tinted  = Image.new("RGBA", (width, bottom_height), (0, 0, 0, 0))
        bottom_tinted.putalpha(bottom_overlay)
        image.paste(bottom_tinted, (0, bottom_start), mask=bottom_tinted)

    # --- Badge / quality overlay ---
    mode   = cfg.badge_display_mode
    tokens = quality_tokens or []

    if mode == 1:
        draw_quality_age_badge(
            image,
            age_rating,
            tokens,
            anchor_x_ratio=cfg.badge_anchor_x,
            anchor_y_ratio=cfg.badge_anchor_y,
            badge_height=cfg.badge_height,
        )

    elif mode == 3:
        # Age rating only — always silver, no quality dependency
        draw_quality_age_badge(
            image,
            age_rating,
            [],
            anchor_x_ratio=cfg.badge_anchor_x,
            anchor_y_ratio=cfg.badge_anchor_y,
            badge_height=cfg.badge_height,
            always_silver=True,
        )

    elif mode == 4:
        # Accent bar — small vertical pill in tier colour, no text
        draw_tier_bar(
            image,
            tokens,
            anchor_x_ratio=cfg.badge_anchor_x,
            anchor_y_ratio=cfg.badge_anchor_y,
            bar_height=cfg.badge_height,
        )

    elif mode == 2:
        allowed_tokens  = {"4K", "1080P", "REMUX", "WEBDL", "DV", "HDR10+", "HDR10"}
        filtered_tokens = [t for t in tokens if t in allowed_tokens]

        if filtered_tokens:
            bx = int(width  * cfg.badge_anchor_x)
            by = int(height * cfg.badge_anchor_y)

            badge_items: list[BadgeItem] = [
                (get_resized_badge(token, cfg.badge_height), _cfg.QUALITY_LABELS.get(token, token))
                for token in filtered_tokens
            ]

            render_badges_left(
                image, badge_items,
                x_start=bx, y_top=by,
                badge_height=cfg.badge_height,
                badge_gap=cfg.badge_gap,
            )

    # --- Logo / fallback title ---
    if logo:
        composite_logo(
            image, logo,
            max_w_ratio=cfg.logo_max_w_ratio,
            max_h_ratio=cfg.logo_max_h_ratio,
            bottom_ratio=cfg.logo_bottom_ratio,
        )
    elif fallback_title:
        # Multi-line aware fallback title rendering using Playfair Display Bold.
        #
        # Font size is scaled down as title length grows so long titles don't
        # feel enormous.  The formula maps character count to a size ratio:
        #   ≤10 chars  → 0.130  (~65 px on a 500 px wide poster)
        #   20 chars   → 0.108
        #   27 chars   → 0.094   ("Anoranzas del viejo cartago")
        #   35 chars   → 0.078
        #   ≥40 chars  → 0.070  (floor)
        # A 2-line target + 80 % width margin keeps the text comfortably inside
        # the poster without touching the edges.  The shrink loop is a safety
        # net for very long or single-word titles that resist wrapping.
        max_width      = int(width * 0.80)
        title_cy       = height - int(height * 0.3)
        _char_count    = len(fallback_title)
        _raw_ratio     = 0.142 - _char_count * 0.0018
        font_size      = int(width * max(0.070, min(0.130, _raw_ratio)))
        MIN_FONT_SIZE  = 26
        MAX_LINES      = 2
        FONT_PATH      = os.path.join(_FONTS_DIR, "NotoSerif-Bold.ttf")

        def _wrap_lines(text: str, current_font) -> list[str]:
            """Greedy word-wrap: each line packs as many words as fit within max_width."""
            words = text.split()
            if not words:
                return []
            lines: list[str] = []
            current: list[str] = []
            for word in words:
                candidate = " ".join(current + [word])
                bb = draw.textbbox((0, 0), candidate, font=current_font)
                if bb[2] - bb[0] <= max_width or not current:
                    current.append(word)
                else:
                    lines.append(" ".join(current))
                    current = [word]
            if current:
                lines.append(" ".join(current))
            return lines

        # Shrink font until the wrapped layout fits in MAX_LINES, then stop.
        try:
            font = ImageFont.truetype(FONT_PATH, font_size)
        except IOError:
            font = ImageFont.load_default()
            lines = [fallback_title]
        else:
            while True:
                lines = _wrap_lines(fallback_title, font)
                if len(lines) <= MAX_LINES or font_size <= MIN_FONT_SIZE:
                    break
                font_size -= 4
                try:
                    font = ImageFont.truetype(FONT_PATH, font_size)
                except IOError:
                    break

        # Centre the multi-line block vertically around title_cy.
        line_height    = int(font_size * 1.15)
        total_height   = line_height * len(lines)
        block_top      = title_cy - total_height // 2
        shadow_offset  = max(2, int(font_size * 0.04))

        for i, line in enumerate(lines):
            line_cy = block_top + i * line_height + line_height // 2
            tx, ty  = _text_center(draw, line, font, width / 2, line_cy)  # type: ignore
            draw.text((tx + shadow_offset, ty + shadow_offset), line, font=font, fill=(0, 0, 0, 180))
            draw.text((tx, ty),                                  line, font=font, fill=(255, 255, 255, 255))

    # Resolve the info-sash pick once, regardless of whether the diagonal sash
    # itself is rendered.  Compact rating mode (4) also reads from this to
    # populate the bottom-line slot with the sash label + colour.
    sash_result = (
        pick_sash(discovery_meta, cfg.sash_priority)
        if discovery_meta is not None
        else None
    )

    # --- Rating / genre label ---
    if cfg.rating_display_mode != 0:

        if cfg.rating_display_mode == 1:
            font_size = int(width * cfg.accent_bar_font_size_ratio)
            # Label suffix is configurable: append year, append sash text, or
            # append both joined by " · ".  Missing data degrades gracefully —
            # if "sash" is requested but no sash triggered, we just show the
            # genre; if "both" but only one is present, we show whichever did.
            #
            # The separator immediately before the sash text becomes "★" when
            # the sash is a winner (sash_type == "win") rather than "·".  Same
            # disambiguation trick used by Compact mode — Best Picture /
            # Golden Globe / festival wins and nominees share their label
            # text, so without this they'd be indistinguishable here.
            _append_year = cfg.accent_bar_append_mode in (0, 2)
            _append_sash = cfg.accent_bar_append_mode in (1, 2)
            _sash_text_for_label, _sash_type_for_label = (
                sash_result if (_append_sash and sash_result) else (None, None)
            )

            _pre_sash = [genre]
            if _append_year and release_year:
                _pre_sash.append(str(release_year))
            _label_main = " · ".join(_pre_sash)

            if _sash_text_for_label:
                _sash_sep = " ★ " if _sash_type_for_label == "win" else " · "
                label = _label_main + _sash_sep + _sash_text_for_label
            else:
                label = _label_main
            rating_cy = height * cfg.accent_bar_y_offset

            try:
                font_meta = ImageFont.truetype(os.path.join(_FONTS_DIR, "Inter-Bold.ttf"), font_size)
            except IOError:
                font_meta = ImageFont.load_default()

            tx, ty = _text_center(draw, label, font_meta, width / 2, rating_cy)  # type: ignore
            draw.text(
                (tx, ty - int(font_size * 0.10)),
                label,
                font=font_meta,
                fill=(200, 200, 200, 255),
            )
            draw_score_bar(
                image, score,
                bottom_margin=int(height * cfg.accent_bar_bottom_ratio),
                glow_threshold=cfg.score_glow_threshold,
                glow_blur=cfg.score_glow_blur,
                glow_alpha=cfg.score_glow_alpha,
                color_mode=cfg.score_color_mode,
            )

        elif cfg.rating_display_mode == 2:
            font_size = int(width * cfg.numeric_score_font_size_ratio)
            # Score formatting:
            #   out of 100 (default): "87", "100", "N/A"
            #   out of 10:            "8.7", "8.0" (always one decimal), "10"
            #                         (no decimal — already two glyphs wide)
            # Non-numeric scores ("N/A") pass through unchanged in either mode.
            if cfg.score_out_of_10 and isinstance(score, (int, float)):
                _score_text = "10" if score >= 100 else f"{score / 10:.1f}"
            else:
                _score_text = str(score)
            label = f"{genre} ★ {_score_text}"
            rating_cy = height * cfg.numeric_score_y_offset

            try:
                font_meta = ImageFont.truetype(os.path.join(_FONTS_DIR, "Inter-Bold.ttf"), font_size)
            except IOError:
                font_meta = ImageFont.load_default()

            tx, ty = _text_center(draw, label, font_meta, width / 2, rating_cy)  # type: ignore
            draw.text(
                (tx, ty - int(font_size * 0.10)),
                label,
                font=font_meta,
                fill=(200, 200, 200, 255),
            )

        elif cfg.rating_display_mode == 3:
            font_size = int(width * cfg.minimalist_mode_font_size_ratio)

            try:
                font_meta = ImageFont.truetype(os.path.join(_FONTS_DIR, "Inter-Bold.ttf"), font_size)
            except IOError:
                font_meta = ImageFont.load_default()

            y = round(height * cfg.minimalist_mode_font_y_offset)
            right_edge = width - int(width * cfg.minimalist_mode_font_x_offset)

            year_text  = str(release_year or "")
            genre_text = genre

            pip_gap = int(font_size * 0.55)
            pip_w   = max(4, int(font_size * 0.18))
            pip_h   = int(font_size * 1.4)

            genre_bb = draw.textbbox((0, 0), genre_text, font=font_meta)
            genre_w  = genre_bb[2] - genre_bb[0]

            if year_text:
                year_bb = draw.textbbox((0, 0), year_text, font=font_meta)
                year_w  = year_bb[2] - year_bb[0]
            else:
                year_w = 0

            pip_x  = right_edge - year_w - pip_gap - pip_w
            pip_cy = round(y + font_size * 0.60)

            genre_x = pip_x - pip_gap - genre_w
            draw.text((genre_x, y), genre_text, font=font_meta, fill=(235, 235, 235, 255))

            if year_text:
                year_x = pip_x + pip_w + pip_gap
                draw.text((year_x, y), year_text, font=font_meta, fill=(235, 235, 235, 255))

            if score not in ("N/A", None):
                draw_score_bar_vertical(
                    image,
                    score,
                    x=pip_x,
                    y_center=pip_cy,
                    height=pip_h,
                    width=pip_w,
                    color_mode=cfg.score_color_mode,
                )

        elif cfg.rating_display_mode == 4:
            # Compact — Genre · Year · Sash text, centred.  Reads the sash
            # pick from the hoisted sash_result above so the sash-text
            # segment still appears even when the diagonal sash itself is
            # hidden (compact is purely additive).  sash_type is passed in
            # so the renderer can switch the preceding separator from "·"
            # to "★" for winners — see draw_compact_label docstring.
            _sash_label, _sash_type = (
                sash_result if sash_result else (None, None)
            )
            draw_compact_label(
                image,
                genre=genre,
                year=release_year,
                score=score,
                sash_label=_sash_label,
                sash_type=_sash_type,
                font_size_ratio=cfg.compact_font_size_ratio,
                y_offset=cfg.compact_y_offset,
                score_color_mode=cfg.score_color_mode,
                show_year=cfg.compact_show_year,
            )

    # --- Discovery sash / badge ---
    if cfg.show_award_sash and sash_result is not None:
        label, sash_type = sash_result
        if cfg.sash_badge:
            image = draw_award_badge(image, label, sash_type=sash_type,
                                     x_ratio=cfg.sash_badge_x,
                                     y_ratio=cfg.sash_badge_y,
                                     size_ratio=cfg.sash_badge_size)
        else:
            image = draw_award_sash(image, label, sash_type=sash_type, muted=cfg.muted,
                                    length_ratio=cfg.sash_length_ratio,
                                    height_ratio=cfg.sash_height_ratio)

    return image


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

async def _cache_prune_loop() -> None:
    """Periodically prune expired rows from all cache tables."""
    # Wait a few minutes after startup before the first run so the service
    # is fully warmed before taking the SQLite write lock.
    await asyncio.sleep(300)
    while True:
        logger.info("Running scheduled cache prune")
        await asyncio.get_running_loop().run_in_executor(None, prune_caches)

        # Evict expired entries from the in-process rating backoff dict.
        # Entries are also removed lazily on access, but titles that are never
        # re-requested would otherwise accumulate indefinitely.
        _now = asyncio.get_running_loop().time()
        expired = [k for k, v in _rating_backoff.items() if v <= _now]
        for k in expired:
            del _rating_backoff[k]
        if expired:
            logger.debug(f"Pruned {len(expired)} expired rating backoff entries")

        await asyncio.sleep(6 * 3600)   # every 6 hours


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _HTTP_CLIENT, _configurator_html
    init_db()
    logger.info(f"Cache initialised (composite TTL {_cfg.COMPOSITE_CACHE_TTL}s / "
                f"{_cfg.COMPOSITE_CACHE_TTL / 86400:.1f}d)")
    _HTTP_CLIENT = _make_http_client()
    logger.info("HTTP client initialised")
    # Warn on quality source misconfiguration
    if _cfg.QUALITY_SOURCE == "scraper" and (bool(_cfg.AIOSTREAMS_URL) or bool(_cfg.AIOSTREAMS_AUTH)):
        logger.warning(
            "QUALITY_SOURCE=scraper but AIOSTREAMS_URL/AIOSTREAMS_AUTH are also set — "
            "scraper will be used; AIOSTREAMS settings are ignored. "
            "Unset AIOSTREAMS_URL and AIOSTREAMS_AUTH to silence this warning."
        )
    if _cfg.QUALITY_SOURCE == "scraper" and not _cfg.SCRAPER_URL:
        logger.warning("QUALITY_SOURCE=scraper but SCRAPER_URL is not set — quality fetching is disabled.")
    if _cfg.QUALITY_SOURCE not in ("aiostreams", "scraper"):
        logger.warning(f"Unknown QUALITY_SOURCE={_cfg.QUALITY_SOURCE!r} — defaulting to aiostreams behaviour.")
    _configurator_html = _load_configurator_html()
    prune_task   = asyncio.create_task(_cache_prune_loop())
    digital_task = asyncio.create_task(digital_release_poll_loop(_HTTP_CLIENT))
    yield
    prune_task.cancel()
    digital_task.cancel()
    # Await the cancelled tasks so their finally: blocks finish unwinding
    # before we close the HTTP client they may still be using.
    with suppress(asyncio.CancelledError):
        await prune_task
    with suppress(asyncio.CancelledError):
        await digital_task
    await _HTTP_CLIENT.aclose()
    logger.info("HTTP client closed")


app = FastAPI(lifespan=lifespan)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_FONTS_DIR = os.path.join(BASE_DIR, "fonts")
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")


@app.middleware("http")
async def remove_server_header(request: Request, call_next):
    response = await call_next(request)
    response.headers["server"] = "unknown"
    return response


# ---------------------------------------------------------------------------
# Server capability endpoint
# ---------------------------------------------------------------------------

@app.get("/server-caps")
async def server_caps(access_key: str = ""):
    if _cfg.ACCESS_KEY and not hmac.compare_digest(access_key, _cfg.ACCESS_KEY):
        raise HTTPException(status_code=403, detail="Unauthorized")
    return {
        "tmdb_key_set":          bool(_cfg.SERVER_TMDB_KEY),
        "mdblist_key_set":       bool(_cfg.SERVER_MDBLIST_KEY),
        "aiostreams_configured": bool(_cfg.AIOSTREAMS_URL and _cfg.AIOSTREAMS_AUTH),
        "quality_configured":    (
            bool(_cfg.AIOSTREAMS_URL and _cfg.AIOSTREAMS_AUTH)
            or (_cfg.QUALITY_SOURCE == "scraper" and bool(_cfg.SCRAPER_URL))
        ),
    }


# ---------------------------------------------------------------------------
# Configurator HTML
# ---------------------------------------------------------------------------

_configurator_html: str | None = None
# Strong ETag for the configurator HTML — short hash of its bytes so the
# browser can revalidate cheaply.  Without this, browsers heuristically
# cache the page and keep serving stale HTML after a container rebuild,
# which is what made sliders / dropdowns drift out of sync with the new
# defaults until a manual Reset.
_configurator_etag: str | None = None


def _load_configurator_html() -> str:
    global _configurator_etag
    html_path = os.path.join(os.path.dirname(__file__), "configurator.html")
    try:
        with open(html_path, "r", encoding="utf-8") as f:
            content = f.read()
        _configurator_etag = '"' + hashlib.md5(content.encode("utf-8")).hexdigest()[:16] + '"'
        return content
    except FileNotFoundError:
        _configurator_etag = '"missing"'
        return "<h1>Configurator not found</h1><p>Place configurator.html alongside main.py</p>"


@app.get("/health")
async def health_check():
    """Lightweight liveness probe — no auth required, used by Docker healthcheck."""
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
async def get_configurator(request: Request, access_key: str = "", reload: str = ""):
    if _cfg.ACCESS_KEY and not hmac.compare_digest(access_key, _cfg.ACCESS_KEY):
        raise HTTPException(status_code=403, detail="Unauthorized. Provide ?access_key=<key>")
    # ?reload=1 re-reads configurator.html from disk — useful while iterating on
    # the UI without restarting the container.  Gated on the access key so it's
    # not a public DoS vector via disk re-reads.
    global _configurator_html
    if reload:
        _configurator_html = _load_configurator_html()
        logger.info("Configurator HTML reloaded from disk")

    if _configurator_html is None:
        _load_configurator_html()  # populates the global

    # 304 short-circuit when the browser's cached copy still matches —
    # saves the 130 KB body re-download on every navigation while still
    # forcing a fresh fetch as soon as the file's contents change.
    _cache_headers = {
        "Cache-Control": "no-cache, must-revalidate",
        "ETag":          _configurator_etag or '""',
    }
    if (
        _configurator_etag
        and request.headers.get("if-none-match") == _configurator_etag
    ):
        return Response(status_code=304, headers=_cache_headers)

    return HTMLResponse(
        content=_configurator_html or _load_configurator_html(),
        headers=_cache_headers,
    )


# ---------------------------------------------------------------------------
# Search endpoint
# ---------------------------------------------------------------------------

@app.get("/search")
async def search_proxy(
    q: str,
    tmdb_key: str = "",
    access_key: str = "",
):
    if _cfg.ACCESS_KEY and not hmac.compare_digest(access_key, _cfg.ACCESS_KEY):
        raise HTTPException(status_code=403, detail="Unauthorized")
    if len(q) > 200:
        raise HTTPException(status_code=400, detail="Query too long")

    effective_key = _resolve_tmdb_key(tmdb_key)
    if not effective_key:
        raise HTTPException(status_code=400, detail="No TMDB API key available")

    if _HTTP_CLIENT is None:
        raise HTTPException(status_code=503, detail="Service unavailable")
    resp = await _HTTP_CLIENT.get(
        "https://api.themoviedb.org/3/search/multi",
        params={
            "api_key": effective_key,
            "query": q,
            "include_adult": "false",
            "page": "1",
        },
    )
    return Response(content=resp.content, media_type="application/json", status_code=resp.status_code)


@app.get("/resolve-imdb")
async def resolve_imdb(
    tmdb_id: str,
    type: str = "movie",
    tmdb_key: str = "",
    access_key: str = "",
):
    if _cfg.ACCESS_KEY and not hmac.compare_digest(access_key, _cfg.ACCESS_KEY):
        raise HTTPException(status_code=403, detail="Unauthorized")

    _check_tmdb_id(tmdb_id)
    _check_type(type)

    effective_key = _resolve_tmdb_key(tmdb_key)
    if not effective_key:
        raise HTTPException(status_code=400, detail="No TMDB API key available")

    endpoint = (
        f"https://api.themoviedb.org/3/tv/{tmdb_id}/external_ids"
        if type == "tv"
        else f"https://api.themoviedb.org/3/movie/{tmdb_id}/external_ids"
    )

    if _HTTP_CLIENT is None:
        raise HTTPException(status_code=503, detail="Service unavailable")
    resp = await _HTTP_CLIENT.get(endpoint, params={"api_key": effective_key})
    return Response(content=resp.content, media_type="application/json", status_code=resp.status_code)


# ---------------------------------------------------------------------------
# Poster endpoint
# ---------------------------------------------------------------------------

@app.get("/poster")
async def get_poster(
    request: Request,
    tmdb_id: str,
    imdb_id: str,
    type: str = "movie",
    quality: str = "",
    season: int = 1,
    episode: int = 1,
    access_key: str = "",
    mdblist_key: str = "",
    tmdb_key: str = "",
    show_award_sash: str | None = None,
    badge_display_mode: str | None = None,
    show_quality_badges: str | None = None,
    rating_display_mode: str | None = None,
    accent_bar_font_size_ratio: str | None = None,
    numeric_score_font_size_ratio: str | None = None,
    accent_bar_y_offset: str | None = None,
    numeric_score_y_offset: str | None = None,
    minimalist_mode_font_size_ratio: str | None = None,
    minimalist_mode_font_x_offset: str | None = None,
    minimalist_mode_font_y_offset: str | None = None,
    score_glow_threshold: str | None = None,
    score_glow_blur: str | None = None,
    score_glow_alpha: str | None = None,
    logo_max_w_ratio: str | None = None,
    logo_max_h_ratio: str | None = None,
    logo_bottom_ratio: str | None = None,
    badge_height: str | None = None,
    badge_gap: str | None = None,
    badge_anchor_x: str | None = None,
    badge_anchor_y: str | None = None,
    movie_weights: str | None = None,
    tv_weights: str | None = None,
    logo_language: str | None = None,
    sash_priority: str | None = None,
    muted: str | None = None,
    textless: str | None = None,
    score_color_mode: str | None = None,
    debug: str | None = None,
):
    if _cfg.ACCESS_KEY and not hmac.compare_digest(access_key, _cfg.ACCESS_KEY):
        raise HTTPException(status_code=403, detail="Unauthorized, your access key is not valid for this instance.")

    _check_tmdb_id(tmdb_id)
    _check_imdb_id(imdb_id)
    _check_type(type)

    # -----------------------------------------------------------------------
    # Single-user mode: check for a cached final poster first.
    # The cache key includes imdb_id and type; quality is intentionally
    # excluded because in single-user mode the quality tokens come from
    # AIOStreams (not from query params) and are themselves cached per-title.
    # If the caller passes an explicit quality= override this bypass is
    # skipped so they always get the exact poster they asked for.
    # -----------------------------------------------------------------------
    effective_tmdb_key    = _resolve_tmdb_key(tmdb_key)
    effective_mdblist_key = _resolve_mdblist_key(mdblist_key)

    if not effective_tmdb_key:
        raise HTTPException(
            status_code=400,
            detail=(
                "No TMDB API key available. Either provide tmdb_key= as a query parameter "
                "or configure the TMDB_API_KEY environment variable on the server."
            ),
        )

    raw_params = {
        k: v for k, v in request.query_params.items()
        if k not in (
            "tmdb_id", "imdb_id", "mdblist_key", "tmdb_key", "type",
            "quality", "season", "episode", "access_key", "debug",
        )
    }
    rcfg = build_request_config(raw_params)

    # ------------------------------------------------------------------
    # Final poster cache — keyed on imdb_id, type, and a short hash of
    # all rendering parameters so different visual configs don't collide.
    # Skipped when an explicit quality= override is supplied (one-off).
    # ------------------------------------------------------------------
    if not quality:
        _params_hash = hashlib.md5(
            "&".join(f"{k}={v}" for k, v in sorted(raw_params.items())).encode()
        ).hexdigest()[:8]
        final_cache_key = f"{imdb_id}:{type}:{_params_hash}"
        cached_jpeg = get_cached_final_poster(final_cache_key)
        if cached_jpeg is not None:
            logger.info(f"Final poster cache hit for {final_cache_key}")
            etag = f'"{final_cache_key}"'
            if request.headers.get("if-none-match") == etag:
                return Response(status_code=304)
            _hit_resp = Response(content=cached_jpeg, media_type="image/jpeg")
            _hit_resp.headers["ETag"] = etag
            if _cfg.CDN_CACHE_TTL > 0:
                _hit_resp.headers["Cache-Control"] = f"public, max-age={_cfg.CDN_CACHE_TTL}"
            return _hit_resp
    else:
        final_cache_key = None

    # ------------------------------------------------------------------
    # Request coalescing: if another request in this worker is already
    # rendering the same poster, await its result instead of duplicating
    # the pipeline.  Quality-override requests (final_cache_key=None) are
    # always rendered independently.
    # ------------------------------------------------------------------
    _render_fut: "asyncio.Future[bytes] | None" = None
    if final_cache_key is not None:
        _existing_fut = _render_inflight.get(final_cache_key)
        if _existing_fut is not None:
            logger.info(f"Coalescing request for {final_cache_key}")
            try:
                _coal_resp = Response(content=await _existing_fut, media_type="image/jpeg")
                _coal_resp.headers["ETag"] = f'"{final_cache_key}"'
                if _cfg.CDN_CACHE_TTL > 0:
                    _coal_resp.headers["Cache-Control"] = f"public, max-age={_cfg.CDN_CACHE_TTL}"
                return _coal_resp
            except Exception:
                # The in-flight render failed; fall through and try ourselves.
                pass
        _render_fut = asyncio.get_running_loop().create_future()
        # Suppress asyncio's "Future exception was never retrieved" warning when
        # the render fails and no other request is coalesced onto this future.
        _render_fut.add_done_callback(
            lambda f: f.exception() if not f.cancelled() and f.exception() else None
        )
        _render_inflight[final_cache_key] = _render_fut

    # Declare globals that are both read and written in this function so Python
    # doesn't complain about use-before-global-declaration.
    global _mdblist_global_cooldown_until

    cached_rating = get_cached_rating(imdb_id)

    if cached_rating is not None:
        (
            cached_ratings_dict,
            cached_genre,
            cached_release_date,
            cached_award_wins,
            cached_award_noms,
            cached_awards_fetched,
            cached_festival_label,
            cached_age_rating,
            cached_is_cult,
            cached_is_true_story,
            cached_is_metacritic,
        ) = cached_rating
    else:
        cached_ratings_dict   = None
        cached_genre          = None
        cached_release_date   = None
        cached_award_wins     = []
        cached_award_noms     = []
        cached_awards_fetched = False
        cached_festival_label = None
        cached_age_rating     = None
        cached_is_cult        = False
        cached_is_true_story  = False
        cached_is_metacritic  = False

    release_date_for_quality_ttl = cached_release_date
    rating_already_cached        = cached_rating is not None

    # ------------------------------------------------------------------
    # Rating fetch coalescing + back-off
    #
    # Goal: ensure at most one MDBlist call per imdb_id per worker at a
    # time, and suppress re-fetches for an hour after a confirmed failure.
    #
    # Back-off check: if a recent fetch returned FETCH_FAILED, skip the
    # API call entirely until the TTL expires.
    #
    # Coalescing: if another coroutine in this worker is already fetching
    # the same imdb_id, wait for its asyncio.Event, then re-read the DB.
    # If it succeeded we get the cached data for free; if it failed we
    # re-check the back-off (now set by the other coroutine) before
    # deciding whether to attempt our own call.
    # ------------------------------------------------------------------
    _rating_event_to_set: asyncio.Event | None = None
    _rating_backoff_active = False  # set when backoff nullifies the key; used to suppress final-poster caching

    if not rating_already_cached and effective_mdblist_key:
        _loop_now = asyncio.get_running_loop().time()

        # Global rate-limit cooldown: when MDBlist is throttling the key, skip all
        # MDBlist calls until the window expires.  This prevents every queued title
        # from individually hitting 429 and accumulating its own per-title backoff.
        if _loop_now < _mdblist_global_cooldown_until:
            _remaining = _mdblist_global_cooldown_until - _loop_now
            logger.debug(
                f"Rating fetch for {imdb_id} skipped "
                f"(MDBlist global rate-limit cooldown: {_remaining:.0f}s remaining)"
            )
            effective_mdblist_key = None
            _rating_backoff_active = True

        # Per-title backoff (network failures, or this specific title's last 429).
        if effective_mdblist_key:
            _backoff_until = _rating_backoff.get(imdb_id)
            if _backoff_until is not None:
                if _loop_now < _backoff_until:
                    logger.debug(f"Rating fetch for {imdb_id} skipped (MDBlist per-title back-off active)")
                    effective_mdblist_key = None
                    _rating_backoff_active = True
                else:
                    del _rating_backoff[imdb_id]       # expired — allow a fresh attempt
                    _rating_fail_count.pop(imdb_id, None)  # reset escalation for clean slate

    if not rating_already_cached and effective_mdblist_key:
        _inflight_event = _rating_fetch_inflight.get(imdb_id)
        if _inflight_event is not None:
            # Another coroutine is mid-fetch — wait and piggyback on its result.
            logger.info(f"Rating fetch coalesced for {imdb_id} — awaiting in-flight fetch")
            await _inflight_event.wait()
            _refreshed = get_cached_rating(imdb_id)
            if _refreshed is not None:
                (
                    cached_ratings_dict,
                    cached_genre,
                    cached_release_date,
                    cached_award_wins,
                    cached_award_noms,
                    cached_awards_fetched,
                    cached_festival_label,
                    cached_age_rating,
                    cached_is_cult,
                    cached_is_true_story,
                    cached_is_metacritic,
                ) = _refreshed
                rating_already_cached        = True
                release_date_for_quality_ttl = cached_release_date
                logger.info(f"Rating coalesce succeeded for {imdb_id} — using cached result")
            else:
                # The other fetch also failed; re-check back-off it may have set.
                _loop_now2    = asyncio.get_running_loop().time()
                _backoff_now2 = _rating_backoff.get(imdb_id)
                if _backoff_now2 is not None and _loop_now2 < _backoff_now2:
                    logger.debug(
                        f"Rating fetch for {imdb_id} suppressed after coalescence (back-off active)"
                    )
                    effective_mdblist_key = None
        else:
            # First request for this imdb_id — claim the fetch slot.
            _rating_event_to_set              = asyncio.Event()
            _rating_fetch_inflight[imdb_id]   = _rating_event_to_set

    # Quality tokens — cache checked exactly once here; fetch fn only writes.
    if quality:
        quality_tokens = parse_quality(quality)
        cached_tokens  = None
    else:
        cached_tokens  = get_cached_quality(imdb_id, release_date_for_quality_ttl)
        quality_tokens = cached_tokens or []

    # A quality source is available when the server has AIOStreams configured,
    # or QUALITY_SOURCE=scraper with a valid SCRAPER_URL.
    _has_quality_source = (
        bool(_cfg.AIOSTREAMS_URL and _cfg.AIOSTREAMS_AUTH)
        or (_cfg.QUALITY_SOURCE == "scraper" and bool(_cfg.SCRAPER_URL))
    )
    quality_needs_fetch = (
        rcfg.badge_display_mode in (1, 2, 4)
        and not quality
        and cached_tokens is None
        and _has_quality_source
    )

    quality_pending = False
    if quality_needs_fetch and not rcfg.wait_for_quality:
        # Fire-and-forget background fetch — poster is served immediately
        # without badges; the cache will be warm on the next request.
        if imdb_id not in _quality_bg_inflight:
            _quality_bg_inflight.add(imdb_id)
            asyncio.create_task(
                _background_quality_fetch(
                    imdb_id, type, season, episode,
                    release_date_for_quality_ttl,
                )
            )
            logger.info(f"Quality fetch deferred to background for {imdb_id}")
        else:
            logger.info(f"Quality background fetch already in progress for {imdb_id}")
        quality_needs_fetch = False
        quality_pending = True

    if not rating_already_cached and not effective_mdblist_key:
        logger.warning(
            f"No MDblist key for {imdb_id} and no cached rating — "
            "poster will be served without rating/award data."
        )

    effective_movie_weights = rcfg.movie_weights or _cfg.MOVIE_WEIGHTS
    effective_tv_weights    = rcfg.tv_weights    or _cfg.TV_WEIGHTS

    if _HTTP_CLIENT is None:
        raise HTTPException(status_code=503, detail="Service unavailable")
    client = _HTTP_CLIENT

    try:
        genre_ids, is_textless, logos, release_year, title, poster_path, backdrop_path, tmdb_data = (
            await fetch_poster_metadata(client, tmdb_id, effective_tmdb_key, type, rcfg.logo_language)
        )

        # Resolve genre string from TMDB genre_ids immediately — this is always
        # available regardless of MDBlist status, so we can use it as a reliable
        # fallback if the rating fetch fails or is skipped entirely.
        _gid_set = set(genre_ids)
        _tmdb_genre = "Unknown"
        for _gid in _cfg.GENRE_PRIORITY:
            if _gid in _gid_set:
                _candidate = _cfg.GENRE_MAP.get(_gid, "")
                if _candidate:
                    _tmdb_genre = _candidate
                    break

        # Backdrop fallback: when no null-language textless poster exists, use
        # the landscape backdrop cropped to portrait.  Backdrops are almost always
        # textless by design and TMDB coverage is near-universal, so this recovers
        # the vast majority of titles that would otherwise fall back to a textual
        # poster — OR, when no poster art exists at all, a genre-tinted canvas.
        #   poster missing entirely  → prefer backdrop over the canvas
        #   poster exists with text  → prefer backdrop over the text-burned poster
        _use_backdrop = bool(backdrop_path) and (poster_path is None or not is_textless)
        if _use_backdrop:
            logger.info(f"No textless poster for {tmdb_id} — using backdrop crop as portrait fallback")
            is_textless = True          # backdrop is textless; enable logo compositing

        if rating_already_cached or not effective_mdblist_key:
            rating_coro = _resolved(
                (cached_ratings_dict, cached_genre, cached_release_date, [], cached_age_rating)
            )
        else:
            global _mdblist_semaphore
            if _mdblist_semaphore is None:
                _mdblist_semaphore = asyncio.Semaphore(_cfg.MDBLIST_CONCURRENCY)

            async def _fetch_rating_gated(
                _client=client, _imdb_id=imdb_id, _key=effective_mdblist_key,
                _gids=genre_ids, _type=type,
                _mw=effective_movie_weights, _tw=effective_tv_weights,
            ):
                async with _mdblist_semaphore:
                    return await _with_retry(
                        fetch_rating,
                        _client, _imdb_id, _key, _gids, _type,
                        movie_weights=_mw, tv_weights=_tw,
                    )

            rating_coro = _fetch_rating_gated()

        # Quality is always fetched in the background (never inline); the 4th
        # gather slot was removed after quality_needs_fetch was made always-False.
        is_no_poster = poster_path is None and not _use_backdrop
        if _use_backdrop:
            _image_coro = fetch_backdrop_image(client, tmdb_id, backdrop_path)
        elif is_no_poster:
            _image_coro = _resolved(_make_fallback_canvas(genre_ids))
        else:
            _image_coro = fetch_poster_image(client, tmdb_id, type, poster_path)

        (
            image,
            logo,
            rating_result,
            trending_rank,
        ) = await asyncio.gather(
            _image_coro,
            fetch_logo(client, logos, rcfg.logo_language, imdb_id=imdb_id, original_language=tmdb_data.get("original_language"), skip_native=not rcfg.logo_native_fallback) if (is_textless and not is_no_poster) else _resolved(None),
            rating_coro,
            fetch_trending_rank(client, tmdb_id, effective_tmdb_key, type),
        )

        # Release the rating inflight event early — the rating is now cached so
        # any coalesced requests can proceed without waiting for the quality fetch.
        if _rating_event_to_set is not None:
            _rating_event_to_set.set()
            _rating_fetch_inflight.pop(imdb_id, None)
            _rating_event_to_set = None   # prevent redundant set in finally block

        # Inline quality wait — runs after gather so rating coalescing is never
        # blocked.  Used for poster-warm workflows where latency doesn't matter.
        if quality_needs_fetch and rcfg.wait_for_quality:
            async def _inline_fetch():
                if _cfg.QUALITY_SOURCE == "scraper" and _cfg.SCRAPER_URL:
                    return await _with_retry(
                        fetch_quality_from_scraper,
                        client, _cfg.SCRAPER_URL,
                        imdb_id, type, season, episode, release_date_for_quality_ttl,
                    )
                return await _with_retry(
                    fetch_quality_from_aiostreams,
                    client, imdb_id, type, season, episode, release_date_for_quality_ttl,
                )
            try:
                fetched = await asyncio.wait_for(
                    _inline_fetch(), timeout=_cfg.QUALITY_WAIT_TIMEOUT
                )
                if fetched is not FETCH_FAILED:
                    quality_tokens = fetched
                    logger.info(f"Inline quality fetch complete for {imdb_id}: {quality_tokens}")
                else:
                    # AIOStreams/scraper returned a transient error — don't cache
                    # the composite poster without quality so the next request retries.
                    logger.warning(
                        f"Inline quality fetch failed for {imdb_id} "
                        "— serving without quality, composite not cached"
                    )
                    quality_pending = True
            except asyncio.TimeoutError:
                logger.warning(
                    f"Quality wait timed out for {imdb_id} "
                    f"after {_cfg.QUALITY_WAIT_TIMEOUT:.0f}s — serving without quality, "
                    "composite not cached so next request retries"
                )
                quality_pending = True
            quality_needs_fetch = False

        # ------------------------------------------------------------------
        # Unpack results
        # ------------------------------------------------------------------
        rate_limited  = isinstance(rating_result, _RateLimited)
        rating_failed = (
            not rating_already_cached
            and effective_mdblist_key
            and (rating_result is FETCH_FAILED or rate_limited)
        )

        if rating_failed:
            if rate_limited:
                # HTTP 429 — honour Retry-After if present, otherwise default 1 h.
                # Cap at 1 h so a misbehaving upstream can't park us indefinitely.
                if rating_result.retry_after:
                    backoff_secs = min(float(rating_result.retry_after), 3600.0)
                    logger.warning(
                        f"MDblist rate-limited {imdb_id} — honouring Retry-After "
                        f"({backoff_secs:.0f}s)"
                    )
                else:
                    backoff_secs = 3600.0
                    logger.warning(f"MDblist rate-limited {imdb_id} — using 1h default back-off")

                # Also set a global cooldown so all other queued MDBlist requests
                # stand down for the same window instead of hitting 429 one by one.
                # Cap the global window at 2 min — enough to cover any realistic
                # rolling-window reset without freezing the whole service for an hour.
                _global_window = min(backoff_secs, 120.0)
                _new_global_until = asyncio.get_running_loop().time() + _global_window
                if _new_global_until > _mdblist_global_cooldown_until:
                    _mdblist_global_cooldown_until = _new_global_until
                    logger.warning(
                        f"MDBlist global cooldown activated: {_global_window:.0f}s "
                        f"(all MDBlist requests paused)"
                    )
            else:
                # Network / timeout failure — escalating back-off so a transient
                # hiccup retries quickly while a sustained outage backs off further.
                # Ladder: 30 s → 2 min → 8 min → 1 h (cap), using 4× multiplier.
                fail_n = _rating_fail_count.get(imdb_id, 0) + 1
                _rating_fail_count[imdb_id] = fail_n
                backoff_secs = min(30 * (4 ** (fail_n - 1)), 3600.0)
                logger.warning(
                    f"Rating fetch failed for {imdb_id} (attempt {fail_n}) "
                    f"— back-off {backoff_secs:.0f}s"
                )
            _rating_backoff[imdb_id] = asyncio.get_running_loop().time() + backoff_secs
            ratings_dict   = {}
            genre          = cached_genre or _tmdb_genre
            rel            = cached_release_date
            score          = "N/A"
            keywords       = []
            award_wins     = cached_award_wins
            award_noms     = cached_award_noms
            festival_label = cached_festival_label
            age_rating     = cached_age_rating
            is_cult        = cached_is_cult
            is_true_story  = cached_is_true_story
            is_metacritic  = cached_is_metacritic
        else:
            ratings_dict, genre, rel, keywords, age_rating = rating_result
            # genre from MDBlist/cache may be None when the key is absent and
            # nothing is cached yet — fall back to the TMDB-derived genre.
            genre = genre or _tmdb_genre

            # Fresh successful fetch — clear any escalation state so future
            # failures start back at the shortest interval.
            if not rating_already_cached and not _rating_backoff_active:
                _rating_fail_count.pop(imdb_id, None)

            if isinstance(ratings_dict, dict):
                weights = (
                    effective_tv_weights
                    if type in ("tv", "series")
                    else effective_movie_weights
                )
                score = calculate_weighted_score(ratings_dict, weights)
            else:
                score = ratings_dict

            if rating_already_cached:
                award_wins     = cached_award_wins
                award_noms     = cached_award_noms
                festival_label = cached_festival_label
                age_rating     = cached_age_rating
                is_cult        = cached_is_cult
                is_true_story  = cached_is_true_story
                is_metacritic  = cached_is_metacritic
            else:
                award_wins, award_noms = parse_mdblist_awards(
                    keywords,
                    tmdb_id=tmdb_id,
                )
                kw_names = {(kw.get("name") or "").lower().strip() for kw in keywords}
                festival_label = next(
                    (label for kw, label in FESTIVAL_KEYWORDS.items() if kw in kw_names),
                    None,
                )
                is_cult       = bool({"cult-classic", "cult-film"} & kw_names)
                is_true_story = "based-on-true-story" in kw_names
                is_metacritic = "metacritic-must-see" in kw_names
                logger.info(f"Awards for {imdb_id}: wins={award_wins} noms={award_noms} "
                            f"festival={festival_label} age_rating={age_rating} "
                            f"cult={is_cult} true_story={is_true_story} metacritic={is_metacritic}")

        # ------------------------------------------------------------------
        # Write rating + awards to cache (only on a fresh fetch).
        # ------------------------------------------------------------------
        if not rating_failed and not rating_already_cached and effective_mdblist_key:
            set_cached_rating(
                imdb_id,
                ratings_dict if isinstance(ratings_dict, dict) else {},
                genre,
                rel,
                award_wins,
                award_noms,
                awards_fetched=True,
                festival_label=festival_label,
                age_rating=age_rating,
                is_cult=is_cult,
                is_true_story=is_true_story,
                is_metacritic=is_metacritic,
            )
            logger.info(f"Rating cached for {imdb_id}: score={score} genre={genre} "
                        f"wins={award_wins} noms={award_noms} festival={festival_label} "
                        f"age_rating={age_rating}")

        logger.info(f"Quality for {imdb_id}: tokens={quality_tokens} year={release_year}")

        # ------------------------------------------------------------------
        # Release status (opt-in via sash_priority — movies make an extra
        # /release_dates API call; TV is free, mapped from tmdb_status)
        # ------------------------------------------------------------------
        _release_status: str | None = None
        if "release_status" in rcfg.sash_priority:
            _release_status = await fetch_release_status(
                client, tmdb_id, effective_tmdb_key, type,
                tmdb_data.get("tmdb_status"),
            )
            # r/movieleaks confirmation overrides TMDB's theatrical/production
            # status — if the film is in the digital-release cache it's already
            # streaming regardless of what the official release dates say.
            if _release_status in ("Cinema", "Production") and is_digital_release(imdb_id):
                _release_status = "Streaming"

        # ------------------------------------------------------------------
        # Build DiscoveryMeta
        # ------------------------------------------------------------------
        discovery_meta = extract_discovery_meta(
            tmdb_data=tmdb_data,
            media_type=type,
            award_wins=award_wins,
            award_noms=award_noms,
            trending_rank=trending_rank,
            release_date=rel,
            keywords=keywords if not rating_already_cached else [],
            festival_label_override=festival_label,
            is_cult_override=is_cult,
            is_true_story_override=is_true_story,
            is_metacritic_override=is_metacritic,
            is_digital_release_override=is_digital_release(imdb_id),
            release_status_override=_release_status,
        )

        # ------------------------------------------------------------------
        # Debug mode: return diagnostic JSON instead of rendering the poster.
        # Useful for troubleshooting wrong sashes, missing ratings, etc.
        # Activate with ?debug=1 (never cached, never stored).
        # ------------------------------------------------------------------
        if debug and debug.strip() in ("1", "true"):
            _sash_result = pick_sash(discovery_meta, rcfg.sash_priority)
            return JSONResponse({
                "imdb_id":           imdb_id,
                "tmdb_id":           tmdb_id,
                "type":              type,
                "score":             score if isinstance(score, str) else int(score),
                "genre":             genre,
                "release_year":      release_year,
                "release_date":      rel,
                "quality_tokens":    quality_tokens,
                "age_rating":        age_rating,
                "award_wins":        award_wins,
                "award_noms":        award_noms,
                "festival_label":    festival_label,
                "sash":              {"label": _sash_result[0], "type": _sash_result[1]} if _sash_result else None,
                "is_cult":           discovery_meta.is_cult,
                "is_true_story":     discovery_meta.is_true_story,
                "is_metacritic":     discovery_meta.is_metacritic_must_see,
                "is_new_release":    discovery_meta.is_new_release,
                "is_digital_release":discovery_meta.is_digital_release,
                "trending_rank":     discovery_meta.trending_rank,
                "original_language": discovery_meta.original_language,
                "matched_studios":   discovery_meta.matched_studios,
                "matched_directors": discovery_meta.matched_directors,
                "matched_cast":      discovery_meta.matched_cast,
                "release_status":    discovery_meta.release_status,
                "sash_priority":     rcfg.sash_priority,
                "badge_display_mode":rcfg.badge_display_mode,
                "rating_display_mode":rcfg.rating_display_mode,
            })

        # Offload CPU-bound PIL compositing + JPEG encoding to the thread pool
        # so the event loop stays free for concurrent requests.
        _bp_args = dict(
            logo=logo if (is_textless and not is_no_poster and not rcfg.textless) else None,
            fallback_title=title if is_no_poster else (title if is_textless and not logo and not rcfg.textless else None),
            discovery_meta=discovery_meta,
            quality_tokens=quality_tokens,
            release_year=release_year,
            age_rating=age_rating,
            no_poster=is_no_poster,
        )

        def _composite_and_encode() -> bytes:
            result = build_poster(image, score, genre, rcfg, **_bp_args)
            buf = io.BytesIO()
            result.convert("RGB").save(buf, format="JPEG", quality=_cfg.JPEG_QUALITY)
            return buf.getvalue()

        img_bytes = await asyncio.get_running_loop().run_in_executor(
            None, _composite_and_encode
        )

        # Persist the finished poster so future requests skip the pipeline.
        # Skipped when:
        #   quality_pending      — badges would be missing; next request caches properly
        #   rating_failed        — MDBlist returned a hard failure; don't lock in N/A score
        #   _rating_backoff_active — a previous failure is still in its cool-down window;
        #                            backoff nullifies effective_mdblist_key so rating_failed
        #                            would evaluate False without this separate flag
        if final_cache_key is not None and not quality_pending and not rating_failed and not _rating_backoff_active:
            set_cached_final_poster(final_cache_key, img_bytes)
            logger.info(f"Final poster cached for {final_cache_key}")

        if _render_fut is not None:
            _render_fut.set_result(img_bytes)

        response = Response(content=img_bytes, media_type="image/jpeg")
        if final_cache_key is not None:
            response.headers["ETag"] = f'"{final_cache_key}"'
        if _cfg.CDN_CACHE_TTL > 0:
            response.headers["Cache-Control"] = f"public, max-age={_cfg.CDN_CACHE_TTL}"
        return response

    except ValueError as exc:
        if _render_fut is not None and not _render_fut.done():
            _render_fut.set_exception(exc)
        logger.warning(f"No poster available for tmdb_id={tmdb_id}: {exc}")
        raise HTTPException(status_code=404, detail=str(exc))
    except httpx.TimeoutException as exc:
        if _render_fut is not None and not _render_fut.done():
            _render_fut.set_exception(exc)
        logger.warning(f"Upstream timeout for tmdb_id={tmdb_id}: {type(exc).__name__}")
        raise HTTPException(status_code=504, detail="Upstream request timed out")
    except httpx.HTTPStatusError as exc:
        if _render_fut is not None and not _render_fut.done():
            _render_fut.set_exception(exc)
        status = exc.response.status_code
        if status == 404:
            # TMDB returned metadata with a poster/image path that no longer exists.
            # Invalidate the metadata cache so the next request re-fetches fresh data.
            _endpoint = "tv" if type in ("tv", "series") else "movie"
            delete_cached_tmdb_metadata(f"{_endpoint}_{tmdb_id}")
            logger.warning(
                f"TMDB image 404 for tmdb_id={tmdb_id} — metadata cache invalidated, "
                f"will self-heal on next request"
            )
            raise HTTPException(status_code=404, detail="Poster image not found on TMDB")
        logger.error(f"Upstream HTTP {status} for tmdb_id={tmdb_id}: {exc}")
        raise HTTPException(status_code=502, detail=f"Upstream error {status}")
    except Exception as exc:
        if _render_fut is not None and not _render_fut.done():
            _render_fut.set_exception(exc)
        logger.exception(f"Error building poster for tmdb_id={tmdb_id}")
        raise HTTPException(status_code=500, detail="Failed to build poster")
    finally:
        # Fire the rating event so any coalesced waiters unblock.  Under normal
        # operation this was already set early (after gather); this is the
        # safety net for error paths where we exit before reaching that point.
        if _rating_event_to_set is not None:
            _rating_event_to_set.set()
            _rating_fetch_inflight.pop(imdb_id, None)
        if final_cache_key is not None:
            _render_inflight.pop(final_cache_key, None)