"""moly.hu, the main Hungarian book database: Hungarian blurb, tags ("címkék"), series, original title, editions
(publisher, year, pages, ISBN, translator), cover and the book page itself as proof link.

robots.txt (checked 2026-10-16): everything is disallowed except, among others, ``/konyvek/<slug>`` (but NOT the
``/konyvek/<slug>/...`` sub-pages) and ``/sitemap*``. The search (/kereses) is forbidden and the API is offline.
So a book is found by its slug: the sitemaps (``sitemap.xml.gz`` -> ``sitemapN.xml.gz``, ~800k book URLs) are
turned into a compact SQLite index of slugs in ``net.cache_dir()`` and refreshed weekly; when the index has no
match, at most 2 guessed slugs ("szerzo-cim", accents stripped) are fetched. Redirects to the home page mean
"no such book".

Covers: the og:image lives on assets.moly.hu, whose robots.txt is the same "Disallow: /" file (no Allow for
/system/covers/), so ``cover_url`` is reported but must not be downloaded automatically.
"""

from __future__ import annotations

import difflib
import re
import sqlite3
import time
import unicodedata
import zlib
from contextlib import closing
from pathlib import Path

from .. import net

BASE = "https://moly.hu"
SITEMAP_INDEX = BASE + "/sitemap.xml.gz"
INDEX_NAME = "moly-slugs.sqlite"
MAX_AGE_DAYS = 7
PRICE_PREFIX = re.compile(r"^\s*Árak és vélemények egy helyen!\s*·\s*(Legolcsóbb ár:\s*[\d\s.]+\s*Ft\s*·\s*)?")
MONTHS = {m: i for i, m in enumerate("január február március április május június július augusztus szeptember október november december".split(), 1)}
BOOK_URL = re.compile(r"^https://moly\.hu/konyvek/([^/?#]+)$")


# --- slugs -------------------------------------------------------------------------------------------------------------

def _ascii(text: str) -> str:
    try:
        from unidecode import unidecode  # optional dependency, also transliterates Cyrillic/CJK like moly does

        return unidecode(text)
    except ImportError:
        return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def slugify(text: str) -> str:
    """moly's slug rule: no accents, lower case, apostrophes dropped, every other run of symbols -> '-'."""

    text = _ascii(unicodedata.normalize("NFC", (text or "").replace("\u200b", ""))).lower()
    text = re.sub(r"['’`´]", "", text)
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")


def _author_variants(authors: list[str]) -> list[str]:
    variants = []
    names = [a.strip() for a in authors if a and a.strip()]
    if len(names) > 1:
        variants.append(" ".join(names[:3]))
    for name in names[:2]:
        variants.append(name)
        parts = name.split()
        if len(parts) == 2:  # "Magda Szabó" <-> "Szabó Magda"
            variants.append(f"{parts[1]} {parts[0]}")
        elif len(parts) > 2 and not re.search(r"\.$", parts[-1]):
            variants.append(" ".join([parts[-1]] + parts[:-1]))
    return list(dict.fromkeys(variants))


def _title_variants(title: str) -> list[str]:
    title = (title or "").replace("\u200b", "").strip()
    variants = [title]
    for sep in (":", " - ", " – ", " — ", ". "):
        if sep in title:
            variants.append(title.split(sep, 1)[0])
    variants.append(re.sub(r"\s*[\[(].*?[\])]\s*", " ", title).strip())  # "(Harry Potter 1.)", "[regény]"
    return [v for v in dict.fromkeys(v.strip() for v in variants) if v]


def candidates(title: str, authors: list[str]) -> list[str]:
    """Slugs to try, best first: every author/title variant ("szabo-magda-az-ajto")."""

    slugs = []
    for author in _author_variants(authors) or [""]:
        for variant in _title_variants(title):
            slug = slugify(f"{author} {variant}")
            if slug and slug not in slugs:
                slugs.append(slug)
    return slugs


# --- sitemap index -----------------------------------------------------------------------------------------------------

def index_path() -> Path:
    return net.cache_dir() / INDEX_NAME


def _no_cache() -> float | None:
    try:
        import requests_cache  # optional dependency

        return getattr(requests_cache, "DO_NOT_CACHE", None)
    except ImportError:
        return None


def parse_sitemap_index(xml: str) -> list[str]:
    return re.findall(r"<loc>\s*(https://moly\.hu/sitemap\d+\.xml(?:\.gz)?)\s*</loc>", xml or "")


def sitemap_slugs(data: bytes):
    """Book slugs of one (gzipped or plain) sitemap, decompressed incrementally."""

    pattern = re.compile(rb"<loc>https://moly\.hu/konyvek/([^<\s/]+)</loc>")
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS) if data[:2] == b"\x1f\x8b" else None
    tail = b""
    for start in range(0, len(data), 1 << 18):
        chunk = data[start:start + (1 << 18)]
        try:
            text = tail + (decoder.decompress(chunk) if decoder else chunk)
        except zlib.error:
            return
        cut = text.rfind(b"</url>")
        body, tail = (text[:cut + 6], text[cut + 6:]) if cut >= 0 else (b"", text)
        for match in pattern.finditer(body):
            yield match.group(1).decode("utf-8", errors="replace")
    for match in pattern.finditer(tail + (decoder.flush() if decoder else b"")):
        yield match.group(1).decode("utf-8", errors="replace")


def build_index(target: Path | None = None) -> int:
    """Download the sitemaps and write the slug index; returns the number of slugs (0 on failure, old index kept)."""

    target = target or index_path()
    response = net.request("GET", SITEMAP_INDEX, expire=_no_cache())
    if response is None:
        return 0
    data = response.content
    try:
        xml = zlib.decompress(data, 16 + zlib.MAX_WBITS).decode("utf-8", "replace") if data[:2] == b"\x1f\x8b" else response.text
    except zlib.error:  # truncated download
        return 0
    maps = parse_sitemap_index(xml)
    if not maps:
        return 0
    temp = target.with_suffix(f".tmp{int(time.time())}")
    count, seen_books, complete = 0, False, False
    try:
        with closing(sqlite3.connect(temp)) as db:
            db.execute("CREATE TABLE slugs (slug TEXT PRIMARY KEY) WITHOUT ROWID")
            db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
            for url in maps:
                part = net.request("GET", url, expire=_no_cache())
                if part is None:
                    break
                slugs = list(sitemap_slugs(part.content))
                if not slugs and seen_books:
                    complete = True
                    break  # the book sitemaps come first, the rest are authors/series/publishers
                seen_books = seen_books or bool(slugs)
                db.executemany("INSERT OR IGNORE INTO slugs VALUES (?)", ((s,) for s in slugs))
                count += len(slugs)
            else:
                complete = True
            db.execute("INSERT INTO meta VALUES ('built', ?)", (str(int(time.time())),))
            db.commit()
        if not complete or not count:
            temp.unlink(missing_ok=True)
            return 0
        temp.replace(target)
    except (OSError, sqlite3.Error):
        temp.unlink(missing_ok=True)
        return 0
    return count


def _rows(path: Path, sql: str, params: tuple = ()) -> list[tuple]:
    try:
        with closing(sqlite3.connect(path)) as db:
            return db.execute(sql, params).fetchall()
    except sqlite3.Error:
        return []


def index_age(path: Path | None = None) -> float | None:
    """Age of the index in days, None when there is none."""

    path = path or index_path()
    if not path.exists():
        return None
    rows = _rows(path, "SELECT value FROM meta WHERE key = 'built'")
    return (time.time() - int(rows[0][0])) / 86400 if rows else None


def ensure_index(build: bool = True) -> Path | None:
    """The slug index, rebuilt when older than a week (a failed rebuild keeps the old one)."""

    path = index_path()
    age = index_age(path)
    failed = path.with_suffix(".failed")
    recent_failure = failed.exists() and time.time() - failed.stat().st_mtime < 6 * 3600
    if (age is None or age > MAX_AGE_DAYS) and build and not recent_failure:
        if build_index(path):
            failed.unlink(missing_ok=True)
        else:
            try:
                failed.touch()  # moly down: do not retry the whole download for every book
            except OSError:
                pass
        age = index_age(path)
    return path if age is not None else None


def known(slugs: list[str], path: Path) -> list[str]:
    if not slugs:
        return []
    found = {r[0] for r in _rows(path, f"SELECT slug FROM slugs WHERE slug IN ({','.join('?' * len(slugs))})", tuple(slugs))}
    return [s for s in slugs if s in found]


def with_prefix(prefix: str, path: Path, limit: int = 5000) -> list[str]:
    if not prefix:
        return []
    upper = prefix[:-1] + chr(ord(prefix[-1]) + 1)
    return [r[0] for r in _rows(path, "SELECT slug FROM slugs WHERE slug >= ? AND slug < ? LIMIT ?", (prefix, upper, limit))]


def index_matches(title: str, authors: list[str], path: Path) -> list[str]:
    """Slugs of the index that fit author + title: exact candidates first, then the author's books whose title
    part is close (difflib >= 0.85), then numbered duplicates ("-2")."""

    exact = known(candidates(title, authors), path)
    found = list(exact)
    wanted = [slugify(t) for t in _title_variants(title)]
    if not exact:
        scored = []
        for author in _author_variants(authors):
            prefix = slugify(author) + "-"
            for slug in with_prefix(prefix, path):
                rest = _no_article(slug[len(prefix):])
                # volume numbers must agree: "death-note-1" is close to "death-note-11" but another book
                score = max((difflib.SequenceMatcher(None, rest, _no_article(w)).ratio() for w in wanted
                             if _numbers(rest) == _numbers(w)), default=0.0)
                if score >= 0.85:
                    scored.append((score, slug))
        found += [s for _, s in sorted(scored, reverse=True) if s not in found]
    if not found:
        found = by_title(wanted[:2], authors, path)
    for slug in exact[:1]:
        found += [s for s in known([f"{slug}-{n}" for n in range(2, 5)], path) if s not in found]
    return found[:6]


def _no_article(slug: str) -> str:
    return re.sub(r"^(a|az|the|der|die|das|le|la|les|el|il)-", "", slug)


def _numbers(text: str) -> list[int]:
    """The numbers of a title ("Death Note 01." and "death-note-1" both give [1])."""

    return [int(n) for n in re.findall(r"\d+", text or "")]


def by_title(titles: list[str], authors: list[str], path: Path) -> list[str]:
    """Slugs ending in "-<title>" whose author part shares a name token with ``authors`` (another transliteration:
    "Ohba Cugumi" vs "Cugumi Oba"); without authors only a unique hit. A full scan, ~0.5 s."""

    names = {w for a in authors for w in slugify(a).split("-") if len(w) > 2}
    found: list[tuple[int, str]] = []
    for title in titles:
        if len(title) < 4:
            continue
        pattern = "%-" + title.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        rows = [r[0] for r in _rows(path, "SELECT slug FROM slugs WHERE slug LIKE ? ESCAPE '\\'", (pattern,))]
        for slug in rows:
            overlap = len(names & set(slug[: -len(title) - 1].split("-")))
            if overlap or (not names and len(rows) == 1):
                found.append((overlap, slug))
        if found:
            break
    return [s for _, s in sorted(found, reverse=True)]


# --- book pages --------------------------------------------------------------------------------------------------------

def _date(text: str) -> str:
    match = re.search(r"(\d{4})\.\s*([a-záéíóöőúüű]+)\s+(\d{1,2})\.", text or "")
    if match and match.group(2) in MONTHS:
        return f"{match.group(1)}-{MONTHS[match.group(2)]:02d}-{int(match.group(3)):02d}"
    return ""


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", (text or "").replace("\u200b", "").replace("\xa0", " "))).strip()


def strip_price_prefix(text: str) -> str:
    return PRICE_PREFIX.sub("", text or "").strip()


def parse_book(page: str, url: str = "") -> dict:
    """{"url", "slug", "title", "authors", "series", "series_index", "original_title", "original_year",
    "description", "tags", "genres", "cover_url", "editions": [{"publisher", "city", "year", "date", "pages",
    "isbn", "ebook", "binding", "translators"}], "translators", "publisher_series"}; {} for a non-book page."""

    try:
        from bs4 import BeautifulSoup  # optional dependency
    except ImportError:
        return {}
    soup = BeautifulSoup(page or "", "html.parser")
    canonical = soup.find("link", rel="canonical")
    url = (canonical.get("href") if canonical else "") or url
    match = BOOK_URL.match(url or "")
    head = soup.select_one("div.head_title h1 span.item")
    if not match or head is None:
        return {}
    result: dict = {"url": url, "slug": match.group(1), "series": "", "series_index": ""}
    series = head.find("a", href=re.compile(r"^/sorozatok/"))
    if series is not None:
        text = _clean(series.get_text()).strip("()")
        number = re.match(r"(.+?)\s+(\d+(?:[.,]\d+)?(?:-\d+)?)\.?$", text)
        result["series"], result["series_index"] = (number.group(1), number.group(2)) if number else (text, "")
        series.extract()
    result["title"] = _clean(head.get_text())
    result["authors"] = [_clean(a.get_text()) for a in soup.select("div.head_title div.authors a") if _clean(a.get_text())]
    description = soup.select_one("div.text.shrinkable") or soup.select_one("#content div.text")
    text = ""
    if description is not None:
        for spoiler in description.select("p.spoiler"):
            spoiler.extract()
        for br in description.find_all("br"):
            br.replace_with("\n")
        paragraphs = [p.get_text() for p in description.find_all("p")] or [description.get_text()]
        text = "\n\n".join(re.sub(r"[ \t]+", " ", unicodedata.normalize("NFC", p.replace("\u200b", ""))).strip() for p in paragraphs)
        text = re.sub(r" *\n *", "\n", text).strip()
    if not text:
        meta = soup.find("meta", attrs={"name": "description"})
        text = strip_price_prefix(meta.get("content", "") if meta else "")
    result["description"] = text
    tags = soup.select("#book_tags a.tag")
    result["tags"] = [_clean(a.get_text()) for a in tags if _clean(a.get_text())]
    result["genres"] = [_clean(a.get_text()) for a in tags if "genre" in (a.get("class") or [])]
    image = soup.find("meta", property="og:image")
    result["cover_url"] = image.get("content", "") if image else ""
    data = {}
    for row in soup.select("table.book_data tr"):
        th, td = row.find("th"), row.find("td")
        if th is not None and td is not None:
            data[_clean(th.get_text())] = td
    original = data.get("Eredeti mű")
    original_text = _clean(original.find("a").get_text()) if original is not None and original.find("a") else ""
    original_title = original_text.split(": ", 1)[1] if ": " in original_text else original_text
    result["original_title"] = re.sub(r"\s*\([a-záéíóöőúüű]+\)$", "", original_title)  # "Dune (angol)" disambiguation
    year = data.get("Eredeti megjelenés éve")
    result["original_year"] = _clean(year.get_text()) if year is not None else ""
    pub_series = data.get("Kiadói sorozatok")
    result["publisher_series"] = [_clean(a.get_text()) for a in pub_series.find_all("a")] if pub_series is not None else []
    result["editions"] = [_edition(node) for node in soup.select("#editions div.edition")]
    result["translators"] = list(dict.fromkeys(t for e in result["editions"] for t in e["translators"]))
    return result


def _edition(node) -> dict:
    content = node.select_one("div.flex_content") or node
    rows = [div for div in content.find_all("div", recursive=False)]
    first = rows[0] if rows else content
    second = rows[1] if len(rows) > 1 else None
    publisher = first.find("a", href=re.compile(r"^/kiadok/"))
    abbr = first.find("abbr")
    first_text = _clean(first.get_text(" "))
    year = re.search(r"\b(1[5-9]\d\d|20\d\d)\b", first_text)
    city = ""
    if publisher is not None:
        after = first_text.split(_clean(publisher.get_text()), 1)[-1]
        city_match = re.match(r"\s*,\s*([^,\d]+?)\s*,", after)
        city = city_match.group(1) if city_match else ""
    second_text = _clean(second.get_text(" ")) if second is not None else ""
    pages = re.search(r"(\d+)\s*oldal", second_text)
    isbn = re.search(r"ISBN\s*:\s*([\dXx\- ]{10,17})", second_text)
    translators = [_clean(a.get_text()) for a in second.find_all("a", href=re.compile(r"^/alkotok/"))] if second is not None and "Fordította" in second_text else []
    binding = re.search(r"(puhatáblás|keménytáblás|spirálozott|leporelló|füles kötött|kötött)", second_text)
    return {
        "publisher": _clean(publisher.get_text()) if publisher is not None else "",
        "city": city,
        "year": year.group(1) if year else "",
        "date": _date(abbr.get("title", "")) if abbr is not None else "",
        "pages": int(pages.group(1)) if pages else 0,
        "isbn": re.sub(r"[^\dX]", "", isbn.group(1).upper()) if isbn else "",
        "ebook": bool(first.find("a", href="/cimkek/ekonyv")) or "ekönyv" in first_text,
        "binding": binding.group(1) if binding else "",
        "translators": translators,
    }


def book(slug: str) -> dict:
    """parse_book() of /konyvek/<slug>; {} when it does not exist (moly redirects unknown slugs to the home page)."""

    if not re.fullmatch(r"[\w%.\-]+", slug or "") or slug.startswith("."):
        return {}
    # unknown slugs are a 302 to the home page, so a failed request is a real outage (recorded in net.FAILURES)
    response = net.get(f"{BASE}/konyvek/{slug}", retries=1)
    if response is None or not BOOK_URL.match(response.url or ""):
        return {}
    response.encoding = "utf-8"
    return parse_book(response.text, response.url)


# --- matching ----------------------------------------------------------------------------------------------------------

def _isbn13(raw: str) -> str:
    from ..local.ebookmeta import isbn13

    return isbn13(raw)


def score(info: dict, title: str, authors: list[str], isbn: str = "") -> tuple[str, float]:
    """How a parsed page fits: ("isbn", 0.95) when an edition has the ISBN, ("title", 0.6-0.85) on title (+author)
    match, ("", 0) otherwise."""

    if not info:
        return "", 0.0
    wanted = _isbn13(isbn)
    if wanted and any(_isbn13(e.get("isbn", "")) == wanted for e in info.get("editions") or []):
        return "isbn", 0.95
    titles = {_no_article(slugify(t)) for t in _title_variants(title)}
    theirs = {_no_article(slugify(t)) for t in _title_variants(info.get("title", ""))}
    if not titles & theirs and max((difflib.SequenceMatcher(None, a, b).ratio() for a in titles for b in theirs
                                    if _numbers(a) == _numbers(b)), default=0) < 0.9:
        return "", 0.0  # close titles with other numbers are other volumes
    surnames = {w for a in authors for w in slugify(a).split("-") if len(w) > 2}
    theirs_names = {w for a in info.get("authors") or [] for w in slugify(a).split("-") if len(w) > 2}
    if surnames and theirs_names and not surnames & theirs_names:
        return "", 0.0
    return "title", 0.85 if surnames & theirs_names else 0.6


def find(title: str, authors: list[str], isbn: str = "", *, use_index: bool = True, max_pages: int = 3) -> dict:
    """The moly book page of a title/author (ISBN confirms an edition): parse_book() plus "match" and
    "confidence"; {} when not found. At most ``max_pages`` book pages are fetched."""

    if not title:
        return {}
    path = ensure_index() if use_index else None
    slugs = index_matches(title, authors, path)[:max_pages] if path else []
    best = _best(slugs, title, authors, isbn)
    if not best:  # not in the index (new book, or no index): at most 2 guessed slugs
        best = _best([s for s in candidates(title, authors) if s not in slugs][:2], title, authors, isbn)
    return best


def _best(slugs: list[str], title: str, authors: list[str], isbn: str) -> dict:
    best: dict = {}
    for slug in slugs:
        info = book(slug)
        kind, confidence = score(info, title, authors, isbn)
        if not kind:
            continue
        info.update(match=kind, confidence=confidence)
        if kind == "isbn":
            return info
        if not best or confidence > best["confidence"]:
            best = info
        if not isbn:  # without an ISBN the first good page wins; with one, look on for the exact edition
            break
    return best
