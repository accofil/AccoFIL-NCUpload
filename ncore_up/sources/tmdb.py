"""TMDB: the anchor for films and series. IMDb/TMDB/Wikidata ids, English and (community) Hungarian titles and
overviews, credits, countries, runtime, posters and season episode lists.

Key: TMDB_API, a v3 API key (or a v4 read access token, sent as a Bearer header). Without a key every function
returns empty results. Attribution: "This product uses the TMDB API but is not endorsed or certified by TMDB".
The hu-HU fields are community translations: some are missing, some are really English and episode names may be
placeholders ("9. epizód"), so Hungarian text is checked before use.
"""

from __future__ import annotations

import re

from .. import keys, net, translate

API = "https://api.themoviedb.org/3"
IMAGE = "https://image.tmdb.org/t/p/{size}{path}"
SITE = "https://www.themoviedb.org/{kind}/{id}"
CREDIT = "TMDB"

# ISO 3166-1 -> English country name as the nCore infobar wants it (wiki: write USA and UK).
COUNTRY = {
    "US": "USA", "GB": "UK", "HU": "Hungary", "DE": "Germany", "FR": "France", "IT": "Italy", "ES": "Spain", "CA": "Canada",
    "AU": "Australia", "NZ": "New Zealand", "IE": "Ireland", "JP": "Japan", "KR": "South Korea", "CN": "China", "HK": "Hong Kong",
    "TW": "Taiwan", "IN": "India", "SE": "Sweden", "NO": "Norway", "DK": "Denmark", "FI": "Finland", "IS": "Iceland",
    "NL": "Netherlands", "BE": "Belgium", "LU": "Luxembourg", "AT": "Austria", "CH": "Switzerland", "PL": "Poland",
    "CZ": "Czech Republic", "SK": "Slovakia", "RO": "Romania", "BG": "Bulgaria", "RS": "Serbia", "HR": "Croatia",
    "SI": "Slovenia", "GR": "Greece", "TR": "Turkey", "RU": "Russia", "UA": "Ukraine", "IL": "Israel", "MX": "Mexico",
    "BR": "Brazil", "AR": "Argentina", "CL": "Chile", "CO": "Colombia", "ZA": "South Africa", "TH": "Thailand", "PT": "Portugal",
}
_NAME_FIX = {"United States of America": "USA", "United States": "USA", "United Kingdom": "UK"}
_PLACEHOLDER_RE = re.compile(r"(?i)^(?:\d+\.\s*(?:epizód|rész)|(?:epizód|rész|episode|ep\.?)\s*#?\d+)$")


def _call(path: str, **params) -> dict | None:
    key = keys.get("TMDB_API")
    if not key:
        return None
    headers = None
    if key.startswith("eyJ"):  # v4 read access token (a JWT)
        headers = {"Authorization": f"Bearer {key}"}
    else:
        params["api_key"] = key
    data = net.get_json(API + path, params=params, headers=headers)
    return data if isinstance(data, dict) else None


def poster_url(path: str, size: str = "w342") -> str:
    return IMAGE.format(size=size, path=path) if path else ""


def page_url(kind: str, tmdb_id) -> str:
    return SITE.format(kind=kind, id=tmdb_id) if kind in {"movie", "tv"} and tmdb_id else ""


def is_placeholder(name: str, number: int | None = None) -> bool:
    """"9. epizód", "Episode 9", or just the episode number (``number``; any bare number when it is None)."""

    name = (name or "").strip()
    if _PLACEHOLDER_RE.match(name):
        return True
    return name.isdigit() and (number is None or int(name) == number)


def countries(production: list, origin: list = ()) -> list[str]:
    """English country names ("USA", "UK", ...) from production_countries, else from origin_country codes."""

    names = []
    for item in production or []:
        code, name = (item.get("iso_3166_1") or "").upper(), item.get("name") or ""
        names.append(COUNTRY.get(code) if code in {"US", "GB"} else _NAME_FIX.get(name, name or COUNTRY.get(code, code)))
    if not names:
        names = [COUNTRY.get(str(code).upper(), str(code).upper()) for code in origin or []]
    return list(dict.fromkeys(n for n in names if n))


# --- parsers ---------------------------------------------------------------------------------------------------

def parse_find(data: dict) -> dict:
    """{"kind": "movie"|"tv", "id"} of a /find answer; an episode/season id gives the show plus "season"
    (and "episode"); {} when nothing matched."""

    data = data if isinstance(data, dict) else {}
    for key, kind in (("movie_results", "movie"), ("tv_results", "tv")):
        for item in data.get(key) or []:
            if isinstance(item, dict) and str(item.get("id") or "").isdigit():
                return {"kind": kind, "id": int(item["id"])}
    for key in ("tv_episode_results", "tv_season_results"):
        for item in data.get(key) or []:
            if isinstance(item, dict) and str(item.get("show_id") or "").isdigit():
                found = {"kind": "tv", "id": int(item["show_id"]), "season": item.get("season_number")}
                if key == "tv_episode_results":
                    found["episode"] = item.get("episode_number")
                return found
    return {}


def parse_search(data: dict, kind: str) -> list[dict]:
    """[{"id", "kind", "title", "original_title", "year", "overview", "original_language", "popularity", "poster_path"}]"""

    results = []
    for item in (data if isinstance(data, dict) else {}).get("results") or []:
        if not isinstance(item, dict) or not str(item.get("id") or "").isdigit():
            continue
        date = item.get("release_date" if kind == "movie" else "first_air_date") or ""
        results.append({
            "id": int(item["id"]), "kind": kind,
            "title": item.get("title" if kind == "movie" else "name") or "",
            "original_title": item.get("original_title" if kind == "movie" else "original_name") or "",
            "year": date[:4], "overview": (item.get("overview") or "").strip(),
            "original_language": item.get("original_language") or "", "popularity": float(item.get("popularity") or 0),
            "poster_path": item.get("poster_path") or "",
        })
    return results


def parse_details(hu: dict, en: dict, kind: str) -> dict:
    """{"id", "kind", "url", "imdb", "wikidata", "tvdb", "title" (English), "title_hu" ('' when TMDB has no Hungarian
    title), "title_original", "original_language", "year", "date", "runtime", "countries", "directors" (series:
    creators), "cast" (top 5), "genres_hu", "genres_en", "overview_hu", "overview_en", "poster_url",
    "seasons": {number: episode_count}}; {} when both answers are empty."""

    hu, en = hu or {}, en or {}
    base = hu or en
    if not base.get("id"):
        return {}
    movie = kind == "movie"
    name_key = "title" if movie else "name"
    original = (base.get("original_title" if movie else "original_name") or "").strip()
    language = base.get("original_language") or ""
    translations = {t.get("iso_639_1"): t.get("data") or {} for t in ((hu.get("translations") or en.get("translations") or {}).get("translations") or [])}
    hu_tr, en_tr = translations.get("hu"), translations.get("en") or {}
    title_hu = ((hu_tr or {}).get(name_key) or "").strip() or (original if language == "hu" else "")
    if hu_tr is not None:
        overview_hu = (hu_tr.get("overview") or "").strip()
    else:  # no translation list: the hu-HU answer is Hungarian only when it differs from the English one
        overview_hu = (hu.get("overview") or "").strip()
        overview_hu = "" if overview_hu == (en.get("overview") or "").strip() else overview_hu
    title = (en.get(name_key) or en_tr.get(name_key) or original or hu.get(name_key) or "").strip()
    overview_en = (en.get("overview") or en_tr.get("overview") or "").strip()
    date = (base.get("release_date" if movie else "first_air_date") or "").strip()
    if movie:
        runtime = en.get("runtime") or en_tr.get("runtime") or hu.get("runtime") or ""
    else:
        runtimes = base.get("episode_run_time") or []
        runtime = runtimes[0] if runtimes else (base.get("last_episode_to_air") or {}).get("runtime") or ""
    credits = hu.get("credits") or en.get("credits") or {}
    if movie:
        directors = [c.get("name", "") for c in credits.get("crew") or [] if c.get("job") == "Director"]
    else:
        directors = [c.get("name", "") for c in base.get("created_by") or []]
    cast = sorted((c for c in credits.get("cast") or [] if isinstance(c, dict)),
                  key=lambda c: c["order"] if isinstance(c.get("order"), int) else 999)
    external = hu.get("external_ids") or en.get("external_ids") or {}
    return {
        "id": int(base["id"]), "kind": kind, "url": page_url(kind, base["id"]),
        "imdb": (external.get("imdb_id") or base.get("imdb_id") or "").lower(),
        "wikidata": external.get("wikidata_id") or "",
        "tvdb": str(external.get("tvdb_id") or ""),
        "title": title, "title_hu": title_hu, "title_original": original, "original_language": language,
        "year": date[:4], "date": date, "runtime": str(runtime or ""),
        "countries": countries(base.get("production_countries") or [], base.get("origin_country") or []),
        "directors": list(dict.fromkeys(d for d in directors if d))[:3],
        "cast": [c.get("name", "") for c in cast if c.get("name")][:5],
        "genres_hu": [g.get("name", "") for g in hu.get("genres") or [] if g.get("name")],
        "genres_en": [g.get("name", "") for g in en.get("genres") or [] if g.get("name")],
        "overview_hu": overview_hu, "overview_en": overview_en,
        "poster_url": poster_url(hu.get("poster_path") or en.get("poster_path") or ""),
        "seasons": {int(s["season_number"]): int(s.get("episode_count") or 0) for s in base.get("seasons") or []
                    if s.get("season_number") is not None},
    }


def parse_season(data: dict) -> list[dict]:
    """[{"season", "episode", "title", "overview", "runtime", "air_date"}] of a /tv/{id}/season/{n} answer."""

    episodes = []
    for item in (data if isinstance(data, dict) else {}).get("episodes") or []:
        if not isinstance(item, dict) or not str(item.get("episode_number", "")).isdigit():
            continue
        episodes.append({
            "season": item.get("season_number"), "episode": int(item["episode_number"]), "title": (item.get("name") or "").strip(),
            "overview": (item.get("overview") or "").strip(), "runtime": str(item.get("runtime") or ""), "air_date": item.get("air_date") or "",
        })
    return episodes


def merge_season(hu: list[dict], en: list[dict], original_language: str = "") -> list[dict]:
    """[{"season", "episode", "title_hu", "title", "overview_hu", "overview_en", "runtime", "air_date"}].

    A Hungarian name is dropped when it is a placeholder or just the English name again (unless the show itself
    is Hungarian); a Hungarian overview is kept only when translate.language() says it is Hungarian.
    """

    by_hu = {e["episode"]: e for e in hu}
    by_en = {e["episode"]: e for e in en}
    merged = []
    for number in sorted(set(by_hu) | set(by_en)):
        h, e = by_hu.get(number) or {}, by_en.get(number) or {}
        title = "" if is_placeholder(e.get("title", ""), number) else e.get("title", "")
        title_hu = h.get("title", "")
        if is_placeholder(title_hu, number) or (title_hu == title and original_language != "hu"):
            title_hu = ""
        overview_hu = h.get("overview", "")
        if overview_hu and (overview_hu == e.get("overview") or translate.language(overview_hu) != "hu"):
            overview_hu = ""
        merged.append({
            "season": h.get("season", e.get("season")), "episode": number, "title_hu": title_hu, "title": title or title_hu,
            "overview_hu": overview_hu, "overview_en": e.get("overview", ""),
            "runtime": e.get("runtime") or h.get("runtime", ""), "air_date": e.get("air_date") or h.get("air_date", ""),
        })
    return merged


# --- fetching --------------------------------------------------------------------------------------------------

def find(imdb_id: str) -> dict:
    imdb_id = (imdb_id or "").strip().lower()
    if not re.fullmatch(r"tt\d{5,10}", imdb_id):
        return {}
    return parse_find(_call(f"/find/{imdb_id}", external_source="imdb_id") or {})


def search(title: str, year: str = "", kind: str = "movie") -> list[dict]:
    if kind not in {"movie", "tv"} or not (title or "").strip():
        return []
    params = {"query": title.strip(), "include_adult": "false"}
    if re.fullmatch(r"\d{4}", str(year or "")):
        params["year" if kind == "movie" else "first_air_date_year"] = str(year)
    return parse_search(_call(f"/search/{kind}", **params) or {}, kind)


def details(kind: str, tmdb_id) -> dict:
    if kind not in {"movie", "tv"} or not str(tmdb_id or "").isdigit():
        return {}
    hu = _call(f"/{kind}/{tmdb_id}", language="hu-HU", append_to_response="credits,external_ids,translations") or {}
    en = _call(f"/{kind}/{tmdb_id}", language="en-US") or {}
    return parse_details(hu, en, kind)


def season(tv_id, number: int, language: str = "hu-HU") -> list[dict]:
    if not str(tv_id or "").isdigit() or number is None:
        return []
    return parse_season(_call(f"/tv/{tv_id}/season/{int(number)}", language=language) or {})


def season_episodes(tv_id, number: int, original_language: str = "") -> list[dict]:
    """merge_season() of the hu-HU and en-US episode lists of one season."""

    return merge_season(season(tv_id, number, "hu-HU"), season(tv_id, number, "en-US"), original_language)
