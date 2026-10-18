"""Resolver for ebooks, comics/manga and magazines (nCore "e-Book (HUN)" / "e-Book (ENG)").

Chains (first source wins per field, later ones only fill gaps):

* Hungarian book: embedded metadata (ISBN!) -> moly.hu -> Google Books (langRestrict=hu) -> Open Library ->
  DeepL of an English description.
* English book: embedded -> Google Books -> Open Library -> DeepL.
* Comics/manga: ComicInfo.xml -> moly.hu (Hungarian editions) -> AniList -> MangaUpdates -> ComicVine (English
  Western comics).
* Magazines: file name (title, year, issue/date) + a per-title YAML in ~/.config/ncore-up/magazines/<slug>.yaml
  (publisher, proof link, country, ISSN, style, fixed description); a stub is written on first sight.

Torrent names follow the nCore wiki: "Író neve - Könyv címe" (at most 3 authors joined with ", ", else
"Various Authors"; no series title, no subtitle), magazines "Név Év - Szám." or "Név Év.Hó.Nap." with the country
code when the magazine has several national editions. Comics and manga (not covered by the wiki examples) get
"Író - Sorozat 3." in Hungarian, "Writer - Series Vol. 3" / "Writer - Series #3" in English. The name is put in
``Meta.extra["torrent_name"]``.

``Meta.extra`` keys: torrent_name, format ("epub, pdf - (OCR)"), scan_ocr (SCAN/OCR for PDF/DJVU), text_layer
(TEXT/OCR/SCAN), series ("A ragyogás 1."), pages, translator, issue, main_file (the file for the proof pages:
``ebookmeta.proof_pages``), magazine_config.
"""

from __future__ import annotations

import difflib
import re
import sqlite3
import zlib
from pathlib import Path

from .. import config, describe, keys, net, translate
from ..local import ebookmeta, nfo
from ..meta import EBOOK_STYLES, Meta
from ..release import COMPANION_DIRS, Release
from ..sources import anilist, comicvine, googlebooks, mangaupdates, moly, openlibrary

MAGAZINE_DIR = config.CONFIG_FILE.parent / "magazines"
FORMAT_ORDER = [".epub", ".azw3", ".mobi", ".azw", ".fb2", ".pdf", ".djvu", ".cbz", ".cbr", ".cb7"]
SCENE_TOKENS = re.compile(r"(?i)^(retail|epub|mobi|azw3?|pdf|djvu|cbz|cbr|ebook|e-book|hun|hungarian|magyar|eng|english|repack|proper|"
                          r"scan|ocr|digital|webrip|web|ebook-\w+|z-lib|1lib|zlib|libgen)$")
HU_MONTHS = {m: i for i, m in enumerate("január február március április május június július augusztus szeptember október november december".split(), 1)}
EN_MONTHS = {m: i for i, m in enumerate("january february march april may june july august september october november december".split(), 1)}
MANGA_HINT = re.compile(r"(?i)\bmanga\b|\bmanhwa\b|\bmanhua\b|\bv(?:ol)?\.?\s?\d{1,3}\b.*\((?:digital|1r0n|danke|oak)")

# --- style mapping (nCore ebook_style vocabulary, meta.EBOOK_STYLES) --------------------------------------------------
# Rules for genre-like labels: applied to every input (moly tags of any kind, categories, subjects, genres).
GENRE_RULES = [
    (r"\bmanga\b|manhwa|manhua|képregény|\bcomics?\b|graphic novel", "manga.képregény"),
    (r"science fiction|\bsci-?fi\b|tudományos[- ]fantasztikus|disztópi|dystopi|cyberpunk|űropera|space opera", "sci-fi"),
    (r"\bfantasy\b|urban fantasy", "fantasy"),
    (r"romantikus|romance|\bromantic\b|szerelmes regény|new adult", "romantikus"),
    (r"erotik|erotic|\bhentai\b", "erotika"),
    (r"krimi|thriller|horror|mystery|mysteries|crime|suspense|detective|detektív|kalandregény|action & adventure|lektűr|western|"
     r"humoros|\bhumor\b", "szórakoztató.irodalom"),
    (r"gyermekkönyv|gyerekkönyv|ifjúsági|juvenile|children|young adult|\bmesék?\b|meseregény|mesekönyv|lapozó|képeskönyv", "gyermek"),
    (r"szakácskönyv|cookbook|cooking|receptek|gasztronómia", "gasztronómia"),
    (r"útikönyv|útleírás|\btravel\b|utazás", "utazás"),
    (r"szótár|dictionar|nyelvkönyv|foreign language study|nyelvtanulás", "szótár.nyelvkönyv"),
    (r"tankönyv|érettségi|study aids|textbook|segédkönyv|munkafüzet", "tankönyv.segédkönyv"),
    (r"lexikon|enciklopédia|encyclopedi", "lexikon.enciklopédia"),
    (r"ismeretterjesztő|popular science|tudományos ismeretterjesztés", "ismeretterjesztő"),
    (r"életrajz|biograph|memoár|memoir|önéletrajz|autobiograph|literary criticism|literary collections|irodalomtörténet|esszé|essays", "irodalom"),
    (r"ezoterika|ezoterikus|asztrológia|body, mind & spirit|spirituali|okkult", "ezoterika"),
    (r"szakkönyv", "szakkönyv"),
]
# Topic rules: only for real classifications (moly genre tags, Google/OL categories outside "Fiction", embedded subjects).
SUBJECT_RULES = [
    (r"hadtörténet|military|hadászat|haditechnika", "hadászat"),
    (r"történelem|\bhistory\b|történettudomány", "történelem"),
    (r"egészség|életmód|\bhealth\b|fitness|\bdiet|diéta|self-help|önsegítő|wellness|táplálkozás", "életmód.egészség"),
    (r"vallás|religio|biblia|\bbible\b|kereszténység|christian|teológia|theolog|buddhizmus|iszlám", "vallás"),
    (r"művészet|\bart\b|festészet|építészet|architecture|fotográfia|photography|\bdesign\b|performing arts|filmművészet", "művészet"),
    (r"\bzene\b|zenetörténet|\bmusic\b", "zene"),
    (r"\bsport|labdarúgás", "sport"),
    (r"számítástechnika|informatika|programozás|\bcomputers?\b|programming|software", "számítástechnika"),
    (r"üzlet|business|gazdaság|economics|befektetés|investing|pénzügy|finance|marketing|menedzsment|management", "pénz.befektetés.üzlet"),
    (r"\beducation\b|oktatás|pedagógia", "tankönyv.segédkönyv"),
    (r"\breference\b", "lexikon.enciklopédia"),
    (r"pszichológia|psychology|szociológia|sociology|filozófia|philosophy|politika|political science|social science|"
     r"társadalomtudomány|\blaw\b|jogtudomány", "társadalomtudomány"),
    (r"természettudomány|\bscience\b|biológia|biology|fizika|physics|kémia|chemistry|csillagászat|astronomy|matematika|"
     r"mathematics|\bmedical\b|orvostudomány|\bnature\b", "természettudomány"),
    (r"kertészet|gardening|lakberendezés|house & home", "kert.és.lakás"),
    (r"hobbi|crafts & hobbies|kézimunka|barkácsolás|\bgames\b", "hobbi"),
    (r"technology & engineering|mérnöki", "szakkönyv"),
]
LITERARY = re.compile(r"(?i)klasszikus|classics|literary fiction|szépirodalom|\bversek?\b|verseskötet|poetry|költészet|\bdráma\b|drama|novellák")
GENERIC_FICTION = re.compile(r"(?i)^(regény|novel|novella|elbeszélés|fiction|fiction / general|general|irodalom)$")
FICTION_STYLES = {"sci-fi", "fantasy", "romantikus", "erotika", "szórakoztató.irodalom", "gyermek", "manga.képregény"}
ANILIST_HU = {
    "Action": "akció", "Adventure": "kaland", "Comedy": "vígjáték", "Drama": "dráma", "Ecchi": "ecchi", "Fantasy": "fantasy",
    "Horror": "horror", "Mahou Shoujo": "varázslólányos", "Mecha": "mecha", "Music": "zenei", "Mystery": "rejtély",
    "Psychological": "lélektani", "Romance": "romantikus", "Sci-Fi": "sci-fi", "Slice of Life": "hétköznapi élet", "Sports": "sport",
    "Supernatural": "természetfeletti", "Thriller": "thriller", "Hentai": "hentai",
}


def map_styles(*, genre_tags: list[str] = (), topic_tags: list[str] = (), theme_tags: list[str] = (), limit: int = 3) -> list[str]:
    """nCore style tags from classifications. ``genre_tags`` (moly "genre" tags, AniList genres) and ``topic_tags``
    (Google/OL categories, embedded subjects) get every rule; ``theme_tags`` (other moly tags) only the genre rules,
    so a novel tagged "vallás" does not become "vallás". Plain "regény"/"Fiction" gives "szépirodalom" only when
    no genre style was found."""

    styles: list[str] = []
    generic = False

    def add(style: str) -> None:
        if style in EBOOK_STYLES and style not in styles:
            styles.append(style)

    for label, topical in [(t, True) for t in list(genre_tags) + list(topic_tags)] + [(t, False) for t in theme_tags]:
        text = (label or "").strip().lower()
        if not text:
            continue
        fiction = text.startswith(("fiction", "juvenile fiction", "young adult fiction", "comics"))
        if GENERIC_FICTION.match(text):
            generic = True
            continue
        if LITERARY.search(text):
            add("szépirodalom")
            continue
        rules = GENRE_RULES + (SUBJECT_RULES if topical and not fiction else [])
        style = next((s for pattern, s in rules if re.search(pattern, text)), "")
        if style:
            add(style)
        elif fiction:
            generic = True
    if generic and not set(styles) & FICTION_STYLES and "szépirodalom" not in styles:
        add("szépirodalom")
    if "szépirodalom" in styles and set(styles) & FICTION_STYLES - {"gyermek", "manga.képregény"}:
        styles.remove("szépirodalom")  # genre fiction is "szórakoztató" rather than "szép" irodalom
    return styles[:limit]


# --- names ----------------------------------------------------------------------------------------------------------------

def authors_text(authors: list[str]) -> str:
    """nCore wiki: at most 3 authors joined with ", ", otherwise "Various Authors"."""

    authors = [a.strip() for a in authors if a and a.strip()]
    return "Various Authors" if len(authors) > 3 else ", ".join(authors)


def book_name(authors: list[str], title: str) -> str:
    title = re.sub(r"\s+", " ", (title or "").replace("​", "")).strip()
    who = authors_text(authors)
    return f"{who} - {title}" if who and title else title or who


def _split_authors(text: str) -> list[str]:
    return [a.strip() for a in re.split(r"\s*(?:,|;|&|\bés\b|\band\b)\s*", text or "") if a.strip()]


def _clean_stem(text: str) -> str:
    text = re.sub(r"(?i)\s*[(\[]\s*(z-?library|1lib\.\w+|z-lib\.\w+|libgen\.\w+|anna'?s archive)[^)\]]*[)\]]", "", text or "")
    text = re.sub(r"(?i)\s*\[(epub|mobi|azw3|pdf|retail|ebook|hun|eng)[^\]]*\]", "", text)
    return text.replace("_", " ").strip()


def parse_name(name: str, *, numbered: bool = True) -> dict:
    """Author/title/series/year/volume from a book or comic file/folder name.

    "Szabó Magda - Az ajtó (Szabó Magda-életmű 12.) [Jaffa, 2016]", "Martha.Wells.-.Network.Effect.2020.RETAIL.EPUB.eBook-GRP",
    "Brandon.Sanderson-The.Way.of.Kings.2010.RETAIL.EPUB.eBook-GRP", "Saga 001 (2012) (Digital)", "Nichijou v01",
    "Death Note 3. kötet". ``numbered=False`` (books) keeps a trailing number in the title ("Fahrenheit 451")."""

    text = _clean_stem(name)
    result = {"author": "", "title": "", "series": "", "index": "", "year": "", "publisher": "", "volume": "", "issue": ""}
    if " " not in text and text.count(".") >= 2:  # scene style
        text = re.sub(r"-[A-Za-z0-9]+$", "", text) if re.search(r"(?i)\.(epub|mobi|pdf|ebook|retail|azw3)", text) else text
        author, sep, rest = text.partition(".-.")
        if not sep:  # "Author.Name-Title.Words": the first hyphen after a multi-word author without digits
            for hyphen in re.finditer(r"-", text):
                left = text[: hyphen.start()]
                if "." in left and not re.search(r"\d", left) and len(left.split(".")) <= 5 and text[hyphen.end():]:
                    author, sep, rest = left, ".-.", text[hyphen.end():]
                    break
        if sep:  # initials of the author ("J.K.Rowling" -> "J. K. Rowling"); a title may start with the article "A."
            text = re.sub(r"\b([A-Z])\.(?=[A-Z])", "\\1\x00", author) + sep + rest
        text = text.replace(".-.", " - ").replace(".", " ").replace("\x00", ". ")
        words = text.split()
        while words and (SCENE_TOKENS.match(words[-1]) or re.fullmatch(r"(19|20)\d\d", words[-1])):
            if re.fullmatch(r"(19|20)\d\d", words[-1]):
                result["year"] = words[-1]
            words.pop()
        text = " ".join(words)
    bracket = re.search(r"\s*\[([^\],\]]+?)(?:,\s*((?:19|20)\d\d))?\]\s*$", text)
    if bracket:
        result["publisher"], result["year"] = bracket.group(1).strip(), bracket.group(2) or result["year"]
        text = text[: bracket.start()].strip()
    year = re.search(r"\s*\(((?:19|20)\d\d)\)", text)
    if year:
        result["year"] = result["year"] or year.group(1)
        text = (text[: year.start()] + text[year.end():]).strip()
    text = re.sub(r"\s*\((?:digital|webrip|c2c|scan|[\w-]*empire|minutemen[^)]*|[^)]*-dcp)\)", "", text, flags=re.I).strip()
    series = re.search(r"\s*\((?!of\s+\d)([^()]+?)\s+#?(\d+(?:[.,]\d+)?)\.?\)\s*$", text)  # not "Saga 001 (of 6)"
    if series:
        result["series"], result["index"] = series.group(1).strip(), series.group(2)
        text = text[: series.start()].strip()
    if re.search(r"\s[-–]\s", text):
        author, title = re.split(r"\s[-–]\s", text, maxsplit=1)
        result["author"], text = author.strip(), title.strip()
    if not numbered:
        result["title"] = text.strip(" -")
        return result
    # group tags after the number: "Saga v01 (F)", "One Piece v100 (1r0n)", "Batman 001 (Son of Ultron-Empire)"
    text = re.sub(r"(?i)((?:\s+v(?:ol\.?\s?)?\d{1,3}|\s+#?\d{1,4}))(?:\s*\((?!of\s+\d)[^()]*\))+\s*$", r"\1", text)
    volume = re.search(r"(?i)\s+(?:v|vol\.?\s?|volume\s|book\s)(\d{1,3})\b|\s+(\d{1,3})\.\s*kötet\b", text)
    issue = re.search(r"\s+#?(\d{1,4})(?:\s+\(of\s+\d+\))?\s*$", text)
    if volume:
        result["volume"] = str(int(volume.group(1) or volume.group(2)))
        text = (text[: volume.start()] + text[volume.end():]).strip()
    elif issue and not re.fullmatch(r"(19|20)\d\d", issue.group(1)):
        result["issue"] = str(int(issue.group(1)))
        text = text[: issue.start()].strip()
    result["title"] = text.strip(" -")
    return result


def parse_magazine(name: str) -> dict:
    """{"name", "year", "month", "day", "issue"} when the name looks like a magazine issue, {} otherwise.

    "Nők Lapja 2024-05-12", "HVG 2024.12", "Chip 2024 - 05.", "PC Guru 2023. december", "National Geographic 05-2024",
    "HVG 2024 - 12. szám"."""

    text = _clean_stem(name).replace("_", " ")
    if " " not in text and text.count(".") >= 2:
        text = text.replace(".", " ")
    words = text.split()
    while words and SCENE_TOKENS.match(words[-1].split("-")[0]):
        words.pop()
    text = re.sub(r"\s*\((?:hu|en|de|magyar|english)\)\s*$", "", " ".join(words), flags=re.I)
    month_names = "|".join(list(HU_MONTHS) + list(EN_MONTHS))
    patterns = [
        r"^(?P<name>.+?)[\s._-]+(?P<year>(?:19|20)\d\d)[.\-/](?P<month>0?[1-9]|1[0-2])[.\-/](?P<day>0?[1-9]|[12]\d|3[01])\.?$",
        r"^(?P<name>.+?)[\s._-]+(?P<year>(?:19|20)\d\d)[.\-/](?P<month>0[1-9]|1[0-2])\.?$",
        r"^(?P<name>.+?)[\s._-]+(?P<year>(?:19|20)\d\d)\.?\s+(?P<monthname>" + month_names + r")\.?$",
        r"^(?P<name>.+?)[\s._-]+(?P<year>(?:19|20)\d\d)\s*[-–]?\s*(?:no\.?|nr\.?|#|issue)?\s*(?P<issue>\d{1,3})\.?(?:\s*szám)?$",
        r"^(?P<name>.+?)[\s._-]+(?:no\.?\s*|#)?(?P<issue>\d{1,3})(?:\.\s*szám)?\s*[-/]\s*(?P<year>(?:19|20)\d\d)$",
    ]
    for pattern in patterns:
        match = re.match(pattern, text, flags=re.I)
        if not match:
            continue
        parts = match.groupdict()
        magazine = parts["name"].strip(" -–.")
        if not magazine or re.search(r"\s[-–]\s", magazine) or re.fullmatch(r"[\d\s.]+", magazine):
            return {}  # "Szerző - Cím 2020" is a book
        month = parts.get("month") or ""
        if parts.get("monthname"):
            month = str(HU_MONTHS.get(parts["monthname"].lower()) or EN_MONTHS.get(parts["monthname"].lower()))
        return {"name": magazine, "year": parts["year"], "month": str(int(month)) if month else "",
                "day": str(int(parts["day"])) if parts.get("day") else "", "issue": str(int(parts["issue"])) if parts.get("issue") else ""}
    return {}


def magazine_name(info: dict, country: str = "") -> str:
    """"Név Év - Szám." or "Név Év.Hó.Nap." ("Név Év.Hó." for monthlies); the country code after the name."""

    name = info["name"] + (f" {country}" if country else "")
    if info.get("issue") and info.get("year"):
        return f"{name} {info['year']} - {int(info['issue']):02d}."
    if info.get("month") and info.get("year"):
        date = f"{info['year']}.{int(info['month']):02d}." + (f"{int(info['day']):02d}." if info.get("day") else "")
        return f"{name} {date}"
    return f"{name} {info.get('year') or ''}".strip()


# --- magazine config ------------------------------------------------------------------------------------------------------

STUB = """# ncore-up magazin-beállítás: {name}
# Töltsd ki, majd futtasd újra a feltöltést. Üres mező = nincs adat.
name: "{name}"            # a lap neve a torrentnévben
publisher: ""             # kiadó
proof_link: ""            # igazoló link: a kiadó lapszám-oldala vagy https://portal.issn.org/resource/ISSN/<issn>
issn: ""
country: ""               # országkód (HU, EN, DE, GR...), ha a lapnak több nemzeti kiadása van
multiple_editions: false  # true: az országkód bekerül a torrentnévbe
language: "{language}"    # a lap nyelve magyarul (magyar, angol, német...)
style: "magazin"          # nCore stílus-címke
description: ""           # állandó magyar leírás a lapról
"""


def _read_yaml(text: str) -> dict:
    try:
        import yaml  # optional dependency

        data = yaml.safe_load(text)
        return data if isinstance(data, dict) else {}
    except ImportError:
        pass
    except Exception:  # noqa: BLE001 - a broken config must not stop the upload
        return {}
    data = {}
    for line in text.splitlines():
        match = re.match(r'^(\w+):\s*(?:"((?:[^"\\]|\\.)*)"|([^#]*?))\s*(?:#.*)?$', line)
        if match:
            value = match.group(2) if match.group(2) is not None else match.group(3)
            data[match.group(1)] = {"true": True, "false": False}.get(value.lower(), value) if isinstance(value, str) else value
    return data


def magazine_config(name: str, language: str, directory: Path | None = None) -> tuple[dict, Path, bool]:
    """(config, path, created) of a magazine; writes a stub on first sight."""

    directory = Path(directory) if directory else MAGAZINE_DIR
    path = directory / f"{moly.slugify(name) or 'magazin'}.yaml"
    if path.exists():
        try:
            return _read_yaml(path.read_text(encoding="utf-8")), path, False
        except OSError:
            return {}, path, False
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path.write_text(STUB.format(name=name.replace('"', "'"), language=language), encoding="utf-8")
    except OSError:
        return {}, path, False
    return {}, path, True


# --- helpers ----------------------------------------------------------------------------------------------------------------

def _norm(text: str) -> str:
    return moly.slugify(text)


def _similar(a: str, b: str) -> float:
    a, b = _norm(a), _norm(b)
    return 1.0 if a and a == b else difflib.SequenceMatcher(None, a, b).ratio() if a and b else 0.0


def _numbers(text: str) -> list[int]:
    return [int(n) for n in re.findall(r"\d+", _norm(text))]


def _title_fits(wanted: str, theirs: str) -> bool:
    """Same title (main part before ':' / ' - ' is enough); volume numbers must agree."""

    if not wanted or not theirs:
        return False
    variants = {wanted, re.split(r"\s*[:–—]\s*|\s+-\s+", wanted)[0]}
    theirs_variants = {theirs, re.split(r"\s*[:–—]\s*|\s+-\s+", theirs)[0]}
    return max((_similar(a, b) for a in variants for b in theirs_variants if _numbers(a) == _numbers(b)), default=0.0) >= 0.9


def _name_tokens(authors: list[str]) -> set[str]:
    return {w for a in authors for w in _norm(a).split("-") if len(w) > 2}


def _authors_fit(wanted: list[str], theirs: list[str]) -> bool:
    """No conflict: a shared name token, or one side unknown."""

    mine, other = _name_tokens(wanted), _name_tokens(theirs)
    return not mine or not other or bool(mine & other)


def _authors_overlap(wanted: list[str], theirs: list[str]) -> bool:
    """Positive evidence: both sides known and sharing a name token."""

    return bool(_name_tokens(wanted) & _name_tokens(theirs))


def _pick(hits: list[dict], title: str, authors: list[str], key_title: str = "title", key_authors: str = "authors") -> dict:
    for hit in hits:
        if _title_fits(title, hit.get(key_title, "")) and _authors_fit(authors, hit.get(key_authors) or []):
            return hit
    return {}


def _surname(authors: list[str]) -> str:
    if not authors:
        return ""
    parts = authors[0].replace(".", " ").split()
    return max(parts, key=len) if parts else ""


def _language_name(code: str) -> str:
    return describe.language(code) if code else ""


def _files(release: Release) -> list[tuple[Path, str]]:
    """(path, extension) of the ebook/comic files, best format first, companions and samples skipped."""

    found = []
    for item in release.files:
        if item.ext not in ebookmeta.BOOK_EXT or item.top_dir in COMPANION_DIRS:
            continue
        path = release.path / item.relpath if release.path.is_dir() else release.path
        found.append((path, item.ext, item.size))
    order = {ext: i for i, ext in enumerate(FORMAT_ORDER)}
    found.sort(key=lambda f: (order.get(f[1], 99), -f[2]))
    return [(p, e) for p, e, _ in found]


def _styles_from_hints(hints: dict) -> list[str]:
    tags = hints.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",")]
    return [t for t in tags if t]


def _set_proofs(meta: Meta, links: list[str]) -> None:
    for link in links:
        if len(meta.proof_links) >= 3:
            break
        meta.add_proof(link)


def _description(meta: Meta, hu: list[tuple[str, str]], en: list[tuple[str, str]]) -> None:
    """Hungarian text from a native source first, else DeepL of the English one (machine_translated)."""

    for text, source in hu:
        if text and len(text) >= 40:
            meta.description_hu, meta.description_source = text.strip(), source
            break
    for text, source in en:
        if text and len(text) >= 40:
            meta.description_en = text.strip()
            if not meta.description_hu:
                translated, done = translate.to_hungarian(meta.description_en)
                meta.description_source = source
                if done:
                    meta.description_hu, meta.machine_translated = translated, True
                else:
                    meta.warnings.append("nincs magyar leírás: az angol szöveg kerül a leírásba (DeepL kulcs nélkül nincs gépi fordítás)")
            break
    if not meta.description_hu and not meta.description_en:
        meta.warnings.append("nem találtam leírást (fülszöveget) — írd be kézzel (--edit)")


# unexpected response shapes, plus local trouble of a source (unwritable cache dir, broken moly index)
PARSE_ERRORS = (ValueError, TypeError, KeyError, AttributeError, IndexError, OSError, sqlite3.Error, zlib.error)


def _safe(meta: Meta, label: str, function, *args, default=None, **kwargs):
    """A source call that never stops the resolver: an unexpected response shape becomes a warning."""

    try:
        return function(*args, **kwargs)
    except PARSE_ERRORS as exc:
        meta.warnings.append(f"{label}: váratlan válasz, kihagytam ({type(exc).__name__})")
        return default


def _moly(meta: Meta, title: str, authors: list[str], isbn: str = "") -> dict:
    """moly.find() that tells "moly.hu is down" apart from "moly.hu does not know the book"."""

    before = len(net.FAILURES)
    found = _safe(meta, "moly.hu", moly.find, title, authors, isbn, default={}) or {}
    if not found and any(f.startswith("moly.hu") for f in net.FAILURES[before:]):
        meta.warnings.append("moly.hu nem érhető el (hiba a lekérésnél) — a magyar adatok és az igazoló link hiányozhatnak")
    return found


def _is_hungarian_text(text: str) -> bool:
    """Hungarian prose: the language check plus accented letters (the fallback check without langdetect also
    counts the English article "a")."""

    if not text:
        return False
    accents = len(re.findall(r"[áéíóöőúüűÁÉÍÓÖŐÚÜŰ]", text))
    return accents >= max(2, len(text) * 0.01) and translate.is_hungarian(text)


# --- resolver -------------------------------------------------------------------------------------------------------------

def _clean_hints(hints: dict | None) -> dict:
    """Text hints as stripped strings (CLI values may arrive as ints or lists); empty ones dropped."""

    result = dict(hints or {})
    for key in ("title", "author", "isbn", "year", "series", "series_index", "publisher", "proof", "kind", "language"):
        value = result.get(key)
        if isinstance(value, (list, tuple)):
            value = ", ".join(str(v) for v in value if v)
        value = str(value).strip() if value is not None else ""
        if value:
            result[key] = value
        else:
            result.pop(key, None)
    return result


def resolve(release: Release, code: str, *, original: bool, hints: dict) -> Meta:
    hints = _clean_hints(hints)
    meta = Meta()
    nfo.annotate(meta, release)
    files = _files(release)
    if not files:
        meta.kind = "book"
        meta.warnings.append("nem találtam e-könyv / képregény fájlt a release-ben")
        return meta
    main, ext = files[0]
    hungarian = code.endswith("_hun") if code else hints.get("language") == "hu"
    local = ebookmeta.read(main)
    meta.add_source("beágyazott metaadat" if local.get("title") or local.get("comic") else "")
    # Embedded metadata and explicit hints retain priority. NFO values seed the
    # normal ISBN/title lookup chain when the book itself has no metadata.
    for key, labels in (("title", ("title", "book title", "cím")), ("publisher", ("publisher", "kiadó"))):
        local[key] = local.get(key) or nfo.value(release, *labels)
    author = nfo.value(release, "author", "authors", "writer", "szerző")
    local["creators"] = local.get("creators") or (_split_authors(author) if author else [])
    raw_isbn = nfo.value(release, "isbn", "isbn13", "isbn 13", "isbn10", "isbn 10")
    isbn = ebookmeta.isbn13(raw_isbn)
    local["isbns"] = local.get("isbns") or ([isbn] if isbn else [])
    local["year"] = local.get("year") or nfo.year(release)
    if local.get("drm"):
        meta.extra["encrypted"] = True
        meta.warnings.append(f"DRM-védett fájl ({main.name}): nem olvasható, így nem tölthető fel")
    stems = {re.sub(r"(?i)\.(epub|pdf|mobi|azw3?|fb2|djvu|cb[zr7])$", "", p.name).lower() for p, _ in files}
    name_source = release.name if release.path.is_dir() else main.stem
    if release.path.is_dir() and not parse_name(release.name)["author"] and parse_name(main.stem)["author"]:
        name_source = main.stem  # a neutral folder ("Konyvek") around a well-named file
    kind = (hints.get("kind") or "").lower()
    magazine = parse_magazine(release.name) or parse_magazine(main.stem)
    if not kind:
        if ext in ebookmeta.COMIC_EXT:
            manga_flag = (local.get("comic") or {}).get("Manga", "")
            kind = "manga" if manga_flag.lower().startswith("yes") or MANGA_HINT.search(name_source) else "comic"
        elif magazine and not local.get("isbns") and not (local.get("creators") and local.get("title") and local["format"] == "epub"):
            kind = "magazine"
        else:
            kind = "book"
    meta.kind = kind
    meta.extra["main_file"] = str(main)
    if len(stems) > 1 and kind in {"book", "magazine"}:
        meta.warnings.append(f"{len(stems)} különböző könyv/lapszám van a release-ben — ellenőrizd a nevet és a leírást")
    if kind == "magazine":
        _magazine(meta, release, magazine or {"name": parse_name(name_source)["title"], "year": hints.get("year", ""), "month": "", "day": "", "issue": ""},
                  local, hungarian, hints)
    elif kind in {"comic", "manga"}:
        _comic(meta, files, local, parse_name(name_source), hungarian, hints)
    else:
        _book(meta, local, parse_name(name_source, numbered=False), hungarian, hints)
    _format(meta, files, local)
    if hints.get("language"):
        meta.languages = [_language_name(hints["language"])]
    _languages(meta, local, hungarian)
    if not meta.cover:
        try:
            meta.cover = ebookmeta.cover(main)
        except Exception:  # noqa: BLE001 - a cover is optional
            meta.cover = b""
    user_tags = _styles_from_hints(hints)
    if user_tags:
        meta.tags = user_tags
        unknown = [t for t in user_tags if t not in EBOOK_STYLES]
        if unknown:
            meta.warnings.append(f"ismeretlen nCore stílus-címke: {', '.join(unknown)} (lehetséges: {', '.join(EBOOK_STYLES)})")
    elif not meta.tags:
        meta.warnings.append("nem tudtam stílus-címkét választani — adj meg egyet (--tags), pl. szépirodalom, sci-fi, szakkönyv")
    if hungarian and kind != "magazine" and not any("moly.hu" in p for p in meta.proof_links):
        meta.warnings.append("EBook/HU: magyar kiadást igazoló link kell (moly.hu nem találta) — ellenőrizd az igazoló linket")
    if not meta.proof_links:
        meta.warnings.append("nincs igazoló link — a szabályzat kötelezővé teszi, add meg kézzel (--edit)")
    if original:
        meta.warnings.append("eredeti release: a név a release neve marad, a javasolt név csak tájékoztató")
    nfo.description(meta, release)
    return meta


def _book(meta: Meta, local: dict, name: dict, hungarian: bool, hints: dict) -> None:
    title = hints.get("title") or local.get("title") or name["title"]
    authors = _split_authors(hints["author"]) if hints.get("author") else (local.get("creators") or _split_authors(name["author"]))
    isbn = ebookmeta.isbn13(hints.get("isbn", "")) or (local.get("isbns") or [""])[0]
    if not title:
        meta.warnings.append("nem sikerült megállapítani a könyv címét — add meg a címet és a szerzőt")
        meta.confidence = 0.1
        return
    hungarian_book = hungarian or ebookmeta.is_hungarian_isbn(isbn) or local.get("language") == "hu"
    found = _moly(meta, title, authors, isbn) if hungarian_book else {}
    if found:
        meta.add_source("moly.hu")
    edition = next((e for e in found.get("editions") or [] if isbn and ebookmeta.isbn13(e.get("isbn", "")) == isbn), {})
    ebook_editions = [e for e in found.get("editions") or [] if e.get("ebook")]
    if not edition and not isbn and len(ebook_editions) == 1 and local.get("format") in {"epub", "mobi", "azw3", "azw"}:
        edition = ebook_editions[0]  # the only e-book edition on moly is very likely this retail e-book
    google: dict = {}
    if keys.get("GOOGLE_BOOKS_KEY"):
        google = _safe(meta, "Google Books", googlebooks.by_isbn, isbn, default={}) if isbn else {}
        if not google:
            hits = _safe(meta, "Google Books", googlebooks.find, title, _surname(authors), lang="hu" if hungarian_book else "", default=[])
            google = _pick(hits, title, authors)
        if google:
            meta.add_source("Google Books")
    else:
        meta.warnings.append("Google Books kihagyva: nincs GOOGLE_BOOKS_KEY a config.env-ben")
    # moly covers are not used: assets.moly.hu has the same "Disallow: /" robots.txt as moly.hu (see sources/moly.py)
    need_more = not (found.get("description") or google.get("description")) or not google.get("cover_url")
    library: dict = {}
    if isbn or need_more:
        library = _safe(meta, "Open Library", openlibrary.by_isbn, isbn, default={}) if isbn else {}
        if not library and need_more:
            hits = _safe(meta, "Open Library", openlibrary.search, title, authors[0] if authors else "", lang="hun" if hungarian_book else "", default=[])
            hit = _pick(hits, title, authors)
            if hit:
                info = _safe(meta, "Open Library", openlibrary.work, hit["work"], default={})
                # a work, not this edition: its publishers, first-publication year, median page count (and, for a
                # Hungarian book, its English subtitle and cover) describe other editions, so they are not used
                library = {**hit, "description": info.get("description", ""), "publishers": [], "year": "", "pages": 0}
                if hungarian_book:
                    library.update(subtitle="", cover_url="")
        if library:
            meta.add_source("Open Library")
    # title and authors: moly (Hungarian edition) > embedded > Google > Open Library > file name
    meta.title = (found.get("title") if found.get("confidence", 0) >= 0.85 else "") or title
    meta.subtitle = local.get("subtitle") or google.get("subtitle") or library.get("subtitle", "")
    if ":" in meta.title and not found:
        main_title, _, sub = meta.title.partition(":")
        if len(main_title.strip()) >= 3:
            meta.title, meta.subtitle = main_title.strip(), meta.subtitle or sub.strip()
    if meta.subtitle and _norm(meta.subtitle) == _norm(meta.title):
        meta.subtitle = ""
    meta.creators = (found.get("authors") if found.get("confidence", 0) >= 0.85 else None) or authors or google.get("authors") or library.get("authors") or []
    if hints.get("author"):
        meta.creators = _split_authors(hints["author"])
    if hungarian_book:
        meta.title_hu = meta.title
    meta.title_original = found.get("original_title", "") if _norm(found.get("original_title", "")) != _norm(meta.title) else ""
    meta.publisher = hints.get("publisher") or local.get("publisher") or edition.get("publisher") or google.get("publisher") or (library.get("publishers") or [""])[0]
    meta.date = local.get("date") or edition.get("date") or google.get("date", "")
    meta.year = hints.get("year") or local.get("year") or edition.get("year") or google.get("year") or library.get("year") or name.get("year", "")
    if meta.date and meta.year and not meta.date.startswith(meta.year):
        meta.date = ""
    if isbn:
        meta.identifiers["isbn"] = isbn
    if found.get("url"):
        meta.identifiers["moly"] = found["slug"]
    if google.get("id"):
        meta.identifiers["google_books"] = google["id"]
    # the volume number comes from the same source as the series name (a publisher series has other numbers)
    pairs = [(local.get("series"), local.get("series_index")), (found.get("series"), found.get("series_index")),
             (name.get("series"), name.get("index"))]
    series, index = next(((s, i or "") for s, i in pairs if s), ("", ""))
    if hints.get("series"):
        series, index = hints["series"], hints.get("series_index") or index
    if not series and library.get("series"):
        match = re.match(r"(.+?)\s*[#,(]\s*(\d+)", library["series"][0])
        series, index = (match.group(1).strip(), match.group(2)) if match else (library["series"][0], "")
    if series and (index or _norm(series) != _norm(meta.title)):
        meta.extra["series"] = f"{series} {index}." if hungarian and index else f"{series} #{index}" if index else series
    translators = (edition.get("translators") or (found.get("translators") if len(found.get("translators") or []) == 1 else [])) or []
    if translators:
        meta.extra["translator"] = ", ".join(translators)
    pages = local.get("pages") or edition.get("pages") or google.get("pages") or library.get("pages") or 0
    if pages:
        meta.extra["pages"] = str(pages)
    moly_text = found.get("description", "")
    moly_hu = _is_hungarian_text(moly_text)  # moly also has pages of foreign editions with English blurbs
    hu_texts = [(moly_text if moly_hu else "", "moly.hu")]
    local_text = local.get("description", "")
    local_hu = _is_hungarian_text(local_text)
    hu_texts.append((local_text if local_hu else "", "kiadói fülszöveg (e-könyv)"))
    google_text = google.get("description", "")
    google_hu = google.get("language") == "hu" or _is_hungarian_text(google_text)
    hu_texts.append((google_text if google_hu else "", "Google Books"))
    en_texts = [(google_text if not google_hu else "", "Google Books"), (local_text if not local_hu else "", "kiadói fülszöveg (e-könyv)"),
                (library.get("description", ""), "Open Library"), (moly_text if not moly_hu else "", "moly.hu")]
    _description(meta, hu_texts, en_texts)
    meta.genres = found.get("genres") or [c.split(" / ")[-1] for c in google.get("categories") or []][:3]
    ol_genres, ol_themes = _ol_subjects(library)
    meta.tags = map_styles(genre_tags=(found.get("genres") or []) + ol_genres,
                           topic_tags=(google.get("categories") or []) + (local.get("subjects") or []),
                           theme_tags=[t for t in found.get("tags") or [] if t not in (found.get("genres") or [])] + ol_themes)
    meta.cover_url = google.get("cover_url") or library.get("cover_url") or (openlibrary.cover_url(isbn) if isbn and library else "")
    links = [found.get("url", "")] if hungarian else []
    links += [google.get("url", ""), library.get("url", "")]
    if not hungarian:
        links.append(found.get("url", ""))
    if hints.get("proof"):
        links.insert(0, hints["proof"])
    _set_proofs(meta, [link for link in links if link])
    if not meta.proof_links and isbn:
        meta.add_proof(f"https://books.google.com/books?vid=ISBN{isbn}")
        meta.warnings.append("igazoló link csak az ISBN alapján (Google Books) — nyisd meg és ellenőrizd")
    meta.confidence = max(found.get("confidence", 0), 0.9 if google and isbn and google.get("isbn13") == isbn else 0.7 if google else 0,
                          0.85 if library and isbn else 0.6 if library else 0, 0.5 if local.get("title") else 0.3)
    if found and found.get("match") == "title" and isbn:
        meta.warnings.append("moly.hu: a könyv megvan, de a fájl ISBN-jével egyező kiadás nincs — ellenőrizd a kiadót/évet")
    if found and found.get("confidence", 0) < 0.85:
        meta.warnings.append(f"moly.hu: csak cím alapján találtam (szerző nélkül), ellenőrizd: {found.get('url')}")
    meta.extra["torrent_name"] = book_name(meta.creators, meta.title)


def _ol_subjects(library: dict) -> tuple[list[str], list[str]]:
    """(genres, themes) of Open Library subjects: "genre:science fiction" is a genre, "Robots" only a theme."""

    genres, themes = [], []
    for subject in library.get("subjects") or []:
        if subject.startswith("genre:"):
            genres.append(subject[6:])
        elif ":" not in subject and len(subject) < 40:
            themes.append(subject)
    return genres[:5], themes[:10]


def _comic(meta: Meta, files: list[tuple[Path, str]], local: dict, name: dict, hungarian: bool, hints: dict) -> None:
    info = local.get("comic") or {}
    series = hints.get("series") or local.get("series") or name["title"]
    number = info.get("Number") or info.get("Volume") or name.get("volume") or name.get("issue") or ""
    numbers = sorted({n for n in (parse_name(p.stem).get("volume") or parse_name(p.stem).get("issue") for p, _ in files) if n}, key=int)
    if len(numbers) > 1:
        number = f"{numbers[0]}-{numbers[-1]}"
    writers = _split_authors(info.get("Writer", "")) or _split_authors(name["author"])
    artists = [a for a in _split_authors(info.get("Penciller", "") + "," + info.get("Artist", "")) if a not in writers]
    authors = _split_authors(hints["author"]) if hints.get("author") else writers + artists
    if not series:
        meta.warnings.append("nem sikerült megállapítani a képregény címét — add meg a címet")
        return
    found: dict = {}
    if hungarian:
        found = _moly(meta, f"{series} {number}." if number and "-" not in number else series, authors)
        if found:
            meta.add_source("moly.hu")
            if any(re.search(r"manga|manhwa", t, re.I) for t in found.get("tags") or []):
                meta.kind = "manga"
    media: dict = {}
    if meta.kind == "manga" or not keys.get("COMICVINE_KEY"):
        media = _anilist_match(meta, series, authors, local.get("year") or name.get("year", ""), name, flagged=meta.kind == "manga")
        if media:
            meta.kind = "manga"
            meta.add_source("AniList")
    updates: dict = {}
    if meta.kind == "manga" and not media:
        hits = _safe(meta, "MangaUpdates", mangaupdates.search, series, default=[])
        hit = next((h for h in hits if any(_similar(series, t) >= 0.92 for t in [h["title"]] + h.get("associated", []))), {})
        updates = (_safe(meta, "MangaUpdates", mangaupdates.series, hit["id"], default={}) or hit) if hit else {}
        if updates:
            meta.add_source("MangaUpdates")
    volume: dict = {}
    issue: dict = {}
    if meta.kind == "comic" and keys.get("COMICVINE_KEY"):
        year = local.get("year") or name.get("year", "")
        volumes = [v for v in _safe(meta, "ComicVine", comicvine.search_volumes, series, default=[]) if _similar(series, v["name"]) >= 0.92]
        volumes.sort(key=lambda v: abs(int(v["year"]) - int(year)) if v["year"].isdigit() and year.isdigit() else 99)
        volume = volumes[0] if volumes else {}
        if volume and number and "-" not in number:
            issue = _safe(meta, "ComicVine", comicvine.issue, volume["id"], number, default={})
        if volume:
            meta.add_source("ComicVine")
    elif meta.kind == "comic" and not hungarian:
        meta.warnings.append("ComicVine kihagyva: nincs COMICVINE_KEY a config.env-ben")
    if not authors:
        authors = media.get("authors") or updates.get("authors", []) + [a for a in updates.get("artists", []) if a not in updates.get("authors", [])]
        authors = authors or comicvine.writers(issue) + comicvine.artists(issue)
    meta.creators = (found.get("authors") if found.get("confidence", 0) >= 0.85 else None) or authors
    meta.title = found.get("title") if found.get("confidence", 0) >= 0.85 else series
    meta.subtitle = info.get("Title", "") if info.get("Title") and _norm(info.get("Title")) != _norm(series) else ""
    meta.title_original = media.get("title_romaji") or found.get("original_title", "")
    if _norm(meta.title_original) == _norm(meta.title or series):
        meta.title_original = ""
    meta.publisher = info.get("Publisher") or next((e["publisher"] for e in found.get("editions") or [] if e.get("publisher")), "") \
        or (updates.get("publisher_english") if not hungarian else "") or volume.get("publisher", "")
    meta.year = hints.get("year") or local.get("year") or next((e["year"] for e in found.get("editions") or [] if e.get("year")), "") \
        or name.get("year") or media.get("year") or updates.get("year") or volume.get("year", "")
    meta.date = local.get("date", "") if (local.get("date") or "").startswith(meta.year or "-") else ""
    if number:
        meta.extra["issue"] = number
    if local.get("isbns"):
        meta.identifiers["isbn"] = local["isbns"][0]
    if media.get("id"):
        meta.identifiers["anilist"] = str(media["id"])
    if local.get("pages"):
        meta.extra["pages"] = str(local["pages"])
    series_text = found.get("series") or ""
    if series_text:
        meta.extra["series"] = f"{series_text} {found.get('series_index')}." if found.get("series_index") else series_text
    moly_text, local_text = found.get("description", ""), local.get("description", "")
    moly_hu, local_hu = _is_hungarian_text(moly_text), _is_hungarian_text(local_text)
    hu_texts = [(moly_text if moly_hu else "", "moly.hu"), (local_text if local_hu else "", "ComicInfo")]
    en_texts = [(issue.get("description") or volume.get("description", ""), "ComicVine"), (media.get("description", ""), "AniList"),
                (updates.get("description", ""), "MangaUpdates"), (local_text if not local_hu else "", "ComicInfo"),
                (moly_text if not moly_hu else "", "moly.hu")]
    _description(meta, hu_texts, en_texts)
    meta.genres = found.get("genres") or [ANILIST_HU.get(g, g) for g in media.get("genres") or updates.get("genres") or []][:5]
    tags = map_styles(genre_tags=(media.get("genres") or updates.get("genres") or []) + (found.get("genres") or []),
                      theme_tags=[t for t in found.get("tags") or [] if t not in (found.get("genres") or [])] + local.get("subjects", []))
    meta.tags = ["manga.képregény"] + [t for t in tags if t not in {"manga.képregény", "szépirodalom", "szórakoztató.irodalom"}][:2]
    meta.cover_url = issue.get("cover_url") or volume.get("cover_url") or media.get("cover_url") or updates.get("cover_url", "")
    web = info.get("Web", "")
    links = [found.get("url", ""), issue.get("url") or volume.get("url", ""), media.get("url", ""), updates.get("url", ""), web if web.startswith("http") else ""]
    if hints.get("proof"):
        links.insert(0, hints["proof"])
    _set_proofs(meta, [link for link in links if link])
    meta.confidence = max(found.get("confidence", 0), 0.8 if media or updates or issue else 0.6 if volume else 0, 0.5 if info else 0.3)
    title = meta.title if found.get("confidence", 0) >= 0.85 else series
    if not found and number:
        if hungarian:
            title = f"{series} {number}."
        else:
            title = f"{series} Vol. {number}" if meta.kind == "manga" or name.get("volume") else f"{series} #{number}"
    meta.extra["torrent_name"] = book_name(meta.creators, title)


def _anilist_match(meta: Meta, series: str, authors: list[str], year: str, name: dict, *, flagged: bool) -> dict:
    """The AniList manga of a comic file. Same title is enough for a file marked as manga (ComicInfo, name, moly);
    a plain comic is a manga only with evidence (shared author, or a volume-numbered name without known authors),
    because AniList also holds Japanese editions of Western comics ("Spider-Man", 1970). A series that started
    after the file's year is another work."""

    hits = [m for m in _safe(meta, "AniList", anilist.search, series, default=[]) or []
            if m.get("format") in {"MANGA", "ONE_SHOT"} and any(_similar(series, t) >= 0.92 for t in anilist.titles(m))]
    if year.isdigit():
        hits = [m for m in hits if not (str(m.get("year", "")).isdigit() and int(m["year"]) > int(year) + 1)]
    best = next((m for m in hits if _authors_overlap(authors, m.get("authors") or [])), {})
    if best or not hits:
        return best
    if flagged:
        return hits[0]
    return hits[0] if not authors and name.get("volume") else {}


def _magazine(meta: Meta, release: Release, info: dict, local: dict, hungarian: bool, hints: dict) -> None:
    language = "magyar" if hungarian else _language_name(local.get("language", "")) or "angol"
    settings, path, created = magazine_config(info["name"], language, hints.get("magazine_dir"))
    meta.extra["magazine_config"] = str(path)
    if created:
        meta.warnings.append(f"új magazin: töltsd ki a beállítását (kiadó, igazoló link, ISSN): {path}")
    elif not settings.get("proof_link") and not settings.get("issn"):
        meta.warnings.append(f"magazin: nincs igazoló link / ISSN a beállításban: {path}")
    name = str(settings.get("name") or info["name"]).strip()
    country = str(settings.get("country") or "").strip().upper() if settings.get("multiple_editions") else ""
    meta.title = name
    meta.year = info.get("year", "")
    if info.get("month") and info.get("day"):
        meta.date = f"{info['year']}-{int(info['month']):02d}-{int(info['day']):02d}"
    if info.get("issue"):
        meta.extra["issue"] = info["issue"]
    meta.publisher = str(settings.get("publisher") or local.get("publisher") or "")
    issn = str(settings.get("issn") or "").strip()
    if issn:
        meta.identifiers["issn"] = issn
    meta.languages = [str(settings.get("language") or language)]
    description = str(settings.get("description") or "").strip()
    if description:
        meta.description_hu, meta.description_source = description, "magazin-beállítás"
    style = str(settings.get("style") or "magazin")
    meta.tags = ["magazin"] + ([style] if style != "magazin" and style in EBOOK_STYLES else [])
    links = [str(settings.get("proof_link") or ""), f"https://portal.issn.org/resource/ISSN/{issn}" if issn else ""]
    if hints.get("proof"):
        links.insert(0, hints["proof"])
    _set_proofs(meta, [link for link in links if link.startswith("http")])
    if local.get("pages"):
        meta.extra["pages"] = str(local["pages"])
    meta.confidence = 0.7 if settings else 0.4
    meta.extra["torrent_name"] = magazine_name({**info, "name": name}, country)


def _format(meta: Meta, files: list[tuple[Path, str]], local: dict) -> None:
    """extra["format"] like "epub, mobi" / "pdf - (OCR)"; the SCAN/OCR label comes from the first PDF/DjVu."""

    exts = list(dict.fromkeys(ext for _, ext in files))
    layer = ""
    paged = next((p for p, e in files if e in {".pdf", ".djvu"}), None)
    if paged is not None:
        layer = ebookmeta.detect_text_layer(paged)
        meta.extra["text_layer"] = layer
        meta.extra["scan_ocr"] = "SCAN" if layer == "SCAN" else "OCR"
        if not layer:
            meta.warnings.append("PDF/DJVU: nem tudtam eldönteni, SCAN vagy OCR — OCR-t írtam, ellenőrizd")
        if paged.suffix.lower() in {".pdf", ".djvu"} and paged == Path(meta.extra.get("main_file", "")) and not meta.extra.get("pages"):
            pages = ebookmeta.page_count(paged)
            if pages:
                meta.extra["pages"] = str(pages)
    meta.extra["format"] = ", ".join(ebookmeta.format_label(e, layer) for e in exts)


def _languages(meta: Meta, local: dict, hungarian: bool) -> None:
    if meta.languages:
        return
    code = local.get("language") or ""
    if not code:
        code = translate.language(local.get("text") or "") or translate.language(meta.description_hu if hungarian else meta.description_en)
    name = _language_name(code)
    if not name or name == "ismeretlen":
        meta.warnings.append("a könyv nyelve nem állapítható meg; add meg az adatlapon: hints.language")
        return
    meta.languages = [name]
    if hungarian and name != "magyar":
        meta.warnings.append(f"a könyv nyelve: {name} — de magyar (HUN) kategóriát választottál")
    elif not hungarian and name == "magyar":
        meta.warnings.append("a könyv magyar nyelvűnek tűnik, de angol (ENG) kategóriát választottál")
