"""The common result of every resolver: what we learnt about one piece of content, with provenance.

Resolvers (``ncore_up/resolve/*.py``) fill a ``Meta`` from local files and outside sources; ``plan.py`` turns it into
the form, the description and the attachments. Every field is optional; ``confidence`` (0..1) says how sure the
identification is, and ``warnings`` carries everything the user should read before an automatic upload.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from urllib.parse import urlsplit


def valid_proof(url: str) -> bool:
    """Wiki 258/30: publication evidence, excluding the explicitly forbidden source types and URL shorteners."""
    parts = urlsplit(url or "")
    host = (parts.hostname or "").casefold()
    forbidden = ("wikipedia.org", "wikidata.org", "youtube.com", "youtu.be", "videa.hu", "rateyourmusic.com",
                 "music-bazaar.com", "blogspot.com", "wordpress.com", "tinyurl.com", "bit.ly", "t.co", "goo.gl")
    return bool(parts.scheme in {"http", "https"} and host
                and not any(host == bad or host.endswith("." + bad) for bad in forbidden)
                and not re.search(r"(?i)(?:^|[/.])(?:forum|forums|blog)(?:[/.]|$)", host + parts.path)
                and not re.search(r"(?i)\.(?:jpe?g|png|gif|webp)(?:$|/)", parts.path))


@dataclass
class Track:
    position: str  # "1", "01", "A1", "2-03"
    title: str
    length: str = ""  # "3:21"
    artist: str = ""  # only when it differs from the album artist


@dataclass
class Meta:
    kind: str = ""  # movie, tv, album, audiobook, concert, book, comic, manga, magazine, game, console_game, app, software
    title: str = ""  # canonical (original/English) title
    title_hu: str = ""
    title_original: str = ""
    subtitle: str = ""
    year: str = ""
    date: str = ""  # full release date when known
    creators: list[str] = field(default_factory=list)  # artists, authors, developers
    publisher: str = ""  # label, publisher, developer company
    description_hu: str = ""
    description_en: str = ""
    description_source: str = ""  # "PORT.hu", "Wikipédia (CC BY-SA)", "TMDB", "Steam", ...
    machine_translated: bool = False
    genres: list[str] = field(default_factory=list)  # free text (Hungarian when available)
    tags: list[str] = field(default_factory=list)  # already mapped to nCore's style vocabulary
    cover_url: str = ""
    cover: bytes = b""
    proof_links: list[str] = field(default_factory=list)  # never Wikipedia/YouTube/Videa (nCore wiki)
    tracks: list[Track] = field(default_factory=list)
    episodes: list[dict] = field(default_factory=list)  # {"season", "episode", "title_hu", "title"}
    version: str = ""
    platform: str = ""
    region: str = ""  # NTSC (USA) / PAL (EUR) / Régiófüggetlen / NTSC-J
    languages: list[str] = field(default_factory=list)  # Hungarian language names
    requirements: str = ""  # hardware / software requirements, Hungarian labels when possible
    install_hu: str = ""  # Hungarian installation guide
    identifiers: dict[str, str] = field(default_factory=dict)  # imdb, tmdb, porthu, wikidata, mbid, discogs, isbn, steam, gog, igdb, cusa, titleid, ...
    extra: dict[str, str] = field(default_factory=dict)  # anything else worth showing (narrator, format, scan/ocr, ...)
    confidence: float = 0.0
    sources: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def add_source(self, name: str) -> None:
        if name and name not in self.sources:
            self.sources.append(name)

    def add_proof(self, url: str) -> None:
        if url and url not in self.proof_links and valid_proof(url):
            self.proof_links.append(url)

    @property
    def description(self) -> str:
        return self.description_hu or self.description_en


MUSIC_STYLES = (
    "60s 70s 80s 90s acid alternative ambient blues breaks classical country dance death.metal disco drum.and.bass dub dubstep "
    "electronic emo euro.disco euro.house eurodance europop experimental folk funk garage goa.trance grunge hardcore hardcore.dance "
    "hardstyle hip.hop house indie.rock industrial italo.disco jazz latin live metal musical new.age ost pop pop.rock "
    "progressive.house progressive.rock progressive.trance psychedelic psytrance punk reggae rhythm.and.blues rock ska soul "
    "synth.pop techno trance trip.hop uk.garage world.music"
).split()

# Scraped from nCore's upload form (style_select_ebook), 2026-10-16.
EBOOK_STYLES = (
    "életmód.egészség erotika ezoterika fantasy gasztronómia gyermek hadászat hangoskönyv hobbi irodalom ismeretterjesztő "
    "kert.és.lakás lexikon.enciklopédia magazin manga.képregény művészet pénz.befektetés.üzlet romantikus sci-fi sport "
    "szakkönyv számítástechnika szépirodalom szórakoztató.irodalom szótár.nyelvkönyv tankönyv.segédkönyv társadalomtudomány "
    "természettudomány történelem utazás vallás zene"
).split()
