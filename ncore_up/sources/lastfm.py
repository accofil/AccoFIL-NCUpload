"""Last.fm API: crowd tags with counts (album.getTopTags / artist.getTopTags, incl. decade tags such as '80s') and the
album wiki summary (album.getInfo, ``lang=hu`` gives the Hungarian wiki when one exists).

Needs LASTFM_KEY (https://www.last.fm/api/account/create, the shared secret is not needed); without it every call
returns empty. Non-commercial use only; Last.fm images are excluded from the licence, so they are never used.
"""

from __future__ import annotations

import html
import re

from .. import keys, net

API = "https://ws.audioscrobbler.com/2.0/"
IGNORED_TAGS = {"seen live", "favorite", "favourite", "favorites", "albums i own", "my top", "awesome", "love", "beautiful", "best",
                "male vocalists", "female vocalists", "hungarian", "magyar", "hungary", "british", "american", "english", "uk", "usa"}


def _call(method: str, **params) -> dict:
    key = keys.get("LASTFM_KEY")
    if not key:
        return {}
    params.update({"method": method, "api_key": key, "format": "json"})
    data = net.get_json(API, params={k: v for k, v in params.items() if v not in (None, "")})
    if not isinstance(data, dict) or "error" in data:
        return {}
    return data


def clean_wiki(text: str) -> str:
    """Wiki text without the trailing '<a href="https://www.last.fm/...">Read more on Last.fm</a>' and the licence note."""

    text = re.sub(r'(?is)\s*<a\s+href="https?://www\.last\.fm/[^"]*"[^>]*>.*?</a>\.?', "", text or "")
    text = re.sub(r"(?is)\s*User-contributed text is available under the Creative Commons.*$", "", text)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    return re.sub(r"[ \t]+", " ", text).strip()


def parse_tags(data: dict) -> list[tuple[str, int]]:
    """[(tag, count)] of album.getTopTags / artist.getTopTags / album.getInfo (getInfo tags have no count: 100 each)."""

    holder = (data or {}).get("toptags") or ((data or {}).get("album") or {}).get("tags") or {}
    tags = holder.get("tag") or []
    if isinstance(tags, dict):
        tags = [tags]
    out = []
    for tag in tags:
        name = str(tag.get("name", "")).strip().lower()
        if not name or name in IGNORED_TAGS:
            continue
        try:
            count = int(tag.get("count", 100))
        except (TypeError, ValueError):
            count = 0
        out.append((name, count))
    return out


def parse_album(data: dict) -> dict:
    """{"name", "artist", "mbid", "url", "tags", "tracks": [{"rank", "name", "duration"}], "wiki_summary", "wiki_content", "published"}"""

    album = (data or {}).get("album") or {}
    if not album:
        return {}
    tracks = (album.get("tracks") or {}).get("track") or []
    if isinstance(tracks, dict):
        tracks = [tracks]
    wiki = album.get("wiki") or {}
    return {
        "name": album.get("name", ""), "artist": album.get("artist", ""), "mbid": album.get("mbid", ""), "url": album.get("url", ""),
        "tags": parse_tags(data), "tracks": [{"rank": int((t.get("@attr") or {}).get("rank") or i + 1), "name": t.get("name", ""),
                                              "duration": int(t.get("duration") or 0)} for i, t in enumerate(tracks)],
        "wiki_summary": clean_wiki(wiki.get("summary", "")), "wiki_content": clean_wiki(wiki.get("content", "")),
        "published": wiki.get("published", ""),
    }


def album_info(artist: str = "", album: str = "", mbid: str = "", lang: str = "hu") -> dict:
    if not mbid and not (artist and album):
        return {}
    return parse_album(_call("album.getinfo", artist=artist, album=album, mbid=mbid, autocorrect=1, lang=lang))


def album_top_tags(artist: str = "", album: str = "", mbid: str = "") -> list[tuple[str, int]]:
    if not mbid and not (artist and album):
        return []
    return parse_tags(_call("album.gettoptags", artist=artist, album=album, mbid=mbid, autocorrect=1))


def artist_top_tags(artist: str = "", mbid: str = "") -> list[tuple[str, int]]:
    if not artist and not mbid:
        return []
    return parse_tags(_call("artist.gettoptags", artist=artist, mbid=mbid, autocorrect=1))
