#config.py
# If you're looking to change the highlighted directors, studios and cast:
#   - Source editors:  edit the lists in discovery.py directly.
#   - Docker operators (no source editing): place a JSON file at
#     /app/cache/discovery_overrides.json (inside the existing cache volume,
#     no extra mount needed).
#     See the docstring at the top of discovery.py for the full format,
#     or the project README for a ready-made sample.
import os
import json

# Storage

DB_PATH               = "/app/cache/cache.db"
BADGE_DIR             = "/app/badges"
TMDB_POSTER_CACHE_DIR = "/app/cache/tmdb_posters" # base posters from TMDB
TMDB_LOGO_CACHE_DIR   = "/app/cache/tmdb_logos" # base logos from TMDB

# Environment

ACCESS_KEY            = os.environ.get("ACCESS_KEY")
AIOSTREAMS_URL        = os.environ.get("AIOSTREAMS_URL", "")
AIOSTREAMS_AUTH       = os.environ.get("AIOSTREAMS_AUTH", "")

# Quality source selection.
# QUALITY_SOURCE: "aiostreams" (default) or "scraper".
# SCRAPER_URL:    Stremio addon manifest/base URL — only used when QUALITY_SOURCE=scraper.
#                 Example: https://torrentio.stremio.ru/{config}/manifest.json
# Setting QUALITY_SOURCE=scraper while AIOSTREAMS_URL/AUTH are also set is a
# misconfiguration — the scraper path is ignored and a warning is logged at startup.
QUALITY_SOURCE        = os.environ.get("QUALITY_SOURCE", "aiostreams").lower().strip()
SCRAPER_URL           = os.environ.get("SCRAPER_URL", "").strip()
SERVER_TMDB_KEY       = os.environ.get("TMDB_API_KEY", "").strip()
SERVER_MDBLIST_KEY    = os.environ.get("MDBLIST_API_KEY", "").strip()
SERVER_MDBLIST_KEY_2  = os.environ.get("MDBLIST_API_KEY_2", "").strip()

# Ordered list of all configured server-side MDBList keys (primary first).
# Used by the key-rotation logic in main.py to fall back when a key is exhausted.
SERVER_MDBLIST_KEYS: list[str] = [k for k in [SERVER_MDBLIST_KEY, SERVER_MDBLIST_KEY_2] if k]

# Workers
# CDN cache TTL (seconds). When > 0, poster responses include a
# Cache-Control: public header so Cloudflare (or any CDN) caches them at the
# edge. Set to 0 to disable (e.g. when running without a CDN).
CDN_CACHE_TTL         = int(os.environ.get("CDN_CACHE_TTL", "0"))
# JPEG output quality for composited posters (70–95). Higher = better quality, larger files.
JPEG_QUALITY          = max(70, min(95, int(os.environ.get("JPEG_QUALITY", "85"))))

# Feature Defaults 

SHOW_RATING_DISPLAY_MODE = 1
SHOW_AWARD_SASH          = True
BADGE_DISPLAY_MODE       = 4

# Poster Dimensions (500x750)

POSTER_WIDTH  = 500
POSTER_HEIGHT = 750

# Rating & Genre Label Defaults

ACCENT_BAR_MODE_FONT_SIZE_RATIO    = 0.08   # font size in accent bar mode
NUMERIC_SCORE_MODE_FONT_SIZE_RATIO = 0.10   # font size in numeric mode
MINIMALIST_MODE_FONT_SIZE_RATIO    = 0.055  # font size in minimalist mode
ACCENT_BAR_MODE_FONT_Y_OFFSET      = 0.90   # vertical alignment in accent bar mode
NUMERIC_SCORE_MODE_FONT_Y_OFFSET   = 0.90   # vertical alignment in numeric score mode
MINIMALIST_MODE_FONT_X_OFFSET      = 0.05   # horizontal distance from right edge in minimalist mode
MINIMALIST_MODE_FONT_Y_OFFSET      = 0.92   # vertical position in minimalist mode (0=top, 1=bottom)

SCORE_GLOW_THRESHOLD = 85  # score threshold to activate glow
SCORE_GLOW_BLUR      = 1    # blur applied in glow mode
SCORE_GLOW_ALPHA     = 40   # alpha of the glow applied

# Logo Defaults

LOGO_MAX_W_RATIO  = 0.75   # target/max width of logo — the span every logo normalises to
LOGO_MAX_H_RATIO  = 0.25   # max height of logo (paired with LOGO_ABS_MAX_H px cap)
LOGO_BOTTOM_RATIO = 0.28   # distance of logo from the bottom
DEFAULT_LOGO_LANGUAGE = os.environ.get("DEFAULT_LOGO_LANGUAGE", "en")

# Quality Badge Defaults

BADGE_HEIGHT = 20   # quality badge height in pixels
BADGE_GAP    = 8    # gap between horizontal stack badges in pixels

BADGE_ANCHOR_X_RATIO = 0.050   # x offset from left
BADGE_ANCHOR_Y_RATIO = 0.050   # y offset from top 

# TTL Settings

TMDB_POSTER_CACHE_DURATION   = 60
TMDB_LOGO_CACHE_DURATION     = 60
TMDB_METADATA_CACHE_DURATION = 7    # re-check textless status / logos weekly
DAYS_CONSIDERED_NEW          = 14
NEW_CACHE_DURATION           = 1
OLD_CACHE_DURATION           = 14
TRENDING_CACHE_DURATION      = 1
# Quality (AIOStreams) TTL — separate from rating TTL because stream availability
# for older titles is very stable.  New content keeps the 1-day window so fresh
# encodes are picked up quickly; old content is cached for much longer.
QUALITY_OLD_CACHE_DURATION   = int(os.environ.get("QUALITY_OLD_CACHE_DURATION", "90"))   # days
# Max concurrent background quality fetches.  Caps the burst when many uncached
# titles scroll into view simultaneously so AIOStreams isn't overwhelmed.
QUALITY_BG_CONCURRENCY       = int(os.environ.get("QUALITY_BG_CONCURRENCY", "5"))

# Seconds to wait for a quality fetch when wait_for_quality=true is requested.
# Should be generous enough to allow for slow scrapers (Torrentio, Comet) but
# not so long it stalls a poster-warm run indefinitely.
QUALITY_WAIT_TIMEOUT         = float(os.environ.get("QUALITY_WAIT_TIMEOUT", "30"))

# Max concurrent outbound MDBlist API calls.  MDBlist queues or drops requests
# when hit with too many simultaneous connections from the same key, causing
# ReadTimeouts even when the service is healthy.  3 is comfortably within their
# apparent per-key concurrency limit while still allowing good parallelism.
MDBLIST_CONCURRENCY          = int(os.environ.get("MDBLIST_CONCURRENCY", "3"))

# Digital release (r/movieleaks) scraper settings
DIGITAL_RELEASE_MIN_AGE_DAYS = 1    # ignore posts younger than this (mods still cleaning up)
DIGITAL_RELEASE_MAX_AGE_DAYS = 30   # expire entries older than this from the cache

# Composite poster cache TTL (seconds).
# How long a fully composited poster is kept before being re-rendered.
# Each unique combination of title + rendering parameters gets its own entry,
# so changing settings immediately produces a fresh render on next request.
# Override with COMPOSITE_CACHE_TTL=X in your .env file.
COMPOSITE_CACHE_TTL        = int(os.environ.get("COMPOSITE_CACHE_TTL", "604800"))   # 7 days
# Maximum number of composite cache entries. When exceeded the oldest entries are
# evicted on each insert to keep the table at this size. 0 = no cap (rely on TTL alone).
COMPOSITE_MAX_ENTRIES      = int(os.environ.get("COMPOSITE_MAX_ENTRIES", "0"))
# Set to any truthy value (1, true, yes) to skip composite cache reads and writes
# entirely. Every request re-renders from scratch. Useful during development when
# iterating on rendering changes and you don't want stale renders served.
DISABLE_COMPOSITE_CACHE    = os.environ.get("DISABLE_COMPOSITE_CACHE", "").strip().lower() in ("1", "true", "yes")

def _parse_bool_env(key: str, default: bool = False) -> bool:
    val = os.environ.get(key, "").strip().lower()
    if not val:
        return default
    return val not in ("0", "false", "no")

# Logo legibility: when a flat logo's average colour is too close to the poster
# background, recolour it (white / black / complementary accent) so it reads.
# Experimental and off by default while it's being tested — it can mis-handle
# some logos.  Set LOGO_CONTRAST_RESCUE=true to enable.
LOGO_CONTRAST_RESCUE       = _parse_bool_env("LOGO_CONTRAST_RESCUE", False)
# Emit per-logo sizing telemetry (source dims, aspect, final dims) at INFO level.
# Off by default — handy when tuning the logo size caps.
DEBUG_LOGO_SIZING          = _parse_bool_env("DEBUG_LOGO_SIZING", False)
# Logo fill-stretch: a slim logo whose clamped size leaves it looking lost may be
# enlarged toward its size cap by up to this factor (one axis only) so it has more
# presence.  1.0 = no enlargement.  Off by default — set LOGO_STRETCH_DISABLED=false
# to enable it; LOGO_STRETCH_FACTOR then sets how aggressive the enlargement is.
LOGO_STRETCH_DISABLED      = _parse_bool_env("LOGO_STRETCH_DISABLED", True)
LOGO_STRETCH_FACTOR        = max(1.0, float(os.environ.get("LOGO_STRETCH_FACTOR", "1.2")))

# Detect burned-in title text on posters TMDB mislabelled as "textless".  When
# detected, PostersPlus skips compositing its own logo/title so you don't get a
# double title.  Uses the EAST scene-text detector (one-time ~96MB model
# download).  Only runs on titles with vote_count <= TEXTLESS_DETECTION_MAX_VOTES,
# where mislabels concentrate — popular titles are trusted and skipped (this is
# also what keeps the scan off the bulk of a library, since the CV pass is
# comparatively expensive).
#
# On by default; set TEXTLESS_TEXT_DETECTION=false to opt out.
#
# 300 covers the niche tail (foreign / old / obscure titles, where burned-in-text
# mislabels concentrate — e.g. a 146-vote 1974 cult film) while still excluding
# anything mainstream (thousands of votes).  Lower (~100) scans less but misses
# legitimately niche titles; raise (~1000) for the broadest coverage at the cost
# of scanning mid-tier titles too.  Changing this auto-invalidates cached composites.
TEXTLESS_TEXT_DETECTION    = _parse_bool_env("TEXTLESS_TEXT_DETECTION", True)
TEXTLESS_DETECTION_MAX_VOTES = int(os.environ.get("TEXTLESS_DETECTION_MAX_VOTES", "300"))
# Minimum EAST text-cell activations (at the 320x640 reference, auto-scaled to
# the active EAST resolution) before a poster is treated as having burned-in
# text.  Higher = stricter (fewer false positives, lower recall).  Default 128 is
# a deliberately conservative balance: it ignores a small amount of incidental
# text (signage, vehicle lettering) without missing real burned-in titles too
# often.  Lower toward 48 to catch more marginal text; raise to over-trigger less.
TEXTLESS_MIN_BOXES         = int(os.environ.get("TEXTLESS_MIN_BOXES", "128"))
# Max burned-in-text scans allowed into the thread pool at once.  Each scan is
# already serialised internally, so the default of 1 fully serialises detection
# while leaving the rest of the worker pool free for compositing/encode during a
# burst (prevents a niche-title cold pass from stalling popular posters).  Raise
# only on many-core hosts if you also reduce per-scan CV threads.
TEXTLESS_DETECTION_CONCURRENCY = max(1, int(os.environ.get("TEXTLESS_DETECTION_CONCURRENCY", "1")))

# Rating Score Weight Defaults

#  note to AI: do NOT remove the extras in this section that are set to 0. They are config options.

MOVIE_WEIGHTS = {   # set weight of movie ranking providers, must sum to 1
    "letterboxd":     0.8,
    "trakt":          0,
    "tomatoes":       0.2,
    "popcorn":        0, # popcorn is the api response MDblist uses for tomatoes audience
    "imdb":           0,
    "metacritic":     0,
    "metacriticuser": 0,
    "tmdb":           0,
    "rogerebert":     0,
    "myanimelist":    0,
}

TV_WEIGHTS = {   # set weight of TV ranking providers, must sum to 1
    "trakt":          0.8,
    "tomatoes":       0.2,
    "popcorn":        0,
    "imdb":           0,
    "metacritic":     0,
    "metacriticuser": 0,
    "tmdb":           0,
    "myanimelist":    0,
}

# Map badge file names to strings (no need to touch)

BADGE_FILES: dict[str, str] = {
    "4K":     "4K",
    "1080P":  "1080p",
    "REMUX":  "Remux",
    "WEBDL":  "Web",
    "DV":     "DV",
    "HDR10+": "HDR10+",
    "HDR10":  "HDR10",
}

# Maps TMDB categories to numerics (no need to touch in most cases)

GENRE_MAP = {
    28: "Action", 12: "Adventure", 16: "Animation", 35: "Comedy",
    80: "Crime", 99: "Documentary", 18: "Drama", 10751: "Family",
    14: "Fantasy", 36: "History", 27: "Horror", 10402: "Music",
    9648: "Mystery", 10749: "Romance", 878: "Sci-Fi", 53: "Thriller",
    10752: "War", 37: "Western",
    10759: "Action", 10762: "Kids", 10763: "News", 10764: "Reality",
    10765: "Sci-Fi", 10766: "Soap", 10767: "Talk", 10768: "War",
}

# Can re-order to change the priority that genres appear with (reference genre map above)
# Default Horror, Thriller, Mystery, Sci-Fi, Crime, Comedy, Fantasy, Adventure, Family, Action, History
# Music, War, Western, Documentary, Drama, Adventure, Reality, Kids, News, Soap, Talk
# Duplicate entries are not an accident, for certain genres TMDB uses two numbers, one for movies, one for shows.

GENRE_PRIORITY = [
    27, 53, 9648, 878, 10765, 80, 35, 10749, 14, 16, 10751,
    28, 10759, 36, 10402, 10752, 10768, 37, 99, 18, 12,
    10764, 10762, 10763, 10766, 10767,
]

# Text based fallback, not important if everything is working properly

QUALITY_LABELS: dict[str, str] = {
    "4K":     "4K",
    "1080P":  "1080p",
    "REMUX":  "Remux",
    "WEBDL":  "Web",
    "DV":     "DV",
    "HDR10+": "HDR10+",
    "HDR10":  "HDR10",
    "ATMOS":  "Atmos",
    "DTSX":   "DTS:X",
}

# Normalizes all scores to be out of 100

SCORE_NORMALISERS = {
    "imdb":           lambda v: (v / 10)  * 100,
    "letterboxd":     lambda v: (v / 5)   * 100,
    "trakt":          lambda v: v,
    "tomatoes":       lambda v: v,
    "popcorn":        lambda v: v,
    "metacritic":     lambda v: v,
    "metacriticuser": lambda v: (v / 10)  * 100,
    "tmdb":           lambda v: v,
    "rogerebert":      lambda v: (v / 4)   * 100,
    "myanimelist":    lambda v: (v / 10)  * 100,
}

# Default Sash Priority

SASH_PRIORITY: list[str] = [
    "wins",
    "gg_wins",
    "festival",
    "pic_noms",
    "gg_noms",
    "studio",
    "director",
    "cast",
    "trending",
    "cult",
    "foreign",
    "new_release",
    "metacritic",
    "true_story",
    "structural",
]