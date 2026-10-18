"""Spotify Web API (client credentials): album search and album details — tracklist, release date, cover, UPC.

OFF unless both SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET are set (https://developer.spotify.com/dashboard).
Last-resort fallback. Development Mode restrictions and available fields depend on the application's API access;
search returns at most 10 results. Developer Policy III.9 restricts transfer to another service, with exceptions
for personal data and user playlist metadata. Successful authentication does not establish permission to export
catalogue metadata. See docs/continuation-0.4.2.md and https://developer.spotify.com/policy.

The app token (client credentials, 1 hour) is cached in ~/.cache/ncore-up/spotify-token.json.
"""

from __future__ import annotations

import base64
import json
import re
import time

from .. import keys, net
from ..workflow import write_json

TOKEN_URL = "https://accounts.spotify.com/api/token"
API = "https://api.spotify.com/v1/"


def enabled() -> bool:
    return bool(keys.get("SPOTIFY_CLIENT_ID") and keys.get("SPOTIFY_CLIENT_SECRET"))


def _token_file():
    return net.cache_dir() / "spotify-token.json"


def token(force_refresh: bool = False) -> str:
    if not enabled():
        return ""
    client_id, secret = keys.get("SPOTIFY_CLIENT_ID"), keys.get("SPOTIFY_CLIENT_SECRET")
    try:
        cached = json.loads(_token_file().read_text(encoding="utf-8"))
        if not force_refresh and cached.get("client_id") == client_id and cached.get("expires", 0) > time.time() + 60:
            _token_file().chmod(0o600)
            return cached["access_token"]
    except (OSError, ValueError, KeyError):
        pass
    basic = base64.b64encode(f"{client_id}:{secret}".encode("utf-8")).decode("ascii")
    data = net.post_json(TOKEN_URL, data={"grant_type": "client_credentials"}, headers={"Authorization": f"Basic {basic}"}, expire=0) or {}
    if not data.get("access_token"):
        return ""
    try:
        write_json(_token_file(), {"client_id": client_id, "access_token": data["access_token"],
                                  "expires": time.time() + int(data.get("expires_in", 3600))})
    except OSError:
        pass
    return data["access_token"]


def _get(path: str, params: dict | None = None) -> dict:
    access = token()
    if not access:
        return {}
    for attempt in range(2):
        before = len(net.FAILURES)
        response = net.get(API + path, params=params, headers={"Authorization": f"Bearer {access}"}, retries=0, expire=0)
        if response is not None:
            try:
                data = response.json()
            except ValueError:
                return {}
            return data if isinstance(data, dict) else {}
        # A cached/revoked token must not disable the source until its old expiry.
        if attempt == 0 and len(net.FAILURES) > before and net.FAILURES[-1] == "api.spotify.com: HTTP 401":
            access = token(force_refresh=True)
            if access:
                continue
        break
    return {}


def _cover(images: list) -> str:
    """The image closest to 300 px (nCore's infobar limit)."""

    images = [i for i in images or [] if i.get("url")]
    if not images:
        return ""
    return min(images, key=lambda i: abs(int(i.get("width") or 0) - 300))["url"]


def parse_search(data: dict) -> list[dict]:
    """[{"id", "name", "artist", "release_date", "total_tracks", "album_type", "cover_url", "url"}]"""

    return [{"id": a.get("id", ""), "name": a.get("name", ""), "artist": ", ".join(x.get("name", "") for x in a.get("artists") or []),
             "release_date": a.get("release_date", ""), "total_tracks": int(a.get("total_tracks") or 0), "album_type": a.get("album_type", ""),
             "cover_url": _cover(a.get("images")), "url": (a.get("external_urls") or {}).get("spotify", "")}
            for a in ((data or {}).get("albums") or {}).get("items") or [] if a]


def parse_album(data: dict) -> dict:
    """{"id", "name", "artist", "release_date", "total_tracks", "tracks": [{"disc", "number", "title", "length", "artist"}],
    "cover_url", "upc", "copyright", "url"}"""

    if not data or not data.get("id"):
        return {}
    artist = ", ".join(x.get("name", "") for x in data.get("artists") or [])
    tracks = []
    for track in (data.get("tracks") or {}).get("items") or []:
        track_artist = ", ".join(x.get("name", "") for x in track.get("artists") or [])
        tracks.append({"disc": int(track.get("disc_number") or 1), "number": int(track.get("track_number") or 0), "title": track.get("name", ""),
                       "length": int(track.get("duration_ms") or 0) / 1000, "artist": track_artist if track_artist != artist else ""})
    return {
        "id": data["id"], "name": data.get("name", ""), "artist": artist, "release_date": data.get("release_date", ""),
        "total_tracks": int(data.get("total_tracks") or len(tracks)), "tracks": tracks, "cover_url": _cover(data.get("images")),
        "upc": (data.get("external_ids") or {}).get("upc", ""), "copyright": "; ".join(c.get("text", "") for c in data.get("copyrights") or []),
        "url": (data.get("external_urls") or {}).get("spotify", ""),
        "tracks_complete": len(tracks) == int(data.get("total_tracks") or len(tracks)),
    }


def search_albums(artist: str, album: str, market: str = "HU", limit: int = 10) -> list[dict]:
    if not enabled() or not album:
        return []
    query = f"album:{album}" + (f" artist:{artist}" if artist else "")
    return parse_search(_get("search", {"q": query, "type": "album", "market": market, "limit": max(1, min(10, limit))}))


def album(album_id: str, market: str = "HU") -> dict:
    if not enabled() or not album_id:
        return {}
    album_id = normalize_album_id(album_id)
    if not album_id:
        return {}
    raw = _get(f"albums/{album_id}", {"market": market})
    if not raw:
        return {}
    tracks = dict(raw.get("tracks") or {})
    items = list(tracks.get("items") or [])
    expected = int(raw.get("total_tracks") or tracks.get("total") or len(items))
    # Never follow an arbitrary 'next' URL with the bearer token. Request only
    # this album's official endpoint, with a bounded and advancing offset.
    for _ in range(20):
        if len(items) >= expected:
            break
        offset = len(items)
        page = _get(f"albums/{album_id}/tracks", {"market": market, "limit": 50, "offset": offset})
        more = page.get("items") or []
        if not more or int(page.get("offset", offset)) != offset:
            break
        positions = {(x.get("disc_number"), x.get("track_number")) for x in items}
        if any((x.get("disc_number"), x.get("track_number")) in positions for x in more):
            break
        items.extend(more)
    raw = {**raw, "total_tracks": expected, "tracks": {**tracks, "items": items}}
    parsed = parse_album(raw)
    if not parsed.get("tracks_complete"):
        net.FAILURES.append("Spotify: hiányos tracklista; az album nem használható automatikus egyezésként")
    return parsed


def normalize_album_id(value: str) -> str:
    value = str(value or "").strip()
    match = re.fullmatch(r"(?:https://open\.spotify\.com/(?:intl-[a-z]{2}/)?album/|spotify:album:)?([A-Za-z0-9]{22})(?:\?[^\s]*)?", value)
    return match[1] if match else ""
