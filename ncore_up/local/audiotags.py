"""Tags and technical data of audio files (mp3, flac, m4a/m4b, ogg, opus, wav, ape, wv, wma, dsf), read-only.

mutagen is used when installed (full tag set, LAME settings, embedded cover, FLAC CUESHEET); missing fields are
filled by ffprobe and MediaInfo, the same reader used for techinfo. Every tag format becomes one canonical, Vorbis-like key
set (``artist``, ``albumartist``, ``musicbrainz_albumid``, ``catalognumber`` ...), then into an ``AudioTags``.
Files are never written (cross-seeding hashes must stay unchanged).
"""

from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
from collections import Counter
from dataclasses import dataclass, field, fields
from pathlib import Path

from .. import safeio

AUDIO_EXT = {".mp3", ".flac", ".m4a", ".m4b", ".mp4", ".aac", ".ogg", ".oga", ".opus", ".wav", ".ape", ".wv", ".wma", ".dsf", ".aif", ".aiff", ".mka"}
LOSSLESS_CODECS = {"flac", "alac", "pcm", "ape", "wavpack", "dsd", "tta", "truehd"}
SKIP_DIRS = {"sample", "samples", "proof", "covers", "cover", "scans", "artwork"}
COVER_NAMES = ("cover", "folder", "front", "albumart", "album art", "borito", "borító", "elolap", "előlap")
NOT_FRONT = ("back", "hatso", "hátsó", "inlay", "inside", "cd", "disc", "tray", "booklet", "spine", "label")

# canonical key <- any spelling seen in ID3 TXXX descriptions, MP4 freeform names, Vorbis/APE keys, ffprobe output
ALIASES = {
    "album artist": "albumartist", "album_artist": "albumartist", "albumartist": "albumartist", "band": "albumartist",
    "year": "date", "release date": "date", "originalyear": "originalyear", "original year": "originalyear",
    "originaldate": "originaldate", "original date": "originaldate", "origyear": "originalyear",
    "track": "tracknumber", "tracknumber": "tracknumber", "trackc": "tracktotal", "totaltracks": "tracktotal", "tracktotal": "tracktotal",
    "disc": "discnumber", "disk": "discnumber", "discnumber": "discnumber", "disctotal": "disctotal", "totaldiscs": "disctotal", "disc total": "disctotal",
    "musicbrainz album id": "musicbrainz_albumid", "musicbrainz_albumid": "musicbrainz_albumid",
    "musicbrainz release group id": "musicbrainz_releasegroupid", "musicbrainz_releasegroupid": "musicbrainz_releasegroupid",
    "musicbrainz album artist id": "musicbrainz_albumartistid", "musicbrainz_albumartistid": "musicbrainz_albumartistid",
    "musicbrainz artist id": "musicbrainz_artistid", "musicbrainz_artistid": "musicbrainz_artistid",
    "musicbrainz release track id": "musicbrainz_releasetrackid", "musicbrainz_releasetrackid": "musicbrainz_releasetrackid",
    "musicbrainz track id": "musicbrainz_trackid", "musicbrainz_trackid": "musicbrainz_trackid", "musicbrainz recording id": "musicbrainz_trackid",
    "musicbrainz disc id": "musicbrainz_discid", "musicbrainz_discid": "musicbrainz_discid",
    "musicbrainz album type": "releasetype", "releasetype": "releasetype", "musicbrainz album status": "releasestatus",
    "musicbrainz album release country": "releasecountry", "releasecountry": "releasecountry",
    "catalognumber": "catalognumber", "catalog number": "catalognumber", "catalog #": "catalognumber", "catalog": "catalognumber",
    "catalogno": "catalognumber", "catno": "catalognumber", "labelno": "catalognumber",
    "barcode": "barcode", "upc": "barcode", "ean": "barcode", "ean/upc": "barcode",
    "label": "label", "publisher": "label", "organization": "label", "record label": "label",
    "discogs_release_id": "discogs_release_id", "discogs release id": "discogs_release_id", "discogs_release": "discogs_release_id",
    "narrator": "narrator", "narratedby": "narrator", "narrated by": "narrator", "reader": "narrator", "read by": "narrator", "felolvassa": "narrator",
    "comment": "comment", "comments": "comment", "description": "comment", "synopsis": "comment",
    "isrc": "isrc", "tsrc": "isrc", "composer": "composer", "media": "media", "cuesheet": "cuesheet", "acoustid id": "acoustid_id",
    "author": "artist", "performer": "performer", "encoder": "encoder", "encoded by": "encodedby", "encodedby": "encodedby",
}
ID3_FRAMES = {
    "TPE1": "artist", "TPE2": "albumartist", "TALB": "album", "TIT2": "title", "TDRC": "date", "TYER": "date", "TDOR": "originaldate",
    "TORY": "originalyear", "TCON": "genre", "TRCK": "tracknumber", "TPOS": "discnumber", "TPUB": "label", "TSRC": "isrc", "TCOM": "composer",
    "TMED": "media", "TIT3": "subtitle", "TSST": "discsubtitle", "TLAN": "language", "TCOP": "copyright", "TSSE": "encoder", "TENC": "encodedby",
    "TPE3": "conductor", "TOAL": "originalalbum",
}
MP4_KEYS = {
    "\xa9ART": "artist", "aART": "albumartist", "\xa9alb": "album", "\xa9nam": "title", "\xa9day": "date", "\xa9gen": "genre",
    "\xa9wrt": "composer", "\xa9nrt": "narrator", "\xa9cmt": "comment", "desc": "comment", "ldes": "longdescription", "cprt": "copyright",
    "\xa9pub": "label", "\xa9too": "encoder", "\xa9grp": "grouping", "\xa9st3": "subtitle",
}
ASF_KEYS = {
    "Author": "artist", "Title": "title", "WM/AlbumTitle": "album", "WM/AlbumArtist": "albumartist", "WM/Year": "date", "WM/Genre": "genre",
    "WM/TrackNumber": "tracknumber", "WM/PartOfSet": "discnumber", "WM/Publisher": "label", "WM/Composer": "composer", "WM/ISRC": "isrc",
    "MusicBrainz/Album Id": "musicbrainz_albumid", "MusicBrainz/Release Group Id": "musicbrainz_releasegroupid",
    "MusicBrainz/Album Artist Id": "musicbrainz_albumartistid", "WM/Barcode": "barcode", "WM/CatalogNo": "catalognumber",
}
NARRATOR_RE = re.compile(r"(?im)^\W*(?:felolvassa|el[őo]adja|el[őo]ad[óo]|narrat(?:or|ed by)|read by)\s*[:\-]?\s*(.+?)\s*$")
MBID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
FILENAME_RE = re.compile(r"^(?:(?P<disc>\d{1,2})[-.](?=\d))?(?P<num>\d{1,3})(?:[\s._)-]+|$)(?:(?P<artist>.+?)\s+-\s+)?(?P<title>.*)$")
SAMPLE_RE = re.compile(r"(?i)^sample(?:$|[\s._-])|-sample$")  # 'sample.flac', 'grp-sample.mp3'; not 'Sampler' or '05 - Free Sample'


@dataclass
class AudioTags:
    path: Path
    artist: str = ""
    albumartist: str = ""
    album: str = ""
    title: str = ""
    date: str = ""
    original_date: str = ""
    genres: list[str] = field(default_factory=list)
    track: int = 0
    track_total: int = 0
    disc: int = 0
    disc_total: int = 0
    duration: float = 0.0  # seconds
    mb_album_id: str = ""
    mb_release_group_id: str = ""
    mb_artist_id: str = ""  # the album artist's MBID when tagged, else the track artist's
    mb_recording_id: str = ""
    discogs_release_id: str = ""
    barcode: str = ""
    catno: str = ""
    label: str = ""
    isrc: str = ""
    composer: str = ""
    narrator: str = ""
    comment: str = ""
    media: str = ""
    release_type: str = ""
    cuesheet: str = ""  # CUE text embedded as a tag
    codec: str = ""  # mp3, flac, aac, alac, vorbis, opus, pcm, ape, wavpack, wma, dsd
    bitrate: int = 0  # bits/s
    bitrate_mode: str = ""  # CBR, VBR, ABR ("" unknown)
    encoder_settings: str = ""  # LAME "-V 0", "-b 320"
    sample_rate: int = 0
    bits_per_sample: int = 0
    channels: int = 0
    total_samples: int = 0
    flac_toc: list[int] = field(default_factory=list)  # FLAC CUESHEET block: track start samples + lead-out
    cover: bytes = b""
    cover_mime: str = ""
    tagged: bool = False  # any title/artist/album tag at all
    raw: dict[str, list[str]] = field(default_factory=dict)
    sources: list[str] = field(default_factory=list)  # readers that contributed data, in priority order

    @property
    def year(self) -> str:
        match = re.search(r"(1[89]|20)\d{2}", self.date or "")
        return match.group(0) if match else ""

    @property
    def lossless(self) -> bool:
        return self.codec in LOSSLESS_CODECS


# --- canonical tags --------------------------------------------------------------------------------------

def canonical_key(key: str) -> str:
    key = (key or "").strip()
    low = key.lower()
    if low.startswith("----:"):
        low = low.rsplit(":", 1)[-1]
    if low.startswith("txxx:"):
        low = low[5:]
    return ALIASES.get(low) or ALIASES.get(low.replace("_", " ")) or re.sub(r"\s+", "_", low)


def normalize(raw: dict[str, list]) -> dict[str, list[str]]:
    """Canonical keys -> non-empty string values (duplicates dropped, order kept)."""

    out: dict[str, list[str]] = {}
    for key, values in raw.items():
        if not isinstance(values, (list, tuple)):
            values = [values]
        name = canonical_key(key)
        for value in values:
            text = value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)
            for part in text.split("\x00"):
                part = part.strip()
                if part and part not in out.setdefault(name, []):
                    out[name].append(part)
    return {k: v for k, v in out.items() if v}


def _number_pair(values: list[str]) -> tuple[int, int]:
    match = re.match(r"\s*(\d+)\s*(?:/\s*(\d+))?", values[0] if values else "")
    if not match:
        return 0, 0
    return int(match.group(1)), int(match.group(2) or 0)


def from_tags(path: Path, tags: dict[str, list[str]]) -> AudioTags:
    """Fill an AudioTags from canonical tags (see ``normalize``)."""

    def first(*names: str) -> str:
        for name in names:
            if tags.get(name):
                return tags[name][0]
        return ""

    item = AudioTags(path=Path(path), raw=tags)
    item.artist = ", ".join(tags.get("artist", [])[:3]) if len(tags.get("artist", [])) > 1 else first("artist")
    item.albumartist, item.album, item.title = first("albumartist"), first("album"), first("title")
    item.date = first("date", "originaldate", "originalyear")
    item.original_date = first("originaldate", "originalyear")
    item.genres = [g.strip() for value in tags.get("genre", []) for g in re.split(r"\s*[;/,|]\s*", value) if g.strip()]
    item.track, item.track_total = _number_pair(tags.get("tracknumber", []))
    item.disc, item.disc_total = _number_pair(tags.get("discnumber", []))
    item.track_total = item.track_total or _number_pair(tags.get("tracktotal", []))[0]
    item.disc_total = item.disc_total or _number_pair(tags.get("disctotal", []))[0]
    for attr, name in (("mb_album_id", "musicbrainz_albumid"), ("mb_release_group_id", "musicbrainz_releasegroupid"),
                       ("mb_recording_id", "musicbrainz_trackid")):
        match = MBID_RE.search(first(name).lower())
        setattr(item, attr, match.group(0) if match else "")
    match = MBID_RE.search((first("musicbrainz_albumartistid") or first("musicbrainz_artistid")).lower())
    item.mb_artist_id = match.group(0) if match else ""
    item.discogs_release_id = (re.search(r"\d+", first("discogs_release_id")) or [""])[0]
    item.barcode = re.sub(r"[^0-9]", "", first("barcode"))
    item.catno, item.label, item.isrc = first("catalognumber"), first("label"), first("isrc")
    item.composer, item.media, item.release_type = first("composer"), first("media"), first("releasetype")
    item.comment = first("comment", "longdescription")
    item.cuesheet = first("cuesheet")
    item.narrator = first("narrator")
    if not item.narrator:
        for text in tags.get("comment", []) + tags.get("longdescription", []):
            match = NARRATOR_RE.search(text)
            if match:
                item.narrator = match.group(1).strip(" .;,")
                break
    item.tagged = bool(item.title or item.album or item.artist)
    return item


# --- mutagen ---------------------------------------------------------------------------------------------

def _mutagen_raw(audio) -> tuple[dict[str, list], bytes, str, list[int]]:
    """(raw tags with format-neutral keys, front cover, cover mime, FLAC CUESHEET offsets) of a mutagen FileType."""

    raw: dict[str, list] = {}
    covers: list[tuple[int, bytes, str]] = []  # (priority, data, mime)
    tags = audio.tags
    kind = type(audio).__name__
    toc: list[int] = []

    def add(key: str, values) -> None:
        if not isinstance(values, (list, tuple)):
            values = [values]
        raw.setdefault(key, []).extend(values)

    try:
        from mutagen.id3 import ID3
    except ImportError:  # pragma: no cover - mutagen always ships id3
        ID3 = ()  # type: ignore[assignment]
    if tags is not None and isinstance(tags, ID3):  # mp3, wav, aiff, dsf
        for frame in tags.values():
            fid = frame.FrameID
            if fid == "TXXX":
                add(frame.desc, [str(t) for t in frame.text])
            elif fid == "TCON":
                add("genre", list(frame.genres))
            elif fid in ID3_FRAMES:
                add(ID3_FRAMES[fid], [str(t) for t in frame.text])
            elif fid == "UFID" and frame.owner == "http://musicbrainz.org":
                add("musicbrainz_trackid", frame.data.decode("ascii", "replace"))
            elif fid == "COMM" and frame.desc.lower() not in {"itunnorm", "itunsmpb", "itunpgap", "itunes_cddb_ids"}:
                add("comment", [str(t) for t in frame.text])
            elif fid == "APIC":
                covers.append((0 if frame.type == 3 else 1, frame.data, frame.mime))
    elif tags is not None and kind in {"MP4", "EasyMP4"}:
        for key, values in tags.items():
            if key == "trkn" and values:
                add("tracknumber", f"{values[0][0]}/{values[0][1]}" if values[0][1] else str(values[0][0]))
            elif key == "disk" and values:
                add("discnumber", f"{values[0][0]}/{values[0][1]}" if values[0][1] else str(values[0][0]))
            elif key == "covr":
                for cover in values:
                    covers.append((0, bytes(cover), "image/png" if getattr(cover, "imageformat", 13) == 14 else "image/jpeg"))
            elif key.startswith("----:"):
                add(key.rsplit(":", 1)[-1], [bytes(v).decode("utf-8", "replace") for v in values])
            elif key in MP4_KEYS:
                add(MP4_KEYS[key], [str(v) for v in values])
    elif tags is not None and kind == "ASF":
        for key, values in tags.items():
            if key == "WM/Picture":
                for value in values:
                    covers.append((1, _asf_picture(bytes(value.value)), "image/jpeg"))
            elif key in ASF_KEYS:
                add(ASF_KEYS[key], [str(v.value) if hasattr(v, "value") else str(v) for v in values])
    elif tags is not None and hasattr(tags, "as_dict"):  # Vorbis comments (flac, ogg, opus)
        for key, values in tags.as_dict().items():
            if key.lower() == "metadata_block_picture":
                for value in values:
                    try:
                        from mutagen.flac import Picture

                        picture = Picture(base64.b64decode(value))
                        covers.append((0 if picture.type == 3 else 1, picture.data, picture.mime))
                    except Exception:  # noqa: BLE001 - broken picture block
                        continue
            elif key.lower() not in {"coverart", "coverartmime"}:
                add(key, values)
    elif tags is not None:  # APEv2 (ape, wv, mpc)
        for key, value in tags.items():
            if getattr(value, "kind", 0) == 1:  # binary
                if key.lower().startswith("cover art"):
                    data = bytes(value.value)
                    covers.append((0 if "front" in key.lower() else 1, data.split(b"\x00", 1)[-1], "image/jpeg"))
            else:
                add(key, str(value).split("\x00"))
    for picture in getattr(audio, "pictures", []) or []:  # FLAC picture blocks
        covers.append((0 if picture.type == 3 else 1, picture.data, picture.mime))
    cue = getattr(audio, "cuesheet", None)
    if cue is not None and getattr(cue, "tracks", None):
        # a track's start_offset is its first index point (INDEX 00 when it has a pregap); the disc TOC needs INDEX 01
        for track in cue.tracks:
            if getattr(track, "type", 0) == 0 or track.track_number in (170, 255):
                index1 = next((int(i.index_offset) for i in getattr(track, "indexes", None) or [] if i.index_number == 1), 0)
                toc.append(int(track.start_offset) + index1)
    covers.sort(key=lambda c: c[0])
    cover, mime = (covers[0][1], covers[0][2]) if covers else (b"", "")
    return raw, cover, mime, toc


def _asf_picture(data: bytes) -> bytes:
    """WM/Picture: type(1) size(4) mime(utf-16 z) description(utf-16 z) data."""

    try:
        pos = 5
        for _ in range(2):
            end = pos
            while data[end:end + 2] != b"\x00\x00":
                end += 2
            pos = end + 2
        return data[pos:]
    except IndexError:
        return b""


def _codec(audio) -> str:
    kind = type(audio).__name__
    info = audio.info
    if kind in {"MP3", "EasyMP3"}:
        return "mp3"
    if kind in {"FLAC", "OggFLAC"}:
        return "flac"
    if kind in {"MP4", "EasyMP4"}:
        codec = str(getattr(info, "codec", "") or "")
        return "alac" if codec.startswith("alac") else "aac" if codec.startswith("mp4a") or not codec else codec.split(".")[0]
    return {"OggVorbis": "vorbis", "OggOpus": "opus", "WAVE": "pcm", "AIFF": "pcm", "MonkeysAudio": "ape", "WavPack": "wavpack",
            "Musepack": "musepack", "ASF": "wma", "DSF": "dsd", "TrueAudio": "tta"}.get(kind, kind.lower())


def _read_mutagen(path: Path, cover: bool) -> AudioTags | None:
    try:
        import mutagen  # optional dependency
    except ImportError:
        return None
    try:
        audio = mutagen.File(str(path))
    except Exception:  # noqa: BLE001 - corrupt/unsupported file: mutagen raises many types
        return None
    if audio is None:
        return None
    try:
        return _from_mutagen(path, audio, cover)
    except Exception:  # noqa: BLE001 - odd frames/atoms in a damaged file: fall back to ffprobe
        return None


def _from_mutagen(path: Path, audio, cover: bool) -> AudioTags:
    raw, image, mime, toc = _mutagen_raw(audio)
    item = from_tags(path, normalize(raw))
    info = audio.info
    item.codec = _codec(audio)
    item.duration = float(getattr(info, "length", 0) or 0)
    item.bitrate = int(getattr(info, "bitrate", 0) or 0)
    item.sample_rate = int(getattr(info, "sample_rate", 0) or 0)
    item.bits_per_sample = int(getattr(info, "bits_per_sample", 0) or 0)
    item.channels = int(getattr(info, "channels", 0) or 0)
    item.total_samples = int(getattr(info, "total_samples", 0) or 0)
    mode = getattr(info, "bitrate_mode", None)
    if mode is not None:
        name = str(getattr(mode, "name", "") or mode).rsplit(".", 1)[-1].upper()  # mutagen's own enum: "BitrateMode.VBR"
        item.bitrate_mode = name if name in {"CBR", "VBR", "ABR"} else ""
    item.encoder_settings = str(getattr(info, "encoder_settings", "") or "")
    item.flac_toc = toc
    if cover:
        item.cover, item.cover_mime = image, mime
    if item.codec == "mp3" and not item.bitrate_mode and item.bitrate and not getattr(info, "sketchy", False):
        item.bitrate_mode = "CBR" if item.bitrate // 1000 in {32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320} else ""
    return item


# --- ffprobe fallback ---------------------------------------------------------------------------------------

def ffprobe_json(path: Path) -> dict:
    if not shutil.which("ffprobe"):
        return {}
    try:
        run = safeio.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", "--", str(path)], timeout=120)
        return json.loads(run.stdout or b"{}")
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}


def parse_ffprobe(path: Path, data: dict) -> AudioTags:
    """AudioTags from ``ffprobe -show_format -show_streams`` JSON (tags of the container, then of the audio stream)."""

    fmt = data.get("format") or {}
    raw: dict[str, list] = {}
    for key, value in (fmt.get("tags") or {}).items():
        raw.setdefault(key, []).append(value)
    audio = next((s for s in data.get("streams") or [] if s.get("codec_type") == "audio"), {})
    for key, value in (audio.get("tags") or {}).items():
        if key.lower() not in {k.lower() for k in raw}:
            raw.setdefault(key, []).append(value)
    item = from_tags(path, normalize(raw))
    codec = str(audio.get("codec_name") or "")
    item.codec = "pcm" if codec.startswith("pcm_") else "dsd" if codec.startswith("dsd_") else {"mp3float": "mp3"}.get(codec, codec)
    item.duration = _number(audio.get("duration")) or _number(fmt.get("duration"))
    item.bitrate = int(_number(audio.get("bit_rate")) or _number(fmt.get("bit_rate")))
    item.sample_rate = int(_number(audio.get("sample_rate")))
    item.bits_per_sample = int(_number(audio.get("bits_per_raw_sample")) or _number(audio.get("bits_per_sample")))
    item.channels = int(_number(audio.get("channels")))
    if item.duration and item.sample_rate:
        item.total_samples = int(_number(audio.get("duration_ts"))) if str(audio.get("time_base", "")) == f"1/{item.sample_rate}" else 0
    return item


def _number(value) -> float:
    """ffprobe numbers come as strings and may be 'N/A'."""

    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def mediainfo_json(path: Path) -> dict:
    """Native MediaInfo JSON; its unformatted Duration values are in seconds."""

    if not shutil.which("mediainfo"):
        return {}
    try:
        run = safeio.run(["mediainfo", "--Output=JSON", "--", str(path.absolute())], timeout=120)
        data = json.loads(run.stdout or b"{}")
        return data if not run.returncode and isinstance(data, dict) else {}
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}


def parse_mediainfo(path: Path, data: dict) -> AudioTags:
    """Read tags and actual stream data; MediaInfo's Track is a title, not a number."""

    tracks = (data.get("media") or {}).get("track") or []
    general = next((t for t in tracks if t.get("@type") == "General"), {})
    audio = next((t for t in tracks if t.get("@type") == "Audio"), {})
    # No audio stream means a file was merely recognised by extension, not decoded.
    if not audio:
        return AudioTags(path=path)
    mapping = {
        "Performer": "artist", "Album_Performer": "albumartist", "Album": "album",
        "Track": "title", "Title": "title", "Recorded_Date": "date", "Original_Released_Date": "originaldate",
        "Genre": "genre", "Track_Position": "tracknumber", "Track_Position_Total": "tracktotal",
        "Part_Position": "discnumber", "Part_Position_Total": "disctotal", "Publisher": "label",
        "Label": "label", "CatalogNumber": "catalognumber", "BarCode": "barcode", "ISRC": "isrc",
        "Composer": "composer", "Comment": "comment",
    }
    raw: dict[str, list] = {}
    for section in (general, audio):
        for key, name in mapping.items():
            value = section.get(key)
            if value and name not in raw:
                raw[name] = value if isinstance(value, list) else [value]
        # MusicBrainz and other custom tags appear in MediaInfo's extra section.
        for key, value in (section.get("extra") or {}).items():
            name = canonical_key(key)
            if value and name not in raw:
                raw[name] = value if isinstance(value, list) else [value]
    item = from_tags(path, normalize(raw))
    fmt = str(audio.get("Format") or "").casefold()
    codecs = {"flac": "flac", "alac": "alac", "aac": "aac", "pcm": "pcm", "adpcm": "adpcm",
              "monkey's audio": "ape", "wavpack": "wavpack", "vorbis": "vorbis", "opus": "opus",
              "dsd": "dsd", "wma": "wma", "dts": "dts", "true audio": "tta", "truehd": "truehd"}
    if fmt == "mpeg audio":
        profile = str(audio.get("Format_Profile") or "").casefold()
        item.codec = next((codec for layer, codec in (("layer 3", "mp3"), ("layer 2", "mp2"), ("layer 1", "mp1")) if layer in profile), "")
    else:
        item.codec = codecs.get(fmt, "")
    item.duration = _number(audio.get("Duration")) or _number(general.get("Duration"))
    item.bitrate = int(_number(audio.get("BitRate")) or _number(general.get("OverallBitRate")))
    item.sample_rate = int(_number(audio.get("SamplingRate")))
    item.bits_per_sample = int(_number(audio.get("BitDepth")))
    item.channels = int(_number(audio.get("Channels")))
    item.total_samples = int(_number(audio.get("SamplingCount")))
    mode = str(audio.get("BitRate_Mode") or general.get("OverallBitRate_Mode") or "").upper()
    item.bitrate_mode = mode if mode in {"CBR", "VBR", "ABR"} else ""
    return item


def _merge_missing(item: AudioTags, fallback: AudioTags, source: str) -> None:
    """Keep earlier readers' values; record only readers that actually filled a gap."""

    changed = False
    for attr in fields(AudioTags):
        name = attr.name
        if name in {"path", "raw", "tagged", "sources", "cover_mime"}:
            continue
        value = getattr(fallback, name)
        if not getattr(item, name) and value:
            setattr(item, name, value)
            if name == "cover":
                item.cover_mime = fallback.cover_mime
            changed = True
    for key, values in fallback.raw.items():
        if key not in item.raw:
            item.raw[key] = values
    item.tagged = bool(item.title or item.album or item.artist)
    if changed and source not in item.sources:
        item.sources.append(source)


def _needs_fallback(item: AudioTags) -> bool:
    return (not (item.artist or item.albumartist)
            or any(not getattr(item, name) for name in ("title", "album", "date", "track", "duration", "codec", "sample_rate", "channels"))
            or (item.codec in {"mp3", "aac"} and not item.bitrate)
            or (item.codec in {"flac", "alac", "pcm", "ape", "wavpack"} and not item.bits_per_sample))


def _ffprobe_cover(path: Path) -> bytes:
    if not shutil.which("ffmpeg"):
        return b""
    try:
        run = safeio.run(["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:v:0", "-frames:v", "1", "-c", "copy", "-f", "image2pipe", "pipe:1"], timeout=60)
    except (OSError, subprocess.SubprocessError):
        return b""
    return run.stdout if run.stdout[:3] == b"\xff\xd8\xff" or run.stdout[:8] == b"\x89PNG\r\n\x1a\n" else b""


# --- public ------------------------------------------------------------------------------------------------

def read(path: Path, *, cover: bool = False) -> AudioTags:
    """Tags and stream data of one file; an AudioTags with only ``path`` when nothing can read it."""

    path = Path(path)
    item = _read_mutagen(path, cover)
    if item is None:
        item = AudioTags(path=path)
    else:
        item.sources.append("mutagen")
    data = {}
    if _needs_fallback(item):
        data = ffprobe_json(path)
        if data:
            _merge_missing(item, parse_ffprobe(path, data), "ffprobe")
    if _needs_fallback(item):
        info = mediainfo_json(path)
        if info:
            _merge_missing(item, parse_mediainfo(path, info), "MediaInfo")
    if cover and not item.cover and data:
        item.cover = _ffprobe_cover(path)
        item.cover_mime = "image/png" if item.cover[:4] == b"\x89PNG" else "image/jpeg" if item.cover else ""
    return item


def natural_key(text: str) -> list:
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", text)]


def audio_files(root: Path, files: list) -> list[Path]:
    """Audio files of a release (``Release.files``), in natural order, without sample/proof folders."""

    root = Path(root)
    if root.is_file():
        return [root] if root.suffix.lower() in AUDIO_EXT else []
    picked = []
    for item in files:
        rel = Path(item.relpath)
        if rel.suffix.lower() not in AUDIO_EXT or any(p.lower() in SKIP_DIRS for p in rel.parts[:-1]) or SAMPLE_RE.search(rel.stem) \
                or rel.name.startswith("._"):  # macOS AppleDouble metadata files carry the audio extension but no audio
            continue
        picked.append(rel)
    return [root / rel for rel in sorted(picked, key=lambda p: natural_key(str(p)))]


def disc_from_path(path: Path) -> int:
    """Disc number from a 'CD2' / 'Disc 2' / '2. lemez' folder name, 0 when none."""

    match = re.search(r"(?i)(?:^|[\s._\-\[(])(?:cd|dis[ck]|lemez|vol(?:ume)?)[\s._\-]*(\d{1,2})(?:$|[\s._\-\])])", path.parent.name)
    if not match:
        match = re.search(r"(?i)^(\d{1,2})\.?\s*lemez", path.parent.name)
    return int(match.group(1)) if match else 0


def parse_filename(name: str) -> dict:
    """{"disc", "number", "artist", "title"} from '01 - Title.flac', '1-03. Artist - Title.mp3', '03_title.mp3'."""

    stem = re.sub(r"\.[A-Za-z0-9]{2,4}$", "", Path(name).name).replace("_", " ").strip()
    match = FILENAME_RE.match(stem)
    if not match:
        return {"disc": 0, "number": 0, "artist": "", "title": stem}
    title = match.group("title").strip(" .-")
    artist = (match.group("artist") or "").strip()
    return {"disc": int(match.group("disc") or 0), "number": int(match.group("num")), "artist": artist, "title": title or stem}


def find_cover_file(root: Path, files: list) -> Path | None:
    """The release's own front image: cover/folder/front by name, else the biggest root image that is not a back/CD scan."""

    root = Path(root)
    if not root.is_dir():
        return None
    images = [f for f in files if Path(f.relpath).suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}]
    for name in COVER_NAMES:
        for image in sorted(images, key=lambda f: len(Path(f.relpath).parts)):
            stem = Path(image.relpath).stem.lower()
            if stem.startswith(name) and not any(bad in stem for bad in NOT_FRONT if bad not in name):
                return root / image.relpath
    plain = [f for f in images if len(Path(f.relpath).parts) == 1 and not any(bad in Path(f.relpath).stem.lower() for bad in NOT_FRONT)]
    return root / max(plain, key=lambda f: f.size).relpath if plain else None


def _khz(rate: int) -> str:
    return f"{rate / 1000:.1f}".rstrip("0").rstrip(".") if rate else ""


def format_label(items: list[AudioTags]) -> str:
    """'FLAC 16/44.1', 'FLAC 24/96', 'MP3 320 kbps CBR', 'MP3 VBR V0', 'MP3 VBR ~245 kbps', 'AAC 256 kbps' ('+' when mixed)."""

    labels = []
    by_codec: dict[str, list[AudioTags]] = {}
    for item in items:
        if item.codec:
            by_codec.setdefault(item.codec, []).append(item)
    for codec, group in by_codec.items():
        name = {"mp3": "MP3", "flac": "FLAC", "aac": "AAC", "alac": "ALAC", "vorbis": "Ogg Vorbis", "opus": "Opus", "pcm": "WAV",
                "ape": "APE", "wavpack": "WavPack", "wma": "WMA", "dsd": "DSD", "musepack": "Musepack"}.get(codec, codec.upper())
        if codec in LOSSLESS_CODECS:
            bits = Counter(i.bits_per_sample for i in group if i.bits_per_sample).most_common(1)
            rate = Counter(i.sample_rate for i in group if i.sample_rate).most_common(1)
            spec = "/".join(x for x in [str(bits[0][0]) if bits else "", _khz(rate[0][0]) if rate else ""] if x)
            labels.append(f"{name} {spec}".strip() if codec != "dsd" else name)
            continue
        rates = [i.bitrate // 1000 for i in group if i.bitrate]
        average = round(sum(rates) / len(rates)) if rates else 0
        if codec == "mp3":
            modes = {i.bitrate_mode for i in group}
            preset = {m.group(1) for i in group for m in [re.search(r"-V\s*(\d)", i.encoder_settings)] if m}
            if modes == {"CBR"} and len(set(rates)) == 1:
                labels.append(f"MP3 {rates[0]} kbps CBR")
            elif "VBR" in modes and len(preset) == 1:
                labels.append(f"MP3 VBR V{preset.pop()}")
            elif "VBR" in modes or "ABR" in modes:
                labels.append(f"MP3 {'ABR' if modes == {'ABR'} else 'VBR'} ~{average} kbps")
            else:
                labels.append(f"MP3 {average} kbps" if average else "MP3")
        else:
            labels.append(f"{name} {average} kbps" if average else name)
    return " + ".join(dict.fromkeys(labels))


def lowest_bitrate_kbps(items: list[AudioTags]) -> int:
    """Lowest (average) bitrate of the lossy files in kbps, 0 when unknown or all lossless (nCore: MP3 >= 128 kbps)."""

    rates = [i.bitrate // 1000 for i in items if i.bitrate and not i.lossless]
    return min(rates) if rates else 0
