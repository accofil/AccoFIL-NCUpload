"""Music resolver: albums/singles (nCore mp3, mp3_hun, lossless, lossless_hun) and audiobooks ("hangoskönyv").

Identification chain, stopping at the first confident match:

1. hints "mbid" / "discogs", MusicBrainz release id embedded in the tags (Picard), MB/Discogs links in the NFO;
2. disc ID / TOC from an EAC/XLD log, a CUE sheet or a FLAC CUESHEET block (exact, then fuzzy lookup);
3. barcode / catalogue number (tags, CUE CATALOG, NFO) on MusicBrainz, then Discogs;
4. AcoustID fingerprints of three tracks (ACOUSTID_KEY), release ids intersected;
5. loose MusicBrainz search + Discogs search, scored against the local track count and durations;
6. Spotify (only with both keys), else nothing: confidence < 0.5 and a warning.

The tracklist always comes from the local files (order, count, durations); database titles only fix the spelling
when the match is confident and the track counts are equal. Style tags map MB genres, Discogs styles/genres and
Last.fm tags onto nCore's vocabulary (``meta.MUSIC_STYLES``); audiobooks get "hangoskönyv" only.
"""

from __future__ import annotations

import re
import shutil
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

from .. import audio as old_audio
from .. import keys, net, translate
from .. import release as rel
from ..local import audiotags, chapters as chaps, cuelog, fingerprint, nfo
from ..meta import MUSIC_STYLES, Meta, Track
from ..sources import acoustid, coverart, discogs, lastfm, musicbrainz as mb, spotify, theaudiodb, wikidata, wikipedia

CONFIDENT = 0.75
MAX_DESCRIPTION = 1200
AUDIOBOOK_GENRES = {"audiobook", "audio book", "audiobooks", "hangoskonyv", "hangoskönyv", "spoken word", "speech", "horbuch", "hörbuch",
                    "books & spoken", "books and spoken", "spokenword", "audio drama", "radio play", "hangjáték", "hangjatek"}
AUDIOBOOK_NAME = re.compile(r"(?i)hangosk[öo]nyv|audio\s*book|h[öo]rbuch|\bunabridged\b|\babridged\b|hangj[aá]t[eé]k")
LIVE_TITLE = re.compile(r"(?i)\blive\s+(?:at|in|from|on)\b|[(\[]live[)\]]|\bkoncert|\bunplugged\b|\bélő(?:ben)?\b|\bin concert\b")
EDITION_RE = re.compile(r"(?i)\s*[(\[][^)\]]*(deluxe|remaster|edition|bonus|expanded|anniversary|reissue|flac|mp3|320|v0|web|cd|vinyl|"
                        r"24.?bit|16.?bit|\d{2}.?\d{2,3}\s*khz|hi.?res|kiadás|limited|special)[^)\]]*[)\]]")
SOURCE_TOKENS = {"WEB": "Digital Media", "WEBFLAC": "Digital Media", "CD": "CD", "CDA": "CD", "CDM": "CD", "CDS": "CD", "CDR": "CD",
                 "VINYL": "Vinyl", "LP": "Vinyl", "VLS": "Vinyl", "CASSETTE": "Cassette", "TAPE": "Cassette", "MC": "Cassette", "DVD": "DVD"}
NFO_MBID = re.compile(r"musicbrainz\.org/release/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")
NFO_DISCOGS = re.compile(r"discogs\.com/(?:[^/\s]+/)?release/(\d+)")
NFO_BARCODE = re.compile(r"(?i)\b(?:upc|ean|barcode|vonalk[oó]d)\s*(?:/\s*ean)?\s*[:#.]?\s*(\d[\d ]{10,15}\d)")
NFO_CATNO = re.compile(r"(?im)\b(?:cat(?:alog(?:ue)?)?[\s._]*(?:no|nr|number|#)?|katal[oó]gussz[aá]m)[\s.]*[:#]\s*"
                       r"([A-Z0-9][A-Z0-9.\-/]{0,15}(?: [A-Z0-9.\-/]{1,10}){0,2})(?=\s{2,}|\s*$|\s*\|)")
VARIOUS = {"various artists", "various", "va", "v a", "valogatas", "válogatás", "vegyes eloadok", "vegyes előadók"}
FEATURING = re.compile(r"(?i)\s+(?:feat\.?|ft\.?|featuring|vs\.?)\s+|\s*[(\[](?:feat\.?|ft\.?|featuring)\s")
PLACEHOLDER_TITLE = re.compile(r"(?i)^(?:audio\s*)?(?:track|sáv|szám|title)?\s*\d{0,3}$|^unknown|^ismeretlen|^untitled")
URL_PROOF_TYPES = ("purchase for download", "purchase for mail-order", "download for free", "discography entry")
NO_PROOF_HOSTS = ("youtube.", "youtu.be", "wikipedia.", "wikidata.", "videa.", "rateyourmusic.", "music-bazaar.")


# --- small text helpers -------------------------------------------------------------------------------------

def fold(text: str) -> str:
    """Accent-free, lower-case, punctuation-free text for comparisons ('10 000' == '10000', leading 'the' dropped)."""

    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).lower().replace("&", " and ")
    text = re.sub(r"(?<=\d)[ .,](?=\d{3}\b)", "", text)
    text = re.sub(r"[^0-9a-z]+", " ", text).strip()
    return re.sub(r"^the ", "", text)


def similarity(a: str, b: str) -> float:
    a, b = fold(a), fold(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    try:
        from rapidfuzz import fuzz  # optional dependency

        return max(fuzz.ratio(a, b), fuzz.token_sort_ratio(a, b) * 0.95) / 100
    except ImportError:
        return SequenceMatcher(None, a, b).ratio()


def length_text(seconds: float) -> str:
    return chaps.length_text(seconds)


def year_suffix(year: str | int) -> str:
    """Hungarian inessive suffix of a year by vowel harmony of its last spoken part: 1969-ben, 1993-ban, 1980-ban,
    1970-ben, 2000-ben, 2020-ban, 1900-ban."""

    number = int(str(year)[:4]) if str(year)[:4].isdigit() else 0
    units, tens, hundreds = number % 10, number // 10 % 10, number // 100 % 10
    if units:
        return "ban" if units in (3, 6, 8) else "ben"
    if tens:
        return "ban" if tens in (2, 3, 6, 8) else "ben"
    if hundreds:
        return "ban"  # száz
    return "ben"  # ezer


def article(name: str) -> str:
    """Hungarian definite article for a band name: 'az' before a vowel sound, else 'a'."""

    name = (name or "").strip()
    if not name:
        return "a"
    match = re.match(r"\d+", name)
    if match:
        digits = match.group(0)
        if digits[0] == "5" or (digits[0] == "1" and len(digits) in (1, 4)):
            return "az"  # öt..., egy, ezer...
        return "a"
    first = unicodedata.normalize("NFKD", name[0].lower())[0]
    if name[:2].upper() == "U2":
        return "a"
    acronym = re.match(r"([A-Z]{1,4})(?![a-z])", name)
    if acronym and not re.search(r"[AEIOU]", acronym.group(1)):  # read letter by letter: el-gé-té, er-gó, ká-ef-té
        return "az" if acronym.group(1)[0] in "FLMNRSX" else "a"
    return "az" if first in "aeiou" else "a"


def cut_text(text: str, limit: int = MAX_DESCRIPTION) -> str:
    text = re.sub(r"\s+\n", "\n", (text or "").strip())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind(".\n"))
    return (cut[: end + 1] if end > limit // 2 else cut.rsplit(" ", 1)[0] + " …").strip()


# --- style tags -----------------------------------------------------------------------------------------------

# normalized term -> nCore tags (the first gets the full weight, the others half)
STYLE_MAP: dict[str, tuple[str, ...]] = {
    "rock": ("rock",), "hard rock": ("rock",), "classic rock": ("rock",), "rock and roll": ("rock",), "rock n roll": ("rock",),
    "blues rock": ("rock", "blues"), "garage rock": ("rock",), "southern rock": ("rock",), "arena rock": ("rock",), "glam rock": ("rock",),
    "stoner rock": ("rock",), "heartland rock": ("rock",), "beat": ("rock",), "beat music": ("rock",), "merseybeat": ("rock",),
    "space rock": ("psychedelic", "rock"), "alternative rock": ("alternative", "rock"), "alternative": ("alternative",),
    "noise rock": ("alternative",), "shoegaze": ("alternative",), "post punk": ("alternative", "punk"), "new wave": ("alternative",),
    "gothic rock": ("alternative", "rock"), "darkwave": ("alternative",), "post rock": ("alternative", "experimental"),
    "math rock": ("alternative",), "dream pop": ("alternative", "pop"), "grunge": ("grunge", "rock"), "post grunge": ("grunge", "rock"),
    "indie rock": ("indie.rock",), "indie": ("indie.rock",), "indie pop": ("indie.rock", "pop"), "lo fi": ("indie.rock",),
    "pop rock": ("pop.rock",), "soft rock": ("pop.rock",), "power pop": ("pop.rock",), "britpop": ("pop.rock",), "pop": ("pop",),
    "dance pop": ("dance", "pop"), "teen pop": ("pop",), "europop": ("europop",), "euro pop": ("europop",), "schlager": ("pop",),
    "chanson": ("pop",), "vocal": ("pop",), "easy listening": ("pop",), "k pop": ("pop",), "j pop": ("pop",), "magyar pop": ("pop",),
    "art pop": ("pop", "experimental"), "psychedelic pop": ("psychedelic", "pop"), "baroque pop": ("pop",), "sunshine pop": ("pop",),
    "progressive rock": ("progressive.rock", "rock"), "prog rock": ("progressive.rock", "rock"), "art rock": ("progressive.rock", "rock"),
    "symphonic rock": ("progressive.rock", "rock"), "symphonic prog": ("progressive.rock",), "progressive": ("progressive.rock",),
    "krautrock": ("progressive.rock", "experimental"), "canterbury scene": ("progressive.rock",), "jazz rock": ("jazz", "rock"),
    "fusion": ("jazz",), "jazz fusion": ("jazz",), "psychedelic rock": ("psychedelic", "rock"), "psychedelic": ("psychedelic",),
    "psychedelia": ("psychedelic",), "acid rock": ("psychedelic", "rock"), "neo psychedelia": ("psychedelic",),
    "heavy metal": ("metal",), "metal": ("metal",), "death metal": ("death.metal", "metal"), "melodic death metal": ("death.metal", "metal"),
    "deathcore": ("death.metal", "metal"), "metalcore": ("metal", "hardcore"), "punk": ("punk",), "punk rock": ("punk", "rock"),
    "pop punk": ("punk",), "hardcore punk": ("punk", "hardcore"), "ska punk": ("punk", "ska"), "emo": ("emo",), "screamo": ("emo",),
    "emocore": ("emo",), "hardcore": ("hardcore",), "happy hardcore": ("hardcore.dance",), "gabber": ("hardcore.dance",),
    "hardcore techno": ("hardcore.dance",), "hardstyle": ("hardstyle",), "synth pop": ("synth.pop",), "synthpop": ("synth.pop",),
    "electropop": ("synth.pop",), "electro pop": ("synth.pop",), "italo disco": ("italo.disco", "disco"), "euro disco": ("euro.disco", "disco"),
    "eurodisco": ("euro.disco", "disco"), "eurodance": ("eurodance", "dance"), "euro house": ("euro.house", "house"), "disco": ("disco",),
    "nu disco": ("disco",), "hi nrg": ("disco", "dance"), "progressive house": ("progressive.house", "house"),
    "progressive trance": ("progressive.trance", "trance"), "goa trance": ("goa.trance", "psytrance"), "goa": ("goa.trance",),
    "psytrance": ("psytrance", "trance"), "psy trance": ("psytrance", "trance"), "psychedelic trance": ("psytrance", "trance"),
    "full on": ("psytrance",), "trance": ("trance",), "acid house": ("acid", "house"), "acid techno": ("acid", "techno"), "acid": ("acid",),
    "techno": ("techno",), "minimal": ("techno",), "minimal techno": ("techno",), "detroit techno": ("techno",), "tech house": ("techno", "house"),
    "house": ("house",), "deep house": ("house",), "electro house": ("house", "electronic"), "french house": ("house",),
    "vocal house": ("house",), "garage house": ("garage", "house"), "speed garage": ("garage",), "uk garage": ("uk.garage", "garage"),
    "2 step": ("uk.garage",), "garage": ("garage",), "drum and bass": ("drum.and.bass",), "drum n bass": ("drum.and.bass",),
    "dnb": ("drum.and.bass",), "jungle": ("drum.and.bass",), "liquid funk": ("drum.and.bass",), "neurofunk": ("drum.and.bass",),
    "dubstep": ("dubstep",), "dub": ("dub", "reggae"), "breaks": ("breaks",), "breakbeat": ("breaks",), "big beat": ("breaks",),
    "trip hop": ("trip.hop",), "downtempo": ("ambient",), "chillout": ("ambient",), "chill out": ("ambient",), "ambient": ("ambient",),
    "dark ambient": ("ambient",), "new age": ("new.age",), "industrial": ("industrial",), "ebm": ("industrial",),
    "industrial rock": ("industrial", "rock"), "industrial metal": ("industrial", "metal"), "experimental": ("experimental",),
    "avant garde": ("experimental",), "avantgarde": ("experimental",), "noise": ("experimental",), "musique concrete": ("experimental",),
    "electronic": ("electronic",), "electronica": ("electronic",), "idm": ("electronic",), "electro": ("electronic",),
    "edm": ("electronic", "dance"), "synthwave": ("electronic",), "dance": ("dance",), "club": ("dance",), "hip hop": ("hip.hop",),
    "rap": ("hip.hop",), "trap": ("hip.hop",), "boom bap": ("hip.hop",), "gangsta rap": ("hip.hop",), "rhythm and blues": ("rhythm.and.blues",),
    "r and b": ("rhythm.and.blues",), "rnb": ("rhythm.and.blues",), "contemporary r and b": ("rhythm.and.blues",), "soul": ("soul",),
    "neo soul": ("soul",), "motown": ("soul",), "funk": ("funk",), "funk soul": ("funk", "soul"), "p funk": ("funk",), "blues": ("blues",),
    "jazz": ("jazz",), "smooth jazz": ("jazz",), "bebop": ("jazz",), "swing": ("jazz",), "big band": ("jazz",), "acid jazz": ("jazz", "funk"),
    "free jazz": ("jazz", "experimental"), "classical": ("classical",), "baroque": ("classical",), "romantic": ("classical",),
    "opera": ("classical",), "choral": ("classical",), "chamber music": ("classical",), "contemporary classical": ("classical",),
    "modern classical": ("classical",), "neo classical": ("classical",), "orchestral": ("classical",), "minimalism": ("classical", "experimental"),
    "country": ("country",), "bluegrass": ("country",), "americana": ("country", "folk"), "country rock": ("country", "rock"),
    "folk": ("folk",), "folk rock": ("folk", "rock"), "singer songwriter": ("folk",), "acoustic": ("folk",), "nepzene": ("folk", "world.music"),
    "magyar nepzene": ("folk", "world.music"), "tanchaz": ("folk",), "hungarian folk": ("folk", "world.music"), "world": ("world.music",),
    "world music": ("world.music",), "ethnic": ("world.music",), "balkan": ("world.music",), "klezmer": ("world.music",),
    "celtic": ("world.music", "folk"), "gypsy": ("world.music",), "romani": ("world.music",), "african": ("world.music",),
    "afrobeat": ("world.music", "funk"), "flamenco": ("world.music", "latin"), "fado": ("world.music",), "latin": ("latin",),
    "salsa": ("latin",), "reggaeton": ("latin",), "bossa nova": ("latin", "jazz"), "samba": ("latin",), "tango": ("latin",),
    "cumbia": ("latin",), "latin pop": ("latin", "pop"), "brazilian": ("latin",), "mpb": ("latin",), "reggae": ("reggae",),
    "roots reggae": ("reggae",), "dancehall": ("reggae",), "ragga": ("reggae",), "ska": ("ska",), "2 tone": ("ska",),
    "rocksteady": ("ska", "reggae"), "soundtrack": ("ost",), "score": ("ost",), "film score": ("ost",), "video game music": ("ost",),
    "original score": ("ost",), "stage and screen": ("ost",), "ost": ("ost",), "musical": ("musical",), "show tunes": ("musical",),
    "broadway": ("musical",), "cast recording": ("musical",), "rock opera": ("rock", "musical"), "live": ("live",),
    "60s": ("60s",), "70s": ("70s",), "80s": ("80s",), "90s": ("90s",), "1960s": ("60s",), "1970s": ("70s",), "1980s": ("80s",), "1990s": ("90s",),
    "concert film": ("live",),
}
SUFFIX_MAP = (
    (" death metal", ("death.metal", "metal")), (" metal", ("metal",)), (" punk", ("punk",)), (" trance", ("trance",)),
    (" house", ("house",)), (" techno", ("techno",)), (" jazz", ("jazz",)), (" blues", ("blues",)), (" soul", ("soul",)),
    (" funk", ("funk",)), (" reggae", ("reggae",)), (" hip hop", ("hip.hop",)), (" rap", ("hip.hop",)), (" folk", ("folk",)),
    (" country", ("country",)), (" disco", ("disco",)), (" ambient", ("ambient",)), (" rock", ("rock",)), (" pop", ("pop",)),
    (" classical", ("classical",)),
)
IGNORED_STYLES = {"hungarian", "magyar", "hungary", "seen live", "male vocalists", "female vocalists", "british", "english", "american",
                  "non music", "children s", "childrens", "comedy", "spoken word", "audiobook", "folk world and country", "brass and military"}


def style_key(term: str) -> str:
    text = unicodedata.normalize("NFKD", term or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = text.replace("&", " and ").replace("'", "").replace("’", "")
    return re.sub(r"\s+", " ", re.sub(r"[-_/.,:;]+", " ", text)).strip()


def map_style(term: str) -> list[tuple[str, float]]:
    """nCore tags of one genre/style/tag name with their weight factor ([] when it has no nCore equivalent)."""

    key = style_key(term)
    if not key or key in IGNORED_STYLES:
        return []
    tags = STYLE_MAP.get(key) or STYLE_MAP.get(re.sub(r" music$", "", key))
    if not tags:
        dotted = key.replace(" ", ".")
        if dotted in MUSIC_STYLES:
            tags = (dotted,)
    if not tags:
        tags = next((t for suffix, t in SUFFIX_MAP if key.endswith(suffix)), ())
    return [(tag, 1.0 if index == 0 else 0.5) for index, tag in enumerate(tags)]


def style_terms(mb_release: dict | None = None, discogs_release: dict | None = None, lastfm_tags: list | None = None,
                local_genres: list[str] | None = None, title: str = "") -> list[tuple[str, float]]:
    """Weighted source terms: Discogs styles 3, Discogs genres 1.5, MB genres 2 (by vote share), MB artist genres 1,
    Last.fm 1 (by count), local genre tags 1; release types (Live, Soundtrack) 3."""

    terms: list[tuple[str, float]] = []
    if discogs_release:
        electronic = "Electronic" in discogs_release.get("genres", []) and "Rock" not in discogs_release.get("genres", [])
        for style in discogs_release.get("styles", []):
            terms.append(("hardcore techno" if style == "Hardcore" and electronic else style, 3.0))
        terms += [(g, 1.5) for g in discogs_release.get("genres", [])]
    if mb_release:
        group = mb_release.get("release_group") or {}
        for votes, weight in ((group.get("genres") or [], 2.0), (mb_release.get("genres") or [], 2.0), (mb_release.get("artist_genres") or [], 1.0)):
            top = max((c for _n, c in votes), default=0) or 1
            terms += [(name, weight * max(0.25, count / top)) for name, count in votes]
        genre_names = {n for n, _c in group.get("genres") or []}
        tags = [(n, c) for n, c in group.get("tags") or [] if n not in genre_names]
        top = max((c for _n, c in tags), default=0) or 1
        terms += [(name, 1.0 * count / top) for name, count in tags]
        types = set(group.get("secondary_types") or [])
        if "Live" in types:
            terms.append(("live", 3.0))
        if "Soundtrack" in types:
            terms.append(("soundtrack", 3.0))
    for name, count in (lastfm_tags or [])[:10]:
        if count >= 20:
            terms.append((name, 1.0 * count / 100))
    terms += [(g, 1.0) for g in local_genres or []]
    if title and LIVE_TITLE.search(title):
        terms.append(("live", 3.0))
    return terms


def style_tags(terms: list[tuple[str, float]], limit: int = 5) -> list[str]:
    """nCore style tags: summed weights >= 2 (else the single best), at most ``limit``, strongest first."""

    scores: Counter = Counter()
    for term, weight in terms:
        for tag, factor in map_style(term):
            scores[tag] += weight * factor
    ranked = [tag for tag, score in sorted(scores.items(), key=lambda x: (-x[1], x[0])) if score > 0]
    strong = [tag for tag in ranked if scores[tag] >= 2]
    return (strong or ranked[:1])[:limit]


# --- local data -------------------------------------------------------------------------------------------------

@dataclass
class LocalTrack:
    disc: int
    number: int
    title: str
    length: float
    artist: str = ""
    from_tag: bool = True
    path: Path | None = None


@dataclass
class Local:
    files: list[audiotags.AudioTags] = field(default_factory=list)
    tracks: list[LocalTrack] = field(default_factory=list)
    artist: str = ""
    album: str = ""
    year: str = ""
    date: str = ""
    genres: list[str] = field(default_factory=list)
    label: str = ""
    catno: str = ""
    barcode: str = ""
    mb_album_id: str = ""
    mb_artist_id: str = ""
    discogs_id: str = ""
    narrator: str = ""
    toc: dict = field(default_factory=dict)
    toc_exact: bool = False  # from a log / CUE / CUESHEET (not guessed from file lengths)
    media_hint: str = ""
    hungarian_hint: bool = False  # nCore *_hun category: Hungarian editions score a little higher
    various: bool = False
    audiobook: bool = False
    cover: bytes = b""
    cover_source: str = ""
    chapters: list[dict] = field(default_factory=list)
    log: dict = field(default_factory=dict)

    @property
    def discs(self) -> int:
        return len({t.disc for t in self.tracks}) or 1

    @property
    def lengths(self) -> list[float]:
        return [t.length for t in self.tracks]


def parse_name(name: str) -> tuple[str, str, str]:
    """(artist, album, year) of a folder name: 'Artist - Album (1993) [FLAC]' or scene 'Artist-Album-(CAT1)-WEB-2026-GRP'."""

    name = re.sub(r"\s*[(\[](?:flac|mp3|320|v0|web|cd|vinyl|24.?bit[^)\]]*|16.?bit[^)\]]*|lossless|hi.?res)[)\]]", "", name, flags=re.I)
    name = re.sub(r"-\([A-Z0-9][A-Z0-9 .-]{1,20}\)-", "-", name)  # scene catalogue number
    artist, album, year = old_audio.parse_folder(name)
    album = re.sub(r"\s*[(\[]\s*[)\]]", "", EDITION_RE.sub("", album)).strip(" -")
    if " - " not in name:  # scene words that follow the title
        album = re.sub(r"(?i)\b(remastered|remaster|reissue|deluxe(?: edition)?|limited(?: edition)?|expanded(?: edition)?|bonus tracks?|"
                       r"proper|repack|retail|promo|advance|ltd)\b", "", album.replace("_", " "))
    return artist.replace("_", " ").strip(), re.sub(r"\s{2,}", " ", album.replace("_", " ")).strip(" -"), year


def media_hint(name: str) -> str:
    tokens = {t.upper() for t in re.split(r"[ ._\-()\[\]]+", name or "") if t}
    for token, media in SOURCE_TOKENS.items():
        if token in tokens:
            return media
    return ""


def _frames(item: audiotags.AudioTags) -> int:
    if item.total_samples and item.sample_rate == 44100:
        return item.total_samples // cuelog.SAMPLES_PER_FRAME
    return int(round(item.duration * cuelog.FRAMES_PER_SECOND))


def _nfo_ids(release: rel.Release) -> dict:
    found = {}
    for key, regex in (("mbid", NFO_MBID), ("discogs", NFO_DISCOGS), ("barcode", NFO_BARCODE), ("catno", NFO_CATNO)):
        match = nfo.unique_match(release, regex)
        if match:
            found[key] = match.replace(" ", "") if key == "barcode" else match
    if found.get("catno", "").lower() in {"none", "n/a", "na", "-", "--", "unknown", "nincs", "tba", "tbc"}:
        found.pop("catno")  # 'Cat.No...: N/A' is no catalogue number
    return found


def _is_various(name: str) -> bool:
    return fold(name) in {fold(v) for v in VARIOUS}


def strip_artist_prefix(title: str, artist: str) -> str:
    """'omega-petróleum lámpa' -> 'petróleum lámpa' when the words before the title are the album artist (scene file names)."""

    wanted = fold(artist).split()
    tokens = list(re.finditer(r"[^\W_]+", title or ""))
    if not wanted or len(tokens) <= len(wanted) or [fold(m.group(0)) for m in tokens[:len(wanted)]] != wanted:
        return title
    rest = title[tokens[len(wanted) - 1].end():].lstrip(" ._-")
    return rest or title


def read_local(release: rel.Release, hints: dict) -> Local:
    """Everything the files themselves tell: tags, tracks, TOC, ids, cover."""

    local = Local()
    paths = audiotags.audio_files(release.path, release.files)
    local.files = [audiotags.read(p, cover=(i == 0)) for i, p in enumerate(paths)]
    root = release.path if release.path.is_dir() else release.path.parent
    folder_artist, folder_album, folder_year = parse_name(release.name)
    local.media_hint = media_hint(release.name)

    def common(values: list[str]) -> str:
        values = [v for v in values if v]
        return Counter(values).most_common(1)[0][0] if values else ""

    files = local.files
    albumartist = common([f.albumartist for f in files])
    artists = [f.artist for f in files if f.artist]
    # 'Drake feat. X' / 'Drake ft. Y' on every track is still one artist's album
    primaries = [FEATURING.split(a)[0].strip() or a for a in artists]
    top_share = Counter(fold(p) for p in primaries).most_common(1)[0][1] / len(primaries) if primaries else 0.0
    local.various = _is_various(albumartist) or (not albumartist and len({fold(p) for p in primaries}) > 3 and top_share < 0.5)
    local.artist = "Various Artists" if local.various and not albumartist else albumartist or common(primaries)
    local.album = common([f.album for f in files])
    local.date = common([f.date for f in files])
    local.year = common([f.year for f in files])
    local.genres = list(dict.fromkeys(g for f in files for g in f.genres))
    local.label, local.catno, local.barcode = common([f.label for f in files]), common([f.catno for f in files]), common([f.barcode for f in files])
    local.mb_album_id, local.mb_artist_id = common([f.mb_album_id for f in files]), common([f.mb_artist_id for f in files])
    local.discogs_id = common([f.discogs_release_id for f in files])
    local.narrator = common([f.narrator for f in files])
    nfo_ids = _nfo_ids(release)
    local.mb_album_id = local.mb_album_id or nfo_ids.get("mbid", "")
    local.discogs_id = local.discogs_id or nfo_ids.get("discogs", "")
    local.barcode = local.barcode or nfo_ids.get("barcode", "")
    local.catno = local.catno or nfo_ids.get("catno", "")
    for key in ("artist", "album", "year"):
        if hints.get(key):
            setattr(local, key, str(hints[key]).strip())
    if hints.get("artist"):
        local.various = _is_various(local.artist)  # the user's artist overrides the Various Artists guess (search, creators)

    # CUE sheets / logs (lossless rips)
    cues = [root / f.relpath for f in release.files if f.ext == ".cue"] if release.path.is_dir() else []
    logs = [root / f.relpath for f in release.files if f.ext == ".log"] if release.path.is_dir() else []
    cue = cuelog.read_cue(cues[0]) if len(cues) == 1 else {}
    if not cue and len(files) == 1 and files[0].cuesheet:
        cue = cuelog.parse_cue(files[0].cuesheet)
    for log_path in logs:
        log = cuelog.read_log(log_path)
        if log.get("toc"):
            local.toc, local.toc_exact, local.log = log["toc"], True, log
            break
    file_frames = {}
    for name in (cue.get("files") or []):
        matched = cuelog.match_file(name, [f.path for f in files])
        item = next((f for f in files if f.path == matched), None)
        if item is not None and (item.duration or item.total_samples):
            file_frames[name] = _frames(item)
    if cue and not local.toc and len(file_frames) == len(cue.get("files") or []):
        local.toc = cuelog.toc_from_cue(cue, file_frames)
        local.toc_exact = bool(local.toc)
    if not local.toc and len(files) == 1 and files[0].flac_toc:
        local.toc, local.toc_exact = cuelog.toc_from_flac_cuesheet(files[0].flac_toc), True
    if cue:
        local.artist = local.artist or cue.get("performer", "")
        local.album = local.album or cue.get("title", "")
        local.year = local.year or (re.search(r"\d{4}", cue.get("date", "")) or [""])[0]
        local.barcode = local.barcode or cue.get("catalog", "")
        if cue.get("genre") and cue["genre"] not in local.genres:
            local.genres.append(cue["genre"])
    if local.log:
        local.artist = local.artist or local.log.get("artist", "")
        local.album = local.album or local.log.get("album", "")
    local.artist = local.artist or nfo.value(release, "artist", "album artist", "performer", "előadó", "author", "szerző")
    local.album = local.album or nfo.value(release, "album", "album title", "title", "book title", "cím")
    local.year = local.year or nfo.year(release)
    local.label = local.label or nfo.value(release, "label", "publisher", "kiadó")
    local.narrator = local.narrator or nfo.value(release, "narrator", "read by", "felolvasó")
    genre = nfo.value(release, "genre", "genres", "műfaj")
    if not local.genres and genre:
        local.genres = [g.strip() for g in re.split(r"[,;/]", genre) if g.strip()]
    local.various = local.various or _is_various(local.artist)
    local.artist, local.album, local.year = local.artist or folder_artist, local.album or folder_album, local.year or folder_year

    # tracks: a single-file image with a CUE gets its tracks from the CUE, otherwise one track per file
    audio_tracks = [t for t in cue.get("tracks", []) if t["type"] == "AUDIO"] if cue else []
    if cue and len(audio_tracks) > len(files) and file_frames:
        lengths = cuelog.cue_lengths(cue, file_frames)
        for track in audio_tracks:
            performer = track["performer"] if track["performer"] and fold(track["performer"]) != fold(local.artist) else ""
            local.tracks.append(LocalTrack(1, track["number"], track["title"], lengths.get(track["number"], 0.0), performer, bool(track["title"])))
    elif len(files) == 1 and len(files[0].flac_toc) > 2 and files[0].sample_rate:  # FLAC image with only a CUESHEET block
        starts = files[0].flac_toc
        for number, (start, end) in enumerate(zip(starts, starts[1:]), 1):
            local.tracks.append(LocalTrack(1, number, "", max(0, end - start) / files[0].sample_rate, "", False))
    else:
        all_numbered = all(f.track for f in files)
        for index, item in enumerate(files, 1):
            disc = item.disc or audiotags.disc_from_path(item.path) or 1
            parsed = audiotags.parse_filename(item.path.name)
            title, from_tag = item.title, bool(item.title)
            if not title:
                title = strip_artist_prefix(parsed["title"], local.artist) if not parsed["artist"] else parsed["title"]
                if parsed["artist"] and not local.various and fold(parsed["artist"]) != fold(local.artist):
                    title = f"{parsed['artist']} - {title}"
            track_artist = item.artist or (parsed["artist"] if local.various else "")
            if track_artist and fold(track_artist) == fold(local.artist):
                track_artist = ""
            number = item.track if all_numbered else (parsed["number"] or index)
            local.tracks.append(LocalTrack(disc, number, title, item.duration, track_artist, from_tag, item.path))
        if all_numbered:
            local.tracks.sort(key=lambda t: (t.disc, t.number))
    if not local.toc and len(files) == len(local.tracks) and files and all(f.lossless and f.sample_rate == 44100 for f in files) \
            and len(files) <= 99 and sum(f.duration for f in files) <= 80 * 60 and local.discs == 1:
        local.toc = cuelog.toc_from_lengths([f.total_samples or int(f.duration * 44100) for f in files])

    titles = nfo.track_titles(release)
    if local.discs == 1 and len(titles) == len(local.tracks) and [t.number for t in local.tracks] == list(range(1, len(titles) + 1)):
        for track, title in zip(local.tracks, titles):
            if not track.title or PLACEHOLDER_TITLE.fullmatch(track.title.strip()):
                track.title = title

    # audiobook?
    names = " ".join([release.name, local.album] + [f.path.name for f in files[:3]])
    local.audiobook = (hints.get("kind") == "audiobook" or any(f.path.suffix.lower() == ".m4b" for f in files)
                       or any(style_key(g) in {style_key(x) for x in AUDIOBOOK_GENRES} for g in local.genres) or bool(AUDIOBOOK_NAME.search(names)))
    if local.audiobook and len(files) == 1 and files[0].path.suffix.lower() in {".m4b", ".m4a", ".mp4", ".mka", ".mp3"}:
        local.chapters = chaps.chapters(files[0].path)

    # cover: the release's own image, else the embedded one
    cover_file = audiotags.find_cover_file(root, release.files) if release.path.is_dir() else None
    if cover_file is not None:
        try:
            from ..safeio import read_file, IMAGE_LIMIT
            data = read_file(cover_file, IMAGE_LIMIT)
        except (OSError, ValueError):
            data = b""
        if coverart.is_image(data):  # a broken or mislabelled file must not become the infobar cover
            local.cover, local.cover_source = data, cover_file.name
    if not local.cover and files and coverart.is_image(files[0].cover):
        local.cover, local.cover_source = files[0].cover, "beágyazott borító"
    return local


# --- matching ---------------------------------------------------------------------------------------------------

@dataclass
class Candidate:
    source: str  # "musicbrainz" / "discogs" / "spotify"
    data: dict
    score: float = 0.0
    method: str = ""


def candidate_tracks(candidate: Candidate) -> list[tuple[str, float]]:
    if candidate.source == "musicbrainz":
        return [(t["title"], t["length"]) for m in candidate.data.get("media", []) for t in m["tracks"]]
    if candidate.source == "discogs":
        return [(t["title"], t["duration"]) for t in candidate.data.get("tracks", [])]
    return [(t["title"], t["length"]) for t in candidate.data.get("tracks", [])]


def candidate_info(candidate: Candidate) -> dict:
    data = candidate.data
    if candidate.source == "musicbrainz":
        return {"title": data.get("title", ""), "artist": data.get("artist", ""), "year": data.get("year", ""), "formats": data.get("formats", []),
                "country": data.get("country", ""), "unofficial": data.get("status", "") in {"Bootleg", "Pseudo-Release"}}
    if candidate.source == "discogs":
        return {"title": data.get("title", ""), "artist": data.get("artist", ""), "year": data.get("year", ""),
                "formats": ["Digital Media" if f == "File" else "Vinyl" if f == "Vinyl" else f for f in data.get("format_names", [])],
                "country": {"Hungary": "HU"}.get(data.get("country", ""), data.get("country", "")), "unofficial": data.get("unofficial", False)}
    return {"title": data.get("name", ""), "artist": data.get("artist", ""), "year": data.get("release_date", "")[:4], "formats": ["Digital Media"],
            "country": "", "unofficial": False}


def duration_agreement(local_lengths: list[float], remote: list[float]) -> float:
    """0..1: share of tracks within max(3 s, 2 %) plus whether the totals agree within 2 % (0.5 when unknown)."""

    if not local_lengths or not remote or not all(local_lengths) or sum(1 for x in remote if x) < len(remote) * 0.8:
        return 0.5
    total_local, total_remote = sum(local_lengths), sum(remote)
    total_ok = 1.0 if abs(total_local - total_remote) <= 0.02 * max(total_local, 1) else 0.0
    if len(local_lengths) != len(remote):
        return 0.5 * total_ok
    close = sum(1 for a, b in zip(local_lengths, remote) if b and abs(a - b) <= max(3.0, 0.02 * a))
    return 0.5 * close / len(local_lengths) + 0.5 * total_ok


def score_candidate(local: Local, candidate: Candidate) -> float:
    info = candidate_info(candidate)
    tracks = candidate_tracks(candidate)
    title_score = similarity(EDITION_RE.sub("", info["title"]), EDITION_RE.sub("", local.album)) if local.album else 0.5
    if local.various and _is_various(info["artist"]):
        artist_score = 1.0
    else:
        artist_score = similarity(info["artist"], local.artist) if local.artist else 0.5
    count = len(local.tracks)
    count_score = 1.0 if len(tracks) == count else 0.4 if abs(len(tracks) - count) == 1 else 0.0
    duration_score = duration_agreement(local.lengths, [length for _t, length in tracks])
    year_score = 0.5
    if local.year[:4].isdigit() and str(info["year"])[:4].isdigit():
        gap = abs(int(local.year[:4]) - int(str(info["year"])[:4]))
        year_score = 1.0 if gap == 0 else 0.5 if gap == 1 else 0.0
    format_score = 0.5
    if local.media_hint and info["formats"]:
        format_score = 1.0 if any(local.media_hint.lower() in f.lower() for f in info["formats"]) else 0.0
    score = 0.25 * title_score + 0.2 * artist_score + 0.2 * count_score + 0.25 * duration_score + 0.05 * year_score + 0.05 * format_score
    if info["country"] == "HU" and local.hungarian_hint:
        score += 0.03
    if info["unofficial"]:
        score -= 0.1
    return round(max(0.0, min(1.0, score)), 3)


def _prerank_mb(local: Local, hit: dict) -> float:
    count = len(local.tracks)
    value = 0.4 * similarity(hit["title"], local.album) + 0.3 * similarity(hit["artist"], local.artist) if local.album else 0.0
    value += 0.2 * (1.0 if hit["track_count"] == count else 0.0) + 0.1 * hit["score"] / 100
    if local.year and hit["year"] == local.year:
        value += 0.05
    if local.media_hint and any(local.media_hint.lower() in f.lower() for f in hit["formats"]):
        value += 0.03
    if hit["status"] in {"Bootleg", "Pseudo-Release"}:
        value -= 0.1
    return value


def _prerank_discogs(local: Local, hit: dict) -> float:
    value = 0.45 * similarity(hit["album"], local.album) + 0.35 * similarity(hit["artist"], local.artist)
    if local.year and hit["year"] == local.year:
        value += 0.1
    if "Unofficial Release" in hit["formats"]:
        value -= 0.2
    if local.media_hint == "CD" and "CD" in hit["formats"] or local.media_hint == "Vinyl" and "Vinyl" in hit["formats"]:
        value += 0.05
    return value


def best_of(local: Local, candidates: list[Candidate]) -> Candidate | None:
    for candidate in candidates:
        candidate.score = score_candidate(local, candidate)
    candidates = [c for c in candidates if c.data]
    return max(candidates, key=lambda c: c.score) if candidates else None


def _mb_candidates(local: Local, hits: list[dict], method: str, top: int = 3) -> list[Candidate]:
    seen, result = set(), []
    for hit in sorted(hits, key=lambda h: -_prerank_mb(local, h)):
        if hit["id"] in seen:
            continue
        seen.add(hit["id"])
        data = mb.release(hit["id"])
        if data:
            result.append(Candidate("musicbrainz", data, method=method))
        if len(result) >= top:
            break
    return result


def _discogs_candidates(local: Local, hits: list[dict], method: str, top: int = 2) -> list[Candidate]:
    result = []
    for hit in sorted((h for h in hits if h["type"] == "release"), key=lambda h: -_prerank_discogs(local, h))[:top]:
        data = discogs.release(hit["id"])
        if data:
            result.append(Candidate("discogs", data, method=method))
    return result


def identify(local: Local, meta: Meta, hints: dict) -> Candidate | None:
    """The best release for the local files (see the module docstring for the chain)."""

    count = len(local.tracks)
    # 1. explicit ids (a hint may be the id or its URL)
    hint_mbid = (audiotags.MBID_RE.search(str(hints.get("mbid") or "").lower()) or [""])[0]
    hint_discogs = str(hints.get("discogs") or "").strip()
    hint_discogs = discogs.release_id_from_url(hint_discogs) or (hint_discogs if hint_discogs.isdigit() else "")
    if hints.get("spotify") and not local.audiobook:
        data = spotify.album(str(hints["spotify"]))
        if data and data.get("tracks_complete"):
            candidate = Candidate("spotify", data, method="Spotify-albumazonosító (megadva)")
            candidate.score = score_candidate(local, candidate)
            if candidate.score >= 0.75 and len(data.get("tracks", [])) == count:
                return candidate
            meta.warnings.append("a megadott Spotify-album nem egyezik eléggé a helyi számokkal; további források ellenőrzése szükséges")
        elif not spotify.enabled():
            meta.warnings.append("Spotify-album megadva, de hiányzik a fejlesztői Client ID / Client Secret")
    if hint_mbid or local.mb_album_id:
        data = mb.release(hint_mbid or local.mb_album_id)
        if data:
            candidate = Candidate("musicbrainz", data, method="MusicBrainz-azonosító" + (" (megadva)" if hint_mbid else " a tagekben"))
            candidate.score = max(score_candidate(local, candidate), 0.97 if data["track_count"] == count else 0.8)
            if data["track_count"] != count:
                meta.warnings.append(f"a megadott MusicBrainz-kiadás {data['track_count']} számos, a mappában {count} szám van")
            return candidate
    if hint_discogs or local.discogs_id:
        data = discogs.release(hint_discogs or local.discogs_id)
        if data:
            candidate = Candidate("discogs", data, method="Discogs-azonosító")
            candidate.score = max(score_candidate(local, candidate), 0.95 if len(data["tracks"]) == count else 0.8)
            return candidate
    # 2. disc id / TOC
    if local.toc and not local.audiobook:
        releases = mb.discid_lookup(local.toc["discid"] if local.toc_exact else "", local.toc["toc"])
        exact = [r for r in releases if any(local.toc["discid"] in m["discids"] for m in r["media"])]
        pool = [Candidate("musicbrainz", r, method="disc ID" if exact else "TOC (közelítő)") for r in (exact or releases)[:10]]
        best = best_of(local, pool)
        if best and exact and local.toc_exact:
            best.score = max(best.score, 0.93)
        if best and best.score >= CONFIDENT:
            return _full_mb(best)
    # 3. barcode / catalogue number
    if local.barcode and not local.audiobook:
        hits = mb.by_barcode(local.barcode)
        best = best_of(local, _mb_candidates(local, hits, "vonalkód", top=2))
        if best and best.score >= 0.6:
            best.score = max(best.score, 0.9 if len(candidate_tracks(best)) == count else best.score)
            return best
        best = best_of(local, _discogs_candidates(local, discogs.search(barcode=local.barcode), "vonalkód (Discogs)"))
        if best and best.score >= 0.6:
            best.score = max(best.score, 0.88 if len(candidate_tracks(best)) == count else best.score)
            return best
    if local.catno and not local.audiobook:
        hits = [h for h in mb.by_catno(local.catno, local.label) if similarity(h["artist"], local.artist) >= 0.6 or not local.artist]
        best = best_of(local, _mb_candidates(local, hits, "katalógusszám", top=2))
        if best and best.score >= CONFIDENT:
            return best
        best = best_of(local, _discogs_candidates(local, discogs.search(catno=local.catno, artist=local.artist), "katalógusszám (Discogs)"))
        if best and best.score >= CONFIDENT:
            return best
    # 4. AcoustID
    if not local.audiobook and acoustid_ready(local):
        best = _acoustid(local, meta)
        if best:
            return best
    # 5. loose search
    candidates: list[Candidate] = []
    if local.album:
        album = EDITION_RE.sub("", local.album)
        artist = "" if local.various else local.artist
        shape = {} if local.audiobook or not count else {"mediums": local.discs} if local.discs > 1 else {"tracks": count}
        hits = mb.search_releases(artist, album, limit=40, **shape)
        if not hits and shape:
            hits = mb.search_releases(artist, album, limit=40)
        candidates += _mb_candidates(local, hits, "keresés (MusicBrainz)")
        best = best_of(local, candidates)
        if not (best and best.score >= 0.85):
            candidates += _discogs_candidates(local, discogs.search(artist=artist, release_title=album), "keresés (Discogs)")
    best = best_of(local, candidates)
    if best and best.score >= 0.5:
        return best
    # 6. Spotify, the last resort
    if spotify.enabled() and local.album and not local.audiobook:
        found = spotify.search_albums("" if local.various else local.artist, EDITION_RE.sub("", local.album))
        pool = [Candidate("spotify", spotify.album(hit["id"]), method="keresés (Spotify)") for hit in found[:2]]
        spot = best_of(local, [c for c in pool if c.data and c.data.get("tracks_complete", True)])
        if spot and spot.score >= 0.6:
            return spot
    return best


def _full_mb(candidate: Candidate) -> Candidate:
    """Disc id answers lack url-rels/genres: replace them with the full release lookup."""

    if candidate.source == "musicbrainz" and not candidate.data.get("urls") and not candidate.data.get("release_group", {}).get("genres"):
        full = mb.release(candidate.data["id"])
        if full:
            candidate.data = full
    return candidate


def acoustid_ready(local: Local) -> bool:
    return bool(keys.get("ACOUSTID_KEY")) and shutil.which("fpcalc") is not None and len(local.tracks) >= 1 and \
        all(t.path is not None for t in local.tracks)


def _acoustid(local: Local, meta: Meta) -> Candidate | None:
    tracks = local.tracks
    picks = sorted({0, len(tracks) // 2, len(tracks) - 1})
    per_track = []
    for index in picks:
        fp = fingerprint.fingerprint(tracks[index].path)
        if fp:
            per_track.append(acoustid.lookup(fp["fingerprint"], fp["duration"]))
    if not per_track:
        return None
    meta.add_source("AcoustID")
    ranked = [rid for rid, hits in acoustid.common_releases(per_track) if hits >= max(1, len(per_track) - 1)]
    pool = []
    for rid in ranked[:4]:
        data = mb.release(rid)
        if data:
            pool.append(Candidate("musicbrainz", data, method="AcoustID"))
    best = best_of(local, pool)
    if best and len(candidate_tracks(best)) == len(tracks):
        best.score = max(best.score, 0.85)
        return best
    return best if best and best.score >= CONFIDENT else None


# --- filling the Meta --------------------------------------------------------------------------------------------

def _apply_tracks(meta: Meta, local: Local, candidate: Candidate | None, confident: bool) -> None:
    remote = candidate_tracks(candidate) if candidate else []
    use_remote = confident and len(remote) == len(local.tracks)
    mismatches = 0
    multi = local.discs > 1
    per_disc: Counter = Counter()
    for index, track in enumerate(local.tracks):
        title = track.title
        if use_remote:
            remote_title = remote[index][0]
            if not title or not track.from_tag or PLACEHOLDER_TITLE.match(title.strip()):
                title = remote_title if not title or similarity(title, remote_title) >= 0.3 or PLACEHOLDER_TITLE.match(title.strip()) else title
            elif similarity(title, remote_title) >= 0.6:
                title = remote_title
            else:
                mismatches += 1
        per_disc[track.disc] += 1
        number = track.number or per_disc[track.disc]
        position = f"{track.disc}-{number:02d}" if multi else f"{number:02d}"
        meta.tracks.append(Track(position=position, title=title or f"{number:02d}", length=length_text(track.length), artist=track.artist))
    if use_remote and mismatches > len(local.tracks) // 3:
        meta.warnings.append(f"{mismatches} szám címe eltér az adatbázistól — a fájlok tagjeit hagytam meg, ellenőrizd a kiadást")


def _catno_key(value: str) -> str:
    return re.sub(r"[^0-9a-z]", "", (value or "").casefold())


def _edition_mismatch(local: Local, candidate: Candidate) -> list[str]:
    """Label / catalogue number of the files that contradict the matched release (another edition of the album)."""

    data = candidate.data if candidate else {}
    found = []
    remote_catno = str(data.get("catno") or "")
    if local.catno and remote_catno and _catno_key(local.catno) and _catno_key(remote_catno) and _catno_key(local.catno) != _catno_key(remote_catno):
        found.append(f"katalógusszám: fájl {local.catno} / adatbázis {remote_catno}")
    remote_label = str(data.get("label") or "")
    if local.label and remote_label and similarity(local.label, remote_label) < 0.6:
        found.append(f"kiadó: fájl {local.label} / adatbázis {remote_label}")
    return found


def _label_and_ids(meta: Meta, local: Local, candidate: Candidate | None, discogs_data: dict) -> None:
    data = candidate.data if candidate else {}
    if candidate and candidate.source == "spotify":
        meta.identifiers["spotify"] = data["id"]
        meta.extra["spotify_url"] = data.get("url", "")
    if candidate and candidate.source == "musicbrainz":
        meta.publisher = data.get("label") or local.label
        meta.extra["catno"] = data.get("catno") or local.catno
        meta.identifiers["mbid"] = data["id"]
        if data.get("release_group", {}).get("id"):
            meta.identifiers["mb_release_group"] = data["release_group"]["id"]
        if data.get("artist_ids"):
            meta.identifiers["mb_artist"] = data["artist_ids"][0]
        if data.get("barcode"):
            meta.identifiers["barcode"] = data["barcode"]
        media = list(dict.fromkeys(m["format"] for m in data.get("media", []) if m["format"]))
        if media:
            meta.extra["media"] = ", ".join(media)
        group = data.get("release_group") or {}
        kinds = [group.get("primary_type", "")] + list(group.get("secondary_types") or [])
        meta.extra["release_type"] = " + ".join(k for k in kinds if k)
    elif candidate and candidate.source == "discogs":
        meta.publisher = data.get("label") or local.label
        meta.extra["catno"] = data.get("catno") or local.catno
        if data.get("barcodes"):
            meta.identifiers["barcode"] = data["barcodes"][0]
        if data.get("format_names"):
            meta.extra["media"] = ", ".join(dict.fromkeys(data["format_names"]))
    else:
        meta.publisher = local.label
        meta.extra["catno"] = local.catno
    if discogs_data:
        meta.identifiers["discogs"] = str(discogs_data["id"])
        meta.publisher = meta.publisher or discogs_data.get("label", "")
        meta.extra["catno"] = meta.extra.get("catno") or discogs_data.get("catno", "")
    if local.barcode and "barcode" not in meta.identifiers:
        meta.identifiers["barcode"] = local.barcode
    if local.toc.get("discid") and local.toc_exact:
        meta.identifiers["discid"] = local.toc["discid"]
    if not meta.extra.get("catno"):
        meta.extra.pop("catno", None)


def _proof_links(meta: Meta, candidate: Candidate | None, discogs_data: dict) -> None:
    if candidate and candidate.source == "musicbrainz":
        meta.add_proof(candidate.data["url"])
    if discogs_data:
        meta.add_proof(discogs_data["url"])
    if candidate and candidate.source == "musicbrainz":
        for item in candidate.data.get("urls", []):
            if item["type"] in URL_PROOF_TYPES and not any(h in item["url"] for h in NO_PROOF_HOSTS) and len(meta.proof_links) < 3:
                meta.add_proof(item["url"])
    if candidate and candidate.source == "spotify" and not meta.proof_links and candidate.data.get("url"):
        meta.add_proof(candidate.data["url"])


def _cover(meta: Meta, local: Local, candidate: Candidate | None, discogs_data: dict) -> None:
    if local.cover:
        meta.cover = local.cover
        return
    data = candidate.data if candidate else {}
    if candidate and candidate.source == "musicbrainz":
        image = coverart.front(data["id"]) if data.get("cover_front", True) else b""
        url = coverart.front_url(data["id"])
        if not image and data.get("release_group", {}).get("id"):
            image, url = coverart.front(data["release_group"]["id"], group=True), coverart.front_url(data["release_group"]["id"], group=True)
        if image:
            meta.cover, meta.cover_url = image, url
            meta.add_source("Cover Art Archive")
            return
    images = (discogs_data or {}).get("images") or (data.get("images") if candidate and candidate.source == "discogs" else []) or []
    if images:
        image = net.get_bytes(images[0], quiet=True)
        if coverart.is_image(image):
            meta.cover, meta.cover_url = image, images[0]
            return
    if candidate and candidate.source == "spotify" and data.get("cover_url"):
        image = net.get_bytes(data["cover_url"], quiet=True)
        if coverart.is_image(image):
            meta.cover, meta.cover_url = image, data["cover_url"]


def _wiki_text(qid: str = "", prop: str = "", value: str = "") -> tuple[str, str]:
    """(Hungarian summary, English summary) of the Wikipedia articles of a Wikidata item (by QID or by property)."""

    info = wikidata.parse_entity(wikidata.entity(qid)) if qid else (wikidata.lookup(prop, value) if value else {})
    if not info:
        return "", ""
    hu = wikipedia.summary(info["huwiki"], "hu").get("extract", "") if info.get("huwiki") else ""
    en = "" if hu or not info.get("enwiki") else wikipedia.summary(info["enwiki"], "en").get("extract", "")
    return hu, en


def template_sentence(meta: Meta, *, artist_type: str = "", release_type: str = "", audiobook: bool = False, video: bool = False) -> str:
    """A factual one-line Hungarian description from the known fields only."""

    title = meta.title
    who = ", ".join(meta.creators)
    if audiobook:
        if who and title:
            return f"{who} „{title}” című művének hangoskönyve."
        return f"„{title}” – hangoskönyv." if title else ""
    if not who or not title or _is_various(who):
        return f"{title} – válogatásalbum." if title and _is_various(who) and not video else ""
    types = set(release_type.split(" + "))
    word = ("koncertalbuma" if "Live" in types else "válogatásalbuma" if "Compilation" in types else "filmzenealbuma" if "Soundtrack" in types
            else "remixalbuma" if "Remix" in types else "kislemeze" if "Single" in types else "középlemeze (EP)" if "EP" in types else "albuma")
    if video:
        word = "koncertfelvétele"
    subject = f"{article(who)} {who}" if artist_type in {"Group", "Orchestra", "Choir"} else who
    when = f" {meta.year}-{year_suffix(meta.year)} megjelent" if meta.year else ""
    return f"{title}: {subject}{when} {word}."


def describe_hu(meta: Meta, *, group: dict, artist_id: str, artist_type: str, artist_qid: str = "", audiobook: bool = False,
                english_fallback: str = "", video: bool = False) -> None:
    """description_hu by the policy: huwiki article of the release group, else of the artist (after the template
    sentence), else TheAudioDB's Hungarian biography, else a machine translation of the English article, else the template."""

    sentence = template_sentence(meta, artist_type=artist_type, release_type=meta.extra.get("release_type", ""), audiobook=audiobook, video=video)
    hu, en = "", ""
    if group.get("id"):
        hu, en = _wiki_text(qid=group.get("wikidata", ""), prop="mb_release_group", value=group["id"])
        if hu or en:
            meta.add_source("Wikidata")
    if hu:
        meta.description_hu, meta.description_source = cut_text(hu), wikipedia.CREDIT
        meta.add_source("Wikipédia")
        return
    artist_hu, _artist_en = ("", "")
    if artist_id or artist_qid:
        artist_hu, _artist_en = _wiki_text(qid=artist_qid, prop="mb_artist", value=artist_id)
    if artist_hu:
        meta.description_hu = cut_text((sentence + "\n\n" if sentence else "") + artist_hu)
        meta.description_source = wikipedia.CREDIT
        meta.add_source("Wikidata")
        meta.add_source("Wikipédia")
        return
    if artist_id and not audiobook:
        bio = theaudiodb.artist(artist_id).get("biography_hu", "")
        if bio and translate.is_hungarian(bio):
            meta.description_hu = cut_text((sentence + "\n\n" if sentence else "") + bio)
            meta.description_source = "TheAudioDB"
            meta.add_source("TheAudioDB")
            return
    english = en or english_fallback
    if english:
        meta.description_en = cut_text(english)
        credit = wikipedia.CREDIT if en else "Last.fm (CC BY-SA)"
        text, translated = translate.to_hungarian(meta.description_en)
        if translated:
            meta.description_hu, meta.machine_translated = text, True
            meta.description_source = credit
            return
        if not sentence:  # the English text itself will be shown: it still needs its CC BY-SA credit
            meta.description_source = credit
    meta.description_hu = sentence


def _audiobook_fill(meta: Meta, local: Local, hints: dict, release: rel.Release) -> None:
    folder_author, folder_title, folder_year = parse_name(release.name)
    first = local.files[0] if local.files else None
    author = str(hints.get("author") or local.artist or folder_author).strip()
    title = str(hints.get("title") or hints.get("album") or local.album or folder_title).strip()
    if first and not first.album and len(local.files) == 1 and first.title:
        title = str(hints.get("title") or first.title)
    meta.creators = [author] if author else []
    meta.title = title
    meta.year = str(hints.get("year") or local.year or folder_year)
    narrator = str(hints.get("narrator") or local.narrator or "").strip()
    if not narrator and first and first.composer and fold(first.composer) != fold(author):
        narrator = first.composer
    if narrator:
        meta.extra["narrator"] = narrator
    else:
        meta.warnings.append("hangoskönyv: nem találtam a felolvasó nevét — írd be a leírásba (Előadó: …)")
    meta.publisher = local.label
    meta.tags = ["hangoskönyv"]
    named = chaps.named(local.chapters)
    if named:
        for chapter in named:
            meta.tracks.append(Track(position=f"{chapter['index']:02d}", title=chapter["title"] or f"{chapter['index']}. fejezet",
                                     length=length_text(chapter["end"] - chapter["start"])))
        meta.extra["chapters"] = str(len(local.chapters))
    elif local.chapters:
        meta.extra["chapters"] = str(len(local.chapters))
    elif len(local.tracks) > 1:
        _apply_tracks(meta, local, None, False)


def resolve(release: rel.Release, code: str, *, original: bool, hints: dict) -> Meta:
    hints = dict(hints or {})
    code = code or ""
    meta = Meta(kind="album")
    nfo.annotate(meta, release)
    local = read_local(release, hints)
    local.hungarian_hint = (code or "").endswith("_hun")
    if not local.files:
        meta.warnings.append("nem találtam hangfájlt a release-ben")
        return meta
    meta.add_source("fájltagek" if any(f.tagged for f in local.files) else "fájlnevek")
    for reader in ("ffprobe", "MediaInfo"):
        if any(reader in f.sources for f in local.files):
            meta.add_source(reader)
    unreadable = [f.path.name for f in local.files if not f.codec or not f.duration]
    if unreadable:
        meta.warnings.append(f"nem olvasható hangfájl ({len(unreadable)} db, sérült vagy üres?): " + ", ".join(unreadable[:5]))
        meta.extra.setdefault("review_reasons", []).append("minden hangfájl kodekjének és hosszának olvashatónak kell lennie")
    meta.extra["format"] = audiotags.format_label(local.files)
    lowest = audiotags.lowest_bitrate_kbps(local.files)
    if lowest:
        meta.extra["bitrate_kbps"] = str(lowest)
    total = sum(f.duration for f in local.files)
    if total:
        meta.extra["duration"] = length_text(total)
    _category_checks(meta, local, code, lowest)
    if not original and not local.audiobook:
        albums = {fold(f.album) for f in local.files if f.album}
        if len(albums) > 1:
            meta.extra.setdefault("review_reasons", []).append("eltérő albumtagek: hivatalos egységes kiadás ellenőrzése szükséges")
        declared_discs = max((f.disc_total for f in local.files), default=0)
        if declared_discs > local.discs:
            meta.extra.setdefault("review_reasons", []).append("a kiadás nem minden lemeze található meg a mappában")
        for disc in {t.disc for t in local.tracks}:
            actual = [t for t in local.tracks if t.disc == disc]
            declared = max((f.track_total for f in local.files if (f.disc or 1) == (disc or 1)), default=0)
            if declared and declared != len(actual):
                meta.extra.setdefault("review_reasons", []).append(f"a(z) {disc or 1}. lemez hiányos vagy eltérő: {declared} jelzett szám, {len(actual)} helyi szám")

    if local.audiobook:
        meta.kind = "audiobook"
        _audiobook_fill(meta, local, hints, release)
        local.album, local.artist = meta.title, (meta.creators[0] if meta.creators else "")
        candidate = identify(local, meta, hints) if meta.title else None
        confident = bool(candidate and candidate.score >= CONFIDENT and candidate.source == "musicbrainz")
        group = candidate.data.get("release_group", {}) if confident else {}
        if confident:
            meta.add_source("MusicBrainz")
            meta.add_proof(candidate.data["url"])
            meta.identifiers["mbid"] = candidate.data["id"]
            meta.year = meta.year or candidate.data.get("year", "")
            meta.publisher = meta.publisher or candidate.data.get("label", "")
        meta.confidence = round(candidate.score, 2) if confident else 0.4 if meta.title and meta.creators else 0.2
        if local.cover:
            meta.cover = local.cover
        elif confident:
            _cover(meta, local, candidate, {})
        describe_hu(meta, group=group, artist_id="", artist_type="", audiobook=True)
        if not meta.proof_links:
            meta.warnings.append("hangoskönyv: nincs igazoló link (kiadói oldal, MusicBrainz) — add meg a leírásban")
        if hints.get("tags"):
            meta.tags = _hint_tags(hints["tags"])
        nfo.description(meta, release)
        return meta

    candidate = identify(local, meta, hints)
    confidence = candidate.score if candidate else 0.0
    confident = confidence >= CONFIDENT
    meta.confidence = round(confidence if candidate else 0.2 if local.album else 0.0, 2)
    discogs_data: dict = {}
    if candidate and confidence >= 0.5:
        meta.add_source({"musicbrainz": "MusicBrainz", "discogs": "Discogs", "spotify": "Spotify"}[candidate.source])
        if candidate.source == "discogs":
            discogs_data = candidate.data
        elif candidate.source == "musicbrainz" and candidate.data.get("discogs_url"):
            discogs_data = discogs.release(discogs.release_id_from_url(candidate.data["discogs_url"]))
            if discogs_data:
                meta.add_source("Discogs")
    match = candidate.data if candidate and confidence >= 0.5 else {}
    source = candidate.source if candidate and confidence >= 0.5 else ""
    if confidence < 0.5:
        meta.warnings.append("nem sikerült azonosítani a kiadást (MusicBrainz/Discogs) — a fájlok tagjeit használtam; ellenőrizd az adatokat, "
                             "és adj meg igazoló linket (vagy --mbid / --discogs)")
        if not keys.get("ACOUSTID_KEY"):
            meta.warnings.append("az ujjlenyomatos azonosítás (AcoustID) kimaradt: nincs ACOUSTID_KEY a config.env-ben")
    elif not confident:
        meta.warnings.append(f"bizonytalan azonosítás ({round(confidence * 100)}%, {candidate.method}): {candidate_info(candidate)['artist']} – "
                             f"{candidate_info(candidate)['title']} — ellenőrizd az igazoló linket")
    if candidate and confidence >= 0.5:
        remote_count = len(candidate_tracks(candidate))
        if remote_count != len(local.tracks):
            meta.warnings.append(f"a talált kiadás {remote_count} számos, a mappában {len(local.tracks)} szám van — a tracklista a fájlokból készült")
            if not original:
                meta.extra.setdefault("review_reasons", []).append("a helyi és az igazoló adatbázis számlistája eltér: a teljes kiadást ellenőrizni kell")

    edition_mismatch = _edition_mismatch(local, candidate) if candidate and confidence >= 0.5 else []
    if edition_mismatch:
        # Same album, other edition (label/catalogue number): the description must not borrow that edition's data,
        # and an unattended upload must not rely on it.
        meta.extra.setdefault("review_reasons", []).append("a talált kiadás más edíció, mint a fájloké: " + "; ".join(edition_mismatch))
        meta.confidence = min(meta.confidence, 0.6)

    # titles and people: the hints, then a confident match, then the local tags
    info = candidate_info(candidate) if match else {}
    if confident and not code.endswith("_hun") and info.get("country") == "HU":
        titles = " ".join(t.title for t in local.tracks if t.title)
        if translate.is_hungarian(titles):
            meta.extra.setdefault("review_reasons", []).append("magyarországi kiadás és magyar számcímek: ellenőrizd a HUN zenei kategóriát")
    meta.title = str(hints.get("album") or (info.get("title") if confident else "") or local.album or info.get("title", ""))
    artist = str(hints.get("artist") or (info.get("artist") if confident else "") or local.artist or info.get("artist", ""))
    meta.creators = ["Various Artists"] if local.various and not hints.get("artist") and (not artist or _is_various(artist)) else [artist] if artist else []
    date = (match.get("date") if source == "musicbrainz" else match.get("released") if source == "discogs" else match.get("release_date", "")) or ""
    meta.year = str(hints.get("year") or (date[:4] if confident and date[:4].isdigit() else "") or local.year or (info.get("year") or "")[:4])
    meta.date = date if confident and date[:4] == meta.year else local.date if local.date[:4] == meta.year else ""
    group = match.get("release_group", {}) if source == "musicbrainz" else {}
    first_year = (group.get("first_release_date") or "")[:4]
    if first_year and first_year != meta.year:
        meta.extra["original_year"] = first_year
    _apply_tracks(meta, local, candidate if match else None, confident)
    if local.discs > 1:
        meta.extra["discs"] = str(local.discs)
    _label_and_ids(meta, local, candidate if confident else None, discogs_data if confident else {})
    if edition_mismatch:
        meta.publisher = local.label or meta.publisher
        if local.catno:
            meta.extra["catno"] = local.catno

    # style tags
    lastfm_tags: list = []
    if hints.get("tags"):
        meta.tags = _hint_tags(hints["tags"])
    else:
        terms = style_terms(match if source == "musicbrainz" else None, discogs_data or None, None, local.genres, meta.title)
        if sum(w for _t, w in terms) < 6 and meta.creators and meta.title:
            lastfm_tags = lastfm.album_top_tags("" if local.various else meta.creators[0], meta.title)
            if lastfm_tags:
                meta.add_source("Last.fm")
                terms += style_terms(None, None, lastfm_tags)
        meta.tags = style_tags(terms)
        if not meta.tags:
            meta.warnings.append("nincs stílus-címke (add meg: --tags rock,pop)")
    meta.genres = [n for n, _c in (group.get("genres") or [])[:5]] or (discogs_data.get("styles") or [])[:5] or local.genres[:5]

    _cover(meta, local, candidate if match else None, discogs_data)
    if not meta.cover:
        meta.warnings.append("nincs borító (cover.jpg a mappában, beágyazott kép vagy Cover Art Archive)")
    _proof_links(meta, candidate if match else None, discogs_data)
    if not meta.proof_links:
        meta.warnings.append("nincs igazoló link (MusicBrainz/Discogs nem talált) — a szabályzat kötelezővé teszi: írd be a leírásba")

    artist_id = meta.identifiers.get("mb_artist", "") or local.mb_artist_id
    artist_type = (match.get("artist_types") or {}).get(artist_id, "") if source == "musicbrainz" else ""
    english = ""
    if lastfm_tags or lastfm_key():
        info_fm = lastfm.album_info("" if local.various else (meta.creators[0] if meta.creators else ""), meta.title, lang="en")
        english = info_fm.get("wiki_summary", "")
    describe_hu(meta, group=group, artist_id=artist_id if not local.various else "", artist_type=artist_type, english_fallback=english)
    nfo.description(meta, release)
    return meta


def lastfm_key() -> bool:
    return bool(keys.get("LASTFM_KEY"))


def _hint_tags(value) -> list[str]:
    items = value if isinstance(value, (list, tuple)) else re.split(r"[,;]", str(value))
    return [str(v).strip() for v in items if str(v).strip()]


def _category_checks(meta: Meta, local: Local, code: str, lowest: int) -> None:
    lossy = [f for f in local.files if f.codec and not f.lossless]
    lossless = [f for f in local.files if f.lossless]
    if code.startswith("mp3") and lowest and lowest < 128:
        meta.warnings.append(f"MP3 kategóriában legalább 128 kbps kell (a leggyengébb fájl: {lowest} kbps)")
        meta.extra.setdefault("category_errors", []).append("MP3 kategóriában legalább 128 kbps szükséges")
    if code.startswith("mp3") and lossless:
        meta.warnings.append("veszteségmentes fájlok: a Lossless kategóriába valók")
        if not lossy:
            meta.extra["category_hint"] = "lossless_hun" if code.endswith("_hun") else "lossless"
        meta.extra.setdefault("category_errors", []).append("veszteségmentes hang nem tölthető MP3 kategóriába")
    if code.startswith("lossless") and lossy:
        meta.warnings.append(f"Lossless kategóriában veszteséges fájl van ({lossy[0].path.name}, {lossy[0].codec})")
        if not lossless:
            meta.extra["category_hint"] = "mp3_hun" if code.endswith("_hun") else "mp3"
        meta.extra.setdefault("category_errors", []).append("veszteséges hang nem tölthető Lossless kategóriába")
