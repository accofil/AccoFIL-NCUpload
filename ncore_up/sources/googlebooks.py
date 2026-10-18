"""Google Books API v1: volumes by ISBN or title/author; Hungarian editions with ``langRestrict=hu``.

Needs GOOGLE_BOOKS_KEY (keyless calls hit a daily quota of 0, so without a key nothing is requested).
Gives title, subtitle, authors, publisher, publishedDate, description (HTML), categories (BISAC-like, English),
pageCount, language, imageLinks and the volume page, which is a valid nCore proof link.
"""

from __future__ import annotations

import html
import re
import unicodedata

from .. import keys, net

# Canonical service root in Google's Books v1 discovery document (2026-10-13).
API = "https://books.googleapis.com/books/v1/volumes"


def _clean(text) -> str:
    return unicodedata.normalize("NFC", str(text or "")).strip()


def html_text(value: str) -> str:
    text = re.sub(r"(?i)</p>|</div>|</h\d>", "\n\n", value or "")
    text = re.sub(r"(?i)<br\s*/?>|</li>", "\n", text)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", re.sub(r" *\n *", "\n", text)).strip()


def parse_volume(item: dict) -> dict:
    """{"id", "title", "subtitle", "authors", "publisher", "date", "year", "description", "categories", "pages",
    "language", "isbn13", "isbn10", "cover_url", "url", "print_type"}"""

    info = item.get("volumeInfo") or {}
    ids = {i.get("type"): i.get("identifier", "") for i in info.get("industryIdentifiers") or []}
    links = info.get("imageLinks") or {}
    cover = next((links[k] for k in ("extraLarge", "large", "medium", "small", "thumbnail", "smallThumbnail") if links.get(k)), "")
    cover = re.sub(r"&edge=curl", "", cover.replace("http://", "https://"))
    date = info.get("publishedDate") or ""
    volume_id = item.get("id", "")
    return {
        "id": volume_id,
        "title": _clean(info.get("title")),
        "subtitle": _clean(info.get("subtitle")),
        "authors": [_clean(a) for a in info.get("authors") or [] if a],
        "publisher": _clean(info.get("publisher")).strip('"'),
        "date": date,
        "year": date[:4] if re.match(r"\d{4}", date) else "",
        "description": html_text(info.get("description") or ""),
        "categories": [_clean(c) for c in info.get("categories") or []],
        "pages": int(info.get("pageCount") or 0),
        "language": (info.get("language") or "").lower(),
        "isbn13": ids.get("ISBN_13", ""),
        "isbn10": ids.get("ISBN_10", ""),
        "cover_url": cover,
        "url": f"https://books.google.com/books?id={volume_id}" if volume_id else (info.get("canonicalVolumeLink") or info.get("infoLink") or ""),
        "print_type": info.get("printType", ""),
    }


def parse_search(data: dict) -> list[dict]:
    return [parse_volume(item) for item in (data or {}).get("items") or [] if item.get("volumeInfo")]


def search(query: str, *, lang: str = "", max_results: int = 10, print_type: str = "books") -> list[dict]:
    """parse_volume() of the hits of a raw query ("isbn:...", 'intitle:"..." inauthor:...'); [] without a key."""

    key = keys.get("GOOGLE_BOOKS_KEY")
    if not key or not query.strip():
        return []
    params = {"q": query, "maxResults": max(1, min(40, max_results)), "printType": print_type.upper(), "key": key}
    if lang:
        params["langRestrict"] = lang
    return parse_search(net.get_json(API, params=params) or {})


def by_isbn(isbn: str) -> dict:
    """The volume with this ISBN-10/13, {} when none."""

    digits = re.sub(r"[^0-9X]", "", (isbn or "").upper())
    if len(digits) not in (10, 13):
        return {}
    # The API can return an empty list for its documented field operators while
    # the equivalent plain query works. A plain ISBN search also finds books
    # merely citing that number, so accept only an exact identifier in either path.
    for query in (f"isbn:{digits}", digits):
        hits = [h for h in search(query, max_results=5) if digits in (h["isbn13"], h["isbn10"])]
        if hits:
            return hits[0]
    return {}


def _phrase(text: str) -> str:
    text = re.sub(r'["\\:]', " ", text or "").strip()
    return f'"{text}"' if " " in text else text


def find(title: str, author: str = "", *, lang: str = "", max_results: int = 10) -> list[dict]:
    """Volumes by title (+ author surname); ``lang="hu"`` for Hungarian editions."""

    if not (title or "").strip():
        return []
    query = f"intitle:{_phrase(title)}"
    if author:
        query += f" inauthor:{_phrase(author)}"
    hits = search(query, lang=lang, max_results=max_results)
    if hits:
        return hits
    # Keep the language filter and the caller's title/author confidence checks.
    # Do not turn an unrelated first plain-query result into a confirmed match.
    plain = re.sub(r'["\\:]', " ", f"{title} {author}")
    return search(" ".join(plain.split()), lang=lang, max_results=max_results)


def volume(volume_id: str) -> dict:
    """One volume by id (full description); {} without a key or on failure."""

    key = keys.get("GOOGLE_BOOKS_KEY")
    if not key or not re.fullmatch(r"[\w-]{6,20}", volume_id or ""):
        return {}
    data = net.get_json(f"{API}/{volume_id}", params={"key": key}) or {}
    return parse_volume(data) if data.get("volumeInfo") else {}
