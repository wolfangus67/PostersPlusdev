#cache.py
import logging
import os
import sqlite3
import threading
import time
import json
from datetime import datetime

logger = logging.getLogger(__name__)

from config import (
    DB_PATH,
    DAYS_CONSIDERED_NEW,
    NEW_CACHE_DURATION,
    OLD_CACHE_DURATION,
    TRENDING_CACHE_DURATION,
    TMDB_POSTER_CACHE_DIR,
    TMDB_POSTER_CACHE_DURATION,
    TMDB_LOGO_CACHE_DIR,
    TMDB_LOGO_CACHE_DURATION,
    TMDB_METADATA_CACHE_DURATION,
    COMPOSITE_CACHE_TTL,
    COMPOSITE_MAX_ENTRIES,
    QUALITY_OLD_CACHE_DURATION,
    DIGITAL_RELEASE_MAX_AGE_DAYS,
)

_db_conn: sqlite3.Connection | None = None
_db_lock = threading.Lock()   # used only for writes; WAL allows concurrent reads

def get_db() -> sqlite3.Connection:
    if _db_conn is None:
        raise RuntimeError("Database not initialized")
    return _db_conn

def init_db() -> None:
    global _db_conn
    os.makedirs(TMDB_POSTER_CACHE_DIR, exist_ok=True)
    os.makedirs(TMDB_LOGO_CACHE_DIR, exist_ok=True)
    _db_conn = sqlite3.connect(DB_PATH, check_same_thread=False)

    # Enable incremental auto-vacuum so prune_caches' PRAGMA incremental_vacuum
    # can actually return freed pages to the OS.  auto_vacuum can only be set
    # before the first table is created; an existing DB would need a full
    # (blocking) VACUUM to convert, which we deliberately avoid at startup.  So
    # we only enable it on a brand-new database — fresh installs reclaim space,
    # existing installs are unchanged (no regression; it was already a no-op
    # there).  Must run before journal_mode/table creation writes any pages.
    _is_new_db = _db_conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
    ).fetchone()[0] == 0
    if _is_new_db:
        _db_conn.execute("PRAGMA auto_vacuum=INCREMENTAL")

    _db_conn.execute("PRAGMA journal_mode=WAL")
    _db_conn.execute("PRAGMA synchronous=NORMAL")       # safe with WAL; avoids unnecessary fsyncs
    _db_conn.execute("PRAGMA cache_size=-32000")        # 32 MB in-process page cache
    _db_conn.execute("PRAGMA temp_store=MEMORY")        # temp tables/indices stay in RAM
    _db_conn.execute("PRAGMA busy_timeout=5000")        # wait up to 5s if another worker holds the write lock
    _db_conn.execute("PRAGMA wal_autocheckpoint=1000")  # fold WAL back into main DB at 1000 pages (~4 MB)

    _db_conn.execute("""
    CREATE TABLE IF NOT EXISTS rating_cache (
        imdb_id        TEXT PRIMARY KEY,
        ratings_json   TEXT,
        genre          TEXT,
        cached_at      INTEGER,
        release_date   TEXT,
        award_wins     TEXT,
        award_noms     TEXT,
        awards_fetched INTEGER NOT NULL DEFAULT 0,
        festival_label TEXT,
        age_rating     INTEGER,
        is_cult        INTEGER NOT NULL DEFAULT 0,
        is_true_story  INTEGER NOT NULL DEFAULT 0,
        is_metacritic  INTEGER NOT NULL DEFAULT 0
    )
    """)

    existing_cols = {
        row[1]
        for row in _db_conn.execute("PRAGMA table_info(rating_cache)").fetchall()
    }
    for col, definition in (
        ("award_wins",     "TEXT NOT NULL DEFAULT ''"),
        ("award_noms",     "TEXT NOT NULL DEFAULT ''"),
        ("awards_fetched", "INTEGER NOT NULL DEFAULT 0"),
        ("festival_label", "TEXT"),
        ("age_rating",     "INTEGER"),
        ("is_cult",        "INTEGER NOT NULL DEFAULT 0"),
        ("is_true_story",  "INTEGER NOT NULL DEFAULT 0"),
        ("is_metacritic",  "INTEGER NOT NULL DEFAULT 0"),
    ):
        if col not in existing_cols:
            _db_conn.execute(
                f"ALTER TABLE rating_cache ADD COLUMN {col} {definition}"
            )

    _db_conn.execute("""
        CREATE TABLE IF NOT EXISTS quality_cache (
            imdb_id      TEXT PRIMARY KEY,
            tokens       TEXT,
            cached_at    INTEGER,
            release_date TEXT
        )
    """)

    _db_conn.execute("""
        CREATE TABLE IF NOT EXISTS trending_cache (
            media_type    TEXT PRIMARY KEY,
            rankings_json TEXT,
            cached_at     INTEGER
        )
    """)

    _db_conn.execute("""
        CREATE TABLE IF NOT EXISTS tmdb_metadata_cache (
            cache_key           TEXT PRIMARY KEY,
            title               TEXT,
            release_year        TEXT,
            genre_ids           TEXT,
            is_textless         INTEGER,
            poster_path         TEXT,
            logos_json          TEXT,
            cached_at           INTEGER,
            credits_json        TEXT,
            production_cos_json TEXT,
            runtime             INTEGER,
            number_of_seasons   INTEGER,
            number_of_episodes  INTEGER,
            original_language   TEXT,
            backdrop_path       TEXT
        )
    """)

    # Final composite poster cache.
    # Stores the fully composited JPEG so warm requests skip the entire pipeline.
    _db_conn.execute("""
        CREATE TABLE IF NOT EXISTS final_poster_cache (
            cache_key  TEXT PRIMARY KEY,
            jpeg_bytes BLOB    NOT NULL,
            cached_at  INTEGER NOT NULL
        )
    """)

    # Digital release cache.
    # Populated by the r/movieleaks poller; one row per IMDB ID.
    # posted_at is the Reddit post's created_utc (used for expiry).
    _db_conn.execute("""
        CREATE TABLE IF NOT EXISTS digital_release_cache (
            imdb_id   TEXT PRIMARY KEY,
            posted_at INTEGER NOT NULL
        )
    """)

    # Release status cache — populated on demand when the "release_status"
    # sash slot is enabled.  Stored separately from the main metadata cache
    # so users who don't enable the feature never pay the extra API call.
    # cache_key = "{media_type}_{tmdb_id}", status = "BluRay"|"Streaming"|"Cinema"|"Production"
    _db_conn.execute("""
        CREATE TABLE IF NOT EXISTS release_status_cache (
            cache_key TEXT PRIMARY KEY,
            status    TEXT NOT NULL,
            cached_at INTEGER NOT NULL
        )
    """)

    # Burned-in-text detection results, keyed by source asset + detection params.
    # The EAST scan (~200ms) depends only on the image bytes and min_boxes, never
    # on the user's URL config — so memoising it here stops the most expensive
    # feature from re-running on every config change (composite-cache miss).
    # TMDB image paths are content-addressed (immutable), so results never go
    # stale; cached_at exists only for housekeeping/pruning.
    _db_conn.execute("""
        CREATE TABLE IF NOT EXISTS text_detection_cache (
            cache_key TEXT PRIMARY KEY,
            has_text  INTEGER NOT NULL,
            cached_at INTEGER NOT NULL
        )
    """)

    # Migrate existing tmdb_metadata_cache rows
    existing_meta_cols = {
        row[1]
        for row in _db_conn.execute("PRAGMA table_info(tmdb_metadata_cache)").fetchall()
    }
    for col, definition in (
        ("credits_json",        "TEXT"),
        ("production_cos_json", "TEXT"),
        ("runtime",             "INTEGER"),
        ("number_of_seasons",   "INTEGER"),
        ("number_of_episodes",  "INTEGER"),
        ("original_language",   "TEXT"),
        ("backdrop_path",       "TEXT"),
        ("tmdb_status",         "TEXT"),
        ("vote_count",          "INTEGER"),
        ("text_backdrop_path",  "TEXT"),
    ):
        if col not in existing_meta_cols:
            _db_conn.execute(
                f"ALTER TABLE tmdb_metadata_cache ADD COLUMN {col} {definition}"
            )

    _db_conn.commit()


# ---------------------------------------------------------------------------
# TTL helper
# ---------------------------------------------------------------------------

def _rating_ttl(release_date: str | None) -> int:
    if not release_date:
        return OLD_CACHE_DURATION
    try:
        days_since = (datetime.now() - datetime.strptime(release_date, "%Y-%m-%d")).days
        return NEW_CACHE_DURATION if days_since <= DAYS_CONSIDERED_NEW else OLD_CACHE_DURATION
    except ValueError:
        return OLD_CACHE_DURATION


def _quality_ttl(release_date: str | None) -> int:
    """Quality data is far more stable than ratings for older titles."""
    if not release_date:
        return QUALITY_OLD_CACHE_DURATION
    try:
        days_since = (datetime.now() - datetime.strptime(release_date, "%Y-%m-%d")).days
        return NEW_CACHE_DURATION if days_since <= DAYS_CONSIDERED_NEW else QUALITY_OLD_CACHE_DURATION
    except ValueError:
        return QUALITY_OLD_CACHE_DURATION


# ---------------------------------------------------------------------------
# Final poster cache
# ---------------------------------------------------------------------------

def get_cached_final_poster(cache_key: str) -> bytes | None:
    """Return cached JPEG bytes for a fully composited poster, or None on miss/expiry."""
    try:
        row = get_db().execute(
            "SELECT jpeg_bytes, cached_at FROM final_poster_cache WHERE cache_key = ?",
            (cache_key,),
        ).fetchone()
        if not row:
            return None
        jpeg_bytes, cached_at = row
        age_secs = time.time() - cached_at
        if age_secs > COMPOSITE_CACHE_TTL:
            logger.info(f"Final poster cache expired for {cache_key} ({age_secs/86400:.1f}d old)")
            with _db_lock:
                get_db().execute(
                    "DELETE FROM final_poster_cache WHERE cache_key = ?", (cache_key,)
                )
                get_db().commit()
            return None
        return bytes(jpeg_bytes)
    except Exception as exc:
        logger.error(f"Final poster cache read error: {exc}")
        return None


def set_cached_final_poster(cache_key: str, jpeg_bytes: bytes) -> None:
    """Store a fully composited JPEG poster, evicting oldest entries if over the cap."""
    try:
        with _db_lock:
            get_db().execute(
                """
                INSERT OR REPLACE INTO final_poster_cache (cache_key, jpeg_bytes, cached_at)
                VALUES (?, ?, ?)
                """,
                (cache_key, jpeg_bytes, int(time.time())),
            )
            if COMPOSITE_MAX_ENTRIES > 0:
                (count,) = get_db().execute(
                    "SELECT COUNT(*) FROM final_poster_cache"
                ).fetchone()
                overflow = count - COMPOSITE_MAX_ENTRIES
                if overflow > 0:
                    get_db().execute(
                        "DELETE FROM final_poster_cache WHERE cache_key IN "
                        "(SELECT cache_key FROM final_poster_cache "
                        " ORDER BY cached_at ASC LIMIT ?)",
                        (overflow,),
                    )
                    logger.info(f"Composite cache cap: evicted {overflow} oldest entries")
            get_db().commit()
    except Exception as exc:
        logger.error(f"Final poster cache write error: {exc}")


def get_cache_stats() -> dict:
    """
    Return row counts for every cache table plus the composite cache's total
    byte size and the DB file size on disk.  Used by the /stats endpoint so
    operators can see cache health at a glance.  Never raises.
    """
    stats: dict = {}
    try:
        db = get_db()
        for table in (
            "rating_cache", "quality_cache", "trending_cache",
            "tmdb_metadata_cache", "final_poster_cache",
            "digital_release_cache", "release_status_cache",
            "text_detection_cache",
        ):
            try:
                (n,) = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
                stats[table] = n
            except Exception:
                stats[table] = None

        try:
            (total,) = db.execute(
                "SELECT COALESCE(SUM(LENGTH(jpeg_bytes)), 0) FROM final_poster_cache"
            ).fetchone()
            stats["composite_bytes"] = int(total)
        except Exception:
            stats["composite_bytes"] = None

        try:
            stats["db_file_bytes"] = os.path.getsize(DB_PATH)
        except OSError:
            stats["db_file_bytes"] = None
    except Exception as exc:
        logger.error(f"Cache stats error: {exc}")
    return stats


def prune_caches() -> None:
    """
    Delete expired rows from every SQLite cache table.

    Called periodically by a background task in main.py.  All tables use a
    simple age cutoff; the composite table is the only one large enough to
    matter for storage, but pruning everything keeps the DB tidy.

    For rating/quality we use the maximum possible TTL as the cutoff so we
    never delete an entry that might still be considered fresh for a new
    release.  Any surviving-but-expired rows will be evicted lazily on the
    next read as before.
    """
    now = int(time.time())
    try:
        with _db_lock:
            db = get_db()

            # Composites — fixed TTL in seconds
            r = db.execute(
                "DELETE FROM final_poster_cache WHERE cached_at < ?",
                (now - COMPOSITE_CACHE_TTL,),
            )
            if r.rowcount:
                logger.info(f"Pruned {r.rowcount} expired composite cache entries")

            # Ratings / quality / metadata — use the most generous TTL so we
            # never evict something that could still be considered fresh.
            rating_cutoff   = now - OLD_CACHE_DURATION           * 86400
            quality_cutoff  = now - QUALITY_OLD_CACHE_DURATION   * 86400
            metadata_cutoff = now - TMDB_METADATA_CACHE_DURATION * 86400

            r = db.execute(
                "DELETE FROM rating_cache WHERE cached_at < ?", (rating_cutoff,)
            )
            if r.rowcount:
                logger.info(f"Pruned {r.rowcount} expired rating cache entries")

            r = db.execute(
                "DELETE FROM quality_cache WHERE cached_at < ?", (quality_cutoff,)
            )
            if r.rowcount:
                logger.info(f"Pruned {r.rowcount} expired quality cache entries")

            r = db.execute(
                "DELETE FROM tmdb_metadata_cache WHERE cached_at < ?", (metadata_cutoff,)
            )
            if r.rowcount:
                logger.info(f"Pruned {r.rowcount} expired TMDB metadata cache entries")

            digital_cutoff = now - DIGITAL_RELEASE_MAX_AGE_DAYS * 86400
            r = db.execute(
                "DELETE FROM digital_release_cache WHERE posted_at < ?", (digital_cutoff,)
            )
            if r.rowcount:
                logger.info(f"Pruned {r.rowcount} expired digital release cache entries")

            release_status_cutoff = now - _RELEASE_STATUS_TTL_DAYS * 86400
            r = db.execute(
                "DELETE FROM release_status_cache WHERE cached_at < ?", (release_status_cutoff,)
            )
            if r.rowcount:
                logger.info(f"Pruned {r.rowcount} expired release status cache entries")

            db.commit()

        # Reclaim free pages left by the deletes.  INCREMENTAL vacuum moves a
        # few pages per call without locking the DB for long.  No-op on DBs
        # created before auto_vacuum=INCREMENTAL was enabled (see init_db).
        with _db_lock:
            get_db().execute("PRAGMA incremental_vacuum(100)")
            get_db().commit()

    except Exception as exc:
        logger.error(f"Cache prune error: {exc}")


# ---------------------------------------------------------------------------
# Rating cache
# ---------------------------------------------------------------------------

def get_cached_rating(
    imdb_id: str,
) -> tuple[
    dict[str, float], str, str | None,
    list[str], list[str], bool,
    str | None, int | None,
    bool, bool, bool,
] | None:
    """
    Returns an 11-tuple:
        (ratings_dict, genre, release_date, award_wins, award_noms,
         awards_fetched, festival_label, age_rating,
         is_cult, is_true_story, is_metacritic)
    Returns None if the row is absent or expired.
    """
    try:
        row = get_db().execute(
            """
            SELECT ratings_json, genre, cached_at, release_date,
                   award_wins, award_noms, awards_fetched, festival_label,
                   age_rating, is_cult, is_true_story, is_metacritic
            FROM rating_cache
            WHERE imdb_id = ?
            """,
            (imdb_id,),
        ).fetchone()

        if not row:
            return None

        (ratings_json, genre, cached_at, release_date,
         wins_raw, noms_raw, awards_fetched_int, festival_label,
         age_rating, is_cult_int, is_true_story_int, is_metacritic_int) = row

        age_days = (time.time() - cached_at) / 86400

        if age_days > _rating_ttl(release_date):
            logger.info(f"Rating cache expired for {imdb_id} ({age_days:.1f}d old)")
            with _db_lock:
                get_db().execute(
                    "DELETE FROM rating_cache WHERE imdb_id = ?",
                    (imdb_id,),
                )
                get_db().commit()
            return None

        ratings_dict = json.loads(ratings_json or "{}")
        wins = [w for w in (wins_raw or "").split("|") if w]
        noms = [n for n in (noms_raw or "").split("|") if n]
        awards_fetched = bool(awards_fetched_int)

        return (ratings_dict, genre, release_date, wins, noms,
                awards_fetched, festival_label, age_rating,
                bool(is_cult_int), bool(is_true_story_int), bool(is_metacritic_int))

    except Exception as exc:
        logger.error(f"Cache read error: {exc}")
        return None


def set_cached_rating(
    imdb_id: str,
    ratings_dict: dict,
    genre: str,
    rel: str | None,
    award_wins: list[str],
    award_noms: list[str],
    awards_fetched: bool = False,
    festival_label: str | None = None,
    age_rating: int | None = None,
    is_cult: bool = False,
    is_true_story: bool = False,
    is_metacritic: bool = False,
) -> None:
    try:
        with _db_lock:
            get_db().execute(
                """
                INSERT OR REPLACE INTO rating_cache
                    (
                        imdb_id,
                        ratings_json,
                        genre,
                        cached_at,
                        release_date,
                        award_wins,
                        award_noms,
                        awards_fetched,
                        festival_label,
                        age_rating,
                        is_cult,
                        is_true_story,
                        is_metacritic
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    imdb_id,
                    json.dumps(ratings_dict),
                    genre,
                    int(time.time()),
                    rel,
                    "|".join(award_wins or []),
                    "|".join(award_noms or []),
                    int(awards_fetched),
                    festival_label,
                    age_rating,
                    int(is_cult),
                    int(is_true_story),
                    int(is_metacritic),
                ),
            )
            get_db().commit()

    except Exception as exc:
        logger.error(f"Cache write error: {exc}")


# ---------------------------------------------------------------------------
# Quality cache
# ---------------------------------------------------------------------------

def get_cached_quality(imdb_id: str, release_date: str | None = None) -> list[str] | None:
    try:
        row = get_db().execute(
            "SELECT tokens, cached_at, release_date FROM quality_cache WHERE imdb_id = ?",
            (imdb_id,),
        ).fetchone()
        if row is None:
            return None

        tokens_raw, cached_at, stored_release = row
        ttl_release = release_date or stored_release
        age_days    = (time.time() - cached_at) / 86400
        if age_days > _quality_ttl(ttl_release):
            logger.info(f"Quality cache expired for {imdb_id} ({age_days:.1f}d old)")
            with _db_lock:
                get_db().execute("DELETE FROM quality_cache WHERE imdb_id = ?", (imdb_id,))
                get_db().commit()
            return None

        return [t for t in (tokens_raw or "").split("|") if t]

    except Exception as exc:
        logger.error(f"Quality cache read error: {exc}")
        return None


def set_cached_quality(
    imdb_id: str,
    tokens: list[str],
    release_date: str | None = None,
) -> None:
    try:
        with _db_lock:
            get_db().execute(
                """
                INSERT OR REPLACE INTO quality_cache
                    (imdb_id, tokens, cached_at, release_date)
                VALUES (?, ?, ?, ?)
                """,
                (imdb_id, "|".join(tokens), int(time.time()), release_date),
            )
            get_db().commit()
    except Exception as exc:
        logger.error(f"Quality cache write error: {exc}")


# ---------------------------------------------------------------------------
# Trending cache  (snapshot-based — one row per media type)
#
# NOTE: The old per-item get_cached_trending / set_cached_trending helpers
# referenced columns ("rank", "tmdb_id") that never existed in the actual
# schema and always raised OperationalError at runtime.  They are removed.
# All callers use get_cached_trending_snapshot / set_cached_trending_snapshot.
# ---------------------------------------------------------------------------

def get_cached_trending_snapshot(media_type: str) -> dict[str, int] | None:
    try:
        row = get_db().execute(
            """
            SELECT rankings_json, cached_at
            FROM trending_cache
            WHERE media_type = ?
            """,
            (media_type,),
        ).fetchone()

        if not row:
            return None

        rankings_json, cached_at = row
        age_days = (time.time() - cached_at) / 86400

        if age_days > TRENDING_CACHE_DURATION:
            return None

        return json.loads(rankings_json)
    except Exception as exc:
        logger.error(f"Trending snapshot cache read error: {exc}")
        return None


def set_cached_trending_snapshot(
    media_type: str,
    rankings: dict[str, int],
) -> None:
    try:
        with _db_lock:
            get_db().execute(
                """
                INSERT OR REPLACE INTO trending_cache
                (media_type, rankings_json, cached_at)
                VALUES (?, ?, ?)
                """,
                (
                    media_type,
                    json.dumps(rankings),
                    int(time.time()),
                ),
            )
            get_db().commit()
    except Exception as exc:
        logger.error(f"Trending snapshot cache write error: {exc}")


# ---------------------------------------------------------------------------
# TMDB poster cache
# ---------------------------------------------------------------------------

def get_cached_tmdb_poster(cache_key: str) -> bytes | None:
    # Extension is now .jpg — posters are stored as JPEG for faster decode.
    path = _safe_cache_path(TMDB_POSTER_CACHE_DIR, cache_key)

    if not os.path.exists(path):
        return None

    age_days = (time.time() - os.path.getmtime(path)) / 86400

    if age_days > TMDB_POSTER_CACHE_DURATION:
        logger.info(f"TMDB poster cache expired for {cache_key}")
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        return None

    try:
        with open(path, "rb") as f:
            return f.read()
    except Exception as exc:
        logger.error(f"TMDB poster cache read error: {exc}")
        return None


def set_cached_tmdb_poster(cache_key: str, data: bytes) -> None:
    # Store as .jpg — written by tmdb.py as JPEG q=92 RGB, then converted
    # back to RGBA on load.  ~4x faster decode vs PNG, ~5x smaller on disk.
    try:
        path = _safe_cache_path(TMDB_POSTER_CACHE_DIR, cache_key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
    except Exception as exc:
        logger.error(f"TMDB poster cache write error: {exc}")


# ---------------------------------------------------------------------------
# TMDB logo cache
# ---------------------------------------------------------------------------

def _remove_if_dir(path: str) -> bool:
    """Remove *path* if it is a directory (stale artefact from a previous bug).
    Returns True if a directory was found and removed."""
    if os.path.isdir(path):
        try:
            os.rmdir(path)
            logger.info(f"Removed stale cache directory at {path}")
        except OSError:
            pass
        return True
    return False


def get_cached_tmdb_logo(cache_key: str) -> bytes | None:
    path = _safe_cache_path(TMDB_LOGO_CACHE_DIR, cache_key)

    if _remove_if_dir(path):
        return None

    if not os.path.exists(path):
        return None

    age_days = (time.time() - os.path.getmtime(path)) / 86400

    if age_days > TMDB_LOGO_CACHE_DURATION:
        logger.info(f"TMDB logo cache expired for {cache_key}")
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        return None

    try:
        with open(path, "rb") as f:
            return f.read()
    except Exception as exc:
        logger.error(f"TMDB logo cache read error: {exc}")
        return None


def set_cached_tmdb_logo(cache_key: str, data: bytes) -> None:
    try:
        path = _safe_cache_path(TMDB_LOGO_CACHE_DIR, cache_key)
        _remove_if_dir(path)
        with open(path, "wb") as f:
            f.write(data)
    except Exception as exc:
        logger.error(f"TMDB logo cache write error: {exc}")

def _safe_cache_path(base_dir: str, filename: str) -> str:
    path = os.path.realpath(os.path.join(base_dir, filename))
    if not path.startswith(os.path.realpath(base_dir)):
        raise ValueError(f"Path traversal attempt: {filename!r}")
    return path        

# ---------------------------------------------------------------------------
# TMDB metadata cache
# ---------------------------------------------------------------------------

def get_cached_tmdb_metadata(cache_key: str) -> dict | None:
    try:
        row = get_db().execute(
            """
            SELECT title, release_year, genre_ids, is_textless, poster_path,
                   logos_json, cached_at,
                   credits_json, production_cos_json,
                   runtime, number_of_seasons, number_of_episodes,
                   original_language, backdrop_path, tmdb_status, vote_count,
                   text_backdrop_path
            FROM tmdb_metadata_cache
            WHERE cache_key = ?
            """,
            (cache_key,),
        ).fetchone()
        if not row:
            return None

        (
            title, release_year, genre_ids_raw, is_textless, poster_path,
            logos_json, cached_at,
            credits_json, production_cos_json,
            runtime, number_of_seasons, number_of_episodes,
            original_language, backdrop_path, tmdb_status, vote_count,
            text_backdrop_path,
        ) = row

        age_days = (time.time() - cached_at) / 86400
        if age_days > TMDB_METADATA_CACHE_DURATION:
            logger.info(f"TMDB metadata cache expired for {cache_key} ({age_days:.1f}d old)")
            with _db_lock:
                get_db().execute(
                    "DELETE FROM tmdb_metadata_cache WHERE cache_key = ?", (cache_key,)
                )
                get_db().commit()
            return None

        return {
            "title":                title,
            "release_year":         release_year,
            "genre_ids":            json.loads(genre_ids_raw or "[]"),
            "is_textless":          bool(is_textless),
            "poster_path":          poster_path,
            "logos":                json.loads(logos_json or "[]"),
            "credits":              json.loads(credits_json or "{}"),
            "production_companies": json.loads(production_cos_json or "[]"),
            "runtime":              runtime,
            "number_of_seasons":    number_of_seasons,
            "number_of_episodes":   number_of_episodes,
            "original_language":    original_language,
            "backdrop_path":        backdrop_path,
            "tmdb_status":          tmdb_status,
            "vote_count":           vote_count,
            "text_backdrop_path":   text_backdrop_path,
        }
    except Exception as exc:
        logger.error(f"TMDB metadata cache read error: {exc}")
        return None


def set_cached_tmdb_metadata(
    cache_key: str,
    title: str,
    release_year: str | None,
    genre_ids: list[int],
    is_textless: bool,
    poster_path: str,
    logos: list[dict],
    *,
    credits: dict | None = None,
    production_companies: list[dict] | None = None,
    original_language: str | None = None,
    runtime: int | None = None,
    number_of_seasons: int | None = None,
    number_of_episodes: int | None = None,
    backdrop_path: str | None = None,
    tmdb_status: str | None = None,
    vote_count: int | None = None,
    text_backdrop_path: str | None = None,
) -> None:
    try:
        with _db_lock:
            get_db().execute(
                """
                INSERT OR REPLACE INTO tmdb_metadata_cache
                    (cache_key, title, release_year, genre_ids, is_textless,
                     poster_path, logos_json, cached_at,
                     credits_json, production_cos_json,
                     runtime, number_of_seasons, number_of_episodes,
                     original_language, backdrop_path, tmdb_status, vote_count,
                     text_backdrop_path)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cache_key,
                    title,
                    release_year,
                    json.dumps(genre_ids),
                    int(is_textless),
                    poster_path,
                    json.dumps(logos),
                    int(time.time()),
                    json.dumps(credits or {}),
                    json.dumps(production_companies or []),
                    runtime,
                    number_of_seasons,
                    number_of_episodes,
                    original_language,
                    backdrop_path,
                    tmdb_status,
                    vote_count,
                    text_backdrop_path,
                ),
            )
            get_db().commit()
    except Exception as exc:
        logger.error(f"TMDB metadata cache write error: {exc}")


def delete_cached_tmdb_metadata(cache_key: str) -> None:
    """Remove a single TMDB metadata entry so the next request re-fetches from TMDB."""
    try:
        with _db_lock:
            get_db().execute(
                "DELETE FROM tmdb_metadata_cache WHERE cache_key = ?", (cache_key,)
            )
            get_db().commit()
        logger.info(f"TMDB metadata cache invalidated for {cache_key}")
    except Exception as exc:
        logger.error(f"TMDB metadata cache delete error: {exc}")


# ---------------------------------------------------------------------------
# Digital release cache
# ---------------------------------------------------------------------------

def is_digital_release(imdb_id: str) -> bool:
    """Return True if the IMDB ID has a matching entry in the digital release cache."""
    try:
        row = get_db().execute(
            "SELECT 1 FROM digital_release_cache WHERE imdb_id = ?", (imdb_id,)
        ).fetchone()
        return row is not None
    except Exception as exc:
        logger.error(f"Digital release cache lookup error: {exc}")
        return False


def add_digital_releases(entries: list[tuple[str, int]]) -> int:
    """
    Insert (imdb_id, posted_at) pairs. Uses INSERT OR IGNORE so the
    original posted_at is never overwritten. Returns the number of new rows inserted.
    """
    if not entries:
        return 0
    inserted = 0
    try:
        with _db_lock:
            for imdb_id, posted_at in entries:
                r = get_db().execute(
                    "INSERT OR IGNORE INTO digital_release_cache (imdb_id, posted_at) VALUES (?, ?)",
                    (imdb_id, posted_at),
                )
                inserted += r.rowcount
            get_db().commit()
    except Exception as exc:
        logger.error(f"Digital release cache write error: {exc}")
    return inserted


# ---------------------------------------------------------------------------
# Release status cache
# ---------------------------------------------------------------------------
# Cached separately from main metadata so the extra TMDB /release_dates call
# only happens for users who have enabled the "release_status" sash slot.
# TTL: 7 days — status changes slowly (Cinema → Streaming → BluRay is one-way).

_RELEASE_STATUS_TTL_DAYS = 7


def get_cached_release_status(cache_key: str) -> str | None:
    """Return the cached release status string, or None if absent / expired."""
    try:
        row = get_db().execute(
            "SELECT status, cached_at FROM release_status_cache WHERE cache_key = ?",
            (cache_key,),
        ).fetchone()
        if not row:
            return None
        status, cached_at = row
        age_days = (time.time() - cached_at) / 86400
        if age_days > _RELEASE_STATUS_TTL_DAYS:
            logger.info(f"Release status cache expired for {cache_key} ({age_days:.1f}d old)")
            return None
        return status
    except Exception as exc:
        logger.error(f"Release status cache read error: {exc}")
        return None


def set_cached_release_status(cache_key: str, status: str) -> None:
    """Upsert a release status entry."""
    try:
        with _db_lock:
            get_db().execute(
                """
                INSERT INTO release_status_cache (cache_key, status, cached_at)
                VALUES (?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET status=excluded.status, cached_at=excluded.cached_at
                """,
                (cache_key, status, int(time.time())),
            )
            get_db().commit()
    except Exception as exc:
        logger.error(f"Release status cache write error: {exc}")


def get_cached_text_detection(cache_key: str) -> bool | None:
    """Return the cached burned-in-text result (True/False), or None if absent.

    Results never expire — they're keyed by an immutable TMDB image path plus the
    detection params, so the answer can't change for a given key.
    """
    try:
        row = get_db().execute(
            "SELECT has_text FROM text_detection_cache WHERE cache_key = ?",
            (cache_key,),
        ).fetchone()
        return None if row is None else bool(row[0])
    except Exception as exc:
        logger.error(f"Text-detection cache read error: {exc}")
        return None


def set_cached_text_detection(cache_key: str, has_text: bool) -> None:
    """Upsert a burned-in-text detection result."""
    try:
        with _db_lock:
            get_db().execute(
                """
                INSERT INTO text_detection_cache (cache_key, has_text, cached_at)
                VALUES (?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET has_text=excluded.has_text, cached_at=excluded.cached_at
                """,
                (cache_key, int(has_text), int(time.time())),
            )
            get_db().commit()
    except Exception as exc:
        logger.error(f"Text-detection cache write error: {exc}")