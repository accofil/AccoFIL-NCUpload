"""API keys of outside services, read from ~/.config/ncore-up/config.env (or the environment).

    DEEPL_KEY=...            DeepL API (Free keys end in ':fx')       Hungarian machine translation
    IGDB_CLIENT_ID=...       Twitch application (IGDB)                games, console
    IGDB_CLIENT_SECRET=...
    GOOGLE_BOOKS_KEY=...     Google Cloud API key (Books API)         ebooks, audiobooks
    DISCOGS_TOKEN=...        Discogs personal access token            music, clip
    DISCOGS_CONSUMER_KEY=... Discogs application key (alternative to the token)
    DISCOGS_CONSUMER_SECRET=...
    ACOUSTID_KEY=...         AcoustID application key                 untagged music
    COMICVINE_KEY=...        ComicVine API key                        English comics
    SETLISTFM_KEY=...        setlist.fm API key                       concert tracklists
    LASTFM_KEY=...           Last.fm API key                          music tags
    TVDB_KEY=...             TheTVDB v4 key                           episode titles
    FANART_KEY=...           fanart.tv key                            artist images, posters
    SPOTIFY_CLIENT_ID=...    Spotify application (optional)           music fallback
    SPOTIFY_CLIENT_SECRET=...
    GITHUB_TOKEN=...         read-only GitHub token                   winget/scoop lookups above 60/h
    TMDB_API=...             TMDB v3 key (falls back to the Upload-Assistant key)

A source whose key is missing is skipped silently; ``ncore-up --check`` lists the missing ones.
"""

from __future__ import annotations

import os
from pathlib import Path

NAMES = [
    "DEEPL_KEY", "IGDB_CLIENT_ID", "IGDB_CLIENT_SECRET", "GOOGLE_BOOKS_KEY", "DISCOGS_TOKEN", "DISCOGS_CONSUMER_KEY", "DISCOGS_CONSUMER_SECRET", "ACOUSTID_KEY", "COMICVINE_KEY",
    "SETLISTFM_KEY", "LASTFM_KEY", "TVDB_KEY", "FANART_KEY", "SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET", "GITHUB_TOKEN", "TMDB_API",
]
_VALUES: dict[str, str] = {}


def load(values: dict[str, str]) -> None:
    """Called by Config.load with the merged config.env values."""

    _VALUES.clear()
    for name in NAMES:
        value = os.environ.get(name) or os.environ.get("NCORE_" + name) or values.get(name) or values.get("NCORE_" + name) or ""
        if value:
            _VALUES[name] = value.strip()
    if "TMDB_API" not in _VALUES:
        ua_key = _ua_tmdb_key()
        if ua_key:
            _VALUES["TMDB_API"] = ua_key


def get(name: str) -> str:
    return _VALUES.get(name, "")


def missing() -> list[str]:
    absent = [n for n in NAMES if not get(n)]
    if get("DISCOGS_TOKEN") or (get("DISCOGS_CONSUMER_KEY") and get("DISCOGS_CONSUMER_SECRET")):
        absent = [n for n in absent if not n.startswith("DISCOGS_")]
    return absent


def _ua_tmdb_key(path: Path = Path.home() / "Upload-Assistant" / "data" / "config.py") -> str:
    import re

    try:
        match = re.search(r'"tmdb_api"\s*:\s*"([0-9a-f]{32})"', path.read_text(encoding="utf-8"))
    except OSError:
        return ""
    return match.group(1) if match else ""
