"""MangaUpdates API v1 (no key for reads): series search (POST) and series details — genres, authors,
publishers with English volume counts, status, year, cover and the series page (proof link).
Acceptable-use policy: space requests reasonably, cache, credit MangaUpdates."""

from __future__ import annotations

import html
import re

from .. import net

API = "https://api.mangaupdates.com/v1"

net.PACE.setdefault("api.mangaupdates.com", 1.0)  # no numeric limit published; belongs in net.PACE


def _text(value: str) -> str:
    text = re.sub(r"(?i)<br\s*/?>", "\n", value or "")
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", text)).strip()


def parse_record(record: dict) -> dict:
    """A search hit's ``record`` or a /series/{id} response -> {"id", "title", "description", "type", "year",
    "genres", "authors", "artists", "publishers", "publisher_original", "publisher_english", "status", "volumes",
    "associated", "cover_url", "url"}."""

    if not record:
        return {}
    people = record.get("authors") or []
    publishers = record.get("publishers") or []
    status = record.get("status") or ""
    volumes = re.search(r"(\d+)\s+Volumes?", status)
    image = ((record.get("image") or {}).get("url") or {})
    return {
        "id": record.get("series_id"),
        "title": html.unescape(record.get("title") or ""),
        "description": _text(record.get("description") or ""),
        "type": record.get("type") or "",
        "year": str(record.get("year") or ""),
        "genres": [g.get("genre", "") for g in record.get("genres") or [] if g.get("genre")],
        "authors": list(dict.fromkeys(p["name"] for p in people if p.get("name") and p.get("type") == "Author")),
        "artists": list(dict.fromkeys(p["name"] for p in people if p.get("name") and p.get("type") == "Artist")),
        "publishers": [p.get("publisher_name", "") for p in publishers if p.get("publisher_name")],
        "publisher_original": next((p.get("publisher_name", "") for p in publishers if p.get("type") == "Original"), ""),
        "publisher_english": next((p.get("publisher_name", "") for p in publishers if p.get("type") == "English"), ""),
        "status": status,
        "volumes": int(volumes.group(1)) if volumes else 0,
        "associated": [a.get("title", "") for a in record.get("associated") or [] if a.get("title")],
        "cover_url": image.get("original") or image.get("thumb") or "",
        "url": record.get("url") or "",
    }


def parse_search(data: dict) -> list[dict]:
    return [parse_record(r.get("record") or {}) for r in (data or {}).get("results") or [] if r.get("record")]


def search(title: str, limit: int = 5) -> list[dict]:
    if not title:
        return []
    return parse_search(net.post_json(f"{API}/series/search", json_body={"search": title[:100], "perpage": limit}) or {})


def series(series_id) -> dict:
    if not re.fullmatch(r"\d{1,15}", str(series_id or "")):
        return {}
    return parse_record(net.get_json(f"{API}/series/{series_id}") or {})
