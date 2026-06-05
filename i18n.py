# i18n.py
#
# Poster-output translation with per-key English fallback.
#
# Each languages/<code>.json supplies genreLabels / sashLabels maps keyed by the
# CANONICAL ENGLISH strings the renderer produces (see languages/en.json for the
# reference vocabulary).  Translation is display-only: every internal decision
# (award-star matching, sash priority, font/colour lookups) stays in English, so
# a missing key, a malformed file, or a language with no JSON at all simply falls
# back to the English canonical string.  Nothing breaks if a translation is
# absent — it just renders in English.
import json
import logging
import os
import re

logger = logging.getLogger(__name__)

_LANG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "languages")
_LANGS: dict[str, dict] = {}

# Trending labels are produced as "#<rank> Today"; translated via the
# "trendingToday" template key (e.g. "#{rank} Aujourd'hui") so the rank stays.
_TRENDING_RE = re.compile(r"^#(\d+)\s+Today$")

# Composite nominee labels are joined with this separator in discovery.pick_sash.
_NOM_SEP = " • "


def load_languages() -> None:
    """Load every languages/*.json into memory once (call at startup)."""
    _LANGS.clear()
    if not os.path.isdir(_LANG_DIR):
        return
    for fn in os.listdir(_LANG_DIR):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(_LANG_DIR, fn), encoding="utf-8") as f:
                data = json.load(f)
            code = (data.get("code") or os.path.splitext(fn)[0]).strip().lower()
            if code:
                _LANGS[code] = data
        except Exception as e:  # malformed file → skip, English fallback stands
            logger.warning(f"i18n: could not load language file {fn!r}: {e}")
    if _LANGS:
        logger.info(f"i18n: loaded languages {sorted(_LANGS)}")


def has_language(lang: str | None) -> bool:
    return bool(lang) and lang.strip().lower() in _LANGS


def _table(lang: str | None, key: str) -> dict:
    return _LANGS.get((lang or "").strip().lower(), {}).get(key, {}) or {}


def translate_genre(name: str | None, lang: str | None) -> str:
    """Canonical English genre name → localized, or unchanged if no translation."""
    if not name:
        return name or ""
    return _table(lang, "genreLabels").get(name, name)


def translate_sash(label: str | None, lang: str | None) -> str:
    """Canonical English sash label → localized, or unchanged if no translation.

    Handles two special shapes: the "#<rank> Today" trending template and the
    " • "-joined composite nominee label (each part translated independently).
    Proper nouns (studio / director / cast / festival names) aren't in the JSON
    so they pass straight through.
    """
    if not label:
        return label or ""
    sl = _table(lang, "sashLabels")
    if not sl:
        return label

    m = _TRENDING_RE.match(label)
    if m:
        tmpl = sl.get("trendingToday")
        return tmpl.replace("{rank}", m.group(1)) if tmpl else label

    if _NOM_SEP in label:
        return _NOM_SEP.join(sl.get(part, part) for part in label.split(_NOM_SEP))

    return sl.get(label, label)
