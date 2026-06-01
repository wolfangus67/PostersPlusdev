"""
discovery.py — "One interesting thing" sash logic.

Caches all discoverable facts about a title, then picks the single highest-
priority one to show, based on a caller-supplied ordered list.

Priority slots
--------------
wins         Oscar Best Picture win / Major Emmy (Outstanding) Series win
gg_wins      Golden Globe win (all top film + TV categories)
festival     Major international festival winner (Cannes, Berlin, Venice, Sundance, …)
pic_noms     Best Picture nomination (film) / Major Emmy nomination (TV)
gg_noms      Golden Globe nomination (all top film + TV categories)
studio       Produced by a notable studio (A24, Pixar, …)
director     Directed by a notable filmmaker
cast         Stars a notable actor / actress
trending     Currently trending on TMDB
cult         Cult Classic / Cult Film (MDblist keyword)
foreign      Non-English original language film
new_release  Newly released — digital/premiere within the last 14 days, or a
             confirmed r/movieleaks digital-release post
metacritic   Metacritic Must-See badge (curated critical acclaim)
true_story   Based on a true story (MDblist keyword)
structural   Short Film | Mini Series | Binge Ready (whichever matches first)

Legacy aliases (still accepted in sash_priority for backward-compat with old URLs):
    emmy_noms       → pic_noms
    digital_release → new_release
    noms            → any nomination (catch-all)

All facts are stored in DiscoveryMeta so they only need to be computed once
and can be re-prioritised at render time without re-fetching.

Operator customisation
----------------------
Self-hosters can supply their own director / studio / cast lists without
editing this file.  Place a JSON file at:

    /app/cache/discovery_overrides.json

This sits inside the existing cache volume mount (./postersplus-cache:/app/cache)
so no extra volume entry is needed in compose.yaml.  The path can be changed
via the DISCOVERY_OVERRIDES_PATH environment variable.

File format:
    {
      "mode": "replace",          -- "replace" (default) or "merge"
      "studios":   { "Studio Name": "Display Label", ... },
      "directors": { "Director Name": "Display Label", ... },
      "cast":      { "Actor Name": "Display Label", ... }
    }

"replace" (default): each provided section fully replaces the built-in list.
  Omit a section to keep its built-in defaults untouched.
"merge": provided entries are added to (and override) the built-in lists.
  Useful for adding names without losing the defaults.

See the project README for a full sample file.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime

from config import SASH_PRIORITY as DEFAULT_SASH_PRIORITY  # single source of truth

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Curated lists
# ---------------------------------------------------------------------------


# Keys are the exact TMDB credit name to match against.
# Values are the display string shown in the sash.
# To customise the label without changing the match, set a different value:
#   "Studio Ghibli": "Ghibli"   →  sash shows "By Ghibli"
#   "Studio Ghibli": "Studio Ghibli"  →  sash shows "By Studio Ghibli"

NOTABLE_STUDIOS: dict[str, str] = {
    "A24":                    "A24 Films",
    "Pixar":                  "Pixar Studio",
    "Studio Ghibli":          "Studio Ghibli",
    "Blumhouse Productions":  "Blumhouse",
    "Neon":                   "NEON Rated",
    "Searchlight Pictures":   "SL Pictures",
    "BBC Films":              "BBC Films",
    "Bad Robot":              "Bad Robot",
    "HBO":                    "HBO Original",
    "Laika Entertainment":    "Laika",
}

# Keys are the exact TMDB credit name to match against.
# Values are the display string shown in the sash.
# Example: "Christopher Nolan": "Nolan"  →  sash shows "By Nolan"

NOTABLE_DIRECTORS: dict[str, str] = {
    "Christopher Nolan":   "C. Nolan",
    "Denis Villeneuve":    "D. Villeneuve",
    "Martin Scorsese":     "M. Scorsese",
    "Wes Anderson":        "Wes Anderson",
    "Sofia Coppola":       "S. Coppola",
    "Bong Joon-ho":        "B. Joon-ho",
    "Hayao Miyazaki":      "H. Miyazaki",
    "David Fincher":       "D. Fincher",
    "Paul Thomas Anderson":"P.T. Anderson",
    "Quentin Tarantino":   "Q. Tarantino",
    "Alfonso Cuarón":      "A. Cuarón",
    "Guillermo del Toro":  "G. del Toro",
    "Ridley Scott":        "R. Scott",
    "Steven Spielberg":    "S. Spielberg",
    "Joel Coen":           "Coen Brothers",
    "Ethan Coen":          "Coen Brothers",
    "David Lynch":         "D. Lynch",
    "Darren Aronofsky":    "D. Aronofsky",
    "Yorgos Lanthimos":    "Y. Lanthimos",
    "Ari Aster":           "Ari Aster",
    "Jordan Peele":        "J. Peele",
    "Greta Gerwig":        "G. Gerwig",
    "Robert Eggers":       "R. Eggers",
    "Céline Sciamma":      "C. Sciamma",
    "Park Chan-wook":      "P. Chan-wook",
    "Wong Kar-wai":        "Wong Kar-wai",
    "Hirokazu Kore-eda":   "H. Kore-eda",
    "Luca Guadagnino":     "L. Guadagnino",
    "Sean Baker":          "Sean Baker",
    "Stanley Kubrick":     "S. Kubrick",
    "Spike Lee":           "Spike Lee",
    "David Cronenberg":    "D. Cronenberg",
    "Michael Mann":        "Michael Mann",
    "Francis Ford Coppola":"F.F Coppola",
    "Jane Campion":        "J. Campion",
    "Terrence Malick":     "T. Malick",
    "Mike Flanagan":       "M. Flanagan",
    "James Cameron":       "J. Cameron",
    "Peter Jackson":       "P. Jackson",
}


# Keys are the exact TMDB credit name to match against.
# Values are the display string shown in the sash.
# Keep values short — the sash is narrow. First name + last name usually fits;
# drop first names or use initials where needed.

NOTABLE_CAST: dict[str, str] = {
    "Cate Blanchett":    "Cate Blanchett",
    "Meryl Streep":      "Meryl Streep",
    "Viola Davis":       "Viola Davis",
    "Tilda Swinton":     "Tilda Swinton",
    "Joaquin Phoenix":   "Joaquin Phoenix",
    "Daniel Day-Lewis":  "D. Day-Lewis",
    "Tom Hanks":         "Tom Hanks",
    "Denzel Washington": "D. Washington",
    "Leonardo DiCaprio": "L. DiCaprio",
    "Natalie Portman":   "Natalie Portman",
    "Nicole Kidman":     "Nicole Kidman",
    "Julianne Moore":    "Julianne Moore",
    "Jessica Lange":     "Jessica Lange",
    "Anthony Hopkins":   "Anthony Hopkins",
    "Gary Oldman":       "Gary Oldman",
    "Ryan Gosling":      "Ryan Gosling",
    "Margot Robbie":     "Margot Robbie",
    "Adam Driver":       "Adam Driver",
    "Saoirse Ronan":     "Saoirse Ronan",
    "Oscar Isaac":       "Oscar Isaac",
    "Mahershala Ali":    "Mahershala Ali",
    "Lupita Nyong'o":    "Lupita Nyong'o",
    "Pedro Pascal":      "Pedro Pascal",
    "Jeff Bridges":      "Jeff Bridges",
    "Charlize Theron":   "Charlize Theron",
    "Timothée Chalamet": "T. Chalamet",
    "Zendaya":           "Zendaya",
    "Florence Pugh":     "Florence Pugh",
    "Austin Butler":     "Austin Butler",
    "Barry Keoghan":     "Barry Keoghan",
    "Paul Mescal":       "Paul Mescal",
    "Carey Mulligan":    "Carey Mulligan",
    "Andrew Garfield":   "Andrew Garfield",
    "Ana de Armas":      "Ana de Armas",
    "Anya Taylor-Joy":   "Anya Taylor-Joy",
    "Frances McDormand":      "F. McDormand",
    "Robert De Niro":         "Robert De Niro",
    "Al Pacino":              "Al Pacino",
    "Willem Dafoe":           "Willem Dafoe",
    "Philip Seymour Hoffman": "P. Hoffman",
    "Jake Gyllenhaal":        "Jake Gyllenhaal",
    "Emma Stone":             "Emma Stone",
    "Christian Bale":         "Christian Bale",
    "Colin Farrell":          "Colin Farrell",
    "Rachel McAdams":         "Rachel McAdams",
    "Amy Adams":              "Amy Adams",
    "Jeremy Strong":          "Jeremy Strong",
    "Ayo Edebiri":            "Ayo Edebiri",
    "Kieran Culkin":          "Kieran Culkin",
    "Jeremy Allen White":     "J. White",
    "Mia Goth":               "Mia Goth",
    "Sebastian Stan":         "Sebastian Stan",
    "Harris Dickinson":       "H. Dickinson",
    "Mikey Madison":          "Mikey Madison",
    "Josh O'Connor":          "Josh O'Connor",
}

# Within the "structural" bucket, checked in this fixed order
_STRUCTURAL_CHECKS = ["short_film", "mini_series", "binge_ready"]

_STRUCTURAL_LABELS: dict[str, str] = {
    "short_film":  "Short Film",
    "mini_series": "Mini Series",
    "binge_ready": "Binge Ready",
}

# MDblist keyword name → sash display label.
# Checked in order; first match wins within the festival slot.
# Add new festivals here — keyword pattern is typically festival-<name>-winner.
# Ordered roughly by prestige (up for debate)
FESTIVAL_KEYWORDS: dict[str, str] = { 
    "festival-cannes-winner":      "Palme d'Or",
    "festival-venice-winner":      "Golden Lion",
    "festival-berlin-winner":      "Golden Bear",
    "festival-toronto-winner":     "People's Choice",
    "festival-sundance-winner":    "Sundance GJ",
    "festival-busan-winner":       "New Currents",
    "festival-locarno-winner":     "Golden Leopard",
    "festival-rotterdam-winner":   "Tiger Award",
    "festival-sxsw-winner":        "SXSW Jury",
    "festival-tribeca-winner":     "Tribeca AA",
}

# ISO 639-1 language code → display label shown on the sash.
# English is intentionally absent — foreign slot only fires for non-English.
# Add languages here; unlisted non-English languages fall back to
# "Foreign Language Film" to ensure the slot always has a label.
LANGUAGE_LABELS: dict[str, str] = {
    "fr": "French",
    "de": "German",
    "es": "Spanish",
    "it": "Italian",
    "pt": "Portuguese",
    "ja": "Japanese",
    "ko": "Korean",
    "zh": "Chinese",
    "da": "Danish",
    "sv": "Swedish",
    "no": "Norwegian",
    "fi": "Finnish",
    "nl": "Dutch",
    "pl": "Polish",
    "ru": "Russian",
    "tr": "Turkish",
    "ar": "Arabic",
    "hi": "Hindi",
    "fa": "Persian",
    "ro": "Romanian",
    "hu": "Hungarian",
    "cs": "Czech",
    "he": "Hebrew",
    "el": "Greek",
}

# Sash type (controls colour) for each priority slot
_SASH_TYPES: dict[str, str] = {
    "wins":            "win",       # gold — Oscar Best Picture + Emmy Outstanding wins
    "gg_wins":         "win",       # gold — Golden Globe wins (separate slot)
    "pic_noms":        "nom",       # silver — Best Picture nom + Major Emmy nom (film vs TV, never coexist)
    "gg_noms":         "nom",       # silver — Golden Globe nomination
    "emmy_noms":       "nom",       # silver — legacy alias for pic_noms
    "noms":            "nom",       # silver — legacy catch-all for any nomination
    "festival":        "win",       # gold — major festival win is prestige-equivalent to Oscar
    "studio":          "prestige",  # purple — production credit
    "director":        "prestige",  # purple — production credit
    "cast":            "cast",      # green — talent credit
    "trending":        "trending",  # blue
    "cult":            "trending",  # blue — popularity signal, closest to trending without a new colour
    "foreign":         "info",      # teal — informational / discovery
    "new_release":     "alert",     # red — newly streaming (release date or r/movieleaks)
    "digital_release": "alert",     # red — legacy alias for new_release
    "metacritic":      "nom",       # silver — critical award, fits with noms not production
    "true_story":      "info",      # teal
    "structural":      "info",      # teal
    "release_status":  "alert",     # red — Physical / Streaming / Cinema / Production
}

NEW_RELEASE_DAYS = 14


def _is_recent(release_date: str | None) -> bool:
    """Return True if *release_date* (YYYY-MM-DD) is within NEW_RELEASE_DAYS of today."""
    if not release_date:
        return False
    try:
        rd = datetime.strptime(release_date, "%Y-%m-%d").date()
        return (date.today() - rd).days <= NEW_RELEASE_DAYS
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Data container
# ---------------------------------------------------------------------------

@dataclass
class DiscoveryMeta:
    """All discoverable facts about a title, computed once and cached."""

    # Awards (from MDblist keywords + Emmy ID set)
    award_wins: list[str] = field(default_factory=list)   # "Best Picture", "Emmy Winner"
    award_noms: list[str] = field(default_factory=list)   # "Best Picture Nom", "Emmy Nominee"

    # Prestige signals (from TMDB credits / production_companies)
    matched_studios:   list[str] = field(default_factory=list)
    matched_directors: list[str] = field(default_factory=list)
    matched_cast:      list[str] = field(default_factory=list)

    # Festival winner (from MDblist keywords)
    festival_label: str | None = None   # e.g. "Palme d'Or Winner"

    # Structural facts (computed from TMDB metadata)
    is_short_film:  bool = False   # movie, runtime < 40 min
    is_mini_series: bool = False   # TV, 1 season, ≤ 8 episodes
    is_binge_ready: bool = False   # TV, ≥ 3 seasons, prestige episode count

    # Language (from TMDB metadata)
    original_language: str | None = None   # ISO 639-1 code, e.g. "ko", "fr"

    # Social proof
    trending_rank: int | None = None

    # New release (MDblist digital-release / premiere date within the last 2 weeks)
    is_new_release: bool = False

    # Keyword-based discovery signals (from MDblist keywords)
    is_cult:              bool = False   # cult-classic or cult-film
    is_true_story:        bool = False   # based-on-true-story
    is_metacritic_must_see: bool = False  # metacritic-must-see

    # Digital release (from r/movieleaks poller — movies only)
    is_digital_release: bool = False

    # Release status — populated on demand when "release_status" is in sash_priority.
    # Movies: "Physical" | "Streaming" | "Cinema" | "Production"
    # TV:     "Returning" | "Ended" | "Cancelled" | "Production"
    release_status: str | None = None


# ---------------------------------------------------------------------------
# Extraction helpers
# ---------------------------------------------------------------------------

def extract_discovery_meta(
    tmdb_data: dict,
    media_type: str,
    award_wins: list[str],
    award_noms: list[str],
    trending_rank: int | None,
    *,
    release_date: str | None = None,
    keywords: list[dict] | None = None,
    festival_label_override:  str | None  = None,
    is_cult_override:         bool | None = None,
    is_true_story_override:   bool | None = None,
    is_metacritic_override:   bool | None = None,
    is_digital_release_override: bool | None = None,
    release_status_override: str | None = None,
    notable_studios:   dict[str, str] | None = None,
    notable_directors: dict[str, str] | None = None,
    notable_cast:      dict[str, str] | None = None,
    festival_keywords: dict[str, str] | None = None,
    language_labels:   dict[str, str] | None = None,
) -> DiscoveryMeta:
    studios        = notable_studios   or NOTABLE_STUDIOS
    directors      = notable_directors or NOTABLE_DIRECTORS
    cast_list      = notable_cast      or NOTABLE_CAST
    fest_keywords  = festival_keywords or FESTIVAL_KEYWORDS

    meta = DiscoveryMeta(
        award_wins=award_wins,
        award_noms=award_noms,
        trending_rank=trending_rank,
        original_language=tmdb_data.get("original_language"),
    )

    # Build keyword name set once — reused for festival detection and the
    # new keyword-based signals (cult, true-story, metacritic).
    keyword_names: set[str] = (
        {(kw.get("name") or "").lower().strip() for kw in keywords}
        if keywords else set()
    )

    # --- Festival winners ---
    # Prefer a pre-resolved label (from cache) to avoid re-scanning keywords.
    if festival_label_override is not None:
        meta.festival_label = festival_label_override
    elif keyword_names:
        for kw_name, label in fest_keywords.items():
            if kw_name in keyword_names:
                meta.festival_label = label
                break

    # --- Keyword-based discovery signals ---
    # Each signal uses an override (from cache) when available; otherwise
    # falls back to live keyword scanning on a fresh MDblist fetch.
    if is_cult_override is not None:
        meta.is_cult = is_cult_override
    elif keyword_names:
        meta.is_cult = bool({"cult-classic", "cult-film"} & keyword_names)

    if is_true_story_override is not None:
        meta.is_true_story = is_true_story_override
    elif keyword_names:
        meta.is_true_story = "based-on-true-story" in keyword_names

    if is_metacritic_override is not None:
        meta.is_metacritic_must_see = is_metacritic_override
    elif keyword_names:
        meta.is_metacritic_must_see = "metacritic-must-see" in keyword_names

    if is_digital_release_override is not None:
        meta.is_digital_release = is_digital_release_override

    if release_status_override is not None:
        meta.release_status = release_status_override

    # --- Studios ---
    for company in tmdb_data.get("production_companies", []):
        name = company.get("name", "")
        if name in studios:
            meta.matched_studios.append(studios[name])

    # --- Credits ---
    credits = tmdb_data.get("credits", {})

    for crew_member in credits.get("crew", []):
        if crew_member.get("job") == "Director":
            name = crew_member.get("name", "")
            if name in directors:
                label = directors[name]
                if label not in meta.matched_directors:
                    meta.matched_directors.append(label)

    for cast_member in credits.get("cast", [])[:10]:
        name = cast_member.get("name", "")
        if name in cast_list:
            meta.matched_cast.append(cast_list[name])

    # --- Structural ---
    is_tv = media_type in ("tv", "series")

    if not is_tv:
        runtime = tmdb_data.get("runtime") or 0
        meta.is_short_film = 0 < runtime < 40
    else:
        num_seasons  = tmdb_data.get("number_of_seasons")  or 0
        num_episodes = tmdb_data.get("number_of_episodes") or 0

        meta.is_mini_series = (
            num_seasons == 1
            and 0 < num_episodes <= 8
        )

        if num_seasons >= 3 and num_episodes > 0:
            eps_per_season = num_episodes / num_seasons
            meta.is_binge_ready = 6 <= eps_per_season <= 20

    # --- New release ---
    if _is_recent(release_date):
        meta.is_new_release = True

    return meta


# ---------------------------------------------------------------------------
# Priority picker
# ---------------------------------------------------------------------------

def pick_sash(
    meta: DiscoveryMeta,
    priority: list[str],
) -> tuple[str, str] | None:
    """
    Walk *priority* (ordered list of slot names) and return the first match
    as ``(label_text, sash_type)``, or ``None`` if nothing matches.
    """
    for slot in priority:
        result = _evaluate_slot(slot, meta)
        if result is not None:
            sash_type = _SASH_TYPES.get(slot, "info")
            return result, sash_type
    return None


def _evaluate_slot(slot: str, meta: DiscoveryMeta) -> str | None:
    """Return a label string if this slot has a match, else None."""

    if slot == "wins":
        # Oscar Best Picture wins and Emmy Outstanding wins only.
        # Golden Globe wins have their own slot (gg_wins) so they can be
        # prioritised independently. A title can win both Oscar and Emmy
        # (impossible in practice) but both share this slot since one is film,
        # one is TV — they never coexist on the same title.
        w = [v for v in meta.award_wins if v != "Golden Globe"]
        return w[0] if w else None

    if slot == "gg_wins":
        # Golden Globe wins — all top film and TV categories.
        return "Golden Globe" if "Golden Globe" in meta.award_wins else None

    if slot in ("pic_noms", "emmy_noms"):
        # Best Picture nominations (film) and Major Emmy nominations (TV) share
        # this slot — they never coexist on the same title, mirroring wins.
        # emmy_noms is kept as a legacy alias for backward-compat with old URLs.
        match = next((n for n in meta.award_noms if "Best Picture" in n or "Emmy" in n), None)
        return match

    if slot == "gg_noms":
        # Golden Globe nominations — all top film and TV categories.
        return "Golden Globe" if "Golden Globe" in meta.award_noms else None

    if slot == "noms":
        # Legacy catch-all: any nomination (kept for backward-compat with
        # hand-crafted sash_priority query params)
        return " • ".join(meta.award_noms) if meta.award_noms else None

    if slot == "festival":
        return meta.festival_label if meta.festival_label else None

    if slot == "foreign":
        lang = meta.original_language
        if not lang or lang == "en":
            return None
        return LANGUAGE_LABELS.get(lang, "Foreign") # return LANGUAGE_LABELS.get(lang, "Foreign Language Film")

    if slot == "studio":
        # matched_studios already holds display labels (dict values)
        return f"{meta.matched_studios[0]}" if meta.matched_studios else None

    if slot == "director":
        # matched_directors already holds display labels (dict values)
        return f"{meta.matched_directors[0]}" if meta.matched_directors else None

    if slot == "cast":
        # matched_cast already holds display labels (dict values)
        return meta.matched_cast[0] if meta.matched_cast else None

    if slot == "trending":
        return f"#{meta.trending_rank} Today" if meta.trending_rank else None

    if slot in ("new_release", "digital_release"):
        # Merged: fires on release-date recency OR r/movieleaks confirmation.
        # "digital_release" is kept as a legacy alias so old sash_priority params
        # still work — both slots check the same combined condition.
        if meta.is_new_release or meta.is_digital_release:
            return "New"
        return None

    if slot == "metacritic":
        return "Must-See" if meta.is_metacritic_must_see else None

    if slot == "cult":
        return "Cult Classic" if meta.is_cult else None

    if slot == "true_story":
        return "True Story" if meta.is_true_story else None

    if slot == "structural":
        for key in _STRUCTURAL_CHECKS:
            if key == "short_film"  and meta.is_short_film:  return _STRUCTURAL_LABELS[key]
            if key == "mini_series" and meta.is_mini_series:  return _STRUCTURAL_LABELS[key]
            if key == "binge_ready" and meta.is_binge_ready:  return _STRUCTURAL_LABELS[key]
        return None

    if slot == "release_status":
        return meta.release_status  # already a display string or None

    return None


# ---------------------------------------------------------------------------
# Default priority
# ---------------------------------------------------------------------------

ALL_PRIORITY_SLOTS: list[str] = [
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
    "emmy_noms",        # legacy alias for pic_noms — still accepted in sash_priority param
    "digital_release",  # legacy alias for new_release
    "noms",             # legacy alias for any nomination
    "release_status",   # opt-in: Blu-ray / Streaming / Cinema / Production — requires extra API call for movies
]


# ---------------------------------------------------------------------------
# Operator override loader
# ---------------------------------------------------------------------------

_OVERRIDE_PATH = os.environ.get(
    "DISCOVERY_OVERRIDES_PATH",
    "/app/cache/discovery_overrides.json",
)


def _load_discovery_overrides() -> None:
    """
    Apply operator-supplied director / studio / cast lists from a JSON file.

    Called once at module import time.  Silently skips when the file is
    absent so the built-in defaults in this file are used unchanged.  Logs
    a warning (and falls back to built-ins) if the file exists but is invalid.

    See the module docstring at the top of this file for the full format.
    """
    try:
        with open(_OVERRIDE_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return                        # normal — no overrides configured
    except Exception as exc:
        logger.warning(
            f"discovery_overrides.json: failed to parse ({exc}) — using built-in lists"
        )
        return

    if not isinstance(data, dict):
        logger.warning("discovery_overrides.json: root must be a JSON object — ignoring")
        return

    mode         = data.get("mode", "replace")
    studios_raw  = data.get("studios")
    dirs_raw     = data.get("directors")
    cast_raw     = data.get("cast")

    counts: list[str] = []

    if mode == "merge":
        # Add / overwrite individual entries; entries absent from the file
        # remain from the built-in lists.
        if isinstance(studios_raw, dict):
            NOTABLE_STUDIOS.update(studios_raw)
            counts.append(f"+{len(studios_raw)} studios")
        if isinstance(dirs_raw, dict):
            NOTABLE_DIRECTORS.update(dirs_raw)
            counts.append(f"+{len(dirs_raw)} directors")
        if isinstance(cast_raw, dict):
            NOTABLE_CAST.update(cast_raw)
            counts.append(f"+{len(cast_raw)} cast")
        logger.info(f"discovery_overrides.json loaded (merge): {', '.join(counts) or 'no sections'}")

    else:
        # "replace" — each supplied section fully replaces its built-in list.
        # Omitted sections keep their defaults.
        if isinstance(studios_raw, dict):
            NOTABLE_STUDIOS.clear()
            NOTABLE_STUDIOS.update(studios_raw)
            counts.append(f"{len(studios_raw)} studios")
        if isinstance(dirs_raw, dict):
            NOTABLE_DIRECTORS.clear()
            NOTABLE_DIRECTORS.update(dirs_raw)
            counts.append(f"{len(dirs_raw)} directors")
        if isinstance(cast_raw, dict):
            NOTABLE_CAST.clear()
            NOTABLE_CAST.update(cast_raw)
            counts.append(f"{len(cast_raw)} cast")
        logger.info(f"discovery_overrides.json loaded (replace): {', '.join(counts) or 'no sections'}")


_load_discovery_overrides()