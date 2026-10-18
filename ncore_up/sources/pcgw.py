"""PCGamingWiki (MediaWiki API, no login): system requirements and the language table (Hungarian interface /
subtitles / audio, fan translations) of a game, found by Steam app id, GOG id or title.

    w/api.php?action=idlookup&field=steamappid|gogid&value=<id>   -> idlookup[0].title.Page
    w/api.php?action=query&list=search&srsearch=<title>            -> search[].title
    w/api.php?action=parse&page=<Page>&prop=wikitext&redirects=1  -> {{System requirements}}, {{L10n/switch}}

Cargo queries need a bot login since 2026-08-23 and the appid.php/gog.php redirects return 403, so neither is
used. Limit: 60 requests/minute (net.PACE). Content is CC BY-NC-SA: only facts are taken, never prose, and the
wiki is not an nCore proof link. Pages can sit behind a Cloudflare check; then every call returns empty.
"""

from __future__ import annotations

import re
from urllib.parse import quote

from .. import net
from . import steam

API = "https://www.pcgamingwiki.com/w/api.php"
PAGE = "https://www.pcgamingwiki.com/wiki/{title}"
FIELDS = {"OS": "Op. rendszer", "CPU": "Processzor", "RAM": "Memória", "GPU": "Videokártya", "VRAM": "Videomemória",
          "DX": "DirectX", "HD": "Tárhely", "audio": "Hangkártya", "other": "Megjegyzés"}


def _call(params: dict) -> dict:
    data = net.get_json(API, params={**params, "format": "json", "formatversion": "2"}, quiet=True)
    return data if isinstance(data, dict) else {}


def parse_idlookup(data: dict) -> str:
    rows = data.get("idlookup") if isinstance(data, dict) else None
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
        return ""
    title = rows[0].get("title")
    return str(title.get("Page") or "") if isinstance(title, dict) else ""


def page_by_id(field: str, value: str) -> str:
    """Page title for a Steam app id (field 'steamappid') or GOG id ('gogid'); '' when unknown."""

    if field not in {"steamappid", "gogid"} or not re.fullmatch(r"\d{1,12}", str(value or "")):
        return ""
    return parse_idlookup(_call({"action": "idlookup", "field": field, "value": value}))


def parse_search(data: dict, title: str) -> str:
    """Best search hit (title similarity >= 0.85) of a list=search response; redirects are fine (parse follows)."""

    query = data.get("query") if isinstance(data, dict) else None
    hits = [{"title": h.get("title", "")} for h in (query.get("search") if isinstance(query, dict) else None) or [] if isinstance(h, dict)]
    best = steam.best_match(hits, [title], key="title")
    return best["title"] if best else ""


def search(title: str) -> str:
    if not title:
        return ""
    return parse_search(_call({"action": "query", "list": "search", "srsearch": title, "srlimit": "8"}), title)


def wikitext(page: str) -> str:
    if not page:
        return ""
    data = _call({"action": "parse", "page": page, "prop": "wikitext", "redirects": "1"})
    text = data["parse"].get("wikitext") if isinstance(data.get("parse"), dict) else ""
    return text if isinstance(text, str) else ""


def _clean(value: str) -> str:
    value = re.sub(r"(?s)<ref[^>]*>.*?</ref>|<ref[^>]*/>", "", value or "")
    value = re.sub(r"(?s)\{\{(?:note|cn|ref|Refurl|Refcheck)\b.*?\}\}", "", value, flags=re.I)
    value = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]*)\]\]", r"\1", value)
    value = re.sub(r"\[https?://\S+\s+([^\]]*)\]", r"\1", value)
    value = re.sub(r"<[^>]+>|'''?", "", value)
    return re.sub(r"\s+", " ", value).strip()


def _template_blocks(text: str, name: str) -> list[str]:
    """Bodies of every {{name ...}} template (nested templates kept inside)."""

    blocks, start = [], 0
    opener = "{{" + name
    while True:
        index = text.find(opener, start)
        if index < 0:
            return blocks
        depth, pos = 0, index
        while pos < len(text) - 1:
            pair = text[pos: pos + 2]
            if pair == "{{":
                depth += 1
                pos += 2
                continue
            if pair == "}}":
                depth -= 1
                pos += 2
                if depth == 0:
                    break
                continue
            pos += 1
        blocks.append(text[index + len(opener): pos - 2])
        start = pos


def _params(body: str) -> dict[str, str]:
    out: dict[str, str] = {}
    depth, current = 0, ""
    pieces = []
    for char in body:
        if char in "{[":
            depth += 1
        elif char in "}]":
            depth -= 1
        if char == "|" and depth == 0:
            pieces.append(current)
            current = ""
        else:
            current += char
    pieces.append(current)
    for piece in pieces:
        if "=" in piece:
            key, value = piece.split("=", 1)
            out[key.strip()] = value.strip()
    return out


def parse_requirements(text: str) -> dict:
    """Windows {{System requirements}} -> {"minimum": [(label, value)], "recommended": [...]}."""

    for body in _template_blocks(text, "System requirements"):
        params = _params(body)
        if params.get("OSfamily", "").strip().lower() != "windows":
            continue
        out: dict[str, list[tuple[str, str]]] = {}
        for prefix, key in (("min", "minimum"), ("rec", "recommended")):
            rows = []
            for field, label in FIELDS.items():
                values = [_clean(params.get(f"{prefix}{field}{n}", "")) for n in ("", "2", "3")]
                values = [v for v in values if v]
                if not values:
                    continue
                value = " / ".join(values)
                if field == "OS":
                    value = "Windows " + value if not value.lower().startswith("windows") else value
                rows.append((label, value))
            out[key] = rows
        return out
    return {"minimum": [], "recommended": []}


def _truth(value: str) -> bool | None:
    value = _clean(value).lower()
    if value in {"true", "yes"}:
        return True
    if value in {"false", "no", ""}:
        return False if value else None
    return None  # "unknown", "hackable", "limited"


def parse_languages(text: str) -> list[dict]:
    """{{L10n/switch}} rows -> [{"language" (English), "name" (Hungarian), "interface", "audio", "subtitles",
    "fan", "notes"}] (each language once)."""

    rows, seen = [], set()
    for body in _template_blocks(text, "L10n/switch"):
        params = _params(body)
        language = _clean(params.get("language", ""))
        if not language or language in seen:
            continue
        seen.add(language)
        rows.append({"language": language, "name": steam.LANG_EN_HU.get(language.lower(), language.lower()),
                     "interface": _truth(params.get("interface", "")), "audio": _truth(params.get("audio", "")),
                     "subtitles": _truth(params.get("subtitles", "")), "fan": _truth(params.get("fan", "")) or False,
                     "notes": _clean(params.get("notes", ""))})
    return rows


def parse_wikitext(text: str) -> dict:
    """{"requirements", "languages", "hungarian" (row or {}), "steam_appid", "gog_id", "official_site", "genres"}."""

    if not text:
        return {}
    infobox = _params(next(iter(_template_blocks(text, "Infobox game")), ""))
    languages = parse_languages(text)
    genres = re.search(r"\{\{Infobox game/row/taxonomy/genres\s*\|([^}]*)\}\}", text)
    return {
        "requirements": parse_requirements(text),
        "languages": languages,
        "hungarian": next((row for row in languages if row["language"].lower() == "hungarian"), {}),
        "steam_appid": _clean(infobox.get("steam appid", "")).split(",")[0].strip(),
        "gog_id": _clean(infobox.get("gogcom id", "")).split(",")[0].strip(),
        "official_site": _clean(infobox.get("official site", "")),
        "genres": [g.strip() for g in (genres.group(1) if genres else "").split(",") if g.strip()],
    }


def page_url(page: str) -> str:
    return PAGE.format(title=quote(page.replace(" ", "_"), safe=":/()")) if page else ""


def lookup(*, steam_appid: str = "", gog_id: str = "", title: str = "") -> dict:
    """parse_wikitext() of the game's page plus {"page", "url"}; {} when no page is found."""

    page = page_by_id("steamappid", steam_appid) if steam_appid else ""
    page = page or (page_by_id("gogid", gog_id) if gog_id else "")
    page = page or search(title)
    info = parse_wikitext(wikitext(page)) if page else {}
    if info:
        info.update({"page": page, "url": page_url(page)})
    return info
