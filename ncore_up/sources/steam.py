"""Steam store (no key): search, app details in Hungarian and English, Hungarian user tags, cover art.

Endpoints (undocumented store API, tested from nvme 2026-10-16; keep to ~3 calls per upload, the IP limit is
around 200 requests / 5 minutes):

    store.steampowered.com/api/storesearch/?term=..&cc=HU&l=hungarian     -> items[].{id, name, type}
    store.steampowered.com/api/appdetails?appids=..&l=hungarian|english   -> {appid: {success, data}}
    api.steampowered.com/IStoreBrowseService/GetItems/v1/?input_json=..   -> tags, assets (Steam Web API)
    api.steampowered.com/IStoreService/GetTagList/v1/?language=hungarian  -> tag id -> Hungarian name

With l=hungarian the genres, the requirement labels ("Op. rendszer", "Processzor", "Memória", "Grafika",
"Tárhely"), language names and the date ("2025. nov. 20.") are Hungarian; the texts only when the publisher
localized the store page. The store URL is a valid nCore proof link.
"""

from __future__ import annotations

import html
import json
import re
import unicodedata
from difflib import SequenceMatcher

from .. import net

STORE = "https://store.steampowered.com/api/"
ITEMS = "https://api.steampowered.com/IStoreBrowseService/GetItems/v1/"
TAGLIST = "https://api.steampowered.com/IStoreService/GetTagList/v1/"
CDN = "https://shared.akamai.steamstatic.com/store_item_assets/"

# English language names (Steam, GOG, PCGamingWiki) -> Hungarian
LANG_EN_HU = {
    "english": "angol", "french": "francia", "german": "német", "italian": "olasz", "spanish": "spanyol",
    "spanish - spain": "spanyol", "spanish - latin america": "latin-amerikai spanyol", "latin american spanish": "latin-amerikai spanyol",
    "spanish (latin american)": "latin-amerikai spanyol", "portuguese": "portugál", "portuguese - portugal": "portugál",
    "portuguese - brazil": "brazíliai portugál", "brazilian portuguese": "brazíliai portugál", "portuguese (brazilian)": "brazíliai portugál",
    "simplified chinese": "egyszerűsített kínai", "chinese simplified": "egyszerűsített kínai", "traditional chinese": "hagyományos kínai",
    "chinese traditional": "hagyományos kínai", "chinese": "kínai", "japanese": "japán", "korean": "koreai", "russian": "orosz",
    "polish": "lengyel", "hungarian": "magyar", "czech": "cseh", "slovak": "szlovák", "turkish": "török", "ukrainian": "ukrán",
    "thai": "thai", "arabic": "arab", "dutch": "holland", "swedish": "svéd", "danish": "dán", "finnish": "finn",
    "norwegian": "norvég", "greek": "görög", "romanian": "román", "bulgarian": "bolgár", "vietnamese": "vietnámi",
    "indonesian": "indonéz", "croatian": "horvát", "serbian": "szerb", "slovenian": "szlovén", "hebrew": "héber",
    "lithuanian": "litván", "latvian": "lett", "estonian": "észt", "catalan": "katalán", "hindi": "hindi", "malay": "maláj",
    "filipino": "filippínó", "persian": "perzsa", "belarusian": "belarusz", "georgian": "grúz", "icelandic": "izlandi",
    "basque": "baszk", "galician": "galíciai", "bangla": "bengáli", "bengali": "bengáli",
}
REQ_LABELS = {  # Steam label (Hungarian or English) -> nCore-style Hungarian label
    "op. rendszer": "Op. rendszer", "os": "Op. rendszer", "operációs rendszer": "Op. rendszer",
    "processzor": "Processzor", "processor": "Processzor", "cpu": "Processzor",
    "memória": "Memória", "memory": "Memória", "ram": "Memória",
    "grafika": "Videokártya", "graphics": "Videokártya", "videokártya": "Videokártya", "video card": "Videokártya", "gpu": "Videokártya",
    "directx": "DirectX", "tárhely": "Tárhely", "storage": "Tárhely", "hard drive": "Tárhely", "hard disk space": "Tárhely",
    "merevlemez": "Tárhely", "hangkártya": "Hangkártya", "sound card": "Hangkártya", "hálózat": "Hálózat", "network": "Hálózat",
    "egyéb megjegyzések": "Megjegyzés", "additional notes": "Megjegyzés", "vr-támogatás": "VR", "vr support": "VR",
}
EARLY_ACCESS_GENRE = "70"
HU_MONTHS = {"jan": 1, "febr": 2, "feb": 2, "márc": 3, "már": 3, "ápr": 4, "máj": 5, "jún": 6, "júl": 7, "aug": 8,
             "szept": 9, "szep": 9, "okt": 10, "nov": 11, "dec": 12}
EN_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


# --- text helpers ---------------------------------------------------------------------------------------------

def html_text(value: str) -> str:
    """Store HTML -> plain text: paragraphs and list items on their own lines, bullets as '• ', no images/video."""

    text = re.sub(r"(?is)<(script|style|video|iframe)\b.*?</\1>", " ", value or "")
    text = re.sub(r"(?i)<img\b[^>]*>", " ", text)
    text = re.sub(r"(?i)</p>|</h\d>|</ul>|</ol>", "\n\n", text)
    text = re.sub(r"(?i)<br\s*/?>|</div>", "\n", text)
    text = re.sub(r"(?i)<li[^>]*>", "\n• ", text)
    text = re.sub(r"(?i)<h\d[^>]*>", "\n\n", text)
    text = re.sub(r"<[^>]*$", "", re.sub(r"<[^>]+>", "", text))  # also a cut-off tag at the end
    text = html.unescape(text).replace("\xa0", " ").replace("\t", " ")
    out: list[str] = []
    for line in (re.sub(r"[ ]{2,}", " ", raw).strip() for raw in text.split("\n")):
        if line in {"", "•"}:
            if out and out[-1] != "":
                out.append("")
            continue
        if line.startswith("•") and len(out) > 1 and out[-1] == "" and out[-2].startswith("•"):
            out.pop()  # keep bullet lists together
        out.append(line)
    return "\n".join(out).strip()


def norm_title(text: str) -> str:
    """Comparable title: casefold, no ™®©, '&' -> 'and', roman numerals -> digits, punctuation -> spaces."""

    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).casefold()
    text = re.sub(r"[™®©]", "", text).replace("&", " and ")
    text = re.sub(r"['’`]", "", text)
    text = re.sub(r"(?<=\d)[,.](?=\d{3}(?!\d))", "", text)  # "40,000" -> "40000"
    text = re.sub(r"[^0-9a-z]+", " ", text)
    roman = {"i": "1", "ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6", "vii": "7", "viii": "8", "ix": "9", "x": "10"}
    words = text.split()
    words = [roman.get(w, w) if i > 0 else w for i, w in enumerate(words)]
    return " ".join(words)


def similarity(a: str, b: str) -> float:
    a, b = norm_title(a), norm_title(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    try:
        from rapidfuzz import fuzz  # optional dependency

        return fuzz.ratio(a, b) / 100.0
    except ImportError:
        return SequenceMatcher(None, a, b).ratio()


# words that make a store item a different product than the one searched for (unless the title has them too)
TRAP_WORDS = {"hd", "remastered", "remaster", "remake", "dlc", "soundtrack", "ost", "demo", "artbook", "bundle", "pack", "season",
              "pass", "upgrade", "expansion", "playtest", "beta", "server", "editor", "sdk", "tools", "vr", "prologue", "chapter"}


def _numbers(normalized: str) -> set[str]:
    """Number words of a norm_title() text, years left out ("Alone in the Dark 2024" is the same game)."""

    return {w for w in normalized.split() if w.isdigit() and not re.fullmatch(r"(?:19|20)\d\d", w)}


def best_match(items: list[dict], titles: list[str], key: str = "name", threshold: float = 0.85) -> dict | None:
    """The item whose ``key`` is closest to one of ``titles`` (most specific title first); None below threshold.
    An item with an extra 'trap' word (HD, Remake, DLC, Soundtrack, Demo, ...) or other numbers ("Frostpunk" vs
    "Frostpunk 2", "Cities: Skylines" vs "... II") loses 0.2."""

    best: tuple[float, dict] | None = None
    for rank, title in enumerate(t for t in titles if t):
        wanted_norm = norm_title(title)
        wanted = set(wanted_norm.split())
        for order, item in enumerate(i for i in items if isinstance(i, dict)):
            name = str(item.get(key) or "")
            score = similarity(title, name)
            have = norm_title(name)
            if (set(have.split()) - wanted) & TRAP_WORDS:
                score -= 0.2
            if _numbers(have) != _numbers(wanted_norm):
                score -= 0.2
            ranked = score - rank * 0.001 - order * 0.0001
            if score >= threshold and (best is None or ranked > best[0]):
                best = (ranked, item)
    return best[1] if best else None


def parse_date(text: str) -> str:
    """'2025. nov. 20.' / '13 Feb, 2024' / 'Feb 13, 2024' -> '2025-11-20'; a bare year -> '2025'; else ''."""

    text = (text or "").strip().lower()
    match = re.match(r"(\d{4})\.\s*([a-záéíóöőúüű]+)\.?\s*(\d{1,2})\.?", text)
    month = next((n for k, n in sorted(HU_MONTHS.items(), key=lambda kv: -len(kv[0])) if match and match.group(2).startswith(k)), 0)
    if match and month:
        return f"{match.group(1)}-{month:02d}-{int(match.group(3)):02d}"
    match = re.match(r"(\d{1,2})\s+([a-z]{3})[a-z]*\.?,?\s+(\d{4})", text) or None
    if match and match.group(2) in EN_MONTHS:
        return f"{match.group(3)}-{EN_MONTHS[match.group(2)]:02d}-{int(match.group(1)):02d}"
    match = re.match(r"([a-z]{3})[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})", text)
    if match and match.group(1) in EN_MONTHS:
        return f"{match.group(3)}-{EN_MONTHS[match.group(1)]:02d}-{int(match.group(2)):02d}"
    match = re.search(r"\b(19|20)\d\d\b", text)
    return match.group() if match else ""


def parse_languages(value: str) -> dict:
    """supported_languages HTML -> {"languages": [Hungarian names], "audio": [names with full audio],
    "hungarian": bool, "hungarian_audio": bool}. A '*' marks full audio support."""

    text = re.sub(r"(?i)<br\s*/?>.*$", "", value or "", flags=re.S)  # the footnote after <br>
    text = re.sub(r"(?i)<strong>\s*\*\s*</strong>", "*", text)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    languages, audio = [], []
    for raw in text.split(","):
        name = raw.strip()
        if not name:
            continue
        star = name.endswith("*")
        name = name.rstrip("*").strip()
        hungarian_name = LANG_EN_HU.get(name.lower(), name.lower())
        if hungarian_name not in languages:
            languages.append(hungarian_name)
        if star and hungarian_name not in audio:
            audio.append(hungarian_name)
    return {"languages": languages, "audio": audio, "hungarian": "magyar" in languages, "hungarian_audio": "magyar" in audio}


def parse_requirements(value: str) -> list[tuple[str, str]]:
    """pc_requirements.minimum/recommended HTML -> [(Hungarian label, value)]; unlabelled lines get label ''."""

    out: list[tuple[str, str]] = []
    items = re.findall(r"(?is)<li[^>]*>(.*?)</li>", value or "")
    if not items and value:
        items = re.split(r"(?i)<br\s*/?>", value)
    for item in items:
        match = re.match(r"(?is)\s*<strong>\s*(.*?)\s*:?\s*</strong>\s*:?\s*(.*)$", item)
        if match:
            label = html.unescape(re.sub(r"<[^>]+>", "", match.group(1))).strip(" :*")
            text = html_text(match.group(2))
        else:
            label, text = "", html_text(item)
        text = re.sub(r"\s+", " ", text).strip()
        if label.lower() in {"minimum", "ajánlott", "recommended"} or not text:
            continue
        mapped = REQ_LABELS.get(label.lower().rstrip(" *"), label)
        if mapped == "DirectX":
            text = re.sub(r"(?i)^(?:verzió|version)\s*:?\s*", "", text)
        if mapped == "Tárhely":
            text = re.sub(r"(?i)\bavailable space\b", "szabad hely", text)
        out.append((mapped, text))
    return out


def requirements_text(requirements: dict) -> str:
    """{"minimum": [(label, value)], "recommended": [...]} -> Hungarian text block for the description."""

    blocks = []
    for key, title in (("minimum", "Minimális gépigény"), ("recommended", "Ajánlott gépigény")):
        rows = [r for r in requirements.get(key) or [] if r[1]]
        if not any(label for label, _ in rows):
            continue
        lines = [f"{title}:"] + [f"{label}: {value}" if label else value for label, value in rows]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


# --- search ---------------------------------------------------------------------------------------------------

def parse_search(data: dict) -> list[dict]:
    """storesearch JSON -> [{"appid", "name", "type"}]."""

    items = data.get("items") if isinstance(data, dict) else None
    return [{"appid": str(item.get("id", "")), "name": item.get("name", ""), "type": item.get("type", "")}
            for item in items or [] if isinstance(item, dict) and item.get("id")]


def search(term: str) -> list[dict]:
    if not term:
        return []
    data = net.get_json(STORE + "storesearch/", params={"term": term, "l": "hungarian", "cc": "HU"}) or {}
    return parse_search(data)


def find(titles: list[str], *, terms: list[str] | None = None) -> dict | None:
    """Best storesearch hit for the candidate ``titles`` (most specific first); ``terms`` are the search texts
    (default: the titles; the bare title finds the base app, the full one editions)."""

    items: list[dict] = []
    terms = terms or titles
    for term in dict.fromkeys(terms[-1:] + terms[:1]):
        for item in search(term):
            if item["appid"] not in {i["appid"] for i in items}:
                items.append(item)
        if best_match(items, titles):
            break
    return best_match(items, titles)


# --- app details ----------------------------------------------------------------------------------------------

def appdetails(appid: str, lang: str = "hungarian") -> dict:
    """Raw appdetails ``data`` for one app ({} on failure or success:false)."""

    if not re.fullmatch(r"\d{1,10}", str(appid or "")):
        return {}
    data = net.get_json(STORE + "appdetails", params={"appids": appid, "l": lang, "cc": "HU"})
    entry = data.get(str(appid)) if isinstance(data, dict) else None
    if not isinstance(entry, dict) or not entry.get("success") or not isinstance(entry.get("data"), dict):
        return {}
    return entry["data"]


def parse_app(hu: dict, en: dict | None = None) -> dict:
    """appdetails data (Hungarian, plus optional English) -> {"appid", "name", "type", "short_hu", "about_hu",
    "short_en", "about_en", "languages", "audio", "hungarian", "hungarian_audio", "developers", "publishers",
    "date", "release_date", "coming_soon", "genres_hu", "genres_en", "early_access", "requirements",
    "website", "url", "cover_url", "header_url", "fullgame"}."""

    hu = hu if isinstance(hu, dict) else {}
    en = en if isinstance(en, dict) else {}
    base = hu or en
    if not base:
        return {}
    appid = str(base.get("steam_appid") or "")
    languages = parse_languages(hu.get("supported_languages") or en.get("supported_languages") or "")
    req_source = hu.get("pc_requirements") if isinstance(hu.get("pc_requirements"), dict) else en.get("pc_requirements")
    req_source = req_source if isinstance(req_source, dict) else {}
    release = base.get("release_date") or {}
    english_date = (en.get("release_date") or {}).get("date", "")
    genres = base.get("genres") or []
    return {
        "appid": appid,
        "name": en.get("name") or base.get("name", ""),
        "type": base.get("type", ""),
        "short_hu": html_text(hu.get("short_description", "")),
        "about_hu": html_text(hu.get("about_the_game", "")),
        "short_en": html_text(en.get("short_description", "")),
        "about_en": html_text(en.get("about_the_game", "")),
        "developers": list(base.get("developers") or []),
        "publishers": list(base.get("publishers") or []),
        "release_date": release.get("date", ""),
        "date": parse_date(release.get("date", "")) or parse_date(english_date),
        "coming_soon": bool(release.get("coming_soon")),
        "genres_hu": [g.get("description", "") for g in (hu.get("genres") or []) if g.get("id") != EARLY_ACCESS_GENRE],
        "genres_en": [g.get("description", "") for g in (en.get("genres") or []) if g.get("id") != EARLY_ACCESS_GENRE],
        "early_access": any(str(g.get("id")) == EARLY_ACCESS_GENRE for g in genres),
        "requirements": {"minimum": parse_requirements(req_source.get("minimum", "")),
                         "recommended": parse_requirements(req_source.get("recommended", ""))},
        "website": html.unescape(base.get("website") or "").strip(),  # the store sends "&amp;" in query strings
        "url": store_url(appid),
        "cover_url": cover_url(appid),
        "header_url": base.get("header_image", ""),
        "fullgame": (base.get("fullgame") or {}).get("appid", ""),
        **languages,
    }


def app(appid: str) -> dict:
    """parse_app() of both store languages; {} when the app is unknown."""

    hu = appdetails(appid, "hungarian")
    if not hu:
        return {}
    return parse_app(hu, appdetails(appid, "english"))


def store_url(appid: str) -> str:
    return f"https://store.steampowered.com/app/{appid}/" if appid else ""


def cover_url(appid: str, assets: dict | None = None) -> str:
    """Vertical 600x900 library capsule (GetItems asset path when known, else the classic CDN path)."""

    if not appid:
        return ""
    assets = assets or {}
    if assets.get("library_capsule") and assets.get("asset_url_format"):
        return CDN + assets["asset_url_format"].replace("${FILENAME}", assets["library_capsule"])
    return f"{CDN}steam/apps/{appid}/library_600x900.jpg"


# --- user tags (Hungarian) ------------------------------------------------------------------------------------

def parse_items(data: dict) -> dict:
    """GetItems JSON -> {"tagids": [...] (by weight), "assets": {...}} of the first item."""

    response = data.get("response") if isinstance(data, dict) else None
    items = response.get("store_items") if isinstance(response, dict) else None
    if not isinstance(items, list) or not items or not isinstance(items[0], dict):
        return {}
    item = items[0]
    tags = sorted((t for t in item.get("tags") or [] if isinstance(t, dict)), key=lambda t: -int(t.get("weight") or 0))
    assets = item.get("assets")
    return {"tagids": [t.get("tagid") for t in tags] or list(item.get("tagids") or []), "assets": assets if isinstance(assets, dict) else {}}


def parse_taglist(data: dict) -> dict[int, str]:
    response = data.get("response") if isinstance(data, dict) else None
    tags = response.get("tags") if isinstance(response, dict) else None
    return {int(t["tagid"]): t.get("name", "") for t in tags or [] if isinstance(t, dict) and str(t.get("tagid", "")).isdigit()}


def store_item(appid: str) -> dict:
    """parse_items() for one app via the Steam Web API (no key needed); {} on failure."""

    if not re.fullmatch(r"\d{1,10}", str(appid or "")):
        return {}
    body = {"ids": [{"appid": int(appid)}], "context": {"language": "hungarian", "country_code": "HU"},
            "data_request": {"include_assets": True, "include_tag_count": 12}}
    return parse_items(net.get_json(ITEMS, params={"input_json": json.dumps(body)}, quiet=True) or {})


def tags_hu(appid: str, limit: int = 8, item: dict | None = None) -> list[str]:
    """Top user tags of an app in Hungarian ("Nyílt világ", "Túlélő", ...)."""

    item = item if isinstance(item, dict) else store_item(appid)
    if not item.get("tagids"):
        return []
    names = parse_taglist(net.get_json(TAGLIST, params={"language": "hungarian"}, quiet=True, expire=30 * 86400) or {})
    return [names[t] for t in item["tagids"] if t in names][:limit]
