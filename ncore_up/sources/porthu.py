"""PORT.hu: Hungarian titles, editorial plots, genres and Hungarian episode titles of films and series.

No key. The first request of a session answers 302 via daemon.indapass.hu (an SSO session check, not a bot
challenge); the shared ``net`` session keeps the cookie, so later requests go straight through. robots.txt allows
/adatlap, /kereso and /search for generic agents: keep this a low-volume per-upload lookup (paced, cached). The
plots are copyrighted editorial text: credit "Forrás: PORT.hu". The detail page is a valid nCore proof link.

URL scheme (the slug is ignored, the id decides):

    detail   https://port.hu/adatlap/film/tv/-/movie-{id}
    season   https://port.hu/adatlap/sorozat/tv/{slug}/evad-szerint/movie-{id}/{season}
    episode  https://port.hu/adatlap/sorozat/tv/{episode-slug}/{series-slug}/episode-{id}
    suggest  https://port.hu/search/suggest-list?q=...            (JSON, 5 mixed hits)
    search   https://port.hu/kereso?q=...&type=movie&s=relevance  (HTML, films/series only)

Detail pages carry JSON-LD (@type Movie, also for series): name, description, genre, duration, countryOfOrigin,
director, actor, image. Its ``datePublished`` is the bogus value ``true``: the year comes from div.summary
("amerikai sci-fi, 144 perc, 1999"). The IMDb id is the a.logo-imdb link. On an episode page the JSON-LD
TVEpisode holds the episode plot, while og:description holds the plot of the whole series.
"""

from __future__ import annotations

import html
import json
import re
import unicodedata

from .. import net
from . import wikidata

BASE = "https://port.hu"
SUGGEST = BASE + "/search/suggest-list"
SEARCH = BASE + "/kereso"
DETAIL = BASE + "/adatlap/film/tv/-/movie-{id}"
SEASON = BASE + "/adatlap/sorozat/tv/-/evad-szerint/movie-{id}/{season}"
CREDIT = "PORT.hu"

_ID_RE = re.compile(r"/movie-(\d+)")
_YEAR_END_RE = re.compile(r"((?:19|20)\d{2})(?:\s*[-–]\s*(?:(?:19|20)\d{2})?)?\s*$")
_LD_RE = re.compile(r"<script[^>]*type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>", re.S | re.I)
_IMDB_RE = re.compile(r"imdb\.com/(?:[a-z]{2}/)?title/(tt\d{5,10})", re.I)
_PLACEHOLDER_RE = re.compile(r"(?i)^(?:\d+\.\s*(?:rész|epizód)|(?:rész|epizód)\s*\d+)$")
_ROMAN = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100}


# --- helpers ---------------------------------------------------------------------------------------------------

def norm(text: str) -> str:
    """Comparable form of a title: lower case, no accents or punctuation, '&' read as 'and', apostrophes dropped
    ("Schindler's List" = release name "Schindlers.List")."""

    text = unicodedata.normalize("NFKD", (text or "").lower().replace("&", " and "))
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"['`’ʼ]", "", text)
    return re.sub(r"[^0-9a-z]+", " ", text).strip()


def _text(fragment) -> str:
    fragment = fragment if isinstance(fragment, str) else ""
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def _placeholder(title: str, number: int | None = None) -> bool:
    """"3. rész", "Epizód 3", or just the episode number ("2" on the Chernobyl season page)."""

    title = (title or "").strip()
    if _PLACEHOLDER_RE.match(title):
        return True
    return title.isdigit() and (number is None or int(title) == number)


def _plot(text) -> str:
    """Plot text with its own paragraph breaks kept (JSON-LD separates them with \\r\\n)."""

    if not isinstance(text, str):
        return ""
    lines = (re.sub(r"[ \t\xa0]+", " ", line).strip() for line in re.split(r"\r\n|\r|\n", html.unescape(text)))
    return "\n".join(line for line in lines if line)


def _https(url: str) -> str:
    """Absolute https URL without query/fragment (the session hop leaves "?token=..." in og:url)."""

    url = re.sub(r"[?#].*$", "", (url or "").strip())
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return BASE + url
    return re.sub(r"^http://", "https://", url)


def _attr(attrs: str, name: str) -> str:
    match = re.search(rf"\b{name}\s*=\s*\"([^\"]*)\"", attrs or "")
    return html.unescape(match.group(1)) if match else ""


def _meta(page: str, prop: str) -> str:
    for attrs in re.findall(r"<meta\b([^>]*)>", page or "", re.I):
        if _attr(attrs, "property") == prop or _attr(attrs, "name") == prop:
            return _attr(attrs, "content").strip()
    return ""


def _ld_json(page: str, types: tuple[str, ...]) -> dict:
    for block in _LD_RE.findall(page or ""):
        try:
            data = json.loads(block, strict=False)
        except ValueError:
            continue
        for item in data if isinstance(data, list) else [data]:
            if not isinstance(item, dict):
                continue
            kinds = item.get("@type") if isinstance(item.get("@type"), list) else [item.get("@type")]
            if any(isinstance(k, str) and k in types for k in kinds):
                return item
    return {}


def _names(value) -> list[str]:
    items = value if isinstance(value, list) else [value] if value else []
    names = [(item.get("name") if isinstance(item, dict) else item) for item in items]
    return [html.unescape(str(n)).strip() for n in names if n and str(n).strip()]


def _minutes(duration) -> str:
    match = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?", str(duration or "").strip())
    if not match or not any(match.groups()):
        return ""
    return str(int(match.group(1) or 0) * 60 + int(match.group(2) or 0))


def _roman(text: str) -> int:
    values = [_ROMAN.get(c, 0) for c in text.upper()]
    return sum(-v if i + 1 < len(values) and v < values[i + 1] else v for i, v in enumerate(values))


def _year_ok(a, b, slack: int = 1) -> bool:
    """Unknown years never contradict."""

    try:
        return abs(int(a) - int(b)) <= slack
    except (TypeError, ValueError):
        return True


def _summary(page: str) -> str:
    """Text of the first div.summary span: "amerikai sci-fi, 144 perc, 1999"."""

    match = re.search(r"<div class=\"summary\">(.*?)</div>", page or "", re.S)
    if not match:
        return ""
    span = re.search(r"<span>(.*?)</span>", match.group(1), re.S)
    return _text(span.group(1) if span else match.group(1))


def _original_title(page: str) -> str:
    match = re.search(r"<div class=\"original-title\">\s*<small>(.*?)</small>", page or "", re.S)
    return _text(match.group(1)) if match else ""


def _imdb(page: str) -> str:
    for attrs in re.findall(r"<a\b([^>]*logo-imdb[^>]*)>", page or ""):
        match = _IMDB_RE.search(_attr(attrs, "href"))
        if match:
            return match.group(1).lower()
    return ""


# --- parsers ---------------------------------------------------------------------------------------------------

def parse_suggest(data) -> list[dict]:
    """[{"id", "title", "year", "subtitle", "url"}] of the film/series hits of suggest-list (other categories dropped)."""

    hits = []
    for item in data if isinstance(data, list) else []:
        category = item.get("category") if isinstance(item, dict) else None
        if (category.get("name") if isinstance(category, dict) else category) != "movie":
            continue
        url = item.get("url") if isinstance(item.get("url"), str) else ""
        match = _ID_RE.search(url)
        if not match:
            continue
        subtitle = _text(item.get("subtitle") or "")
        year = _YEAR_END_RE.search(subtitle)
        hits.append({"id": match.group(1), "title": _text(item.get("name") or ""), "year": year.group(1) if year else "",
                     "subtitle": subtitle, "url": _https(url)})
    return hits


def parse_search(page: str) -> list[dict]:
    """The same shape as parse_suggest() from the full search page (article[data-category=movie])."""

    hits = []
    for block in re.findall(r"<article data-category=\"movie\">(.*?)</article>", page or "", re.S):
        for attrs, label in re.findall(r"<a\b([^>]*)>(.*?)</a>", block, re.S):
            if "title" not in _attr(attrs, "class").split():
                continue
            match = _ID_RE.search(_attr(attrs, "href"))
            if not match:
                break
            summary = re.search(r"<label class=\"summary\">(.*?)</label>", block, re.S)
            subtitle = _text(summary.group(1)) if summary else ""
            year = _YEAR_END_RE.search(subtitle)
            hits.append({"id": match.group(1), "title": _text(label), "year": year.group(1) if year else "",
                         "subtitle": subtitle, "url": _https(_attr(attrs, "href"))})
            break
    return hits


def parse_detail(page: str) -> dict:
    """{"id", "url", "kind" (movie/tv), "title", "title_original", "description", "genre", "countries" (Hungarian
    adjectives), "runtime", "year", "directors", "cast", "image", "imdb", "seasons": {number: url}, "summary"};
    {} when the page is no film/series page."""

    ld = _ld_json(page, ("Movie", "TVSeries"))
    url = _https(_meta(page, "og:url"))
    match = _ID_RE.search(url)
    if not ld and not match:
        return {}
    port_id = match.group(1) if match else ""
    summary = _summary(page)
    year = _YEAR_END_RE.search(summary)
    runtime = _minutes(ld.get("duration"))
    if not runtime:
        perc = re.search(r"(\d+)\s*perc", summary)
        runtime = perc.group(1) if perc else ""
    genre = ", ".join(_names(ld.get("genre")))
    seasons = {}
    for href, sid, number in re.findall(r"href=\"(/adatlap/sorozat/[^\"]*?/evad-szerint/movie-(\d+)/(\d+))\"", page or ""):
        if not port_id or sid == port_id:
            seasons.setdefault(int(number), _https(href))
    image = ld.get("image")
    image = image[0] if isinstance(image, list) and image else image
    image = (image.get("url") if isinstance(image, dict) else image if isinstance(image, str) else "") or _meta(page, "og:image")
    title = _text(ld.get("name") or "")
    if not title:
        h1 = re.search(r"<h1[^>]*>(.*?)</h1>", page or "", re.S)
        title = _text(h1.group(1)) if h1 else ""
    return {
        "id": port_id,
        "url": url or (DETAIL.format(id=port_id) if port_id else ""),
        "kind": "tv" if seasons or "sorozat" in genre or "sorozat" in summary else "movie",
        "title": title,
        "title_original": _original_title(page),
        "description": _plot(ld.get("description")),
        "genre": genre,
        "countries": _names(ld.get("countryOfOrigin")),
        "runtime": runtime,
        "year": year.group(1) if year else "",
        "directors": _names(ld.get("director")),
        "cast": _names(ld.get("actor")),
        "image": _https(str(image or "")),
        "imdb": _imdb(page),
        "seasons": seasons,
        "summary": summary,
    }


def parse_season(page: str, season: int | None = None) -> list[dict]:
    """[{"season", "episode", "title_hu", "url", "label"}] from a season page (schema.org TVEpisode microdata).

    The season comes from the "I/1" label when present, else from ``season``. Placeholder titles ("3. rész", or
    just the episode number) give an empty ``title_hu``.
    """

    episodes = []
    for block in re.findall(r"<div[^>]*itemprop=\"episode\"[^>]*>(.*?)</div>", page or "", re.S):
        link = re.search(r"<a\b([^>]*)>(.*?)</a>", block, re.S)
        if not link:
            continue
        label_match = re.search(r"<span class=\"ep-number\">(.*?)</span>", link.group(2), re.S)
        label = _text(label_match.group(1)) if label_match else ""
        title = _text(re.sub(r"<span class=\"ep-number\">.*?</span>", " ", link.group(2), flags=re.S))
        number = re.search(r"itemprop=\"episodeNumber\"\s+content=\"(\d+)\"", block)
        parts = re.match(r"([IVXLC]+)\s*/\s*(\d+)", label)
        ep_season = _roman(parts.group(1)) if parts else season
        ep_number = int(number.group(1)) if number else int(parts.group(2)) if parts else None
        if ep_number is None:
            continue
        episodes.append({"season": ep_season, "episode": ep_number, "title_hu": "" if _placeholder(title, ep_number) else title,
                         "url": _https(_attr(link.group(1), "href")), "label": label})
    return episodes


def parse_episode(page: str) -> dict:
    """{"title", "title_original", "description", "show_description", "season", "episode", "runtime", "year"} of an
    episode page; ``description`` is the episode's own plot, ``show_description`` the plot of the series."""

    ld = _ld_json(page, ("TVEpisode",))
    description = _plot(ld.get("description"))
    if not description:
        match = re.search(r"<strong>\s*Az aktuális rész ismertetője:\s*</strong>(.*?)</p>", page or "", re.S)
        description = _text(match.group(1)) if match else ""
    if not ld and not description:
        return {}
    summary = _summary(page)
    year = _YEAR_END_RE.search(summary)
    perc = re.search(r"(\d+)\s*perc", summary)
    original = _original_title(page)
    season = (ld.get("partOfSeason") or {}).get("seasonNumber") if isinstance(ld.get("partOfSeason"), dict) else None
    title = _text(ld.get("name") or "")
    number = int(ld["episodeNumber"]) if str(ld.get("episodeNumber", "")).isdigit() else None
    return {
        "title": "" if _placeholder(title, number) else title,
        "title_original": original.split(" / ", 1)[1].strip() if " / " in original else "",
        "description": description,
        "show_description": _plot(_meta(page, "og:description")),
        "season": int(season) if str(season or "").isdigit() else None,
        "episode": number,
        "runtime": perc.group(1) if perc else "",
        "year": year.group(1) if year else "",
    }


# --- fetching --------------------------------------------------------------------------------------------------

def suggest(query: str) -> list[dict]:
    query = (query or "").strip()
    if not query:
        return []
    return parse_suggest(net.get_json(SUGGEST, params={"q": query}))  # requests percent-encodes the UTF-8 query


def search(query: str) -> list[dict]:
    query = (query or "").strip()
    if not query:
        return []
    return parse_search(net.get_text(SEARCH, params={"q": query, "type": "movie", "s": "relevance"}) or "")


def detail(port_id) -> dict:
    """parse_detail() of the page with this id (digits or 'movie-123'); {} when missing."""

    match = re.fullmatch(r"(?:movie-)?(\d+)", str(port_id or "").strip())
    if not match:
        return {}
    info = parse_detail(net.get_text(DETAIL.format(id=match.group(1))) or "")
    if info and not info["id"]:
        info["id"] = match.group(1)
    return info


def season(port_id, number: int, url: str = "") -> list[dict]:
    """Episodes of one season; ``url`` is the season link from parse_detail()["seasons"] when known."""

    if not url and not re.fullmatch(r"\d+", str(port_id or "")):
        return []
    page = net.get_text(url or SEASON.format(id=port_id, season=int(number))) or ""
    return [e for e in parse_season(page, int(number)) if e["season"] == int(number)]


def episode(url: str) -> dict:
    url = _https(url)
    if not url.startswith(BASE + "/adatlap/"):
        return {}
    return parse_episode(net.get_text(url) or "")


def _accepts(page: dict, wanted: set[str], year, imdb: str, kind: str, runtime) -> str:
    """How ``page`` matches: "imdb", "title" or '' (rejected)."""

    if not page:
        return ""
    if imdb and page.get("imdb"):
        return "imdb" if page["imdb"] == imdb else ""
    names = {norm(page.get("title", "")), norm(page.get("title_original", ""))} - {""}
    if not names & wanted or not year or not page.get("year") or not _year_ok(page["year"], year):
        return ""
    if kind and page.get("kind") and page["kind"] != kind:
        return ""
    try:
        ours, theirs = int(runtime), int(page.get("runtime") or 0)
    except (TypeError, ValueError):
        ours = theirs = 0
    if ours and theirs and abs(ours - theirs) > max(10, ours * 0.15):
        return ""
    return "title"


def find(titles, year="", *, imdb: str = "", porthu_id: str = "", kind: str = "", runtime="", budget: int = 4) -> dict:
    """parse_detail() of the PORT.hu page of a film/series plus "match" ("wikidata", "imdb" or "title"); {} when none.

    A ``porthu_id`` (Wikidata P905) is taken unless that page links another IMDb title. Otherwise suggest-list, then
    the full search, is queried with each title in turn; a candidate is accepted only when its IMDb link equals
    ``imdb``, or (when the page has no IMDb link, or ``imdb`` is unknown) when its Hungarian or original title and
    its year (±1) match, the kind agrees and the runtime is close. At most ``budget`` detail pages are fetched.
    """

    imdb = (imdb or "").lower()
    tried: set[str] = set()
    if porthu_id:
        page = detail(porthu_id)
        if page and (not imdb or not page.get("imdb") or page["imdb"] == imdb):
            page["match"] = "wikidata"
            return page
        tried.add(str(porthu_id))
    queries = {}
    for title in titles or []:
        if title and norm(title) and norm(title) not in queries:
            queries[norm(title)] = title.strip()
    wanted = set(queries)
    for query in queries.values():
        exact_seen = False
        for lookup in (suggest, search):
            if lookup is search and exact_seen:
                break
            hits = [h for h in lookup(query) if h["id"] not in tried and _year_ok(h["year"], year)]
            hits.sort(key=lambda h: (norm(h["title"]) not in wanted, not h["year"]))
            exact_seen = any(norm(h["title"]) in wanted for h in hits)
            for hit in hits[:3]:
                if budget <= 0:
                    return {}
                tried.add(hit["id"])
                budget -= 1
                page = detail(hit["id"])
                how = _accepts(page, wanted, year, imdb, kind, runtime)
                if how:
                    page["match"] = how
                    return page
    return {}


def find_by_imdb(imdb: str, titles=(), year="", *, porthu_id: str | None = None, kind: str = "", runtime="") -> dict:
    """find() for a known IMDb id: the PORT.hu id comes from Wikidata (P905) unless the caller already looked it up
    (``porthu_id``; '' means Wikidata has none)."""

    if not re.fullmatch(r"tt\d{5,10}", (imdb or "").lower()):
        return find(titles, year, kind=kind, runtime=runtime)
    if porthu_id is None:
        porthu_id = wikidata.claim(wikidata.lookup("imdb", imdb.lower()), "porthu")
    return find(titles, year, imdb=imdb, porthu_id=porthu_id, kind=kind, runtime=runtime)
