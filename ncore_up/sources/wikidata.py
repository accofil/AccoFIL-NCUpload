"""Wikidata: exact ID bridges (IMDb -> PORT.hu, MusicBrainz -> huwiki, Steam -> huwiki ...) and Hungarian labels.

Data is CC0. A Wikidata or Wikipedia URL is never used as nCore proof link (the wiki forbids online encyclopedias).

Useful properties: P345 IMDb, P905 PORT film ID, P4947 TMDB movie, P4983 TMDB TV, P434 MusicBrainz artist,
P436 MusicBrainz release group, P1954 Discogs master, P1733 Steam app, P2725 GOG app, P5794 IGDB game,
P212 ISBN-13, P957 ISBN-10, P856 official website, P154 logo image, P178 developer, P123 publisher.
"""

from __future__ import annotations

import re

from .. import net

SPARQL = "https://query.wikidata.org/sparql"
ENTITY = "https://www.wikidata.org/wiki/Special:EntityData/{qid}.json"

PROP = {
    "imdb": "P345", "porthu": "P905", "tmdb_movie": "P4947", "tmdb_tv": "P4983", "mb_artist": "P434", "mb_release_group": "P436",
    "discogs_master": "P1954", "steam": "P1733", "gog": "P2725", "igdb": "P5794", "isbn13": "P212", "isbn10": "P957",
    "website": "P856", "logo": "P154", "developer": "P178", "publisher": "P123",
}
_SAFE_VALUE = re.compile(r"^[\w.:\-/ ]{1,80}$")


def find_by(prop: str, value: str) -> str:
    """QID of the item whose ``prop`` (a key of PROP or a raw P-number) equals ``value``; '' when none/ambiguous."""

    pid = PROP.get(prop, prop)
    if not re.fullmatch(r"P\d+", pid) or not _SAFE_VALUE.match(value or ""):
        return ""
    query = f'SELECT ?item WHERE {{ ?item wdt:{pid} "{value}" . }} LIMIT 2'
    data = net.get_json(SPARQL, params={"query": query, "format": "json"}, headers={"Accept": "application/sparql-results+json"}) or {}
    rows = (data.get("results") or {}).get("bindings") or []
    if len(rows) != 1:
        return ""
    return rows[0]["item"]["value"].rsplit("/", 1)[-1]


def entity(qid: str) -> dict:
    if not re.fullmatch(r"Q\d+", qid or ""):
        return {}
    data = net.get_json(ENTITY.format(qid=qid)) or {}
    return (data.get("entities") or {}).get(qid) or {}


def parse_entity(item: dict) -> dict:
    """{"qid", "label_hu", "label_en", "description_hu", "huwiki", "enwiki", "claims": {pid: [values]}}"""

    def text(group: str, lang: str) -> str:
        return ((item.get(group) or {}).get(lang) or {}).get("value", "")

    claims: dict[str, list[str]] = {}
    for pid, statements in (item.get("claims") or {}).items():
        values = []
        for statement in statements:
            value = ((statement.get("mainsnak") or {}).get("datavalue") or {}).get("value")
            if isinstance(value, dict):
                value = value.get("id") or value.get("text") or value.get("time") or ""
            if value:
                values.append(str(value))
        if values:
            claims[pid] = values
    sitelinks = item.get("sitelinks") or {}
    return {
        "qid": item.get("id", ""),
        "label_hu": text("labels", "hu"),
        "label_en": text("labels", "en"),
        "description_hu": text("descriptions", "hu"),
        "huwiki": (sitelinks.get("huwiki") or {}).get("title", ""),
        "enwiki": (sitelinks.get("enwiki") or {}).get("title", ""),
        "claims": claims,
    }


def lookup(prop: str, value: str) -> dict:
    """parse_entity() of the item with ``prop == value``, {} when not found."""

    qid = find_by(prop, value)
    return parse_entity(entity(qid)) if qid else {}


def claim(info: dict, prop: str) -> str:
    values = (info.get("claims") or {}).get(PROP.get(prop, prop)) or []
    return values[0] if values else ""


def commons_file_url(filename: str, width: int = 300) -> str:
    """Thumbnail URL of a Wikimedia Commons file (e.g. a P154 logo)."""

    if not filename:
        return ""
    import hashlib
    from urllib.parse import quote

    name = filename.replace(" ", "_")
    digest = hashlib.md5(name.encode("utf-8")).hexdigest()
    thumb = f"{width}px-{name}" + (".png" if name.lower().endswith(".svg") else "")
    return f"https://upload.wikimedia.org/wikipedia/commons/thumb/{digest[0]}/{digest[:2]}/{quote(name)}/{quote(thumb)}"


def search(name: str, language: str = "en", limit: int = 5) -> list[dict]:
    """wbsearchentities: [{"qid", "label", "description"}] for a name (to be confirmed by the caller)."""

    if not name:
        return []
    data = net.get_json("https://www.wikidata.org/w/api.php", params={
        "action": "wbsearchentities", "search": name, "language": language, "uselang": language, "type": "item",
        "limit": limit, "format": "json"}) or {}
    return [{"qid": hit.get("id", ""), "label": hit.get("label", ""), "description": hit.get("description", "")}
            for hit in data.get("search") or [] if isinstance(hit, dict)]
