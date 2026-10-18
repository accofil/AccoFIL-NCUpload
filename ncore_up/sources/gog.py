"""GOG.com (no key): catalog search, product data (description, languages, installers), v2 game data
(localizations, requirements, developers), content-system build versions.

Endpoints (unofficial, the same ones GOG Galaxy, Heroic and GOGDB use; tested from nvme 2026-10-16):

    catalog.gog.com/v1/catalog?query=like:<title>&locale=en-US&countryCode=HU   (locale=hu-HU returns nothing)
    api.gog.com/products/<id>?expand=description,downloads   -> description, languages {code: native name}, installers
    api.gog.com/v2/games/<id>?locale=en-US                   -> localizations (text/audio), requirements, developers
    content-system.gog.com/products/<id>/os/windows/builds?generation=2  -> build version_name + date_published

The embed.gog.com/games/ajax/filtered search is dead (0 results). Texts are English only; the Hungarian part is
the language flag ("hu": "magyar"). The product card URL is a valid nCore proof link.
"""

from __future__ import annotations

import re

from .. import net
from . import steam

CATALOG = "https://catalog.gog.com/v1/catalog"
PRODUCT = "https://api.gog.com/products/{id}"
GAME = "https://api.gog.com/v2/games/{id}"
BUILDS = "https://content-system.gog.com/products/{id}/os/{os}/builds"

# GOG's own language codes that are not ISO 639-1
GOG_LANG = {"br": "brazíliai portugál", "cn": "egyszerűsített kínai", "cz": "cseh", "gk": "görög", "jp": "japán", "ko": "koreai",
            "sb": "szerb", "es_mx": "latin-amerikai spanyol", "zh": "hagyományos kínai", "ar": "arab", "hu": "magyar"}
REQ_IDS = {"system": "Op. rendszer", "processor": "Processzor", "memory": "Memória", "graphics": "Videokártya", "directx": "DirectX",
           "storage": "Tárhely", "sound": "Hangkártya", "network": "Hálózat", "other": "Megjegyzés"}
GENRES_HU = {
    "action": "Akció", "adventure": "Kaland", "role-playing": "Szerepjáték", "rpg": "Szerepjáték", "strategy": "Stratégia",
    "simulation": "Szimuláció", "shooter": "Lövöldözős", "racing": "Verseny", "sports": "Sport", "puzzle": "Logikai",
    "platformer": "Platformer", "fighting": "Verekedős", "horror": "Horror", "tactical": "Taktikai", "survival": "Túlélő",
    "indie": "Indie", "casual": "Casual", "arcade": "Árkád", "point-and-click": "Point & click", "turn-based": "Körökre osztott",
    "real-time": "Valós idejű", "open world": "Nyílt világ", "sandbox": "Homokozó", "management": "Menedzser",
    "building": "Építő", "exploration": "Felfedező", "fantasy": "Fantasy", "sci-fi": "Sci-fi", "stealth": "Lopakodós",
    "story rich": "Történetközpontú", "visual novel": "Vizuális regény", "educational": "Oktató", "music": "Zenei",
    "roguelike": "Roguelike", "roguelite": "Roguelite", "metroidvania": "Metroidvania", "soulslike": "Soulslike",
    "point&click": "Point & click", "rpg/action": "Akció-szerepjáték", "action rpg": "Akció-szerepjáték",
    "first-person": "Belső nézetes", "third person": "Külső nézetes", "racing/driving": "Verseny", "sport": "Sport",
}


def lang_name(code: str, english: str = "") -> str:
    """GOG language code / English name -> Hungarian language name."""

    from .. import describe

    if english and english.lower() in steam.LANG_EN_HU:
        return steam.LANG_EN_HU[english.lower()]
    code = (code or "").lower()
    return GOG_LANG.get(code) or describe.language(code)


def genres_hu(names: list[str]) -> list[str]:
    out = []
    for name in names:
        hungarian = GENRES_HU.get((name or "").lower(), name)
        if hungarian and hungarian not in out:
            out.append(hungarian)
    return out


def _strip_html(value: str) -> str:
    return steam.html_text(value)


# --- search ---------------------------------------------------------------------------------------------------

def parse_catalog(data: dict) -> list[dict]:
    """catalog JSON -> [{"id", "slug", "title", "type", "release_date", "developers", "publishers", "genres",
    "cover_url", "url"}]."""

    out = []
    for item in (data.get("products") if isinstance(data, dict) else None) or []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        out.append({
            "id": str(item["id"]), "slug": item.get("slug", ""), "title": item.get("title", ""), "type": item.get("productType", ""),
            "release_date": (item.get("releaseDate") or "").replace(".", "-"), "developers": list(item.get("developers") or []),
            "publishers": list(item.get("publishers") or []),
            "genres": [g.get("name", "") for g in item.get("genres") or [] if isinstance(g, dict)],
            "cover_url": item.get("coverVertical", ""), "url": item.get("storeLink", "") or store_url(item.get("slug", "")),
        })
    return out


def search(title: str, limit: int = 10, *, dlc: bool = False) -> list[dict]:
    """Catalog search. ``like:`` matches title substrings; a query that matches nothing returns unrelated
    products, so callers must check the title (``find``)."""

    title = re.sub(r"\s+", " ", re.sub(r"\s+-\s+|[:™®©]", " ", title or "")).strip()
    if not title:
        return []
    data = net.get_json(CATALOG, params={"query": "like:" + title, "limit": limit, "locale": "en-US", "countryCode": "HU",
                                         "currencyCode": "EUR", "productType": "in:game,pack,dlc" if dlc else "in:game,pack"}) or {}
    return parse_catalog(data)


def find(titles: list[str], *, terms: list[str] | None = None, dlc: bool = False) -> dict | None:
    """Best catalog hit for the candidate ``titles`` (most specific first, similarity >= 0.85). Searches the
    shortest term first: long queries ("... Complete Edition") often match nothing."""

    items: list[dict] = []
    terms = terms or titles
    for term in dict.fromkeys(terms[-1:] + terms[:1]):
        for item in search(term, limit=20 if dlc else 10, dlc=dlc):
            if item["id"] not in {i["id"] for i in items}:
                items.append(item)
        hit = steam.best_match(items, titles, key="title")
        if hit:
            return hit
    return None


def store_url(slug: str) -> str:
    return f"https://www.gog.com/en/game/{slug}" if slug else ""


# --- product (v1) ---------------------------------------------------------------------------------------------

def parse_product(data: dict) -> dict:
    """products/<id> JSON -> {"id", "title", "slug", "type", "languages" ([Hungarian names]), "hungarian",
    "description_en", "lead_en", "release_date", "url", "installers": [{"id", "os", "language", "version",
    "size", "parts"}], "patches": [...], "dlcs": [ids]}."""

    if not isinstance(data, dict) or not data.get("id"):
        return {}
    description = data.get("description") if isinstance(data.get("description"), dict) else {}
    downloads = data.get("downloads") if isinstance(data.get("downloads"), dict) else {}
    languages = data.get("languages") if isinstance(data.get("languages"), dict) else {}

    def installers(key: str) -> list[dict]:
        return [{"id": i.get("id", ""), "os": i.get("os", ""), "language": i.get("language", ""), "version": i.get("version") or "",
                 "size": int(i.get("total_size") or 0), "parts": len(i.get("files") or []), "name": i.get("name", "")}
                for i in downloads.get(key) or [] if isinstance(i, dict)]

    links = data.get("links") or {}
    return {
        "id": str(data["id"]), "title": data.get("title", ""), "slug": data.get("slug", ""), "type": data.get("game_type", ""),
        "languages": [lang_name(code) for code in languages], "hungarian": "hu" in languages,
        "description_en": _strip_html(description.get("full") or description.get("lead") or ""),
        "lead_en": _strip_html(description.get("lead") or ""),
        "release_date": (data.get("release_date") or "")[:10],
        "url": (links.get("product_card") or "").replace("https://www.gog.com/game/", "https://www.gog.com/en/game/") or store_url(data.get("slug", "")),
        "installers": installers("installers"), "patches": installers("patches"),
        "dlcs": [str(d.get("id")) for d in (data.get("dlcs") or {}).get("products") or [] if d.get("id")]
        if isinstance(data.get("dlcs"), dict) else [],
    }


def product(gog_id: str) -> dict:
    if not re.fullmatch(r"\d{5,12}", str(gog_id or "")):
        return {}
    return parse_product(net.get_json(PRODUCT.format(id=gog_id), params={"expand": "description,downloads", "locale": "en-US"}) or {})


# --- game (v2) ------------------------------------------------------------------------------------------------

def parse_game(data: dict) -> dict:
    """v2/games/<id> JSON -> {"localizations": [{"code", "name", "language", "text", "audio"}], "languages",
    "audio", "hungarian", "hungarian_audio", "developers", "publishers", "date", "requirements",
    "tags", "cover_url", "url", "description_en", "dosbox"}."""

    if not isinstance(data, dict) or not isinstance(data.get("_embedded"), dict):
        return {}
    embedded = data["_embedded"]
    scopes: dict[str, dict] = {}
    for item in embedded.get("localizations") or []:
        if not isinstance(item, dict):
            continue
        inner = item.get("_embedded") or {}
        language = inner.get("language") or {}
        code = language.get("code", "")
        entry = scopes.setdefault(code, {"code": code, "name": language.get("name", ""), "text": False, "audio": False})
        entry[(inner.get("localizationScope") or {}).get("type", "text")] = True
    localizations = list(scopes.values())
    for entry in localizations:
        entry["language"] = lang_name(entry["code"], entry["name"])
    requirements: dict[str, list[tuple[str, str]]] = {"minimum": [], "recommended": []}
    for system in embedded.get("supportedOperatingSystems") or []:
        if ((system.get("operatingSystem") or {}).get("name") or "") != "windows":
            continue
        for block in system.get("systemRequirements") or []:
            rows = [(REQ_IDS.get(r.get("id", ""), (r.get("name") or "").strip(" :")), (r.get("description") or "").strip())
                    for r in block.get("requirements") or [] if (r.get("description") or "").strip()]
            rows = [(label, re.sub(r"(?i)^(?:version|verzió)\s*", "", value) if label == "DirectX" else value) for label, value in rows]
            rows = [(label, re.sub(r"(?i)\bavailable space\b", "szabad hely", value) if label == "Tárhely" else value) for label, value in rows]
            if block.get("type") in requirements:
                requirements[block["type"]] = rows
    product_info = embedded.get("product") or {}
    links = data.get("_links") or {}
    return {
        "localizations": localizations,
        "languages": [e["language"] for e in localizations if e["text"] or e["audio"]],
        "audio": [e["language"] for e in localizations if e["audio"]],
        "hungarian": any(e["code"] == "hu" and e["text"] for e in localizations),
        "hungarian_audio": any(e["code"] == "hu" and e["audio"] for e in localizations),
        "developers": [d.get("name", "") for d in embedded.get("developers") or [] if d.get("name")],
        "publishers": [p.get("name", "") for p in embedded.get("publishers") or [] if p.get("name")]
        or ([embedded["publisher"]["name"]] if (embedded.get("publisher") or {}).get("name") else []),
        "date": (product_info.get("globalReleaseDate") or product_info.get("gogReleaseDate") or "")[:10],
        "requirements": requirements,
        "tags": [t.get("name", "") for t in embedded.get("tags") or [] if t.get("name")],
        "cover_url": (links.get("boxArtImage") or {}).get("href", ""),
        "url": (links.get("store") or {}).get("href", ""),
        "description_en": _strip_html(data.get("description") or data.get("overview") or ""),
        "dosbox": bool(data.get("isUsingDosBox")),
        "title": product_info.get("title", ""),
    }


def game(gog_id: str) -> dict:
    if not re.fullmatch(r"\d{5,12}", str(gog_id or "")):
        return {}
    return parse_game(net.get_json(GAME.format(id=gog_id), params={"locale": "en-US"}) or {})


# --- builds ---------------------------------------------------------------------------------------------------

def parse_builds(data: dict) -> list[dict]:
    """content-system builds JSON -> [{"version", "date", "branch", "public", "generation"}], newest first."""

    out = []
    for item in (data.get("items") if isinstance(data, dict) else None) or []:
        if not isinstance(item, dict):
            continue
        out.append({"version": (item.get("version_name") or "").strip(), "date": (item.get("date_published") or "")[:10],
                    "branch": item.get("branch") or "", "public": bool(item.get("public", True)), "generation": item.get("generation")})
    return out


def builds(gog_id: str, os_name: str = "windows") -> list[dict]:
    if not re.fullmatch(r"\d{5,12}", str(gog_id or "")):
        return []
    return parse_builds(net.get_json(BUILDS.format(id=gog_id, os=os_name), params={"generation": "2"}, quiet=True) or {})


def _flat(version: str) -> str:
    return re.sub(r"[\s_()]+", " ", (version or "").lower()).strip()


def match_version(version: str, candidates: list[dict]) -> dict | None:
    """The build/installer whose version equals the release version (case/space/parenthesis-insensitive)."""

    wanted = _flat(version)
    if not wanted:
        return None
    for candidate in candidates:
        if _flat(candidate.get("version", "")) == wanted:
            return candidate
    for candidate in candidates:  # "1.3.10" in "1.3.10 16733"
        have = _flat(candidate.get("version", ""))
        if have.startswith(wanted + " ") or wanted.startswith(have + " "):
            return candidate
    return None
