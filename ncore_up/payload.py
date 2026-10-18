"""Versioned payload rules from nCore Wiki 30, 139, 140, 142, 143 and 144.

KEEP/OMIT/BLOCK is deliberate: unknown installer dependencies and unsupported
main formats must never disappear merely to make an apparently valid torrent.
"""
from __future__ import annotations

import re
from pathlib import Path

from . import categories as cats, release as rel, safeio

RULE_VERSION = "2026-10-18-wiki426"
DISC_DIRS = {"bdmv", "certificate", "aacs", "bdsvm", "any!", "fab!"}
PROTECTED_DIRS = DISC_DIRS | {"video_ts", "audio_ts", "ps3_game", "ps3_update", "psp_game", "sddata", "android"}
SOFT_DIRS = {"crack", "cracks", "keygen", "patch", "fix", "medicine"}
NATIVE_PACKAGES = cats.EBOOK_EXT | {".apk", ".apks", ".xapk", ".aab", ".ipa", ".jar", ".msix", ".appx"}


def decision(release: rel.Release, item: rel.ReleaseFile, category: cats.Category, original: bool) -> tuple[str, str]:
    path = Path(item.relpath)
    ext, name = item.ext, path.name.casefold()
    parts = {p.casefold() for p in path.parts[:-1]}
    if ext in rel.PARTIAL_EXT:
        return "BLOCK", "befejezetlen letöltés"
    if name in rel.JUNK_FILES or ext == ".torrent" or "__macosx" in parts or name.startswith("._"):
        return "OMIT", "rendszer-/tracker-segédfájl"
    if original:
        return "KEEP", "eredeti release változatlan fájlja"
    if ext == ".nfo":
        return "OMIT", "NFO feldolgozva metaadatként"
    group = category.group
    if group in {"video", "clip"}:
        if release.has_bdmv and parts & DISC_DIRS:
            return "KEEP", "teljes Blu-ray struktúra"
        if ext in {".vob", ".ifo", ".bup"}:
            return "BLOCK", "DVD könyvtár helyett lemezkép szükséges"
        if ext in {".ts", ".m2ts", ".avi", ".m4v", ".mp4"}:
            return "BLOCK", "nem engedélyezett fő videóformátum; átkódolás nem történt"
        dvd = category.code.startswith(("dvd", "dvdser"))
        allowed = cats.IMAGE_EXT | (cats.DISC_IMAGE_EXT if dvd else {".mkv", ".srt", ".sub", ".idx"})
        if not dvd and (category.code.startswith("hd") or group == "clip"):
            allowed |= cats.DISC_IMAGE_EXT
        if ext == ".mkv" and dvd and "sample" in item.relpath.casefold():
            allowed.add(ext)
        if ext in {".mkv"} | cats.DISC_IMAGE_EXT and ext not in allowed:
            return "BLOCK", "a fő tartalom formátuma nem illik a kiválasztott kategóriához"
        return ("KEEP", "videó vagy engedélyezett kísérőfájl") if ext in allowed else ("OMIT", "nem megengedett videókísérő")
    if group == "music":
        lossless = category.code.startswith("lossless")
        audio = {".flac", ".ape", ".wav", ".dts", ".mka"} if lossless else {".mp3", ".m4a"}
        if ext in (cats.MP3_EXT | cats.LOSSLESS_EXT) - audio:
            return "BLOCK", "nem megengedett hangformátum; albumrészt nem hagyok ki"
        allowed = audio | cats.IMAGE_EXT | {".m3u", ".sfv", ".pdf"}
        if lossless:
            allowed |= cats.DISC_IMAGE_EXT | {".cue"}
            if name == "audiochecker.log":
                return "KEEP", "Audiochecker napló"
            if ext == ".txt":
                source = release.path / item.relpath if release.path.is_dir() else release.path
                text = safeio.read_file(source).decode("utf-8", "replace")
                if re.search(r"(?i)Exact Audio Copy|EAC extraction logfile|X Lossless Decoder|XLD extraction logfile|CUETools|Audiochecker|auCDtect", text):
                    return "KEEP", "lossless ripellenőrzési napló"
        return ("KEEP", "zene vagy engedélyezett kísérőfájl") if ext in allowed else ("OMIT", "nem megengedett zenekísérő")
    if group == "ebook":
        return ("KEEP", "könyvformátum") if ext in cats.EBOOK_EXT else ("OMIT", "könyvön kívüli kísérőfájl")
    if group in {"game", "program", "console", "mobil"}:
        if parts & SOFT_DIRS or name in {"serial.txt", "install.txt"}:
            return "KEEP", "telepítéshez szükséges melléklet"
        if group == "mobil":
            if ext in {".apk", ".apks", ".xapk", ".aab", ".ipa", ".sis", ".sisx", ".jar", ".jad", ".obb"} or parts & {"sddata", "android", "obb", "data"}:
                return "KEEP", "mobilcsomag / szükséges alkalmazásadat"
        elif group == "console":
            if ext in cats.DISC_IMAGE_EXT | {".nsp", ".nsz", ".xci", ".xcz", ".cia", ".3ds", ".cci", ".wbfs", ".rvz", ".gcm", ".pkg", ".pbp", ".cso", ".chd"} or parts & {"ps3_game", "ps3_update", "psp_game"} or name == "ps3_disc.sfb":
                return "KEEP", "konzolcsomag / lemezstruktúra"
        else:
            if ext in cats.DISC_IMAGE_EXT or (category.code != "iso" and ext in {".exe", ".msi"}):
                return "KEEP", "lemezkép / telepítő"
            if group == "game" and ("GOG" in cats.tokens(release.name) and parts & {"extras", "bonus", "goodies"}):
                return "KEEP", "GOG extra tartalom"
            if group == "game" and ext == ".bin" and name.startswith("setup"):
                return "KEEP", "játék telepítő adatrésze"
        if ext in cats.IMAGE_EXT | {".url", ".lnk", ".diz"} or (ext == ".txt" and name not in {"serial.txt", "install.txt"}):
            return "OMIT", "nem megengedett telepítőkísérő"
        return "BLOCK", "ismeretlen telepítő-/adatfájl: a szükségességét ellenőrizni kell"
    return "BLOCK", "ismeretlen kategória fájlszabálya"


def common_wrapper(paths: list[str]) -> tuple[str, ...]:
    """Remove only a common outer wrapper, never disc, album-disc or data roots."""
    parts = [Path(p).parts for p in paths]
    removed = []
    while parts and all(len(p) > 1 and p[0] == parts[0][0] for p in parts):
        name = parts[0][0]
        if name.casefold() in PROTECTED_DIRS | SOFT_DIRS or re.fullmatch(r"(?i)(?:cd|disc|disk)[ _.-]*\d+", name):
            break
        removed.append(name)
        parts = [p[1:] for p in parts]
    return tuple(removed)
