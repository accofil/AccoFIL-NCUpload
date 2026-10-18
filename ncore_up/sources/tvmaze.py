"""TVmaze: series lookup by IMDb id, the show page (nCore's usual "other link" for English series) and English
episode titles per season.

No key. Documented limit: 20 calls per 10 seconds per IP. Data is CC BY-SA (credit TVmaze). No Hungarian text.
"""

from __future__ import annotations

import html
import re

from .. import net

API = "https://api.tvmaze.com"
CREDIT = "TVmaze"


def _plain(fragment: str) -> str:
    text = re.sub(r"(?i)</p>\s*<p>", "\n", fragment or "")
    return re.sub(r"[ \t]+", " ", html.unescape(re.sub(r"<[^>]+>", "", text))).strip()


def parse_show(data: dict) -> dict:
    """{"id", "url", "name", "year", "premiered", "runtime", "language", "genres", "imdb", "thetvdb", "image", "summary"}"""

    data = data if isinstance(data, dict) else {}
    if not str(data.get("id") or "").isdigit():
        return {}
    externals = data.get("externals") if isinstance(data.get("externals"), dict) else {}
    image = data.get("image") if isinstance(data.get("image"), dict) else {}
    premiered = str(data.get("premiered") or "")
    return {
        "id": int(data["id"]), "url": data.get("url") or "", "name": data.get("name") or "",
        "year": premiered[:4], "premiered": premiered,
        "runtime": str(data.get("runtime") or data.get("averageRuntime") or ""),
        "language": data.get("language") or "", "genres": list(data.get("genres") or []),
        "imdb": (externals.get("imdb") or "").lower(), "thetvdb": str(externals.get("thetvdb") or ""),
        "image": image.get("original") or image.get("medium") or "",
        "summary": _plain(data.get("summary") or ""),
    }


def parse_search(data) -> list[dict]:
    """parse_show() of every hit of /search/shows, best first."""

    hits = [parse_show(item.get("show") or {}) for item in data if isinstance(item, dict)] if isinstance(data, list) else []
    return [h for h in hits if h]


def parse_episodes(data, season: int | None = None) -> list[dict]:
    """[{"season", "episode", "title", "airdate", "runtime", "summary"}] (regular episodes; specials dropped)."""

    episodes = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict) or not str(item.get("number", "")).isdigit() or (season is not None and item.get("season") != season):
            continue
        episodes.append({
            "season": item.get("season"), "episode": int(item["number"]), "title": (item.get("name") or "").strip(),
            "airdate": item.get("airdate") or "", "runtime": str(item.get("runtime") or ""), "summary": _plain(item.get("summary") or ""),
        })
    return episodes


def lookup(imdb_id: str) -> dict:
    """parse_show() of the show with this IMDb id; {} when TVmaze does not know it (404 is normal, not logged)."""

    imdb_id = (imdb_id or "").strip().lower()
    if not re.fullmatch(r"tt\d{5,10}", imdb_id):
        return {}
    return parse_show(net.get_json(API + "/lookup/shows", params={"imdb": imdb_id}, quiet=True) or {})


def search(title: str) -> list[dict]:
    if not (title or "").strip():
        return []
    return parse_search(net.get_json(API + "/search/shows", params={"q": title.strip()}))


def episodes(show_id, season: int | None = None) -> list[dict]:
    if not str(show_id or "").isdigit():
        return []
    return parse_episodes(net.get_json(f"{API}/shows/{show_id}/episodes"), season)
