"""ComicVine API (English Western comics): volumes (series) and issues — name, start year, publisher, deck,
description, cover, credits and the site page (proof link).

Needs COMICVINE_KEY (free account). Limits: 200 requests per resource per hour plus velocity detection, so
``net.PACE`` spaces calls 18 s apart; every lookup here costs one or two requests. Terms: non-commercial use,
link back to Comic Vine.
"""

from __future__ import annotations

import html
import re

from .. import keys, net

API = "https://comicvine.gamespot.com/api"
VOLUME_FIELDS = "id,name,start_year,publisher,deck,description,image,site_detail_url,count_of_issues"
ISSUE_FIELDS = "id,name,issue_number,cover_date,store_date,deck,description,image,site_detail_url,volume,person_credits"


def _text(value: str) -> str:
    """Plain text of a ComicVine description, without the wiki sections ("Collected Editions", credits tables)."""

    text = re.sub(r"(?is)<(figure|table)\b.*?</\1>", "", value or "")
    text = re.split(r"(?i)<h[2-4]\b", text)[0] if re.search(r"(?i)^\s*<p\b", text) else text
    text = re.sub(r"(?i)</p>|</h\d>", "\n\n", text)
    text = re.sub(r"(?i)<br\s*/?>|</li>", "\n", text)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", text)).strip()


def _image(item: dict) -> str:
    image = item.get("image") or {}
    return image.get("original_url") or image.get("super_url") or image.get("medium_url") or ""


def parse_volume(item: dict) -> dict:
    """{"id", "name", "year", "publisher", "deck", "description", "issues", "cover_url", "url"}"""

    return {
        "id": item.get("id"),
        "name": item.get("name") or "",
        "year": str(item.get("start_year") or ""),
        "publisher": ((item.get("publisher") or {}).get("name")) or "",
        "deck": _text(item.get("deck") or ""),
        "description": _text(item.get("description") or ""),
        "issues": int(item.get("count_of_issues") or 0),
        "cover_url": _image(item),
        "url": item.get("site_detail_url") or "",
    }


def parse_issue(item: dict) -> dict:
    """{"id", "name", "number", "date", "deck", "description", "volume", "volume_id", "credits", "cover_url", "url"}"""

    volume = item.get("volume") or {}
    credits = [{"name": p.get("name", ""), "role": p.get("role", "")} for p in item.get("person_credits") or [] if p.get("name")]
    return {
        "id": item.get("id"),
        "name": item.get("name") or "",
        "number": str(item.get("issue_number") or ""),
        "date": item.get("cover_date") or item.get("store_date") or "",
        "deck": _text(item.get("deck") or ""),
        "description": _text(item.get("description") or ""),
        "volume": volume.get("name") or "",
        "volume_id": volume.get("id"),
        "credits": credits,
        "cover_url": _image(item),
        "url": item.get("site_detail_url") or "",
    }


def _call(path: str, **params) -> dict:
    key = keys.get("COMICVINE_KEY")
    if not key:
        return {}
    data = net.get_json(f"{API}/{path}", params={"api_key": key, "format": "json", **params}) or {}
    return data if data.get("status_code") == 1 else {}


def search_volumes(name: str, limit: int = 10) -> list[dict]:
    if not name:
        return []
    data = _call("search/", resources="volume", query=name[:100], field_list=VOLUME_FIELDS, limit=limit)
    return [parse_volume(item) for item in data.get("results") or []]


def issue(volume_id, number: str) -> dict:
    """Issue ``number`` of a volume; {} when unknown or without a key."""

    if not re.fullmatch(r"\d{1,9}", str(volume_id or "")) or not re.fullmatch(r"[\w.\-]{1,10}", number or ""):
        return {}
    data = _call("issues/", filter=f"volume:{volume_id},issue_number:{number}", field_list=ISSUE_FIELDS, limit=1)
    results = data.get("results") or []
    return parse_issue(results[0]) if results else {}


def writers(item: dict) -> list[str]:
    return [c["name"] for c in item.get("credits") or [] if re.search(r"(?i)writer", c.get("role", ""))]


def artists(item: dict) -> list[str]:
    return [c["name"] for c in item.get("credits") or [] if re.search(r"(?i)artist|penciler|penciller", c.get("role", ""))]
