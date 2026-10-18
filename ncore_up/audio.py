"""Music releases: tags of the audio files (via ffprobe), tracklist, cover file, nCore style tags."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from . import safeio

from . import categories as cats

COVER_NAMES = ("cover", "folder", "front", "albumart")

# The style list of nCore's upload form (music_style suggestions).
NCORE_MUSIC_STYLES = set(
    "60s 70s 80s 90s acid alternative ambient blues breaks classical country dance death.metal disco drum.and.bass dub dubstep "
    "electronic emo euro.disco euro.house eurodance europop experimental folk funk garage goa.trance grunge hardcore hardcore.dance "
    "hardstyle hip.hop house indie.rock industrial italo.disco jazz latin live metal musical new.age ost pop pop.rock "
    "progressive.house progressive.rock progressive.trance psychedelic psytrance punk reggae rhythm.and.blues rock ska soul "
    "synth.pop techno trance trip.hop uk.garage world.music".split()
)
STYLE_ALIASES = {
    "hip hop": "hip.hop", "rap": "hip.hop", "r&b": "rhythm.and.blues", "rnb": "rhythm.and.blues", "drum and bass": "drum.and.bass",
    "dnb": "drum.and.bass", "soundtrack": "ost", "score": "ost", "electronica": "electronic", "edm": "dance", "heavy metal": "metal",
    "death metal": "death.metal", "indie": "indie.rock", "synthpop": "synth.pop", "synth-pop": "synth.pop", "world": "world.music",
    "classic rock": "rock", "hard rock": "rock", "pop rock": "pop.rock", "trip hop": "trip.hop", "new age": "new.age",
}


@dataclass
class Track:
    number: str
    title: str
    length: str
    path: Path


@dataclass
class Album:
    artist: str = ""
    album: str = ""
    year: str = ""
    genres: list[str] = field(default_factory=list)
    tracks: list[Track] = field(default_factory=list)
    cover: Path | None = None
    codec: str = ""
    bitrate: str = ""


def audio_files(root: Path, files: list) -> list[Path]:
    exts = cats.MP3_EXT | cats.LOSSLESS_EXT
    return [root / f.relpath for f in files if f.ext in exts] if root.is_dir() else [root]


def _probe(path: Path) -> dict:
    if not shutil.which("ffprobe"):
        return {}
    try:
        run = safeio.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", "--", str(path)], timeout=60)
        return json.loads(run.stdout or b"{}")
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}


def read_album(root: Path, files: list) -> Album:
    album = Album()
    paths = sorted(audio_files(root, files))
    for index, path in enumerate(paths, 1):
        data = _probe(path)
        fmt = data.get("format") or {}
        tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
        for stream in data.get("streams") or []:
            tags.update({k.lower(): v for k, v in (stream.get("tags") or {}).items() if k.lower() not in tags})
            if stream.get("codec_type") == "audio" and not album.codec:
                album.codec = stream.get("codec_name", "")
                rate = int(fmt.get("bit_rate") or stream.get("bit_rate") or 0)
                album.bitrate = f"{rate // 1000} kbps" if rate else ""
        album.artist = album.artist or tags.get("album_artist") or tags.get("albumartist") or tags.get("artist", "")
        album.album = album.album or tags.get("album", "")
        album.year = album.year or (tags.get("date") or tags.get("year") or "")[:4]
        for genre in re.split(r"[;/,]", tags.get("genre", "")):
            if genre.strip() and genre.strip() not in album.genres:
                album.genres.append(genre.strip())
        seconds = int(float(fmt.get("duration") or 0))
        number = (tags.get("track") or str(index)).split("/")[0]
        album.tracks.append(Track(number.zfill(2), tags.get("title") or path.stem, f"{seconds // 60}:{seconds % 60:02d}" if seconds else "", path))
    album.cover = find_cover(root, files)
    return album


def find_cover(root: Path, files: list) -> Path | None:
    if not root.is_dir():
        return None
    images = [f for f in files if f.ext in {".jpg", ".jpeg", ".png"}]
    for name in COVER_NAMES:
        for image in images:
            if Path(image.relpath).stem.lower().startswith(name):
                return root / image.relpath
    return root / max(images, key=lambda f: f.size).relpath if images else None


def ncore_styles(values: list[str]) -> list[str]:
    """Map genres/tags to nCore's style vocabulary; unknown ones are kept (dotted) after the known ones."""

    known, other = [], []
    for value in values:
        key = value.strip().lower()
        style = STYLE_ALIASES.get(key) or key.replace(" ", ".").replace("-", ".")
        (known if style in NCORE_MUSIC_STYLES else other).append(style)
    result = []
    for style in known + other:
        if style and style not in result:
            result.append(style)
    return result[:6]


def parse_folder(name: str) -> tuple[str, str, str]:
    """Artist, album, year from 'Artist-Album-WEB-2026-GRP' (scene) or 'Artist - Album (2026)'."""

    if " - " in name:
        artist, _, rest = name.partition(" - ")
        year = re.search(r"\b(19|20)\d{2}\b", rest)
        album = re.sub(r"\s*[(\[]\s*(19|20)\d{2}\s*[)\]]\s*", " ", rest).strip()
        return artist.strip(), album, year.group(0) if year else ""
    parts = name.split("-")
    year = next((p for p in parts if re.fullmatch(r"(19|20)\d{2}", p)), "")
    stop = {"WEB", "CD", "CDM", "CDS", "CDR", "VINYL", "FLAC", "MP3", "SINGLE", "EP", "LP", "WEBFLAC", "24BIT", "16BIT", "DIGITAL"}
    words = []
    for part in parts:
        if part.upper() in stop or part == year:
            break
        words.append(part.replace("_", " "))
    artist = words[0] if words else ""
    album = " ".join(words[1:]) if len(words) > 1 else ""
    return artist, album, year
