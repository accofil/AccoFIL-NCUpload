"""PlayStation Store legacy "chihiro" API (PS4 products; PS5/PS3 are not served) and ORBISPatches / PROSPEROPatches
(PS4 / PS5 title pages: name, content id, publisher, region).

chihiro: ``container/<country>/<lang>/999/<CONTENT_ID>`` returns the exact product while it is still sold in that
store; ``titlecontainer/<country>/<lang>/999/<CUSA>_00`` returns the title's representative product (sometimes a
bundle). The Hungarian store is English-only (HU/hu → 404, HU/en works). Delisted products (e.g. the original
UNCHARTED 4 EP9000-CUSA00917_00-UNCHARTED4000000) only resolve through the title container.

The patches sites allow everything in robots.txt but load the update list through a Turnstile-protected call: only the
plain HTML title page is read (one request), and a challenge page stops the lookup (never bypassed).
"""

from __future__ import annotations

import html
import re

from .. import net
from .googleplay import html_text, robots_allows

CHIHIRO = "https://store.playstation.com/store/api/chihiro/00_09_000/"
PRODUCT_PAGE = "https://store.playstation.com/en-hu/product/{}"
PATCH_SITES = {"CUSA": "https://orbispatches.com/{}", "PPSA": "https://prosperopatches.com/{}"}
CONTENT_ID_RE = re.compile(r"\b([A-Z]{2}\d{4}-[A-Z]{4}\d{5}_\d{2}-[A-Z0-9]{16})\b")
_IMAGE_ORDER = (1, 10, 12, 13, 2)


def stores_for(content_id: str) -> list[tuple[str, str]]:
    """(country, language) pairs to try for a content id prefix."""

    prefix = (content_id or "")[:2].upper()
    if prefix == "UP":
        return [("US", "en"), ("GB", "en")]
    if prefix == "JP":
        return [("JP", "ja"), ("US", "en")]
    if prefix in {"HP", "KP"}:
        return [("HK", "en"), ("US", "en")]
    return [("HU", "en"), ("GB", "en"), ("US", "en")]


_BOILERPLATE_RE = re.compile(r"(?im)^.*(to play this game on ps5|playstation\.com/bc|^https?://\S+$).*$\n?")


def clean_description(text: str) -> str:
    """Store text without the PS5 backward-compatibility notice and bare URL lines."""

    text = _BOILERPLATE_RE.sub("", text or "")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def parse_container(data: dict) -> dict:
    """chihiro product → {"content_id", "title_id", "name", "description", "publisher", "release_date", "content_type",
    "platforms", "genres", "rating", "age_limit", "cover_url", "screenshots", "url"}."""

    if not isinstance(data, dict) or not data.get("id"):
        return {}
    images = {}
    for image in _list(data.get("images")):
        if isinstance(image, dict):
            images.setdefault(image.get("type"), str(image.get("url") or ""))
    cover = next((images[t] for t in _IMAGE_ORDER if images.get(t)), "")
    metadata = _dict(data.get("metadata"))
    genres = [str(g) for g in _list(_dict(metadata.get("genre")).get("values"))]
    shots = [str(s["url"]) for s in _list(_dict(data.get("mediaList")).get("screenshots")) if isinstance(s, dict) and s.get("url")]
    content_id = str(data["id"])
    return {
        "content_id": content_id,
        "title_id": content_id[7:16],
        "name": _text(data.get("name")).strip(),
        "description": clean_description(html_text(_text(data.get("long_desc")))),
        "publisher": _text(data.get("provider_name")),
        "release_date": _text(data.get("release_date"))[:10],
        "content_type": _text(data.get("game_contentType")),
        "platforms": [re.sub(r"[^\w ]", "", str(p)) for p in _list(data.get("playable_platform"))],
        "genres": genres,
        "rating": _text(_dict(data.get("content_rating")).get("description")).strip(),
        "age_limit": data.get("age_limit"),
        "cover_url": cover,
        "screenshots": shots,
        "url": PRODUCT_PAGE.format(content_id),
    }


def _text(value) -> str:
    return value if isinstance(value, str) else ""


def _list(value) -> list:
    return value if isinstance(value, list) else []


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def container(content_id: str, country: str = "HU", lang: str = "en") -> dict:
    if not CONTENT_ID_RE.fullmatch(content_id or ""):
        return {}
    return parse_container(net.get_json(f"{CHIHIRO}container/{country}/{lang}/999/{content_id}", quiet=True) or {})


def title_container(title_id: str, country: str = "HU", lang: str = "en") -> dict:
    if not re.fullmatch(r"CUSA\d{5}", title_id or ""):
        return {}
    return parse_container(net.get_json(f"{CHIHIRO}titlecontainer/{country}/{lang}/999/{title_id}_00", quiet=True) or {})


def lookup(content_id: str = "", title_id: str = "") -> dict:
    """Best PS Store record: the exact product (``exact`` True) or the title's representative product; a bundle or a
    different product carries a Hungarian ``warning``. {} when the store knows neither."""

    title_id = title_id or (content_id[7:16] if content_id else "")
    stores = stores_for(content_id)
    if content_id:
        for country, lang in stores:
            found = container(content_id, country, lang)
            if found:
                found["exact"] = True
                return found
    for country, lang in stores:
        found = title_container(title_id, country, lang)
        if found:
            found["exact"] = found["content_id"] == content_id
            kind = f" ({found['content_type']})" if found.get("content_type") else ""
            if not found["exact"] and content_id:
                found["warning"] = (f"a PS Store-ban ez a termék már nem érhető el ({content_id}); a cím képviselő "
                                    f"termékét használtam: {found['name']}{kind} — a leírást ellenőrizd")
            elif found.get("content_type") == "Bundle":
                found["warning"] = f"a PS Store a cím csomagját adta vissza: {found['name']}{kind} — a leírást ellenőrizd"
            return found
    return {}


def parse_patches_html(page: str, url: str = "") -> dict:
    """ORBISPatches/PROSPEROPatches title page → {"title", "title_id", "content_id", "publisher", "publisher_id",
    "region", "icon", "url"}; {} for a challenge or an unknown title."""

    if not page or re.search(r"(?i)<title>\s*Just a moment|cf-chl-|challenge-platform/h/", page):
        return {}
    fields = {}
    for block in re.findall(r'<li class="bd-links-group[^"]*">(.*?)</li>', page, re.S):
        head = re.search(r"<strong[^>]*>(.*?)</strong>(.*)", block, re.S)
        if not head:
            continue
        label = re.sub(r"<small.*?</small>|<[^>]+>", "", head.group(1), flags=re.S).strip()
        value = html.unescape(re.sub(r"<[^>]+>", " ", head.group(2))).strip()
        if label:
            fields[label] = re.sub(r"\s+", " ", value)
    title = re.search(r'<h1 class="bd-title[^"]*"[^>]*>(.*?)</h1>', page, re.S)
    icon = re.search(r"url\((https://cdn\.[a-z]+patches\.com/titles/[^)]+/icon0\.[a-z]+)\)", page)
    if not fields.get("Content ID") and not title:
        return {}
    return {
        "title": html.unescape(re.sub(r"<[^>]+>", "", title.group(1))).strip() if title else "",
        "title_id": fields.get("Title ID", ""),
        "content_id": fields.get("Content ID", ""),
        "publisher": fields.get("Publisher", ""),
        "publisher_id": fields.get("Publisher ID", ""),
        "region": fields.get("Region", ""),
        "icon": icon.group(1) if icon else "",
        "url": url,
    }


def patches_page(title_id: str) -> dict:
    """parse_patches_html() of a PS4 (CUSA) or PS5 (PPSA) title id; one plain GET, robots.txt respected."""

    site = PATCH_SITES.get((title_id or "")[:4])
    if not site or not re.fullmatch(r"[A-Z]{4}\d{5}", title_id):
        return {}
    url = site.format(title_id)
    if not robots_allows(url):
        return {}
    data = parse_patches_html(net.get_text(url, quiet=True) or "", url)
    if not data:
        net.FAILURES.append(f"{url.split('/')[2]}: nincs adat (vagy ellenőrző oldal)")
    return data
