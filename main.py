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
# Caps how many burned-in-text scans may occupy thread-pool workers at once.
# The scan is already serialised by text_detect._infer_lock, so extra in-flight
# scan tasks would just block on that lock while squatting on pool workers —
# starving compositing/encode of the rest of a poster burst.  Gating admission
# here keeps the shared executor free.  Created inside the event loop.
_detect_semaphore:              "asyncio.Semaphore | None" = None


def _get_detect_semaphore() -> "asyncio.Semaphore":
    """Lazily create the detection-admission semaphore inside the event loop."""
    global _detect_semaphore
    if _detect_semaphore is None:
        _detect_semaphore = asyncio.Semaphore(_cfg.TEXTLESS_DETECTION_CONCURRENCY)
    return _detect_semaphore
# Per-key cooldown timestamps (event-loop time). Keyed by the API key string so
# rotation is independent — a rate-limited key stands down while the other serves.
_mdblist_key_cooldown: dict[str, float] = {}
# Index into _cfg.SERVER_MDBLIST_KEYS for the currently active server-side key.
_mdblist_active_key_idx: int = 0


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
from awards import sample_frosted_notch_rgb
from ratings import sample_frosted_bar_rgb
from awards import FETCH_FAILED, _RateLimited, draw_award_badge, draw_award_sash, parse_mdblist_awards
from cache import (
    get_cached_quality,
    get_cached_rating,
    get_cached_final_poster,
    set_cached_final_poster,
    get_cached_text_detection,
    set_cached_text_detection,
    init_db,
    is_digital_release,
    set_cached_rating,
    delete_cached_tmdb_metadata,
    prune_caches,
    get_cache_stats,
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
from ratings import calculate_weighted_score, draw_score_bar, fetch_rating, draw_score_bar_vertical, _draw_solid_pip, draw_frosted_bar, _score_color, _score_color_alt, _score_color_metal
from tmdb import composite_logo, logo_centre_y, fetch_logo, fetch_poster_metadata, fetch_poster_image, fetch_backdrop_image, fetch_trending_rank, fetch_release_status, svg_logo_supported, _CROP_VERSION

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
    if _cfg.SERVER_MDBLIST_KEYS:
        return _cfg.SERVER_MDBLIST_KEYS[_mdblist_active_key_idx % len(_cfg.SERVER_MDBLIST_KEYS)]
    return None


def _detection_vote_ok(vote_count: int | None) -> bool:
    """
    Text detection only runs on titles we can confirm are low-vote (where TMDB
    mislabels concentrate).  A NULL/unknown vote_count — e.g. a metadata row
    cached before the column existed — must NOT pass the gate, otherwise
    detection would run on every cached title regardless of popularity.
    """
    return vote_count is not None and vote_count <= _cfg.TEXTLESS_DETECTION_MAX_VOTES


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
    # What to append after the genre in Minimalist mode:
    #   0 = Year (Genre + year, rating as a colour-coded pip — the original look)
    #   1 = Rating (Genre | Score, score printed as text)
    #   2 = Year + Rating (Genre | Year | Score)
    minimalist_append_mode: int = 0

    # Frosted bar (rating_display_mode == 4)
    bar_height_ratio:        float = 0.080
    bar_font_size_ratio:     float = 0.55
    bar_frost_opacity:       float = 0.85
    bar_bottom_inset:        float = 0.007
    bar_style:               str   = "frosted"  # "frosted"|"silver"|"gold"|"rating_black"|"rating_frosted"
    bar_accent:              str   = "silver"   # "silver"|"gold"|"palette_0"|"palette_1"|"palette_2"
    bar_score_out_of_10:     bool  = False
    bar_match_notch:         bool  = False  # share one frosted tint with the sash notch
    bar_append:              str   = "rating_year"  # "rating_year"|"rating"|"year"|"sash"

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
    # Logo resolution priority.  "native" = the viewer's chosen logo_language
    # (e.g. en); "original" = the content's own original language (e.g. ja for
    # an anime).  "text" = render the translated title as text.
    #   "native_original" (default): native → original → text
    #   "original_native":           original → native → text
    #   "native_text":               native → text (no original-language logo)
    logo_priority: str = "native_original"
    # Fallback-poster style for titles with no art: "minimal" (procedural textured
    # backdrop) or "photoreal" (hand-made photographic art that blends with real
    # posters).  Missing photoreal art degrades to the minimal set.
    fallback_bg_style: str = "minimal"
    # Original-art mode: serve TMDB's primary poster (title/logo baked into the
    # art) as-is, skipping our own logo overlay, text detection and the textless/
    # backdrop fallbacks.  The logo is part of the art in this mode.
    use_original_art: bool = False
    # Which poster original-art mode serves:
    #   "primary"   = TMDB's designated default poster (most recognisable)
    #   "top_rated" = highest-voted poster, by logo_priority language order
    original_art_source: str = "primary"
    sash_priority: list[str] = field(default_factory=lambda: list(_cfg.SASH_PRIORITY))
    muted: bool = False
    textless: bool = False
    score_color_mode: int = 2
    top_gradient:    str = "high"   # off | low | medium | high — strength of the top vignette
    bottom_gradient: str = "high"   # off | low | medium | high — strength of the bottom vignette
    sash_badge: bool = False              # True → centred notch badge instead of diagonal sash
    sash_badge_style:  str   = "frosted" # "silver" | "gold" | "frosted"
    sash_badge_size_w: float = 1.05      # horizontal scale of badge
    sash_badge_size_h: float = 1.05      # vertical scale of badge
    sash_badge_notch_offset: float = 0.0   # downward text nudge as fraction of badge height
    sash_badge_inset: float = 0.009        # top-edge offset as fraction of poster height (± small)
    sash_badge_font_ratio:   float = 0.43  # font size as fraction of badge height
    sash_badge_frost_opacity: float = 0.75 # frosted overlay opacity (0.0–1.0)
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
    cfg.sash_badge              = _b("sash_badge",              cfg.sash_badge)
    cfg.sash_badge_notch_offset  = _f("sash_badge_notch_offset",  cfg.sash_badge_notch_offset,  -0.5, 0.5)
    cfg.sash_badge_inset         = _f("sash_badge_inset",         cfg.sash_badge_inset,         -0.02, 0.02)
    cfg.sash_badge_font_ratio    = _f("sash_badge_font_ratio",    cfg.sash_badge_font_ratio,    0.10, 1.0)
    cfg.sash_badge_frost_opacity = _f("sash_badge_frost_opacity", cfg.sash_badge_frost_opacity, 0.0, 1.0)
    cfg.sash_badge_size_w       = _f("sash_badge_size_w",       cfg.sash_badge_size_w,       0.5, 2.0)
    cfg.sash_badge_size_h       = _f("sash_badge_size_h",       cfg.sash_badge_size_h,       0.5, 2.0)
    _style_raw = params.get("sash_badge_style", cfg.sash_badge_style)
    if _style_raw in ("silver", "gold", "frosted", "black"):
        cfg.sash_badge_style = _style_raw
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
    cfg.minimalist_append_mode = _i("minimalist_append_mode", cfg.minimalist_append_mode, 0, 2)

    cfg.bar_height_ratio        = _f("bar_height_ratio",        cfg.bar_height_ratio,        0.04, 0.20)
    cfg.bar_font_size_ratio     = _f("bar_font_size_ratio",     cfg.bar_font_size_ratio,     0.15, 0.70)
    cfg.bar_frost_opacity       = _f("bar_frost_opacity",       cfg.bar_frost_opacity,       0.0,  1.0)
    cfg.bar_bottom_inset        = _f("bar_bottom_inset",        cfg.bar_bottom_inset,        0.0,  0.10)
    _bst = (params.get("bar_style") or "").strip().lower()
    if _bst in ("frosted", "pure_black", "silver", "gold", "rating_black", "rating_frosted"):
        cfg.bar_style = _bst
    _bac = (params.get("bar_accent") or "").strip().lower()
    if _bac in ("silver", "gold", "sample", "palette_0", "palette_1", "palette_2"):
        cfg.bar_accent = _bac
    cfg.bar_score_out_of_10     = _b("bar_score_out_of_10",     cfg.bar_score_out_of_10)
    cfg.bar_match_notch         = _b("bar_match_notch",         cfg.bar_match_notch)
    _bap = (params.get("bar_append") or "").strip().lower()
    if _bap in ("rating_year", "rating", "year", "sash"):
        cfg.bar_append = _bap

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
    _lp = params.get("logo_priority")
    if _lp in ("native_original", "original_native", "native_text"):
        cfg.logo_priority = _lp
    elif "logo_native_fallback" in params:
        # Legacy param (boolean): true → native_original, false → native_text.
        cfg.logo_priority = "native_original" if _b("logo_native_fallback", True) else "native_text"
    _fbs = (params.get("fallback_bg_style") or "").strip().lower()
    if _fbs in ("minimal", "photoreal"):
        cfg.fallback_bg_style = _fbs
    cfg.use_original_art      = _b("use_original_art", cfg.use_original_art)
    _oas = (params.get("original_art_source") or "").strip().lower()
    if _oas in ("primary", "top_rated"):
        cfg.original_art_source = _oas
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
    "medium": (0.40, 210),
    "high":   (0.50, 225),
}
# Easing exponent shared across all bottom-gradient presets — controls the
# curve shape (1.0 = linear; >1 starts darker at the bottom and fades faster
# at the top).  Decoupled from strength so retuning one doesn't affect the
# other.
_BOTTOM_GRADIENT_CURVE = 1.5

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

# Display-only label shortenings.  Some genre names are too wide for the poster
# label strip; shortening them reads better than shrinking the font.  These map
# the genre name to its *printed* form only — the original genre key is still
# used for font / colour / background lookups.
_GENRE_LABEL_OVERRIDES: dict[str, str] = {
    "Documentary": "Doc",
}


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

    # Printed form of the genre (e.g. "Documentary" → "Doc").  Use this only for
    # text drawn on the poster; keep `genre` for font / colour lookups.
    genre_label = _GENRE_LABEL_OVERRIDES.get(genre, genre)

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
        # ── Genre-aware font selection ────────────────────────────────────────
        # Titles are bucketed by genre and rendered in a thematically matching
        # font so different content categories feel distinct.
        #
        # Bucket → font mapping:
        #   Horror / Thriller / Mystery  → Creepster  (gothic, unsettling)
        #   Action / Sci-Fi / Adventure  → Bebas Neue (bold, cinematic)
        #   Comedy / Animation / Family  → Pacifico   (friendly, rounded)
        #   Drama / Romance / History    → Playfair   (elegant, literary)
        #   Crime / War / Documentary    → Oswald     (authoritative, strong)
        #   Default                      → NotoSerif  (neutral, readable)
        _GENRE_FONTS: dict[str, str] = {
            "Horror":           "Creepster-Regular.ttf",
            "Thriller":         "Creepster-Regular.ttf",
            "Mystery":          "Creepster-Regular.ttf",
            "Action":           "BebasNeue-Bold.ttf",
            "Sci-Fi":           "BebasNeue-Bold.ttf",
            "Adventure":        "BebasNeue-Bold.ttf",
            "Fantasy":          "BebasNeue-Bold.ttf",
            "Western":          "BebasNeue-Bold.ttf",
            "Comedy":           "Pacifico-Regular.ttf",
            "Animation":        "Pacifico-Regular.ttf",
            "Family":           "Pacifico-Regular.ttf",
            "Drama":            "PlayfairDisplay-Bold.ttf",
            "Romance":          "PlayfairDisplay-Bold.ttf",
            "History":          "PlayfairDisplay-Bold.ttf",
            "Music":            "PlayfairDisplay-Bold.ttf",
            "Crime":            "Oswald-Bold.ttf",
            "War":              "Oswald-Bold.ttf",
            "Documentary":      "Oswald-Bold.ttf",
        }
        _font_file = _GENRE_FONTS.get(genre, "NotoSerif-Bold.ttf")

        # Fallback-title rendering, sized to fill the SAME envelope a logo fills
        # (cfg.logo_max_w_ratio width × logo_max_h_ratio height) so a text title
        # looks as substantial as a logo would — short titles like "SELF-HELP"
        # grow to fill the width instead of being pinned tiny by a char-count
        # heuristic.  The logo size ratios therefore tune the fallback text too.
        max_w          = int(width  * cfg.logo_max_w_ratio)
        max_h          = int(height * cfg.logo_max_h_ratio)
        # Sit on the exact same vertical centre line composite_logo uses, so a
        # text-title poster lines up with a logo poster in the same row.
        title_cy       = logo_centre_y(height, cfg.logo_bottom_ratio)
        MIN_FONT_SIZE  = 22
        MAX_LINES      = 2
        FONT_PATH      = os.path.join(_FONTS_DIR, _font_file)

        def _wrap_lines(text: str, current_font) -> list[str]:
            """Greedy word-wrap: each line packs as many words as fit within max_w."""
            words = text.split()
            if not words:
                return []
            lines: list[str] = []
            current: list[str] = []
            for word in words:
                candidate = " ".join(current + [word])
                if draw.textlength(candidate, font=current_font) <= max_w or not current:
                    current.append(word)
                else:
                    lines.append(" ".join(current))
                    current = [word]
            if current:
                lines.append(" ".join(current))
            return lines

        # Pick the largest font whose wrapped block fits the logo envelope: scan
        # high→low and take the first fit.  A 2-line block gets a taller budget
        # than a single logo, since two stacked lines read fine a bit beyond one
        # logo's height.  Falls back to the wrapped layout at MIN_FONT_SIZE.
        try:
            font_size = MIN_FONT_SIZE
            font      = ImageFont.truetype(FONT_PATH, font_size)
            lines     = _wrap_lines(fallback_title, font)
            for _fs in range(int(height * 0.26), MIN_FONT_SIZE - 1, -2):
                _f  = ImageFont.truetype(FONT_PATH, _fs)
                _ls = _wrap_lines(fallback_title, _f)
                if len(_ls) > MAX_LINES:
                    continue
                _widest  = max((draw.textlength(ln, font=_f) for ln in _ls), default=0)
                _block_h = int(_fs * 1.15) * len(_ls)
                _budget  = max_h if len(_ls) == 1 else int(max_h * 1.7)
                if _widest <= max_w and _block_h <= _budget:
                    font, font_size, lines = _f, _fs, _ls
                    break
        except OSError:
            font      = ImageFont.load_default()
            font_size = MIN_FONT_SIZE
            lines     = [fallback_title]

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
    # itself is rendered independently.
    sash_result = (
        pick_sash(discovery_meta, cfg.sash_priority)
        if discovery_meta is not None
        else None
    )

    # --- Shared frosted tint (Match Notch Colour) ---------------------------
    # When the frosted rating bar (mode 4) and a frosted sash notch are BOTH on
    # and bar_match_notch is set, sample each region now (before either is drawn)
    # and force both to the more saturated of the two colours so they match.
    _shared_tint: tuple[float, float, float] | None = None
    _notch_active = (
        cfg.show_award_sash and sash_result is not None
        and cfg.sash_badge and cfg.sash_badge_style == "frosted"
    )
    if (
        cfg.bar_match_notch
        and cfg.rating_display_mode == 4
        and cfg.bar_style in ("frosted", "rating_frosted")
        and _notch_active
    ):
        _bar_rgb   = sample_frosted_bar_rgb(image, cfg.bar_height_ratio, cfg.bar_bottom_inset)
        _notch_rgb = sample_frosted_notch_rgb(
            image, sash_result[0], sash_type=sash_result[1],
            size_ratio_w=cfg.sash_badge_size_w, size_ratio_h=cfg.sash_badge_size_h,
            font_size_ratio=cfg.sash_badge_font_ratio, notch_inset=cfg.sash_badge_inset,
        )
        def _sat(rgb: tuple[float, float, float]) -> float:
            import colorsys
            return colorsys.rgb_to_hsv(rgb[0] / 255, rgb[1] / 255, rgb[2] / 255)[1]
        _shared_tint = _bar_rgb if _sat(_bar_rgb) >= _sat(_notch_rgb) else _notch_rgb

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

            _pre_sash = [genre_label]
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
            label = f"{genre_label} ★ {_score_text}"
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
            _ink = (235, 235, 235, 255)

            # Segments, each tagged with the SEPARATOR that precedes it:
            #   "pip"  — silver vertical pip (before the year)
            #   "star" — ★ glyph (before the rating/score)
            #   "rpip" — pip COLOURED by score (mode 0 only: the rating shown
            #            purely by colour, no number)
            # Mode 0 ("Year"): genre [rating-pip] year
            # Mode 1 ("Rating"): genre ★ score
            # Mode 2 ("Year + Rating"): genre [pip] year ★ score
            _has_score = score not in ("N/A", None)
            parts = [(genre_label, None)]   # (text, separator_before)
            if cfg.minimalist_append_mode == 0:
                if release_year:
                    parts.append((str(release_year), "rpip"))
            elif cfg.minimalist_append_mode == 1:
                if _has_score:
                    parts.append((str(score), "star"))
            else:  # 2 — Year + Rating
                if release_year:
                    parts.append((str(release_year), "pip"))
                if _has_score:
                    parts.append((str(score), "star"))

            pip_gap = int(font_size * 0.55)
            pip_w   = max(4, int(font_size * 0.18))
            pip_h   = int(font_size * 1.4)
            pip_cy  = round(y + font_size * 0.60)
            star_w  = draw.textlength("★", font=font_meta)

            # Lay out right-to-left: each segment, with its separator to its left.
            cursor = right_edge
            ops    = []   # (kind, x[, text]); kind in text|pip|rpip|star
            for i in range(len(parts) - 1, -1, -1):
                seg, sep = parts[i]
                seg_x = cursor - draw.textlength(seg, font=font_meta)
                ops.append(("text", seg_x, seg))
                cursor = seg_x
                if sep:
                    cursor -= pip_gap
                    sep_w  = star_w if sep == "star" else pip_w
                    sep_x  = cursor - sep_w
                    ops.append((sep, sep_x))
                    cursor = sep_x - pip_gap

            for op in ops:
                kind, ox = op[0], op[1]
                if kind == "text":
                    draw.text((ox, y), op[2], font=font_meta, fill=_ink)
                elif kind == "star":
                    draw.text((ox, y), "★", font=font_meta, fill=_ink)
                elif kind == "rpip":
                    draw_score_bar_vertical(image, score, x=ox, y_center=pip_cy,
                                            height=pip_h, width=pip_w,
                                            color_mode=cfg.score_color_mode)
                else:  # "pip"
                    _draw_solid_pip(image, x=ox, y_center=pip_cy,
                                    width=pip_w, height=pip_h, color=(192, 192, 200))

        elif cfg.rating_display_mode == 4:
            # Frosted bar — centred dot-separated label at the bottom.
            # Format: Year · Genre · ★ Rating  (omit any missing field)
            _has_score = score not in ("N/A", None)
            if _has_score:
                if cfg.bar_score_out_of_10:
                    _score_str = "10" if int(score) >= 100 else f"{int(score) / 10:.1f}"
                else:
                    _score_str = str(score)
            else:
                _score_str = ""
            _year_str  = str(release_year) if release_year else ""
            _bar_sash, _ = sash_result if sash_result else (None, None)
            if cfg.bar_append == "rating_year":
                _parts = [_year_str, genre_label or "", f"★ {_score_str}" if _score_str else ""]
            elif cfg.bar_append == "rating":
                _parts = [genre_label or "", f"★ {_score_str}" if _score_str else ""]
            elif cfg.bar_append == "year":
                _parts = [_year_str, genre_label or ""]
            else:  # "sash"
                _parts = [genre_label or "", _bar_sash or ""]
            _parts = [p for p in _parts if p]
            _sep = "  ·  " if len(_parts) <= 2 else " · "
            image = draw_frosted_bar(
                image,
                left_text   = "",
                center_text = _sep.join(_parts),
                right_text  = "",
                bar_height_ratio = cfg.bar_height_ratio,
                font_size_ratio  = cfg.bar_font_size_ratio,
                frost_opacity    = cfg.bar_frost_opacity,
                bottom_inset     = cfg.bar_bottom_inset,
                style            = cfg.bar_style,
                score            = score if score not in ("N/A", None) else None,
                fill_color       = (
                    None  # "sample" → let draw_frosted_bar derive from bar tint
                    if cfg.bar_accent == "sample" else
                    {"silver": (210, 210, 218), "gold": (212, 175, 55)}.get(cfg.bar_accent)
                    or (
                        {0: _score_color, 1: _score_color_alt, 2: _score_color_metal}
                        .get(int(cfg.bar_accent[-1]), _score_color)(int(score))[0]
                        if score not in ("N/A", None) else (210, 210, 218)
                    )
                ) if cfg.bar_style in ("rating_black", "rating_frosted") else None,
                tint_rgb         = _shared_tint,
            )

    # --- Discovery sash / badge ---
    if cfg.show_award_sash and sash_result is not None:
        label, sash_type = sash_result
        if cfg.sash_badge:
            image = draw_award_badge(image, label, sash_type=sash_type,
                                     size_ratio_w=cfg.sash_badge_size_w,
                                     size_ratio_h=cfg.sash_badge_size_h,
                                     notch_style=cfg.sash_badge_style,
                                     notch_text_offset=cfg.sash_badge_notch_offset,
                                     notch_inset=cfg.sash_badge_inset,
                                     font_size_ratio=cfg.sash_badge_font_ratio,
                                     frost_opacity=cfg.sash_badge_frost_opacity,
                                     tint_rgb=_shared_tint)
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
    # Warm the genre fallback backgrounds into memory so no-art posters render
    # with zero extra latency (same idea as the badge cache warm-up).
    try:
        _warmed = 0
        for _style in _GENRE_BG_STYLES:
            _sdir = os.path.join(_GENRE_BG_DIR, _style)
            if not os.path.isdir(_sdir):
                continue
            for _fn in os.listdir(_sdir):
                if _fn.lower().endswith(".png"):
                    if _load_genre_background(_fn[:-4], _style) is not None:
                        _warmed += 1
        if _warmed:
            logger.info(f"Genre backgrounds warmed: {_warmed} entries")
        else:
            logger.info("No genre background art found — using gradient fallbacks")
    except Exception as exc:
        logger.warning(f"Genre background warm-up skipped: {exc}")
    # Burned-in-text detection: fetch + load the EAST model in the background so
    # the first low-vote textless request isn't blocked by the one-time ~96 MB
    # download.  On by default; skipped when the operator has opted out.
    if _cfg.TEXTLESS_TEXT_DETECTION:
        async def _warm_east():
            try:
                from text_detect import warm_model
                ok = await asyncio.get_running_loop().run_in_executor(None, warm_model)
                logger.info(f"Burned-in-text detection {'ready' if ok else 'unavailable'}")
            except Exception as exc:
                logger.warning(f"EAST warm-up failed: {exc}")
        asyncio.create_task(_warm_east())

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


# ── Genre fallback backgrounds ────────────────────────────────────────────
# Atmospheric 500x750 PNGs (procedurally generated by genre_backgrounds.py, or
# hand-made overrides dropped into the same folder) used as the base for no-art
# fallback posters instead of the flat gradient.  Cached in memory; a *copy* is
# returned per request because build_poster draws onto the base.
_GENRE_BG_DIR = os.path.join(BASE_DIR, "static", "genre_bg")
# Two interchangeable fallback-background sets, chosen per request via
# fallback_bg_style: "minimal" (procedural textured) or "photoreal" (hand-made
# photographic art that blends with real posters).
_GENRE_BG_STYLES = ("minimal", "photoreal")
_genre_bg_cache: dict[str, "Image.Image | None"] = {}   # keyed "style/genre"


def _genre_bg_path(style: str, name: str) -> "str | None":
    """Filesystem path to a genre-background PNG, or None if it doesn't exist."""
    p = os.path.join(_GENRE_BG_DIR, style, f"{name}.png")
    return p if os.path.exists(p) else None


def _load_genre_background(genre: str, style: str = "minimal") -> "Image.Image | None":
    """Return a fresh RGBA copy of the genre fallback background for *style*, or
    None if none exists.  A missing image degrades gracefully: the style's
    default.png → the minimal set's genre/default → None (caller then renders the
    procedural gradient canvas).  So selecting a not-yet-populated style never
    breaks — it just falls back to minimal."""
    if style not in _GENRE_BG_STYLES:
        style = "minimal"
    key = f"{style}/{genre}"
    if key not in _genre_bg_cache:
        path = (
            _genre_bg_path(style, genre)
            or _genre_bg_path(style, "default")
            or (_genre_bg_path("minimal", genre) if style != "minimal" else None)
            or _genre_bg_path("minimal", "default")
        )
        try:
            _genre_bg_cache[key] = Image.open(path).convert("RGBA") if path else None
        except Exception:
            _genre_bg_cache[key] = None
    base = _genre_bg_cache[key]
    return base.copy() if base is not None else None


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
        "mdblist_key_set":       bool(_cfg.SERVER_MDBLIST_KEYS),
        "mdblist_key_count":     len(_cfg.SERVER_MDBLIST_KEYS),
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


@app.get("/stats")
async def stats(access_key: str = ""):
    """
    Operator diagnostics: cache row counts / sizes plus live runtime state
    (in-flight renders, background quality fetches, MDBList key cooldowns).
    Gated behind the access key when one is configured.
    """
    if _cfg.ACCESS_KEY and not hmac.compare_digest(access_key, _cfg.ACCESS_KEY):
        raise HTTPException(status_code=403, detail="Unauthorized")

    now = asyncio.get_running_loop().time()
    keys = _cfg.SERVER_MDBLIST_KEYS
    mdblist_keys = []
    for i, k in enumerate(keys):
        cd = _mdblist_key_cooldown.get(k, 0.0)
        mdblist_keys.append({
            "index":         i + 1,
            "active":        i == (_mdblist_active_key_idx % len(keys)),
            "cooling_down":  now < cd,
            "cooldown_secs": max(0, round(cd - now)),
        })

    return {
        "cache":   get_cache_stats(),
        "runtime": {
            "renders_in_flight":        len(_render_inflight),
            "quality_fetches_in_flight": len(_quality_bg_inflight),
            "rating_fetches_in_flight":  len(_rating_fetch_inflight),
            "rating_backoff_titles":     len(_rating_backoff),
            "mdblist_keys":              mdblist_keys,
            "composite_cache_disabled":  _cfg.DISABLE_COMPOSITE_CACHE,
            "svg_logo_support":          svg_logo_supported(),
        },
    }


# TMDB genre name → id, used only by the debug canvas preview below.
_DEBUG_GENRE_IDS = {
    "Action": 28, "Adventure": 12, "Animation": 16, "Comedy": 35, "Crime": 80,
    "Documentary": 99, "Drama": 18, "Family": 10751, "Fantasy": 14, "History": 36,
    "Horror": 27, "Music": 10402, "Mystery": 9648, "Romance": 10749,
    "Sci-Fi": 878, "Thriller": 53, "War": 10752, "Western": 37,
}


@app.get("/debug/canvas")
async def debug_canvas(genre: str = "Action", title: str = "Sample Title",
                       style: str = "minimal", year: str = "2024",
                       score: str = "84", access_key: str = ""):
    """
    Render a no-art fallback card exactly as a poster-less title would: the genre
    fallback background (minimal or photoreal set) with the genre-aware title and
    the usual rating label composited on top.  Lets you eyeball any genre/style
    without hunting for a title that happens to lack poster art.
    """
    if _cfg.ACCESS_KEY and not hmac.compare_digest(access_key, _cfg.ACCESS_KEY):
        raise HTTPException(status_code=403, detail="Unauthorized")
    gid = _DEBUG_GENRE_IDS.get(genre)
    canvas = _load_genre_background(genre, style)
    if canvas is None:
        canvas = _make_fallback_canvas([gid] if gid else None).convert("RGBA")
    cfg = RequestConfig()
    _score = int(score) if score.isdigit() else "—"
    img = build_poster(canvas, _score, genre, cfg, fallback_title=title,
                       release_year=(year or None), no_poster=True)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=90)
    return Response(content=buf.getvalue(), media_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})


@app.get("/debug/fallback-gallery", response_class=HTMLResponse)
async def fallback_gallery(style: str = "minimal", access_key: str = ""):
    """
    Self-contained gallery of every genre's no-art fallback card (live
    /debug/canvas renders), so an operator can review the fallback backgrounds +
    genre fonts at a glance and compare the minimal vs photoreal sets.  Gated
    behind the access key when configured.
    """
    if _cfg.ACCESS_KEY and not hmac.compare_digest(access_key, _cfg.ACCESS_KEY):
        raise HTTPException(status_code=403, detail="Unauthorized. Provide ?access_key=<key>")
    if style not in _GENRE_BG_STYLES:
        style = "minimal"
    _ak = f"&access_key={access_key}" if access_key else ""

    # Every genre that has a background (covers the full genre map + any future
    # additions), derived from the minimal set so the gallery is never stale.
    try:
        _genres = sorted(
            f[:-4] for f in os.listdir(os.path.join(_GENRE_BG_DIR, "minimal"))
            if f.lower().endswith(".png") and f[:-4].lower() != "default"
        )
    except OSError:
        _genres = sorted(_DEBUG_GENRE_IDS)

    tiles = "".join(
        f'<figure><img loading="lazy" src="/debug/canvas?genre={g}'
        f'&title={g.replace(" ", "+")}&style={style}{_ak}" alt="{g}">'
        f'<figcaption>{g}</figcaption></figure>'
        for g in _genres
    )
    _tabs = "".join(
        f'<a class="{"on" if s == style else ""}" '
        f'href="/debug/fallback-gallery?style={s}{_ak}">{s.capitalize()}</a>'
        for s in _GENRE_BG_STYLES
    )
    html = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fallback art preview</title>
<style>
  body {{ margin:0; background:#0e0e10; color:#e8e8ea;
         font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif; }}
  header {{ padding:18px 20px; border-bottom:1px solid #2a2a2e;
           display:flex; align-items:center; gap:16px; flex-wrap:wrap; }}
  h1 {{ font-size:18px; margin:0; }} p {{ color:#9a9aa0; margin:0; font-size:13px; }}
  .tabs a {{ display:inline-block; padding:5px 12px; margin-right:6px; border-radius:8px;
            font-size:13px; text-decoration:none; color:#c7c7cc; background:#1c1c20;
            border:1px solid #2a2a2e; }}
  .tabs a.on {{ background:#3a3a44; color:#fff; border-color:#4a4a56; }}
  .grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(170px,1fr));
           gap:16px; padding:20px; }}
  figure {{ margin:0; }}
  img {{ width:100%; border-radius:10px; display:block; background:#1a1a1d; }}
  figcaption {{ text-align:center; padding-top:8px; font-size:13px; color:#c7c7cc; }}
</style></head><body>
<header>
  <h1>Fallback art preview</h1>
  <div class="tabs">{_tabs}</div>
  <p>Live no-art render for every genre — {len(_genres)} genres, "{style}" set.</p>
</header>
<div class="grid">{tiles}</div></body></html>"""
    return HTMLResponse(content=html)


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
    nocache: str | None = None,
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
            "quality", "season", "episode", "access_key", "debug", "nocache",
        )
    }
    rcfg = build_request_config(raw_params)

    # Operator force-refresh: ?nocache=1 skips the composite cache READ so a fresh
    # render is produced (and re-cached), letting an operator invalidate a single
    # title without flushing the whole cache.  Only honoured when an ACCESS_KEY is
    # configured (and therefore already validated above) so open instances can't
    # be made to burn CPU on forced re-renders.
    _force_refresh = bool(
        nocache and nocache.strip().lower() in ("1", "true", "yes") and _cfg.ACCESS_KEY
    )

    # ------------------------------------------------------------------
    # Final poster cache — keyed on imdb_id, type, and a short hash of
    # all rendering parameters so different visual configs don't collide.
    # Skipped when an explicit quality= override is supplied (one-off).
    # ------------------------------------------------------------------
    if not quality and not _cfg.DISABLE_COMPOSITE_CACHE:
        # Server-side detection settings affect the rendered output but aren't URL
        # params, so fold a signature into the hash.  Toggling detection or
        # changing its thresholds then auto-busts stale composites (and leaves
        # cache keys unchanged when the feature is off — backward compatible).
        if _cfg.TEXTLESS_TEXT_DETECTION:
            from text_detect import DETECT_RES_SIG
            _detect_sig = (
                f"|td={_cfg.TEXTLESS_MIN_BOXES}:{_cfg.TEXTLESS_DETECTION_MAX_VOTES}:{DETECT_RES_SIG}"
            )
        else:
            _detect_sig = ""
        _params_hash = hashlib.md5(
            ("&".join(f"{k}={v}" for k, v in sorted(raw_params.items())) + _detect_sig).encode()
        ).hexdigest()[:8]
        final_cache_key = f"{imdb_id}:{type}:{_params_hash}"
        cached_jpeg = None if _force_refresh else get_cached_final_poster(final_cache_key)
        if _force_refresh:
            logger.info(f"Force refresh (nocache) for {final_cache_key} — bypassing cache read")
        if cached_jpeg is not None:
            logger.info(f"Final poster cache hit for {final_cache_key}")
            etag = f'"{final_cache_key}"'
            if request.headers.get("if-none-match") == etag:
                return Response(status_code=304)
            _hit_resp = Response(content=cached_jpeg, media_type="image/jpeg")
            _hit_resp.headers["ETag"] = etag
            # This path is only reached when composite caching is enabled, so a
            # no-store branch would be dead here — CDN TTL is the only option.
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
                # Coalescing only happens when caching is on (final_cache_key set),
                # so no-store can't apply here — CDN TTL only.
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
    global _mdblist_active_key_idx

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

        # Per-key cooldown: if the current active key is still cooling down, try to
        # rotate to the next available key before giving up entirely.
        if effective_mdblist_key and _loop_now < _mdblist_key_cooldown.get(effective_mdblist_key, 0.0):
            _server_keys = _cfg.SERVER_MDBLIST_KEYS
            _rotated = False
            for _i in range(1, len(_server_keys)):
                _candidate = _server_keys[(_mdblist_active_key_idx + _i) % len(_server_keys)]
                if _loop_now >= _mdblist_key_cooldown.get(_candidate, 0.0):
                    _mdblist_active_key_idx = (_mdblist_active_key_idx + _i) % len(_server_keys)
                    effective_mdblist_key = _candidate
                    logger.info(f"MDBlist key rotated to key #{_mdblist_active_key_idx + 1} for {imdb_id}")
                    _rotated = True
                    break
            if not _rotated:
                _remaining = _mdblist_key_cooldown.get(effective_mdblist_key, 0.0) - _loop_now
                logger.debug(
                    f"Rating fetch for {imdb_id} skipped "
                    f"(all MDBlist keys cooling down; {_remaining:.0f}s remaining on active key)"
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

        # Original-art mode: serve a TMDB poster (title baked into the art) as-is.
        # Override the textless/backdrop selection, force is_textless=False so the
        # existing gates skip our logo, text detection and the backdrop rescue.
        # Poster language reuses logo_priority (there's no text fallback here):
        #   original_native → content's original-language poster first
        #   native_original / native_text → viewer-language poster first
        # "native" is the REQUEST's logo_language (selected from poster_langs at
        # render time, so it isn't baked to whatever language first cached this
        # title).  Both fall back to the primary poster; off if none exist.
        _plangs    = tmdb_data.get("poster_langs") or {}
        _p_native  = _plangs.get(rcfg.logo_language)
        _p_orig    = _plangs.get(tmdb_data.get("original_language"))
        _p_default = tmdb_data.get("original_poster_path")
        # Determine the priority-first language so we know what "output language"
        # the user is targeting.
        _original_lang = tmdb_data.get("original_language") or ""
        if rcfg.logo_priority == "original_native":
            _priority_lang = _original_lang
        else:  # native_original / native_text
            _priority_lang = rcfg.logo_language or ""
        # art_source only matters when the priority-first language is English —
        # the two TMDB English poster candidates (editorial primary vs
        # community top-rated) can differ meaningfully.  For non-English
        # priority languages TMDB has no separate "primary" concept so we
        # always use the vote-ranked poster regardless of art_source.
        _use_primary = (
            _priority_lang == "en"
            and rcfg.original_art_source == "primary"
        )
        if rcfg.logo_priority == "original_native":
            if _use_primary:
                _orig_art = _p_default or _p_orig or _p_native
            else:
                _orig_art = _p_orig or _p_native or _p_default
        else:  # native_original / native_text
            if _use_primary:
                _orig_art = _p_default or _p_native or _p_orig
            else:
                _orig_art = _p_native or _p_orig or _p_default
        _use_original_art = rcfg.use_original_art and bool(_orig_art)
        if _use_original_art:
            poster_path   = _orig_art
            is_textless   = False
            _use_backdrop = False
            logger.info(f"Original-art mode for {tmdb_id} — poster {poster_path} "
                        f"(priority={rcfg.logo_priority})")

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

        # Quality is normally fetched in the background (not in this gather).
        # The one exception — wait_for_quality — is handled inline after the
        # gather completes so it never blocks rating coalescing.
        _backdrop_rescued = False
        is_no_poster = poster_path is None and not _use_backdrop
        if _use_backdrop:
            # Option B: even null-language backdrops sometimes carry residual
            # text — bias the crop away from it when detection is enabled.
            _image_coro = fetch_backdrop_image(
                client, tmdb_id, backdrop_path, avoid_text=_cfg.TEXTLESS_TEXT_DETECTION)
        elif is_no_poster:
            # Prefer the atmospheric genre background (minimal or photoreal set,
            # per the request); fall back to the flat genre-tinted gradient if no
            # background art exists for this genre in either set.
            _bg = _load_genre_background(_tmdb_genre, rcfg.fallback_bg_style)
            _image_coro = _resolved(_bg if _bg is not None else _make_fallback_canvas(genre_ids))
        else:
            # Option A: the title has only text-bearing art (no textless poster
            # or backdrop).  Before settling for the busy official poster, try a
            # text-aware crop of a text-bearing backdrop; if it comes out clean
            # we get a nicer image plus our own logo.  Gated to low-vote titles.
            _rescued = None
            _tbp = tmdb_data.get("text_backdrop_path")
            if (_cfg.TEXTLESS_TEXT_DETECTION and not is_textless and _tbp
                    and not _use_original_art
                    and _detection_vote_ok(tmdb_data.get("vote_count"))):
                try:
                    _cand = await fetch_backdrop_image(client, tmdb_id, _tbp, avoid_text=True)
                    # Memoise per (candidate backdrop, min_boxes, scan resolution)
                    # — same rationale as the suppress path: config-independent.
                    from text_detect import DETECT_RES_SIG
                    _resc_key = f"bd:{_tbp}:{_CROP_VERSION}|mb={_cfg.TEXTLESS_MIN_BOXES}:{DETECT_RES_SIG}"
                    _still_text = get_cached_text_detection(_resc_key)
                    if _still_text is None:
                        from text_detect import poster_has_burned_in_text
                        async with _get_detect_semaphore():
                            _still_text = await asyncio.get_running_loop().run_in_executor(
                                None,
                                lambda: poster_has_burned_in_text(_cand, min_boxes=_cfg.TEXTLESS_MIN_BOXES))
                        set_cached_text_detection(_resc_key, _still_text)
                    if not _still_text:
                        _rescued = _cand
                        logger.info(f"Text-aware backdrop crop clean for {tmdb_id} — using it with logo")
                    else:
                        logger.info(f"Text-aware backdrop crop still has text for {tmdb_id} — keeping official poster")
                except Exception as exc:
                    logger.warning(f"Backdrop rescue failed for {tmdb_id}: {exc}")
            if _rescued is not None:
                is_textless = True            # we now have textless art → composite logo
                _backdrop_rescued = True
                _image_coro = _resolved(_rescued)
            else:
                _image_coro = fetch_poster_image(client, tmdb_id, type, poster_path)

        (
            image,
            logo,
            rating_result,
            trending_rank,
        ) = await asyncio.gather(
            _image_coro,
            fetch_logo(client, logos, rcfg.logo_language, imdb_id=imdb_id, original_language=tmdb_data.get("original_language"), logo_priority=rcfg.logo_priority) if (is_textless and not is_no_poster) else _resolved(None),
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

                # Mark this specific key as cooling down, then rotate to the next
                # available key so subsequent requests can proceed immediately.
                _now2 = asyncio.get_running_loop().time()
                _mdblist_key_cooldown[effective_mdblist_key] = _now2 + backoff_secs
                _server_keys = _cfg.SERVER_MDBLIST_KEYS
                if len(_server_keys) > 1:
                    for _i in range(1, len(_server_keys)):
                        _candidate = _server_keys[(_mdblist_active_key_idx + _i) % len(_server_keys)]
                        if _now2 >= _mdblist_key_cooldown.get(_candidate, 0.0):
                            _mdblist_active_key_idx = (_mdblist_active_key_idx + _i) % len(_server_keys)
                            logger.warning(
                                f"MDBlist key #{(_mdblist_active_key_idx - _i) % len(_server_keys) + 1} rate-limited "
                                f"— rotated to key #{_mdblist_active_key_idx + 1}"
                            )
                            break
                    else:
                        logger.warning(
                            f"MDBlist rate-limited — all {len(_server_keys)} keys cooling down "
                            f"for up to {backoff_secs:.0f}s"
                        )
                else:
                    logger.warning(
                        f"MDBlist rate-limited — single key cooling down for {backoff_secs:.0f}s "
                        f"(add MDBLIST_API_KEY_2 for automatic rotation)"
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

        # ------------------------------------------------------------------
        # Experimental burned-in-text detection (opt-in).  When a poster TMDB
        # tagged "textless" actually has the title burned in, compositing our
        # own logo/title would double it — so detect that and skip our overlay.
        # Gated to low-vote titles (where mislabels concentrate); popular titles
        # are trusted to avoid false positives.  Runs in the thread pool.
        # ------------------------------------------------------------------
        _suppress_overlay = False
        if (_cfg.TEXTLESS_TEXT_DETECTION and is_textless and not is_no_poster
                and not _backdrop_rescued):
            _vc = tmdb_data.get("vote_count")
            if _detection_vote_ok(_vc):
                # The scanned image is the backdrop crop (when we fell back to it)
                # or the TMDB poster — both identified by an immutable image path.
                # Memoise the result per (asset, min_boxes): the EAST scan depends
                # only on the image + threshold, never on the URL config, so this
                # stops a ~200ms re-scan on every config change (composite miss).
                # Backdrop crops depend on the crop-logic version, so key them
                # by it; TMDB poster paths are content-addressed (immutable).
                from text_detect import DETECT_RES_SIG
                _det_src = (f"bd:{backdrop_path}:{_CROP_VERSION}" if _use_backdrop
                            else f"ps:{poster_path}")
                _det_key = f"{_det_src}|mb={_cfg.TEXTLESS_MIN_BOXES}:{DETECT_RES_SIG}"
                _suppress_overlay = get_cached_text_detection(_det_key)
                if _suppress_overlay is None:
                    from text_detect import poster_has_burned_in_text
                    async with _get_detect_semaphore():
                        _suppress_overlay = await asyncio.get_running_loop().run_in_executor(
                            None,
                            lambda: poster_has_burned_in_text(image, min_boxes=_cfg.TEXTLESS_MIN_BOXES),
                        )
                    set_cached_text_detection(_det_key, _suppress_overlay)
                if _suppress_overlay:
                    logger.info(
                        f"Burned-in text detected on 'textless' poster {tmdb_id} "
                        f"(votes={_vc}) — skipping logo/title overlay"
                    )
            elif _vc is None:
                logger.debug(
                    f"Text detection skipped for {tmdb_id}: vote_count unknown "
                    f"(stale metadata cache — refreshes on next TTL miss)"
                )

        # Offload CPU-bound PIL compositing + JPEG encoding to the thread pool
        # so the event loop stays free for concurrent requests.
        _bp_args = dict(
            logo=logo if (is_textless and not is_no_poster and not rcfg.textless
                          and not _suppress_overlay) else None,
            fallback_title=(
                title if is_no_poster
                else (title if is_textless and not logo and not rcfg.textless
                      and not _suppress_overlay else None)
            ),
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
        if _cfg.DISABLE_COMPOSITE_CACHE:
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
            response.headers["Pragma"] = "no-cache"
        elif _cfg.CDN_CACHE_TTL > 0:
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
            # Invalidate the (per-language) metadata cache so the next request
            # re-fetches fresh data.
            _endpoint = "tv" if type in ("tv", "series") else "movie"
            delete_cached_tmdb_metadata(f"{_endpoint}_{tmdb_id}_{rcfg.logo_language}")
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