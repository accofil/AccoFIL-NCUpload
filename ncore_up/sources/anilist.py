"""AniList GraphQL API (POST only, no key): manga titles (romaji/English/native), English description, genres,
tags, staff, volumes, cover and the anilist.co page (proof link). Rate limit currently 30 requests/minute."""

from __future__ import annotations

import html
import re

from .. import net

API = "https://graphql.anilist.co"
QUERY = """query ($search: String, $type: MediaType) { Page(perPage: 5) { media(search: $search, type: $type, sort: SEARCH_MATCH) {
  id idMal title { romaji english native } synonyms description(asHtml: false) format status countryOfOrigin isAdult
  startDate { year month day } volumes chapters genres tags { name rank isMediaSpoiler }
  staff(perPage: 8) { edges { role node { name { full native } } } }
  coverImage { extraLarge large } siteUrl } } }"""
ORIGINAL_ROLE = re.compile(r"(?i)^\s*(story|art\b|original (creator|story|work)|author|illustration)")
LOCAL_ROLE = re.compile(r"(?i)translat|letter|touch-?up|assist|edit|supervis")


def clean_description(text: str) -> str:
    """AniList descriptions carry <i>/<br> and a trailing "(Source: ...)" note."""

    text = re.sub(r"(?i)<br\s*/?>", "\n", text or "")
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    text = re.sub(r"\n?\s*\((?:Source|Forrás)[^)]*\)\s*$", "", text.strip(), flags=re.I)
    text = re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", text))
    return text.strip()


def parse_media(media: dict) -> dict:
    """{"id", "title_romaji", "title_english", "title_native", "synonyms", "description", "format", "status",
    "country", "adult", "year", "volumes", "chapters", "genres", "tags", "authors", "staff", "cover_url", "url"}"""

    title = media.get("title") or {}
    staff = []
    for edge in (media.get("staff") or {}).get("edges") or []:
        name = ((edge.get("node") or {}).get("name") or {}).get("full") or ""
        if name:
            staff.append({"name": name, "role": edge.get("role") or ""})
    # creators only: "Story & Art (vols 1-41)", "Original Creator"; not "Touch-up Art & Lettering (English)", translators
    authors = [s["name"] for s in staff if ORIGINAL_ROLE.search(s["role"]) and not LOCAL_ROLE.search(s["role"])]
    cover = media.get("coverImage") or {}
    return {
        "id": media.get("id"),
        "title_romaji": title.get("romaji") or "",
        "title_english": title.get("english") or "",
        "title_native": title.get("native") or "",
        "synonyms": list(media.get("synonyms") or []),
        "description": clean_description(media.get("description") or ""),
        "format": media.get("format") or "",
        "status": media.get("status") or "",
        "country": media.get("countryOfOrigin") or "",
        "adult": bool(media.get("isAdult")),
        "year": str(((media.get("startDate") or {}).get("year")) or ""),
        "volumes": media.get("volumes") or 0,
        "chapters": media.get("chapters") or 0,
        "genres": list(media.get("genres") or []),
        "tags": [t["name"] for t in media.get("tags") or [] if t.get("name") and not t.get("isMediaSpoiler") and (t.get("rank") or 0) >= 60],
        "authors": list(dict.fromkeys(authors)),
        "staff": staff,
        "cover_url": cover.get("extraLarge") or cover.get("large") or "",
        "url": media.get("siteUrl") or (f"https://anilist.co/manga/{media.get('id')}" if media.get("id") else ""),
    }


def parse_search(data: dict) -> list[dict]:
    media = (((data or {}).get("data") or {}).get("Page") or {}).get("media") or []
    return [parse_media(m) for m in media if m]


def search(title: str, kind: str = "MANGA") -> list[dict]:
    """Up to 5 parse_media() hits in AniList's search order; [] on failure."""

    if not title or kind not in {"MANGA", "ANIME"}:
        return []
    data = net.post_json(API, json_body={"query": QUERY, "variables": {"search": title[:100], "type": kind}},
                         headers={"Accept": "application/json", "Content-Type": "application/json"})
    return parse_search(data or {})


def titles(item: dict) -> list[str]:
    return [t for t in [item.get("title_romaji"), item.get("title_english"), item.get("title_native")] + item.get("synonyms", []) if t]
