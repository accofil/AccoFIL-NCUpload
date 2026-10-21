"""Films and series (nCore hd/xvid/dvd/dvd9 films and series, _hun and plain).

Identification: the IMDb id from the hints (NFO or user) > TMDB find, else TMDB search by title + year, else
IMDb's suggestion API by name. Wikidata (P345) bridges to the PORT.hu id (P905) and the huwiki article; a PORT.hu
page found by search is accepted only when its IMDb link matches. TVmaze adds the show page and English episode
titles for series.

Hungarian title: PORT.hu > TMDB hu > Wikidata hu label > nCore. Plot: PORT.hu > TMDB hu (only when it really is
Hungarian) > huwiki summary > DeepL of the English TMDB overview > English. TMDB wins over nCore's IMDb data
(hints["ncore_imdb"]); every disagreement becomes a Hungarian warning.

Hints: imdb, title, year, season, episode, episodes ("1-6"), series (bool), ncore_imdb (nCore ajax imdb_movie:
movie_angol_cim, movie_magyar_cim, movie_megjelenes_eve, movie_orszag, movie_hossz, movie_rendezo,
movie_szereplok). Missing title/year/season/episode are taken from the release name.

Confidence: 1.0 IMDb id from the hints confirmed by TMDB or PORT.hu, 0.8 confirmed but with a year conflict,
0.7 name search with an exact title + year hit, 0.4 fuzzy name hit, 0 nothing found. At most 0.5 when TMDB/IMDb
call it a film/series against the category, or a film's release name has another year (wrong NFO id, remake).
An episode IMDb id is mapped to its show (TMDB), else the show is searched by name.
"""

from __future__ import annotations

import difflib
import re
from urllib.parse import quote

from .. import keys, net, parse, translate
from ..meta import Meta
from ..sources import mafab, porthu, tmdb, tvmaze, wikidata, wikipedia

IMDB_SUGGEST = "https://v3.sg.media-imdb.com/suggestion/x/{query}.json"
IMDB_TITLE = "https://www.imdb.com/title/{id}/"
SERIES_KINDS = {"TV series", "TV mini-series"}
MOVIE_KINDS = {"feature", "TV movie", "video"}
SERIES_CODES = {"xvidser", "dvdser", "hdser"}
_COUNTRY_FIX = {"United States": "USA", "United States of America": "USA", "United Kingdom": "UK", "England": "UK"}


# --- small helpers ---------------------------------------------------------------------------------------------

_norm = porthu.norm


def _int(*values) -> int | None:
    for value in values:
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().isdigit():
            return int(value)
    return None


def _year_diff(a, b) -> int:
    try:
        return abs(int(str(a)[:4]) - int(str(b)[:4]))
    except (TypeError, ValueError):
        return 0


def _similar(a: str, b: str) -> bool:
    a, b = _norm(a), _norm(b)
    return bool(a and b) and (a == b or difflib.SequenceMatcher(None, a, b).ratio() >= 0.8)


def _episode_set(text, single: int | None) -> set[int]:
    """{1, 2, ..., 6} from "1-6" / "01-06" / "1,3" / [1, 3]; {single} when only one episode is known; empty when
    unknown."""

    if isinstance(text, (list, tuple, set, frozenset)):
        text = ",".join(str(t) for t in text)
    elif isinstance(text, int) and not isinstance(text, bool):
        text = str(text)
    numbers: set[int] = set()
    for part in re.split(r"[,\s]+", text if isinstance(text, str) else ""):
        span = re.fullmatch(r"[Ee]?(\d{1,4})(?:\s*-\s*[Ee]?(\d{1,4}))?", part.strip())
        if span:
            first, last = int(span.group(1)), int(span.group(2) or span.group(1))
            numbers.update(range(first, min(last, first + 500) + 1))
    if not numbers and single is not None:
        numbers.add(single)
    return numbers


def _imdb_id(value) -> str:
    match = re.search(r"tt\d{5,10}", str(value or "").lower())
    return match.group(0) if match else ""


def _safe(meta: Meta, label: str, func, *args, default=None, **kwargs):
    """Call a source; a parsing bug in it becomes a warning instead of stopping the upload."""

    try:
        result = func(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - a broken source must never stop the resolver
        meta.warnings.append(f"{label}: hiba az adatok feldolgozásakor ({type(exc).__name__})")
        return default
    return default if result is None else result


def _split(text: str) -> list[str]:
    return [p.strip() for p in str(text or "").split(",") if p.strip()]


# --- IMDb suggestion API (the JSON behind imdb.com's search box) -------------------------------------------------

def parse_imdb_suggest(data) -> list[dict]:
    """[{"id", "title", "year", "kind", "stars"}] of the title hits."""

    hits = []
    for item in (data.get("d") if isinstance(data, dict) else None) or []:
        if isinstance(item, dict) and str(item.get("id", "")).startswith("tt"):
            hits.append({"id": item["id"], "title": item.get("l", ""), "year": str(item.get("y") or ""), "kind": item.get("q", ""),
                         "stars": item.get("s", "")})
    return hits


def imdb_suggest(text: str) -> list[dict] | None:
    """parse_imdb_suggest() for an id or a title; None when the service could not be reached."""

    text = (text or "").strip().lower()
    if not text:
        return []
    if not keys.flag("NCORE_IMDB_SUGGEST"):
        return None  # opt-in only: IMDb's terms forbid automated data gathering without consent
    data = net.get_json(IMDB_SUGGEST.format(query=quote(text)), quiet=True)
    return None if data is None else parse_imdb_suggest(data)


def _imdb_hit(imdb: str) -> dict | None:
    """The suggestion hit of exactly this id: {} when IMDb does not know it, None when IMDb could not be reached."""

    hits = imdb_suggest(imdb)
    return None if hits is None else next((h for h in hits if h["id"] == imdb), {})


# --- identification --------------------------------------------------------------------------------------------

def _pick(results: list[dict], title: str, year: str, filtered: bool = False) -> tuple[dict, bool]:
    """(best result, exact?) of TMDB search results; exact = same normalised title and the same year. A search
    already filtered by the year (TMDB matches any release date of that year: festival, local premiere) also
    accepts the main date one year off."""

    wanted = _norm(title)
    exact = [r for r in results if wanted in {_norm(r["title"]), _norm(r["original_title"])} and year
             and (r["year"] == year or (filtered and _year_diff(r["year"], year) <= 1))]
    if exact:
        return max(exact, key=lambda r: r["popularity"]), True
    close = [r for r in results if (_similar(r["title"], title) or _similar(r["original_title"], title))
             and (not year or not r["year"] or _year_diff(r["year"], year) <= 1)]
    return (close[0], False) if close else ({}, False)


def _tmdb_search(meta: Meta, title: str, year: str, kind: str) -> tuple[dict, bool]:
    other = "movie" if kind == "tv" else "tv"
    for search_kind, search_year in dict.fromkeys(((kind, year), (kind, ""), (other, year))):
        results = _safe(meta, "TMDB", tmdb.search, title, search_year, search_kind, default=[])
        hit, exact = _pick(results, title, year, filtered=bool(search_year))
        if hit:
            return hit, exact and search_kind == kind
    return {}, False


def _imdb_search(title: str, year: str, series: bool) -> tuple[dict, bool]:
    hits = imdb_suggest(f"{title} {year}".strip()) or []
    hits = [h for h in hits if h["kind"] in (SERIES_KINDS if series else MOVIE_KINDS)]
    exact = [h for h in hits if _norm(h["title"]) == _norm(title) and year and h["year"] == year]
    if exact:
        return exact[0], True
    close = [h for h in hits if _similar(h["title"], title) and (not year or _year_diff(h["year"], year) <= 1)]
    return (close[0], False) if close else ({}, False)


# --- episodes --------------------------------------------------------------------------------------------------

def _merge_episodes(season: int, port_eps: list[dict], tmdb_eps: list[dict], maze_eps: list[dict]) -> list[dict]:
    """One row per episode: PORT.hu's Hungarian title over TMDB's; the English title from TMDB, else TVmaze."""

    rows: dict[int, dict] = {}

    def row(number: int) -> dict:
        return rows.setdefault(number, {"season": season, "episode": number, "title_hu": "", "title": "", "plot_hu": "", "url": ""})

    english: set[int] = set()  # rows whose title is a real English one (merge_season falls back to the HU name)
    for item in tmdb_eps:
        entry = row(item["episode"])
        entry.update(title_hu=item.get("title_hu", ""), title=item.get("title", ""), plot_hu=item.get("overview_hu", ""))
        if entry["title"] and entry["title"] != entry["title_hu"]:
            english.add(item["episode"])
    for item in maze_eps:
        entry = row(item["episode"])
        name = item.get("title", "")
        if name and not tmdb.is_placeholder(name, item["episode"]) and item["episode"] not in english:
            entry["title"] = name
    for item in port_eps:
        entry = row(item["episode"])
        if item.get("title_hu"):
            entry["title_hu"] = item["title_hu"]
        entry["url"] = item.get("url", "")
    return [rows[n] for n in sorted(rows)]


# --- the resolver ----------------------------------------------------------------------------------------------

def resolve(release, code: str, *, original: bool = False, hints: dict | None = None) -> Meta:
    from ..local import nfo
    meta = Meta()
    nfo.annotate(meta, release)
    try:
        _resolve(meta, release, code or "", dict(hints or {}))
    except Exception as exc:  # noqa: BLE001 - last line of defence: a resolver never stops the upload
        meta.warnings.append(f"film/sorozat adatgyűjtés: váratlan hiba ({type(exc).__name__}: {exc})")
    nfo.description(meta, release)
    return meta


def _resolve(meta: Meta, release, code: str, hints: dict) -> None:
    from ..local import nfo
    from ..release import IMDB_RE
    name = str(getattr(release, "name", "") or "")
    parsed = parse.parse(name) if name else parse.ParsedName(title="")
    if "series" in hints:
        series = bool(hints["series"])
    elif code:
        series = code.replace("_hun", "") in SERIES_CODES
    else:
        series = parsed.season is not None
    kind = "tv" if series else "movie"
    title_hint = str(hints.get("title") or nfo.value(release, "title", "movie title", "film", "cím") or parsed.title or "").strip()
    years = [m.group(0) for v in (hints.get("year"), parsed.year, nfo.year(release)) for m in [re.search(r"(?:19|20)\d{2}", str(v or ""))] if m]
    year_hint = years[0] if years else ""
    season_no = _int(hints.get("season"), parsed.season)
    episode_no = _int(hints.get("episode"), parsed.episode if parsed.episode_last is None else None)
    wanted_eps = _episode_set(hints.get("episodes") or (parsed.episodes if parsed.episode_last else ""), episode_no)
    ncore_raw = hints.get("ncore_imdb")
    ncore = {str(k): str(v or "").strip() for k, v in (ncore_raw.items() if isinstance(ncore_raw, dict) else ())}
    imdb = _imdb_id(hints.get("imdb") or getattr(release, "imdb_id", "") or nfo.unique_match(release, IMDB_RE) or "")
    how = "hint" if imdb else ""
    has_key = bool(keys.get("TMDB_API"))
    if not has_key:
        meta.warnings.append("nincs TMDB_API kulcs: TMDB nélkül gyengébb az azonosítás (név szerinti keresés csak a PORT.hu-n és az IMDb-n)")

    # 1. TMDB (by IMDb id, else by name)
    found: dict = {}
    if imdb:
        found = _safe(meta, "TMDB", tmdb.find, imdb, default={})
        if found.get("episode") is not None or found.get("season") is not None:
            meta.identifiers["imdb_episode"] = imdb
            season_no = season_no if season_no is not None else _int(found.get("season"))
            if found.get("episode") is not None and episode_no is None and not wanted_eps:
                episode_no = _int(found.get("episode"))
                wanted_eps = {episode_no} if episode_no is not None else set()
        elif not found and (_safe(meta, "IMDb", _imdb_hit, imdb, default={}) or {}).get("kind") == "TV episode":
            # an episode id that TMDB cannot map to its show: find the show by name, keep the id as the episode's
            meta.identifiers["imdb_episode"] = imdb
            meta.warnings.append(f"az IMDb-azonosító egy epizódé ({imdb}), a TMDB nem ismeri: a sorozatot név alapján keresem")
            imdb, how = "", ""
    if not imdb and not found and title_hint and has_key:
        hit, exact = _tmdb_search(meta, title_hint, year_hint, kind)
        if hit:
            found, how = {"kind": hit["kind"], "id": hit["id"]}, "exact" if exact else "fuzzy"
    td = _safe(meta, "TMDB", tmdb.details, found["kind"], found["id"], default={}) if found else {}

    # 2. IMDb suggestion when neither the hints nor TMDB gave an id
    if not imdb and not td.get("imdb") and title_hint:
        hit, exact = _safe(meta, "IMDb", _imdb_search, title_hint, year_hint, series, default=({}, False))
        if hit and td and not (_similar(hit["title"], td.get("title", "")) and _year_diff(hit["year"], td.get("year")) <= 1):
            hit = {}  # IMDb's guess is another title than TMDB's: keep TMDB's, without an IMDb id
        if hit:
            imdb, how = hit["id"], how or ("exact" if exact else "fuzzy")
            meta.add_source("IMDb")
            if not td:
                found = _safe(meta, "TMDB", tmdb.find, imdb, default={})
                td = _safe(meta, "TMDB", tmdb.details, found["kind"], found["id"], default={}) if found else {}
            meta.warnings.append(f"IMDb-azonosító a név alapján: {imdb} ({hit['title']} {hit['year']}) — ellenőrizd!")
    id_conflict = False  # TMDB's record names another IMDb id than the one given
    if td:
        meta.add_source("TMDB")
        if imdb and meta.identifiers.get("imdb_episode") == imdb and td.get("imdb"):
            meta.warnings.append(f"az IMDb-azonosító egy epizódé ({imdb}); a sorozatét használom: {td['imdb']}")
            imdb = td["imdb"]
        elif td.get("imdb") and imdb and td["imdb"] != imdb:
            id_conflict = True
            meta.warnings.append(f"a TMDB más IMDb-azonosítót ad: {td['imdb']} (megadva: {imdb}) — a megadottat használom")
        elif td.get("imdb") and not imdb:
            imdb = td["imdb"]
            meta.warnings.append(f"IMDb-azonosító a TMDB keresésből: {imdb} ({td.get('title')} {td.get('year')}) — ellenőrizd!")
    tmdb_kind = td.get("kind") or found.get("kind") or ""
    doubt = False  # the identification may be wrong: never good enough for an automatic upload
    if tmdb_kind and tmdb_kind != kind:
        doubt = True
        meta.warnings.append("a TMDB szerint ez " + ("sorozat" if tmdb_kind == "tv" else "film") + ", a kategória szerint "
                             + ("sorozat" if series else "film") + " — ellenőrizd a kategóriát")

    # 3. Wikidata: PORT.hu id, Hungarian label, huwiki article
    wd: dict = {}
    if td.get("wikidata"):
        wd = _safe(meta, "Wikidata", lambda q: wikidata.parse_entity(wikidata.entity(q)), td["wikidata"], default={})
        if wd and imdb and (wd.get("claims") or {}).get("P345") and imdb not in wd["claims"]["P345"]:
            wd = {}
    if not wd and imdb:
        wd = _safe(meta, "Wikidata", wikidata.lookup, "imdb", imdb, default={})
    if wd:
        meta.add_source("Wikidata")

    # 4. PORT.hu
    year_guess = td.get("year") or ncore.get("movie_megjelenes_eve") or year_hint
    titles = [td.get("title_hu"), wd.get("label_hu"), ncore.get("movie_magyar_cim"), td.get("title_original"), td.get("title"),
              ncore.get("movie_angol_cim"), title_hint]
    titles = [t for t in titles if t]
    port: dict = {}
    port_kind = tmdb_kind or kind
    if imdb:
        port_id = wikidata.claim(wd, "porthu") if wd else None
        port = _safe(meta, "PORT.hu", porthu.find_by_imdb, imdb, titles, year_guess, porthu_id=port_id, kind=port_kind,
                     runtime=td.get("runtime", ""), default={})
    elif titles and year_guess:
        port = _safe(meta, "PORT.hu", porthu.find, titles, year_guess, kind=port_kind, runtime=td.get("runtime", ""), default={})
        if port and not how:
            how = "fuzzy"
    if port:
        meta.add_source("PORT.hu")
        if not imdb and port.get("imdb"):
            imdb = port["imdb"]
        if port.get("match") == "title":
            meta.warnings.append(f"PORT.hu-adatlap cím és év alapján (nincs rajta IMDb-link): {port.get('url')} — ellenőrizd!")

    hungarian_page = _safe(meta, "MAFAB", mafab.find_by_imdb, imdb, titles, str(year_guess or ""), default={}) if imdb and not port else {}
    if hungarian_page:
        meta.add_source("MAFAB")

    # 5. TVmaze (series)
    maze: dict = {}
    if series:
        maze = _safe(meta, "TVmaze", tvmaze.lookup, imdb, default={}) if imdb else {}
        if not maze and title_hint and not imdb:
            hits = _safe(meta, "TVmaze", tvmaze.search, title_hint, default=[])
            maze = next((h for h in hits if _norm(h["name"]) == _norm(title_hint) and (not year_hint or h["year"] == year_hint)), {})
        if maze:
            meta.add_source("TVmaze")

    # 6. fields
    meta.kind = tmdb_kind or kind
    meta.title = td.get("title") or ncore.get("movie_angol_cim") or port.get("title_original") or maze.get("name") or title_hint
    meta.title_original = td.get("title_original") or port.get("title_original") or meta.title
    hu_source, meta.title_hu = next(((s, t) for s, t in (("PORT.hu", port.get("title")), ("TMDB", td.get("title_hu")),
                                                        ("Wikidata", wd.get("label_hu")), ("nCore", ncore.get("movie_magyar_cim")), ("MAFAB", hungarian_page.get("title"))) if t),
                                     ("", ""))
    meta.year = td.get("year") or port.get("year") or ncore.get("movie_megjelenes_eve") or maze.get("year") or year_hint
    meta.date = td.get("date", "")
    meta.creators = list(td.get("directors") or port.get("directors", [])[:3] or _split(ncore.get("movie_rendezo"))[:3])
    cast = td.get("cast") or port.get("cast", [])[:5] or _split(ncore.get("movie_szereplok"))[:5]
    if cast:
        meta.extra["cast"] = ", ".join(cast)
    countries = td.get("countries") or [_COUNTRY_FIX.get(c, c) for c in _split(ncore.get("movie_orszag"))]
    if countries:
        meta.extra["countries"] = ", ".join(countries)
    runtime = td.get("runtime") or port.get("runtime") or ncore.get("movie_hossz") or maze.get("runtime") or ""
    if runtime:
        meta.extra["runtime"] = str(runtime)
    genres = _split(port.get("genre", "")) + [g.lower() for g in td.get("genres_hu", [])]
    meta.genres = list(dict.fromkeys(genres)) or list(td.get("genres_en", []))
    meta.cover_url = td.get("poster_url") or port.get("image") or maze.get("image") or ""
    for key, value in (("imdb", imdb), ("tmdb", td.get("id") or found.get("id")), ("tmdb_kind", tmdb_kind), ("porthu", port.get("id")),
                       ("wikidata", wd.get("qid") or td.get("wikidata")), ("tvmaze", maze.get("id"))):
        if value:
            meta.identifiers[key] = str(value)
    links = {"porthu_url": port.get("url", ""), "mafab_url": hungarian_page.get("url", ""), "tmdb_url": td.get("url", ""), "tvmaze_url": maze.get("url", ""),
             "imdb_url": IMDB_TITLE.format(id=imdb) if imdb else ""}
    meta.extra.update({k: v for k, v in links.items() if v})
    meta.add_proof(links["porthu_url"])
    meta.add_proof(links["imdb_url"])
    # Prefer a verified Hungarian page also for foreign-audio releases; IMDb is always kept separately.
    order = ("porthu_url", "mafab_url", "tvmaze_url", "tmdb_url") if series else ("porthu_url", "mafab_url", "tmdb_url", "tvmaze_url")
    other = next((links[k] for k in order if links[k]), "")
    if other:
        meta.extra["other_link"] = other

    # 7. plot
    _describe(meta, port, td, wd, maze)

    # 8. episodes
    if series and season_no is not None:
        _episodes(meta, season_no, wanted_eps, port, td, maze)
    elif series and season_no is None:
        meta.warnings.append("sorozat, de nem tudom az évadot: add meg (pl. --season 1)")

    # 9. cross-checks and confidence
    conflict, imdb_known, doubtful = _cross_check(meta, td, port, ncore, imdb, year_hint, series, hu_source)
    conflict = conflict or id_conflict
    if how == "hint":
        agree = bool(td) or port.get("match") in {"imdb", "wikidata"}
        meta.confidence = (0.8 if conflict else 1.0) if agree else (0.5 if port or ncore or imdb_known else 0.0)
        if not agree:
            meta.warnings.append(f"az IMDb-azonosítót ({imdb}) sem a TMDB, sem a PORT.hu nem erősítette meg")
    elif how == "exact":
        meta.confidence = 0.7
    elif how == "fuzzy":
        meta.confidence = 0.4
        # Series names rarely carry a year: an exact title that PORT.hu confirms by IMDb id (or TVmaze by IMDb lookup)
        # is two independent sources agreeing on the same title, which is as good as an exact title+year hit.
        exact_title = _norm(title_hint) in {_norm(t) for t in (td.get("title"), td.get("title_original"), port.get("title_original"),
                                                                 port.get("title"), maze.get("name")) if t}
        confirmed = port.get("match") in {"imdb", "wikidata"} or bool(maze and maze.get("imdb") and maze.get("imdb") == imdb)
        if exact_title and confirmed and not conflict:
            meta.confidence = 0.7
    else:
        meta.confidence = 0.0
    if (doubt or doubtful) and meta.confidence > 0.5:
        meta.confidence = 0.5
        meta.warnings.append("bizonytalan azonosítás (a kategória vagy a release évszáma nem illik az adatbázisokhoz): "
                             "automatikus feltöltés helyett ellenőrizd")
    if not imdb:
        meta.warnings.append("nem találtam IMDb-azonosítót: add meg (--imdb tt...)")
    if not (td or port or meta.identifiers.get("imdb")):
        meta.confidence = 0.0
        meta.warnings.append("a filmet/sorozatot nem sikerült azonosítani: add meg az IMDb-azonosítót")


def _describe(meta: Meta, port: dict, td: dict, wd: dict, maze: dict) -> None:
    english, english_source = (td.get("overview_en"), tmdb.CREDIT) if td.get("overview_en") else (maze.get("summary", ""), tvmaze.CREDIT)
    meta.description_en = english or ""
    if port.get("description"):
        meta.description_hu, meta.description_source = port["description"], porthu.CREDIT
        return
    if td.get("overview_hu") and translate.language(td["overview_hu"]) == "hu":
        meta.description_hu, meta.description_source = td["overview_hu"], tmdb.CREDIT
        return
    if wd.get("huwiki"):
        summary = _safe(meta, "Wikipédia", wikipedia.summary, wd["huwiki"], "hu", default={})
        if summary.get("extract"):
            meta.description_hu, meta.description_source = summary["extract"], wikipedia.CREDIT
            meta.add_source("Wikipédia")
            return
    if english:
        text, translated = _safe(meta, "DeepL", translate.to_hungarian, english, default=(english, False))
        if translated and text:
            meta.description_hu, meta.description_source, meta.machine_translated = text, english_source, True
            meta.warnings.append("a magyar ismertető gépi fordítás (DeepL): olvasd át")
            return
        meta.description_source = english_source
        meta.warnings.append("nincs magyar ismertető: az angol szöveg maradt (DEEPL_KEY-jel gépi fordítás lehetne) — írd át, ha van magyar")
        return
    meta.warnings.append("nem találtam ismertetőt (sem magyar, sem angol)")


def _episodes(meta: Meta, season: int, wanted: set[int], port: dict, td: dict, maze: dict) -> None:
    port_eps: list[dict] = []
    seasons = port.get("seasons") or {}  # a page without a season list has no episode data at all
    if season in seasons:
        port_eps = _safe(meta, "PORT.hu", porthu.season, port.get("id"), season, seasons[season], default=[])
    elif seasons:
        meta.warnings.append(f"a PORT.hu-n nincs {season}. évad (van: {', '.join(str(s) for s in sorted(seasons))})")
    tmdb_eps = []
    if td.get("kind") == "tv":
        tmdb_eps = _safe(meta, "TMDB", tmdb.season_episodes, td["id"], season, td.get("original_language", ""), default=[])
    maze_eps = _safe(meta, "TVmaze", tvmaze.episodes, maze["id"], season, default=[]) if maze.get("id") else []
    if port_eps and tmdb_eps and abs(len(port_eps) - len(tmdb_eps)) > 1:
        meta.warnings.append(f"a PORT.hu és a TMDB epizódszáma eltér a {season}. évadban ({len(port_eps)} / {len(tmdb_eps)}) — "
                             "ellenőrizd az epizódcímeket")
    rows = _merge_episodes(season, port_eps, tmdb_eps, maze_eps)
    if wanted:
        missing = sorted(wanted - {r["episode"] for r in rows})
        rows = [r for r in rows if r["episode"] in wanted]
        if missing and (port_eps or tmdb_eps or maze_eps):
            meta.warnings.append("nincs adat ezekről az epizódokról: " + ", ".join(f"S{season:02d}E{n:02d}" for n in missing[:10]))
    meta.episodes = [{"season": r["season"], "episode": r["episode"], "title_hu": r["title_hu"], "title": r["title"]} for r in rows]
    # the series-level TMDB runtime may be the finale's (last_episode_to_air): the median of these episodes is better
    numbers = {r["episode"] for r in rows}
    for source in (tmdb_eps, maze_eps):
        minutes = sorted(int(e["runtime"]) for e in source if e.get("episode") in numbers and str(e.get("runtime") or "").isdigit())
        if minutes:
            meta.extra["runtime"] = str(minutes[len(minutes) // 2])
            break
    if not rows and not (port_eps or tmdb_eps or maze_eps):
        meta.warnings.append(f"nem találtam epizódlistát a {season}. évadhoz")
    if len(wanted) == 1 and rows:
        row = rows[0]
        title = row["title_hu"] or row["title"]
        if title:
            meta.extra["episode_title"] = title
        if not row["title_hu"] and row["title"]:
            meta.warnings.append(f"nincs magyar epizódcím (S{season:02d}E{row['episode']:02d}): az angolt használom")
        plot = ""
        plot_source = ""
        if row["url"]:
            plot = _safe(meta, "PORT.hu", porthu.episode, row["url"], default={}).get("description", "")
            plot_source = porthu.CREDIT if plot else ""
        if not plot:
            plot, plot_source = row.get("plot_hu", ""), "TMDB"
        if plot:
            meta.extra["episode_plot"] = plot
            meta.description_hu, meta.description_source = plot, plot_source
            meta.machine_translated = False


def _cross_check(meta: Meta, td: dict, port: dict, ncore: dict, imdb: str, year_hint: str, series: bool,
                 hu_source: str = "") -> tuple[bool, bool, bool]:
    """Hungarian warnings for every disagreement. (the sources disagree about the year?, IMDb knows the id?, the id
    may belong to another title: a film whose release name has another year, or IMDb calls it a series/film
    against the category?)"""

    conflict = known = doubtful = False
    final_source = "TMDB" if td else "PORT.hu" if port else "release-név"
    if ncore:
        meta.add_source("nCore IMDb")
    if ncore and (td or port):
        checks = (
            ("év", ncore.get("movie_megjelenes_eve"), meta.year, final_source, lambda a, b: _year_diff(a, b) > 1),
            ("angol cím", ncore.get("movie_angol_cim"), meta.title, final_source,
             lambda a, b: _norm(a) not in {_norm(b), _norm(meta.title_original)}),
            ("magyar cím", ncore.get("movie_magyar_cim"), meta.title_hu, hu_source or final_source, lambda a, b: _norm(a) != _norm(b)),
            ("hossz", ncore.get("movie_hossz"), meta.extra.get("runtime", ""), final_source, _runtime_differs),
        )
        for label, theirs, ours, source, differs in checks:
            if theirs and ours and source != "nCore" and differs(theirs, ours):
                meta.warnings.append(f"nCore IMDb-adat eltér ({label}): nCore „{theirs}” / {source} „{ours}” — a {source} adatát használom")
    if td.get("year") and port.get("year") and _year_diff(td["year"], port["year"]) > 1:
        conflict = True
        meta.warnings.append(f"évszám eltér: TMDB {td['year']} / PORT.hu {port['year']}")
    if year_hint and meta.year and _year_diff(year_hint, meta.year) > 1:
        doubtful = doubtful or not series  # a series name may carry a revival/season year; a film's is its own
        meta.warnings.append(f"a release nevében más év áll ({year_hint}), az adatbázisokban {meta.year} — ellenőrizd az azonosítást")
    if imdb:
        hits = _safe(meta, "IMDb", imdb_suggest, imdb, default=None)
        if hits is not None:
            hit = next((h for h in hits if h["id"] == imdb), None)
            if hit is None:
                meta.warnings.append(f"az IMDb keresője nem ismeri ezt az azonosítót: {imdb} — ellenőrizd")
            else:
                known = True
                meta.add_source("IMDb")
                if hit["year"] and meta.year and _year_diff(hit["year"], meta.year) > 1:
                    conflict = True
                    meta.warnings.append(f"évszám eltér: IMDb {hit['year']} / {final_source} {meta.year}")
                names = [t for t in (meta.title, meta.title_original, meta.title_hu) if t]
                if hit["title"] and names and not any(_similar(hit["title"], t) for t in names):
                    meta.warnings.append(f"cím eltér: IMDb „{hit['title']}” / {final_source} „{meta.title}”")
                if hit["kind"] and (hit["kind"] in SERIES_KINDS) != series and hit["kind"] in SERIES_KINDS | MOVIE_KINDS:
                    doubtful = True
                    meta.warnings.append(f"az IMDb szerint ez {hit['kind']} — ellenőrizd a kategóriát (film/sorozat)")
    return conflict, known, doubtful


def _runtime_differs(theirs: str, ours: str) -> bool:
    try:
        a, b = int(re.sub(r"\D", "", theirs) or 0), int(re.sub(r"\D", "", ours) or 0)
    except ValueError:
        return False
    return bool(a and b) and abs(a - b) > max(5, 0.1 * max(a, b))
