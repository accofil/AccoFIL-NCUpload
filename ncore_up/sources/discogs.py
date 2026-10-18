"""Discogs API: release/master search and details — tracklist (positions, durations), genres + styles (the best
source for nCore style tags), labels + catalogue numbers, formats (CD, Vinyl, File, DVD, Blu-ray ...), notes, images.

Public details can be read without a key; database search requires authentication. DISCOGS_TOKEN (a personal
access token) or DISCOGS_CONSUMER_KEY + DISCOGS_CONSUMER_SECRET are read from config.env. Create them at
https://www.discogs.com/settings/developers. Release titles, tracklists and identifiers are
CC0 data. A release URL (https://www.discogs.com/release/<id>) is a valid nCore proof link.
"""

from __future__ import annotations

import re
import time

from .. import keys, net

API = "https://api.discogs.com/"
SITE = "https://www.discogs.com/"
VIDEO_FORMATS = {"DVD", "Blu-ray", "VHS", "HD DVD", "Laserdisc", "VCD", "UMD", "DVDr", "BD-r"}
HEADINGS_EXTRA = re.compile(r"(?i)\b(bonus|extras?|features?|documentar|interview|videos?|tour stuff|screen films?|gallery|trailer|"
                            r"behind the scenes|making of|credits)\b")
HEADINGS_MAIN = re.compile(r"(?i)\b(dis[ck]|cd|dvd|blu-?ray|concert|live|show|set|part|one|two|three|four|[1-9])\b")


def _headers() -> dict:
    token = keys.get("DISCOGS_TOKEN")
    headers = {"Accept": "application/vnd.discogs.v2.discogs+json"}
    if token:
        headers["Authorization"] = f"Discogs token={token}"
    else:
        key, secret = keys.get("DISCOGS_CONSUMER_KEY"), keys.get("DISCOGS_CONSUMER_SECRET")
        if key and secret:
            headers["Authorization"] = f"Discogs key={key}, secret={secret}"
    return headers


def _get(path: str, params: dict | None = None):
    response = net.request("GET", API + path, params=params, headers=_headers())
    if response is None:
        return None
    try:
        remaining = int(response.headers.get("X-Discogs-Ratelimit-Remaining", "60"))
    except ValueError:
        remaining = 60
    if remaining <= 2 and not getattr(response, "from_cache", False):
        time.sleep(10)  # moving 60 s window: let a few requests expire instead of running into 429
    try:
        return response.json()
    except ValueError:
        return None


def duration_seconds(text: str) -> float:
    """'3:21' / '1:02:03' -> seconds; 0 when empty or odd."""

    parts = (text or "").strip().split(":")
    if not parts or not all(p.isdigit() for p in parts):
        return 0.0
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + int(part)
    return float(seconds)


def split_position(position: str) -> tuple[int, str]:
    """(disc, track) from '1-01', 'CD1-3', '2.5', 'A1' (side letters stay in the track part), '7' -> (0, '7')."""

    text = (position or "").strip()
    match = re.match(r"^(?:CD|DVD|BD|Disc|Disk)?\s*(\d{1,2})\s*[-.]\s*(\d{1,3}[a-z]?)$", text, re.I)
    if match:
        return int(match.group(1)), match.group(2)
    match = re.match(r"^(?:CD|DVD|BD)(\d{1,2})$", text, re.I)
    if match:
        return int(match.group(1)), ""
    return 0, text


def _artists(items: list) -> str:
    text = ""
    for artist in items or []:
        name = re.sub(r"\s+\(\d+\)$", "", artist.get("anv") or artist.get("name") or "")  # "Harvest (2)" -> "Harvest"
        join = (artist.get("join") or "").strip()
        text += name + (f" {join} " if join and join != "," else ", " if join == "," else "")
    return text.strip(" ,")


def _clean_name(name: str) -> str:
    return re.sub(r"\s+\(\d+\)$", "", (name or "").strip())


def parse_tracklist(items: list) -> list[dict]:
    """[{"position", "disc", "number", "title", "duration", "artist", "heading"}] with index tracks expanded and
    headings carried on as the section name of the tracks after them (heading rows themselves are dropped)."""

    tracks: list[dict] = []
    heading = ""
    extra = False  # inside a 'Bonus' / 'Features' section (its sub-headings included) until a main section heading

    def add(item: dict, parent_title: str = "") -> None:
        disc, number = split_position(item.get("position", ""))
        title = item.get("title", "")
        tracks.append({"position": item.get("position", ""), "disc": disc, "number": number,
                       "title": f"{parent_title}: {title}" if parent_title and title else title or parent_title,
                       "duration": duration_seconds(item.get("duration", "")), "artist": _artists(item.get("artists")),
                       "heading": heading, "extra": extra})

    for item in items or []:
        kind = item.get("type_", "track")
        if kind == "heading":
            heading = (item.get("title") or "").strip()
            if HEADINGS_EXTRA.search(heading):
                extra = True
            elif HEADINGS_MAIN.search(heading):
                extra = False
        elif kind == "index" and item.get("sub_tracks"):
            for sub in item["sub_tracks"]:
                if sub.get("type_", "track") == "track":
                    add(sub, item.get("title", ""))
        elif kind == "track":
            add(item)
    return tracks


def parse_release(data: dict) -> dict:
    """{"id", "title", "artist", "year", "released", "country", "genres", "styles", "labels": [{"name", "catno"}], "label",
    "catno", "formats": [{"name", "qty", "descriptions", "text"}], "format_names", "is_video", "tracks", "notes", "images",
    "barcodes", "master_id", "url", "data_quality"}"""

    if not data or not data.get("id"):
        return {}
    labels = [{"name": _clean_name(li.get("name", "")), "catno": (li.get("catno") or "").strip()} for li in data.get("labels") or []]
    labels = [li for li in labels if li["name"] or li["catno"]]
    formats = [{"name": f.get("name", ""), "qty": str(f.get("qty") or ""), "descriptions": list(f.get("descriptions") or []),
                "text": f.get("text") or ""} for f in data.get("formats") or []]
    images = sorted((i for i in data.get("images") or [] if i.get("uri")), key=lambda i: i.get("type") != "primary")
    release_id = data["id"]
    return {
        "id": release_id, "title": data.get("title", ""), "artist": _artists(data.get("artists")), "year": str(data.get("year") or "") if data.get("year") else "",
        "released": data.get("released") or "", "country": data.get("country") or "", "genres": list(data.get("genres") or []),
        "styles": list(data.get("styles") or []), "labels": labels, "label": next((li["name"] for li in labels if li["name"]), ""),
        "catno": next((li["catno"] for li in labels if li["catno"] and li["catno"].lower() != "none"), ""), "formats": formats,
        "format_names": [f["name"] for f in formats], "is_video": any(f["name"] in VIDEO_FORMATS for f in formats),
        "unofficial": any("Unofficial Release" in f["descriptions"] for f in formats),
        "tracks": parse_tracklist(data.get("tracklist")), "notes": (data.get("notes") or "").strip(),
        "images": [i["uri"] for i in images], "barcodes": [re.sub(r"\D", "", i.get("value", "")) for i in data.get("identifiers") or []
                                                         if i.get("type") == "Barcode" and re.sub(r"\D", "", i.get("value", ""))],
        "master_id": data.get("master_id") or 0, "url": f"{SITE}release/{release_id}", "data_quality": data.get("data_quality") or "",
    }


def parse_master(data: dict) -> dict:
    if not data or not data.get("id"):
        return {}
    return {
        "id": data["id"], "title": data.get("title", ""), "artist": _artists(data.get("artists")), "year": str(data.get("year") or "") if data.get("year") else "",
        "genres": list(data.get("genres") or []), "styles": list(data.get("styles") or []), "tracks": parse_tracklist(data.get("tracklist")),
        "main_release": data.get("main_release") or 0, "images": [i["uri"] for i in data.get("images") or [] if i.get("uri")],
        "url": f"{SITE}master/{data['id']}",
    }


def parse_search(data: dict) -> list[dict]:
    """[{"id", "type", "title", "artist", "album", "year", "country", "formats", "format_names", "labels", "catno", "genres", "styles",
    "barcodes", "master_id", "cover_image", "url"}] — "title" is "Artist - Album" as Discogs gives it."""

    hits = []
    for item in (data or {}).get("results") or []:
        title = item.get("title", "")
        artist, _, album = title.partition(" - ")
        kind = item.get("type", "")
        uri = item.get("uri") or ""
        hits.append({
            "id": item.get("id"), "type": kind, "title": title, "artist": _clean_name(artist) if album else "", "album": album or title,
            "year": str(item.get("year") or ""), "country": item.get("country") or "", "formats": list(item.get("format") or []),
            "format_names": [f.get("name", "") for f in item.get("formats") or []], "labels": [_clean_name(x) for x in item.get("label") or []],
            "catno": item.get("catno") or "", "genres": list(item.get("genre") or []), "styles": list(item.get("style") or []),
            "barcodes": [re.sub(r"\D", "", b) for b in item.get("barcode") or [] if re.sub(r"\D", "", b)], "master_id": item.get("master_id") or 0,
            "cover_image": item.get("cover_image") or "", "url": (SITE.rstrip("/") + uri) if uri.startswith("/") else uri or f"{SITE}{kind}/{item.get('id')}",
        })
    return hits


# --- fetch -------------------------------------------------------------------------------------------------

def search(*, q: str = "", artist: str = "", release_title: str = "", barcode: str = "", catno: str = "", label: str = "",
           year: str = "", format: str = "", kind: str = "release", per_page: int = 10) -> list[dict]:
    params = {k: v for k, v in {"q": q, "artist": artist, "release_title": release_title, "barcode": re.sub(r"\D", "", barcode),
                                "catno": catno, "label": label, "year": year, "format": format}.items() if v}
    if not params:
        return []
    params.update({"type": kind, "per_page": max(1, min(50, per_page))})
    return parse_search(_get("database/search", params) or {})


def release(release_id: int | str) -> dict:
    if not str(release_id).isdigit():
        return {}
    return parse_release(_get(f"releases/{release_id}") or {})


def master(master_id: int | str) -> dict:
    if not str(master_id).isdigit():
        return {}
    return parse_master(_get(f"masters/{master_id}") or {})


def release_id_from_url(url: str) -> str:
    match = re.search(r"discogs\.com/(?:[a-z]{2}/)?release/(\d+)", url or "")
    return match.group(1) if match else ""


def main_tracks(tracks: list[dict]) -> list[dict]:
    """Video releases: the concert itself, without sections headed 'Bonus', 'Features', 'Interview' ..."""

    return [t for t in tracks if not t.get("extra")]
