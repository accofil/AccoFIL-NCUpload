"""Apple iTunes Search/Lookup API for iOS apps (no key; Apple asks for ~20 calls per minute, net paces it).

Hungarian text needs ``lang=hu_hu``: ``country=hu`` alone returns the English listing ("Google Maps"), with both it
is "Google Térkép" and the Hungarian description. ``languageCodesISO2A`` lists the app's UI languages ("HU").
Proof link: ``trackViewUrl`` (apps.apple.com/hu/app/...).
"""

from __future__ import annotations

import re

from .. import net
from .googleplay import robots_allows

LOOKUP = "https://itunes.apple.com/lookup"
SEARCH = "https://itunes.apple.com/search"


def parse_result(item: dict) -> dict:
    """One software result → {"track_id", "bundle_id", "name", "description", "version", "release_notes", "seller",
    "developer", "genre", "genres", "release_date", "updated", "min_os", "languages", "hungarian", "size", "price",
    "rating", "ratings", "content_rating", "icon", "screenshots", "ipad_screenshots", "devices", "url"}."""

    if not isinstance(item, dict) or item.get("kind") not in (None, "software", "mac-software"):
        return {}
    languages = [str(x).lower() for x in item.get("languageCodesISO2A") or []]
    url = str(item.get("trackViewUrl") or "")
    return {
        "track_id": item.get("trackId"),
        "bundle_id": item.get("bundleId", ""),
        "name": (item.get("trackName") or "").strip(),
        "description": (item.get("description") or "").strip(),
        "version": item.get("version", ""),
        "release_notes": (item.get("releaseNotes") or "").strip(),
        "seller": item.get("sellerName", ""),
        "developer": item.get("artistName", ""),
        "genre": item.get("primaryGenreName", ""),
        "genres": list(item.get("genres") or []),
        "release_date": (item.get("releaseDate") or "")[:10],
        "updated": (item.get("currentVersionReleaseDate") or "")[:10],
        "min_os": item.get("minimumOsVersion", ""),
        "languages": languages,
        "hungarian": "hu" in languages,
        "size": int(item["fileSizeBytes"]) if str(item.get("fileSizeBytes") or "").isdigit() else 0,
        "price": item.get("formattedPrice", ""),
        "rating": item.get("averageUserRating"),
        "ratings": item.get("userRatingCount"),
        "content_rating": item.get("trackContentRating") or item.get("contentAdvisoryRating", ""),
        "icon": item.get("artworkUrl512") or item.get("artworkUrl100", ""),
        "screenshots": list(item.get("screenshotUrls") or []),
        "ipad_screenshots": list(item.get("ipadScreenshotUrls") or []),
        "devices": list(item.get("supportedDevices") or []),
        "url": re.sub(r"[?&]uo=\d+", "", url),
    }


def lookup(bundle_id: str, country: str = "hu", lang: str = "hu_hu") -> dict:
    """App Store record of a CFBundleIdentifier; {} when not (or no longer) in the store."""

    if not re.fullmatch(r"[\w.-]+", bundle_id or ""):
        return {}
    data = net.get_json(LOOKUP, params={"bundleId": bundle_id, "country": country, "lang": lang}, quiet=True) or {}
    results = data.get("results") if isinstance(data, dict) and isinstance(data.get("results"), list) else []
    return parse_result(results[0]) if results else {}


def search(term: str, country: str = "hu", lang: str = "hu_hu", limit: int = 5) -> list[dict]:
    """Name search; empty while itunes.apple.com's robots.txt disallows /search* (it did on 2026-10-16)."""

    if not (term or "").strip() or not robots_allows(SEARCH):
        return []
    data = net.get_json(SEARCH, params={"term": term, "country": country, "entity": "software", "limit": limit, "lang": lang},
                        quiet=True) or {}
    return [r for r in (parse_result(x) for x in data.get("results") or []) if r]
