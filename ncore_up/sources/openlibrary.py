"""Open Library: editions by ISBN, works (description, subjects), title/author search and covers. No key.

Text comes back partly in Unicode NFD ("Magveto" + combining accent), so every string is normalized to NFC.
Descriptions are mostly English (Hungarian editions usually have none). Limits: about 1 request/s anonymous.
The work page (openlibrary.org/works/OL...W) is accepted as an nCore proof link.
"""

from __future__ import annotations

import re
import unicodedata

from .. import net

BASE = "https://openlibrary.org"
COVERS = "https://covers.openlibrary.org"
SEARCH_FIELDS = "key,title,subtitle,author_name,first_publish_year,isbn,language,publisher,cover_i,edition_key,subject,number_of_pages_median"

net.PACE.setdefault("openlibrary.org", 1.0)  # documented anonymous limit; belongs in net.PACE
net.PACE.setdefault("covers.openlibrary.org", 1.0)


def _nfc(value) -> str:
    return unicodedata.normalize("NFC", str(value or "")).strip()


def _int(value) -> int:
    """Page counts are ints, but user-edited records sometimes hold text ("xii, 282")."""

    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _text(value) -> str:
    """A description/notes field: plain string or {"type": "/type/text", "value": ...}; Markdown-ish cleaned."""

    if isinstance(value, dict):
        value = value.get("value", "")
    text = _nfc(value).replace("\r\n", "\n")
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.split(r"\n-{3,}\s*\n|\n\s*(?:Also contained in|See also):", text)[0]
    text = re.sub(r'^"(.+)"\s*--\s*[\w .,&\'-]{2,40}\.?$', r"\1", text.strip(), flags=re.S)  # '"Blurb"--Amazon.com.'
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def parse_edition_data(data: dict, isbn: str) -> dict:
    """/api/books?jscmd=data response for one ISBN -> {"title", "subtitle", "authors", "publishers", "date", "year",
    "pages", "subjects", "cover_url", "url", "edition"} ({} when unknown)."""

    item = (data or {}).get(f"ISBN:{isbn}") or {}
    if not item:
        return {}
    date = _nfc(item.get("publish_date"))
    match = re.search(r"\b(1[5-9]\d\d|20\d\d)\b", date)
    cover = item.get("cover") or {}
    return {
        "title": _nfc(item.get("title")),
        "subtitle": _nfc(item.get("subtitle")),
        "authors": [_nfc(a.get("name")) for a in item.get("authors") or [] if a.get("name")],
        "publishers": [_nfc(p.get("name")) for p in item.get("publishers") or [] if p.get("name")],
        "date": date,
        "year": match.group(1) if match else "",
        "pages": _int(item.get("number_of_pages")),
        "subjects": [_nfc(s.get("name")) for s in item.get("subjects") or [] if s.get("name")],
        "cover_url": cover.get("large") or cover.get("medium") or "",
        "url": (item.get("url") or "").replace("http://", "https://"),
        "edition": item.get("key", ""),
    }


def parse_edition(data: dict) -> dict:
    """/isbn/{isbn}.json (an edition record) -> {"edition", "work", "title", "subtitle", "series", "languages",
    "description", "pages", "publishers", "date"}."""

    if not data or not data.get("key"):
        return {}
    return {
        "edition": data.get("key", ""),
        "work": ((data.get("works") or [{}])[0] or {}).get("key", ""),
        "title": _nfc(data.get("title")),
        "subtitle": _nfc(data.get("subtitle")),
        "series": [_nfc(s) for s in data.get("series") or []],
        "languages": [str(lang.get("key", "")).rsplit("/", 1)[-1] for lang in data.get("languages") or [] if isinstance(lang, dict)],
        "description": _text(data.get("description")),
        "pages": _int(data.get("number_of_pages")),
        "publishers": [_nfc(p) for p in data.get("publishers") or []],
        "date": _nfc(data.get("publish_date")),
        "covers": [c for c in data.get("covers") or [] if isinstance(c, int) and c > 0],
    }


def parse_work(data: dict) -> dict:
    """/works/{id}.json -> {"work", "title", "description", "subjects", "covers", "url"}."""

    if not data or not data.get("key"):
        return {}
    return {
        "work": data["key"],
        "title": _nfc(data.get("title")),
        "description": _text(data.get("description")),
        "subjects": [_nfc(s) for s in data.get("subjects") or []],
        "covers": [c for c in data.get("covers") or [] if isinstance(c, int) and c > 0],
        "url": BASE + data["key"],
    }


def parse_search(data: dict) -> list[dict]:
    """search.json docs -> [{"work", "title", "subtitle", "authors", "year", "isbns", "languages", "publishers",
    "subjects", "cover_url", "url"}]."""

    results = []
    for doc in (data or {}).get("docs") or []:
        cover = doc.get("cover_i")
        results.append({
            "work": doc.get("key", ""),
            "title": _nfc(doc.get("title")),
            "subtitle": _nfc(doc.get("subtitle")),
            "authors": [_nfc(a) for a in doc.get("author_name") or []],
            "year": str(doc.get("first_publish_year") or ""),
            "isbns": [i for i in doc.get("isbn") or [] if isinstance(i, str) and len(i) == 13],
            "languages": list(doc.get("language") or []),
            "publishers": [_nfc(p) for p in doc.get("publisher") or []][:5],
            "subjects": [_nfc(s) for s in doc.get("subject") or []][:15],
            "pages": _int(doc.get("number_of_pages_median")),
            "cover_url": f"{COVERS}/b/id/{cover}-L.jpg" if cover else "",
            "url": BASE + doc["key"] if doc.get("key") else "",
        })
    return results


def work(key: str) -> dict:
    if not re.fullmatch(r"/works/OL\d+W", key or ""):
        return {}
    return parse_work(net.get_json(f"{BASE}{key}.json") or {})


def by_isbn(isbn: str) -> dict:
    """Edition + work of an ISBN-13: {"title", "subtitle", "authors", "publishers", "date", "year", "pages",
    "subjects", "series", "languages", "description", "cover_url", "url" (work page), "edition", "work"}; {} when unknown."""

    if not re.fullmatch(r"\d{13}|\d{9}[\dX]", isbn or ""):
        return {}
    data = parse_edition_data(net.get_json(f"{BASE}/api/books", params={"bibkeys": f"ISBN:{isbn}", "format": "json", "jscmd": "data"}) or {}, isbn)
    if not data:
        return {}
    edition = parse_edition(net.get_json(f"{BASE}/isbn/{isbn}.json", quiet=True) or {})
    info = work(edition.get("work", "")) if edition else {}
    data.update({
        "series": edition.get("series", []),
        "languages": edition.get("languages", []),
        "description": info.get("description") or edition.get("description", ""),  # the work has the canonical blurb
        "work": edition.get("work", ""),
        "subjects": data["subjects"] or info.get("subjects", []),
    })
    if info.get("url"):
        data["url"] = info["url"]
    if not data["cover_url"] and (edition.get("covers") or info.get("covers")):
        data["cover_url"] = f"{COVERS}/b/id/{(edition.get('covers') or info.get('covers'))[0]}-L.jpg"
    return data


def search(title: str, author: str = "", lang: str = "", limit: int = 5) -> list[dict]:
    """Works by title (+ author; ``lang`` as a MARC code like "hun"/"eng")."""

    if not title:
        return []
    params = {"title": title, "limit": limit, "fields": SEARCH_FIELDS}
    if author:
        params["author"] = author
    if lang:
        params["language"] = lang
    return parse_search(net.get_json(f"{BASE}/search.json", params=params) or {})


def cover_url(isbn: str) -> str:
    """Cover by ISBN; ``default=false`` makes a missing cover a 404 instead of a blank image."""

    return f"{COVERS}/b/isbn/{isbn}-L.jpg?default=false" if isbn else ""
