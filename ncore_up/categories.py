"""nCore categories: codes of the upload form, what an original release may contain, and automatic detection.

The codes are the values of the upload form's ``tipus`` select. The allowed-file rules follow the nCore wiki
("Feltöltési szabályok", original releases): unpacked, release name and file names unchanged, the content plus
its NFO, and in addition only the release's own sample, subtitles, Proof folder and similar companions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

VIDEO_EXT = {".mkv", ".mp4", ".avi", ".m4v"}
DISC_IMAGE_EXT = {".iso", ".img", ".bin", ".cue", ".mdf", ".mds", ".nrg"}
SUBTITLE_EXT = {".srt", ".sub", ".idx", ".sup", ".ass", ".ssa", ".vtt"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
MP3_EXT = {".mp3", ".m4a", ".m4b", ".ogg", ".opus"}
LOSSLESS_EXT = {".flac", ".ape", ".wav", ".dts", ".mka", ".wv", ".dsf", ".dff"}
EBOOK_EXT = {".epub", ".pdf", ".mobi", ".prc", ".azw", ".azw3", ".djvu", ".djv", ".fb2", ".cbr", ".cbz", ".cb7", ".doc", ".docx", ".rtf", ".lit"}
ARCHIVE_RE = re.compile(r"(?i)\.(rar|zip|7z|r\d{2}|s\d{2}|\d{3})$")


@dataclass(frozen=True)
class Category:
    code: str
    label: str
    group: str  # video, music, clip, ebook, game, program, console, mobil, xxx
    allowed: frozenset[str] = field(default_factory=frozenset)  # extra extensions allowed besides the content
    packed_ok: bool = False  # RIP categories may stay packed


_VIDEO_EXTRA = frozenset(SUBTITLE_EXT | IMAGE_EXT | {".nfo", ".sfv"})  # approved original film/series releases keep their SFV
_MUSIC_EXTRA = frozenset(IMAGE_EXT | {".nfo", ".m3u", ".m3u8", ".sfv", ".pdf", ".cue", ".log", ".txt", ".accurip"})
_EBOOK_EXTRA = frozenset(IMAGE_EXT | {".nfo", ".sfv", ".txt"})
_SOFT_EXTRA = frozenset(IMAGE_EXT | {".nfo", ".sfv", ".txt", ".diz"})

CATEGORIES: dict[str, Category] = {c.code: c for c in [
    Category("xvid_hun", "Film (HUN SD)", "video", _VIDEO_EXTRA),
    Category("xvid", "Film (ENG SD)", "video", _VIDEO_EXTRA),
    Category("dvd_hun", "Film (HUN DVD)", "video", _VIDEO_EXTRA),
    Category("dvd", "Film (ENG DVD)", "video", _VIDEO_EXTRA),
    Category("dvd9_hun", "Film (HUN DVD9)", "video", _VIDEO_EXTRA),
    Category("dvd9", "Film (ENG DVD9)", "video", _VIDEO_EXTRA),
    Category("hd_hun", "Film (HUN HD)", "video", _VIDEO_EXTRA),
    Category("hd", "Film (ENG HD)", "video", _VIDEO_EXTRA),
    Category("xvidser_hun", "Sorozat (HUN SD)", "video", _VIDEO_EXTRA),
    Category("xvidser", "Sorozat (ENG SD)", "video", _VIDEO_EXTRA),
    Category("dvdser_hun", "Sorozat (HUN DVD)", "video", _VIDEO_EXTRA),
    Category("dvdser", "Sorozat (ENG DVD)", "video", _VIDEO_EXTRA),
    Category("hdser_hun", "Sorozat (HUN HD)", "video", _VIDEO_EXTRA),
    Category("hdser", "Sorozat (ENG HD)", "video", _VIDEO_EXTRA),
    Category("mp3_hun", "Mp3 (HUN)", "music", _MUSIC_EXTRA),
    Category("mp3", "Mp3 (ENG)", "music", _MUSIC_EXTRA),
    Category("lossless_hun", "Lossless (HUN)", "music", _MUSIC_EXTRA),
    Category("lossless", "Lossless (ENG)", "music", _MUSIC_EXTRA),
    Category("clip", "Klip", "clip", _VIDEO_EXTRA),
    Category("game_iso", "Játék (ISO)", "game", _SOFT_EXTRA),
    Category("game_rip", "Játék (RIP)", "game", _SOFT_EXTRA, packed_ok=True),
    Category("console", "Konzol", "console", _SOFT_EXTRA),
    Category("ebook_hun", "e-Book (HUN)", "ebook", _EBOOK_EXTRA),
    Category("ebook", "e-Book (ENG)", "ebook", _EBOOK_EXTRA),
    Category("iso", "Program (ISO)", "program", _SOFT_EXTRA),
    Category("misc", "Program", "program", _SOFT_EXTRA, packed_ok=True),
    Category("mobil", "Mobil", "mobil", _SOFT_EXTRA),
    Category("xxx_xvid", "XXX (SD)", "xxx", _VIDEO_EXTRA),
    Category("xxx_dvd", "XXX (DVD/DVD9)", "xxx", _VIDEO_EXTRA),
    Category("xxx_imageset", "XXX (Imageset)", "xxx", _VIDEO_EXTRA),
    Category("xxx_hd", "XXX (HD)", "xxx", _VIDEO_EXTRA),
]}

# Fields of the upload form that belong to the "infobar" of films and series (shown for these codes).
VIDEO_CODES = {c for c, cat in CATEGORIES.items() if cat.group == "video"}

HD_TOKENS = {"720P", "1080P", "1080I", "2160P", "UHD", "BD25", "BD50", "BD66", "BD100", "REMUX", "BLURAY", "BDRIP"}
DVD_TOKENS = {"DVDR", "DVD9", "DVD5", "NTSC", "PAL", "DVD"}
SD_TOKENS = {"XVID", "DVDRIP", "HDTV", "WEBRIP", "X264", "X265", "H264", "H265", "HEVC"}
HUN_TOKENS = {"HUN", "HUNGARIAN", "MAGYAR", "HUNDUB"}
SERIES_RE = re.compile(r"(?i)(?:^|[ ._-])S\d{1,3}(?:E\d{1,4}|D\d{1,3})?(?:[ ._-]|$)|(?:^|[ ._-])E\d{2,4}(?:[ ._-]|$)")


def tokens(name: str) -> set[str]:
    return {t.upper() for t in re.split(r"[ ._\-()\[\]]+", name) if t}


@dataclass
class Detection:
    code: str | None
    reasons: list[str]
    candidates: list[str]


def detect(name: str, extensions: Iterable[str], has_bdmv: bool, has_video_ts: bool, hungarian_audio: bool | None) -> Detection:
    """Pick the category of an ORIGINAL release from its name and content. ``code`` is None when unsure."""

    words = tokens(name)
    exts = [e.lower() for e in extensions]
    counts = {
        "video": sum(e in VIDEO_EXT for e in exts),
        "mp3": sum(e in MP3_EXT for e in exts),
        "lossless": sum(e in LOSSLESS_EXT for e in exts),
        "ebook": sum(e in EBOOK_EXT for e in exts),
        "image": sum(e in IMAGE_EXT for e in exts),
        "disc": sum(e in DISC_IMAGE_EXT for e in exts),
    }
    hun = bool(words & HUN_TOKENS) or bool(hungarian_audio)
    suffix = "_hun" if hun else ""
    reasons = [f"magyar: {'igen' if hun else 'nem'} (" + ("név" if words & HUN_TOKENS else "hangsáv" if hungarian_audio else "nincs jel") + ")"]

    other_media = counts["mp3"] or counts["lossless"] or counts["ebook"]
    # Blu-ray/DVD structures carry AACS files such as CPSUnit00001.cci: no console or mobile signature inside a disc.
    disc = has_bdmv or has_video_ts
    # a packed (RAR) video release has no video files yet; the name still tells it apart from games
    is_video = has_bdmv or has_video_ts or counts["video"] > 0 or (not other_media and bool(words & (HD_TOKENS | DVD_TOKENS | SD_TOKENS)))
    if "XXX" in words:
        if is_video:
            quality = "xxx_hd" if has_bdmv or words & HD_TOKENS else "xxx_dvd" if has_video_ts or words & DVD_TOKENS else "xxx_xvid"
            return Detection(quality, reasons + ["XXX a névben"], [])
        if counts["image"]:
            return Detection("xxx_imageset", ["XXX a névben, képek"], [])
        return Detection("xxx_xvid", ["XXX a névben"], [])
    if not disc and set(exts) & {".apk", ".apks", ".xapk", ".aab", ".ipa", ".sis", ".sisx", ".jad"}:
        return Detection("mobil", ["mobil alkalmazáscsomag"], [])
    if not disc and set(exts) & {".nsp", ".nsz", ".xci", ".xcz", ".cia", ".3ds", ".cci", ".wbfs", ".rvz", ".gcm", ".xex", ".xbe", ".sfo"}:
        return Detection("console", ["konzol fájlformátum"], [])
    if words & {"PS1", "PS2", "PS3", "PS4", "PS5", "PSP", "PSV", "NSW", "XBOX", "XBOX360", "WII", "WIIU"}:
        return Detection("console", ["konzol platform a névben"], [])
    if (words & {"CONCERT", "KONCERT", "VIDEOCLIP"} or {"LIVE", "AT"} <= words) and counts["video"] and not SERIES_RE.search(name):
        return Detection("clip", reasons + ["koncert/klip jelölés és videó"], ["hd", "xvid"])
    if is_video:
        series = bool(SERIES_RE.search(name))
        if has_bdmv or words & HD_TOKENS:
            base = "hdser" if series else "hd"
        elif has_video_ts or (counts["disc"] and words & DVD_TOKENS):
            base = "dvdser" if series else "dvd9"  # wiki 30: DVD5 films also belong in DVD9
        else:
            base = "xvidser" if series else "xvid"
        reasons.append(("sorozat" if series else "film") + f", minőség: {base}")
        return Detection(base + suffix, reasons, ["clip"])
    if counts["lossless"] or counts["mp3"]:
        base = "lossless" if counts["lossless"] >= counts["mp3"] else "mp3"
        music_hun = hun or "HU" in words
        reasons[0] = f"magyar: {'igen' if music_hun else 'nem'}"
        return Detection(base + ("_hun" if music_hun else ""), reasons + [f"{base} fájlok"], [])
    if counts["ebook"]:
        return Detection("ebook" + suffix, reasons + ["e-book fájlok"], [])
    return Detection(None, ["nem egyértelmű (játék, program, konzol, mobil?): add meg: --category"], ["game_iso", "game_rip", "iso", "misc", "console", "mobil"])
