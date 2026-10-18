"""setlist.fm API: the songs played at a concert (sets and encores), for concert videos without a tracklist.

Needs SETLISTFM_KEY (https://www.setlist.fm/settings/api, header ``x-api-key``); without it every call returns [].
Searches by artist MBID (or name) plus date (dd-MM-yyyy), year, venue or city. "No result" is HTTP 404.
A setlist URL (https://www.setlist.fm/setlist/...) is a usable proof link for a concert recording.
"""

from __future__ import annotations

import re

from .. import keys, net

API = "https://api.setlist.fm/rest/1.0/"


def parse_setlists(data: dict) -> list[dict]:
    """[{"id", "date" (yyyy-mm-dd), "artist", "artist_mbid", "venue", "city", "country", "tour", "url",
    "sets": [{"name", "encore", "songs": [{"name", "info", "tape", "cover"}]}]}]"""

    result = []
    for item in (data or {}).get("setlist") or []:
        venue = item.get("venue") or {}
        city = venue.get("city") or {}
        day = item.get("eventDate") or ""
        match = re.fullmatch(r"(\d{2})-(\d{2})-(\d{4})", day)
        sets = []
        for block in ((item.get("sets") or {}).get("set")) or []:
            songs = [{"name": (s.get("name") or "").strip(), "info": s.get("info") or "", "tape": bool(s.get("tape")),
                      "cover": ((s.get("cover") or {}).get("name") or "")} for s in block.get("song") or []]
            sets.append({"name": block.get("name") or "", "encore": int(block.get("encore") or 0), "songs": songs})
        result.append({
            "id": item.get("id", ""), "date": f"{match.group(3)}-{match.group(2)}-{match.group(1)}" if match else day,
            "artist": (item.get("artist") or {}).get("name", ""), "artist_mbid": (item.get("artist") or {}).get("mbid", ""),
            "venue": venue.get("name", ""), "city": city.get("name", ""), "country": (city.get("country") or {}).get("name", ""),
            "tour": (item.get("tour") or {}).get("name", ""), "url": item.get("url", ""), "sets": sets,
        })
    return result


def songs(setlist: dict, with_tape: bool = False) -> list[dict]:
    """Ordered songs of a setlist: [{"title", "section"}]; section is '' for the main set, 'Ráadás' / 'Ráadás 2' for encores.
    Intro/outro tapes are left out unless ``with_tape``."""

    out = []
    for block in setlist.get("sets") or []:
        section = block["name"] or (("Ráadás" + (f" {block['encore']}" if block["encore"] > 1 else "")) if block["encore"] else "")
        for song in block["songs"]:
            if not song["name"] or (song["tape"] and not with_tape):
                continue
            out.append({"title": song["name"], "section": section})
    return out


def search(*, artist_mbid: str = "", artist_name: str = "", date: str = "", year: str = "", venue: str = "", city: str = "") -> list[dict]:
    """``date`` as yyyy-mm-dd or dd-mm-yyyy."""

    key = keys.get("SETLISTFM_KEY")
    if not key or not (artist_mbid or artist_name):
        return []
    match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", date or "")
    if match:
        date = f"{match.group(3)}-{match.group(2)}-{match.group(1)}"
    params = {k: v for k, v in {"artistMbid": artist_mbid, "artistName": "" if artist_mbid else artist_name, "date": date,
                                "year": year if not date else "", "venueName": venue, "cityName": city, "p": 1}.items() if v}
    data = net.get_json(API + "search/setlists", params=params, quiet=True,
                        headers={"x-api-key": key, "Accept": "application/json", "Accept-Language": "en"})
    return parse_setlists(data or {})
