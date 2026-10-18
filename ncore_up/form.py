"""Build the nCore upload form for every (non-XXX) category, original release or not.

Which fields exist for which category follows the upload page's own logic (ncorev12.js ``lapoz`` /
``utolso_csekk``):

* original release: NFO required; films/series get the infobar (IMDb + titles, country, length, director,
  cast, DVD date); music and clips only style tags; ebooks only style tags; screenshots optional (films and
  series: required when the release has no sample).
* non-release: techinfo required for films, series, music and clips; films and series also the infobar,
  HD/DVD films menu/subtitles/audio/extras, series season (a 0-23 select)/episodes/episode title; music and clips
  artist/album/year (+ tracklist in the description for music); ebooks title/year/format; three screenshots for
  everything except mp3 and lossless (the page does not ask them for ebooks, the wiki does: 3 page images).
* games, programs, console and mobile have no own fields.

Style tags are joined with ", " as the page's tag picker joins them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import categories as cats
from .meta import EBOOK_STYLES, MUSIC_STYLES

# nCore wiki (infobar): "Anglia helyett UK-t, Amerikai Egyesült Államok helyett USA-t írjunk".
COUNTRY_FIX = {"United States": "USA", "United States of America": "USA", "United Kingdom": "UK", "England": "UK"}
SERIES_CODES = {"xvidser_hun", "xvidser", "dvdser_hun", "dvdser", "hdser_hun", "hdser"}
DISC_FILM_CODES = {"dvd_hun", "dvd", "dvd9_hun", "dvd9", "hd_hun", "hd"}
MUSIC_CODES = {"mp3_hun", "mp3", "lossless_hun", "lossless", "clip"}
EBOOK_CODES = {"ebook_hun", "ebook"}
NO_SCREENSHOT_CODES = {"mp3_hun", "mp3", "lossless_hun", "lossless"}  # the wiki also wants 3 page images for ebooks


def renders_video_details(code: str, original: bool) -> bool:
    """nCore prepends menu/subtitle/audio/extra fields to these descriptions."""
    return not original and code in DISC_FILM_CODES
MAX_SEASON = 23  # the upload page's season select
KNOWN_STYLES = frozenset(MUSIC_STYLES) | frozenset(EBOOK_STYLES)
TRACKLIST_HEAD_RE = re.compile(r"(?im)^\s*(?:\[b\])?\s*(?:tracklist|tracklista|fejezetek|számlista|dalok)\b")
FIRST_TRACK_RE = re.compile(r"(?m)^\s*(?:\d{1,2}[-.])?0{0,2}1[.)]\s|^\s*[A-D]1[.)\s]")


def country(value: str | list[str]) -> str:
    parts = value if isinstance(value, list) else [p.strip() for p in value.split(",")]
    return ", ".join(COUNTRY_FIX.get(p, p) for p in parts if p)


def style_tags(values: list[str]) -> str:
    """nCore style tags: lower case, words joined with dots (punk.rock). The page's own vocabulary is kept as it is:
    it has dashes and accented letters ("sci-fi", "életmód.egészség")."""

    tags: list[str] = []
    for value in values:
        for part in str(value).split(","):
            tag = re.sub(r"[\s_/]+", ".", part.strip().lower().replace("&", " and ")).strip(".")
            if tag not in KNOWN_STYLES:
                tag = re.sub(r"\.{2,}", ".", re.sub(r"[^\w.]+", ".", tag)).strip(".")
                if tag.replace(".", "-") in KNOWN_STYLES:
                    tag = tag.replace(".", "-")
            if tag and tag not in tags:
                tags.append(tag)
    return ", ".join(tags)


@dataclass
class Infobar:
    imdb_id: str = ""
    other_link: str = ""
    year: str = ""
    dvd_date: str = ""
    countries: list[str] = field(default_factory=list)
    runtime: str = ""
    title_hu: str = ""
    title_en: str = ""
    title_other: str = ""  # "Eredeti/magyar cím": the original title when the torrent has the Hungarian one, and vice versa
    directors: list[str] = field(default_factory=list)
    cast: list[str] = field(default_factory=list)


@dataclass
class UploadData:
    category: str
    name: str
    original: bool
    description: str = ""
    infobar: Infobar | None = None
    # non-release films/series
    menu: bool | None = None
    subtitles: str = ""
    audio: str = ""
    extras: str = ""
    season: int | None = None
    episodes: str = ""
    episode_title: str = ""
    # music
    artist: str = ""
    album: str = ""
    album_year: str = ""
    # ebook
    book_title: str = ""
    book_year: str = ""
    book_format: str = ""
    styles: list[str] = field(default_factory=list)
    request_id: str = ""


def build(token: str, data: UploadData) -> dict[str, str]:
    code = data.category
    request = str(data.request_id or "").strip()
    form = {
        "getUnique": token,
        "tipus": code,
        "torrent_nev": data.name.strip(),
        "eredeti": "igen" if data.original else "nem",
        "szoveg": description_text(data),
        "keresre": "igen" if request else "nem",
        "keres_kodja": request,
        "mindent_tud1": "szabalyzat",
        "mindent_tud3": "seedeles",
    }
    if code in cats.VIDEO_CODES and data.infobar:
        bar = data.infobar
        form.update({
            "imdb_id": bar.imdb_id.strip(), "film_adatbazis": bar.other_link.strip(), "megjelent": str(bar.year), "dvd_megjelent": bar.dvd_date,
            "orszag": country(bar.countries), "hossz": str(bar.runtime), "film_magyar_cim": bar.title_hu, "film_angol_cim": bar.title_en,
            "film_idegen_cim": bar.title_other, "rendezo": ", ".join(bar.directors), "szereplok": ", ".join(bar.cast),
        })
        if renders_video_details(code, data.original):
            form.update({"film_felirat": data.subtitles, "film_hangsav": data.audio, "film_extra": data.extras})
            if data.menu is not None:
                form["menu"] = "van" if data.menu else "nincs"
        if not data.original and code in SERIES_CODES:
            form.update({"szezon": "" if data.season is None else str(data.season), "epizod_szamok": data.episodes, "epizod_cim": data.episode_title})
    if code in MUSIC_CODES:
        form["music_style"] = style_tags(data.styles)
        if not data.original:
            form.update({"eloado": data.artist, "album": data.album, "album_megjelenes": str(data.album_year)})
    if code in EBOOK_CODES:
        form["ebook_style"] = style_tags(data.styles)
        if not data.original:
            form.update({"konyv_cime": data.book_title, "konyvv_kiadva": str(data.book_year), "konyv_formatum": data.book_format})
    return form


def description_text(data: UploadData) -> str:
    """Keep fields nCore prepends out of generated, supplied and edited free text."""
    if data.original:
        return data.description
    from . import describe
    if data.category in MUSIC_CODES:
        return describe.clean_music_description(data.description, artist=data.artist, album=data.album, year=data.album_year)
    if data.category in cats.VIDEO_CODES:
        return describe.clean_video_description(data.description, series=data.category in SERIES_CODES,
                                                structured_details=renders_video_details(data.category, False))
    return data.description


def needs_techinfo(code: str, original: bool) -> bool:
    return not original and (code in cats.VIDEO_CODES or code in MUSIC_CODES)


def needs_screenshots(code: str, original: bool, has_sample: bool = False) -> bool:
    """Own uploads: always 3 pictures (except music). Original films/series: 3 pictures only when the release has no
    sample (upload page: "Film és sorozat esetén sample hiányában 3 darab mintakép megadása kötelező!")."""

    if original:
        return code in cats.VIDEO_CODES and not has_sample
    return code not in NO_SCREENSHOT_CODES


def has_tracklist(description: str) -> bool:
    return bool(TRACKLIST_HEAD_RE.search(description) or FIRST_TRACK_RE.search(description))


def missing(form: dict[str, str], files: dict[str, object], has_sample: bool = False) -> list[str]:
    """What the upload page itself (or the wiki) would refuse."""

    code, original = form["tipus"], form["eredeti"] == "igen"
    problems = []
    if len(form["torrent_nev"]) < 6:
        problems.append("a torrent neve legalább 6 karakter")
    if original and "nfo_fajl" not in files:
        problems.append("eredeti release-hez NFO kell")
    if needs_techinfo(code, original) and "techinfo_fajl" not in files:
        problems.append("techinfo fájl kell (MediaInfo / BDInfo)")
    if needs_screenshots(code, original, has_sample) and not all(k in files for k in ("kep1", "kep2", "kep3")):
        problems.append("3 kép kell" + (" (az eredeti release-ben nincs sample)" if original else ""))
    if code in cats.VIDEO_CODES:
        if not form.get("imdb_id") and not form.get("film_adatbazis"):
            problems.append("infobar: IMDb azonosító vagy más filmadatbázis link kell")
        elif form.get("imdb_id") and not re.fullmatch(r"tt\d+", form["imdb_id"]):
            problems.append(f"infobar: hibás IMDb azonosító ({form['imdb_id']}), tt1234567 alakú kell")
        if not form.get("film_magyar_cim") and not form.get("film_angol_cim"):
            problems.append("infobar: cím kell")
    if code in SERIES_CODES and not original and form.get("szezon"):
        season = form["szezon"]
        if not season.isdigit() or int(season) > MAX_SEASON:
            problems.append(f"évad: az nCore űrlapján 0–{MAX_SEASON} választható (ez: {season})")
    if code in MUSIC_CODES and not original and not (form.get("eloado") and form.get("album")):
        problems.append("zene: előadó és album kell")
    if code in EBOOK_CODES and not original and not form.get("konyv_cime"):
        problems.append("e-könyv: a könyv címe kell")
    if form.get("keresre") == "igen" and not form.get("keres_kodja"):
        problems.append("kérés teljesítése: hiányzik a kérés kódja")
    return problems
