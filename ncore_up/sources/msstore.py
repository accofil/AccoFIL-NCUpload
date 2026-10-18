"""Microsoft Store display catalog (the public endpoint behind the Store web pages) for Xbox games, with market=HU and
languages=hu-hu: the only console source with real Hungarian text when the publisher localized the listing (e.g.
Forza Horizon 5 "Korlátok nélküli, szórakoztató vezetési élmény..."); otherwise the "neutral" (English) text.

Xbox 360 titles are found by their XEX title id through the legacy marketplace product id
``66acd000-77fe-1000-9115-d802<titleid>``. Proof link: https://www.xbox.com/hu-HU/games/store/x/<ProductId>.
"""

from __future__ import annotations

import re

from .. import net

API = "https://displaycatalog.mp.microsoft.com/v7.0/"
MARKET = {"market": "HU", "languages": "hu-hu,neutral"}
STORE_PAGE = "https://www.xbox.com/hu-HU/games/store/x/{}"


def _https(uri: str) -> str:
    return "https:" + uri if uri.startswith("//") else uri


def _first(value) -> dict:
    """First element of a catalog list when it is an object, else {}."""

    first = value[0] if isinstance(value, list) and value else {}
    return first if isinstance(first, dict) else {}


def parse_autosuggest(data: dict) -> list[dict]:
    """[{"product_id", "title", "type", "icon"}] of an autosuggest response."""

    out = []
    for family in (data or {}).get("Results") or []:
        for product in family.get("Products") or []:
            if product.get("ProductId"):
                out.append({"product_id": product["ProductId"], "title": product.get("Title", ""), "type": product.get("Type", ""),
                            "icon": _https(product.get("Icon", ""))})
    return out


def parse_product(product: dict) -> dict:
    """One display-catalog product → {"product_id", "title", "short_title", "description", "short_description",
    "language", "publisher", "developer", "category", "release_date", "box_art", "poster", "screenshots", "platforms",
    "alternate_ids", "url", "hungarian"}."""

    if not isinstance(product, dict) or not product.get("ProductId"):
        return {}
    local = _first(product.get("LocalizedProperties"))
    images: dict[str, list[str]] = {}
    for image in local.get("Images") if isinstance(local.get("Images"), list) else []:
        if not isinstance(image, dict):
            continue
        uri = _https(str(image.get("Uri") or ""))
        if uri and uri not in images.setdefault(image.get("ImagePurpose", ""), []):
            images[image.get("ImagePurpose", "")].append(uri)
    properties = product.get("Properties") if isinstance(product.get("Properties"), dict) else {}
    market = _first(product.get("MarketProperties"))
    language = str(local.get("Language") or "").lower()
    return {
        "product_id": product["ProductId"],
        "title": (local.get("ProductTitle") or "").strip(),
        "short_title": (local.get("ShortTitle") or "").strip(),
        "description": (local.get("ProductDescription") or "").strip(),
        "short_description": (local.get("ShortDescription") or "").strip(),
        "language": language,
        "hungarian": language.startswith("hu"),
        "publisher": (local.get("PublisherName") or "").strip(),
        "developer": (local.get("DeveloperName") or "").strip(),
        "category": properties.get("Category") or "",
        "release_date": (market.get("OriginalReleaseDate") or "")[:10],
        "box_art": (images.get("BoxArt") or images.get("Poster") or [""])[0],
        "poster": (images.get("Poster") or [""])[0],
        "screenshots": images.get("Screenshot") or [],
        "platforms": list(properties.get("XboxConsoleGenCompatible") or []),
        "alternate_ids": {a.get("IdType", ""): a.get("Value", "") for a in product.get("AlternateIds") or [] if isinstance(a, dict)},
        "family": product.get("ProductFamily", ""),
        "url": STORE_PAGE.format(product["ProductId"]),
    }


def search(query: str, limit: int = 5) -> list[dict]:
    if not query.strip():
        return []
    data = net.get_json(API + "productFamilies/autosuggest", params={"market": "HU", "languages": "hu-hu", "query": query,
                                                                       "productFamilyNames": "Games"}, quiet=True) or {}
    return parse_autosuggest(data)[:limit]


def product(product_id: str) -> dict:
    if not re.fullmatch(r"[0-9A-Z]{12}", product_id or ""):
        return {}
    data = net.get_json(API + "products", params={"bigIds": product_id, **MARKET}, quiet=True) or {}
    products = data.get("Products") or []
    return parse_product(products[0]) if products else {}


def legacy_id(title_id: str) -> str:
    """Xbox 360 marketplace product id of a title id: 4D5307E6 → 66acd000-77fe-1000-9115-d8024d5307e6."""

    return f"66acd000-77fe-1000-9115-d802{title_id.lower()}" if re.fullmatch(r"[0-9A-Fa-f]{8}", title_id or "") else ""


def lookup_xbox360(title_id: str) -> dict:
    """Store product of an Xbox 360 game (backward compatible titles sold on the Xbox Store); {} when none."""

    legacy = legacy_id(title_id)
    if not legacy:
        return {}
    data = net.get_json(API + "products/lookup", params={"alternateId": "LegacyXboxProductId", "value": legacy, **MARKET},
                        quiet=True) or {}
    ids = [p.get("ProductId") for p in data.get("Products") or [] if p.get("ProductId")] or list(data.get("BigIds") or [])
    first: dict = {}
    for product_id in ids[:3]:
        item = product(product_id)
        if item and item.get("family") == "Games" and item.get("description"):
            return item
        first = first or item
    # back-compat entries are thin "Apps" wrappers: the game's own listing is found by its title
    if first.get("title"):
        game = find_game(first["title"])
        if game:
            game["via_title"] = True  # the store's listing of that title, possibly a newer (PC/Xbox One) edition
            return game
    return first


def find_game(title: str) -> dict:
    """Best store product whose title starts with ``title`` (editions like "Standard kiadás" allowed); {} when none."""

    def simple(text: str) -> str:
        return re.sub(r"[^0-9a-z]+", " ", text.lower()).strip()

    wanted = simple(title)
    hits = [h for h in search(title, 8) if h["type"] in {"Game", ""} and wanted
            and (simple(h["title"]) == wanted or simple(h["title"]).startswith(wanted + " "))]
    hits.sort(key=lambda h: (simple(h["title"]) != wanted, not re.search(r"(?i)standard|alap", h["title"]), len(h["title"])))
    return product(hits[0]["product_id"]) if hits else {}
