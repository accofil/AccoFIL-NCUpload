"""Console game identification from the files themselves, without decryption keys.

What can be read in plain form (offsets from the public format documentation: switchbrew, psdevwiki, LibOrbisPkg,
3dbrew, wiibrew, free60/xenia):

* Switch NSP/NSZ: PFS0 container (unencrypted file table). ``<rightsId>.tik`` names carry the title id; dumps made
  with authoring-tool data also hold ``*.cnmt.xml`` (type, id, version) and ``*.nacp.xml`` (title, display version).
  XCI/XCZ: only the cart header ("HEAD") is plain; the title id comes from the file name ("[0100…][v123]").
* PS4/PS5 PKG (big-endian): magic 7F 43 4E 54, content id at 0x40, entry table with an unencrypted param.sfo
  (entry 0x1000) and icon0.png (0x1200). PS3/PSP/Vita PKG: magic 7F 50 4B 47, content id at 0x30.
* PARAM.SFO (PS3 folders/ISO, PSP ISO, PS4 PKG), PS5 ``sce_sys/param.json``.
* PS1/PS2: SYSTEM.CNF BOOT line serial read straight from the ISO9660 file system of an ISO or raw BIN (2352-byte
  sectors), 7z as fallback for UDF-only images.
* Wii/GC: ID6 + title in the disc header (ISO/GCM, the copies in WBFS/WIA/RVZ/CISO headers), ``wit ID6`` fallback.
* 3DS: CIA → TMD title id/version + SMDH (title, publisher, region lockout); .3ds/.cci → NCSD/NCCH product code.
* Xbox 360: XDVDFS → default.xex execution info (title id, media id, disc) and region flags; GOD/XBLA STFS headers.

Nothing here runs a program from the release: only bytes are read (and 7z/wit read the image, never execute it).
Region codes: USA / EUR / JPN / ASIA / RF; ``region_label`` gives the nCore wording.
"""

from __future__ import annotations

import re
import shutil
import struct
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from .. import safeio

# platform code -> (nCore name tag, full name)
PLATFORMS = {
    "switch": ("NSW", "Nintendo Switch"),
    "ps5": ("PS5", "PlayStation 5"),
    "ps4": ("PS4", "PlayStation 4"),
    "ps3": ("PS3", "PlayStation 3"),
    "ps2": ("PS2", "PlayStation 2"),
    "ps1": ("PS1", "PlayStation"),
    "psp": ("PSP", "PlayStation Portable"),
    "vita": ("PSV", "PlayStation Vita"),
    "xbox360": ("XBOX360", "Xbox 360"),
    "xbox": ("XBOX", "Xbox"),
    "wii": ("Wii", "Nintendo Wii"),
    "wiiu": ("WiiU", "Nintendo Wii U"),
    "gc": ("GC", "Nintendo GameCube"),
    "3ds": ("3DS", "Nintendo 3DS"),
}
PLATFORM_ALIASES = {
    "nsw": "switch", "ns": "switch", "switch": "switch", "nintendo switch": "switch",
    "ps5": "ps5", "ps4": "ps4", "ps3": "ps3", "ps2": "ps2", "ps2dvd": "ps2", "ps2cd": "ps2", "ps1": "ps1", "psx": "ps1",
    "psone": "ps1", "psp": "psp", "psv": "vita", "vita": "vita", "psvita": "vita",
    "xbox360": "xbox360", "x360": "xbox360", "360": "xbox360", "xbox": "xbox", "xbox1": "xbox",
    "wii": "wii", "wiiu": "wiiu", "gc": "gc", "ngc": "gc", "gamecube": "gc", "3ds": "3ds",
}

REGION_LABEL = {
    "USA": "NTSC (USA)", "EUR": "PAL (EUR)", "RF": "Régiófüggetlen (RF)", "JPN": "NTSC-J (JPN)", "ASIA": "NTSC-J (ASIA)",
}

SWITCH_EXT = {".nsp", ".nsz", ".xci", ".xcz"}
PKG_EXT = {".pkg"}
N3DS_EXT = {".cia", ".3ds", ".cci", ".cxi"}
WII_EXT = {".wbfs", ".rvz", ".wia", ".ciso", ".wdf", ".gcm", ".gcz", ".nkit"}
DISC_EXT = {".iso", ".bin", ".img", ".chd", ".cso", ".mdf"}
ARCHIVE_EXT = {".rar", ".7z", ".zip"}
CONTENT_EXT = SWITCH_EXT | PKG_EXT | N3DS_EXT | WII_EXT | DISC_EXT | {".xex", ".xbe", ".sfo"}
HEAD_LIMIT = 64 * 1024 * 1024  # bytes streamed from a packed image (enough for headers, file systems, SYSTEM.CNF)


@dataclass
class ConsoleInfo:
    platform: str = ""  # key of PLATFORMS
    format: str = ""  # NSP, NSZ, XCI, PKG, ISO, BIN/CUE, CHD, WBFS, RVZ, CIA, 3DS, GOD, mappa ...
    title: str = ""  # from the files (PARAM.SFO TITLE, disc header, SMDH, STFS) or the release name
    title_from_name: bool = False  # True when ``title`` is only the cleaned release name
    title_id: str = ""  # Switch/3DS 16 hex, Xbox 360 8 hex, PS3/PS4/PS5 "BLES01807" / "CUSA00917"
    base_title_id: str = ""  # Switch/3DS base application of an update/DLC
    content_id: str = ""  # PS3/PS4/PS5 "EP9000-CUSA00917_00-UNCHARTED4000000"
    serial: str = ""  # PS1/PS2/PSP/PS3 disc serial "SLES-50330"
    id6: str = ""  # Wii/GC "RMCP01"
    product_code: str = ""  # 3DS "CTR-P-AREP", Switch "HAC-P-AAACA"
    media_id: str = ""  # Xbox 360
    content_type: str = ""  # base / update / dlc
    version: str = ""  # display version: "1.05", "1.0.2", "v65536"
    version_int: int | None = None  # Switch/3DS/Xbox numeric version
    region: str = ""  # USA / EUR / JPN / ASIA / RF
    languages: list[str] = field(default_factory=list)  # two-letter codes of the game's languages seen in the files
    title_languages: list[str] = field(default_factory=list)  # PARAM.SFO TITLE_xx: localized titles, a hint only
    hungarian: bool = False
    sfo: dict = field(default_factory=dict)
    icon: bytes = b""  # PS4 icon0.png
    contents: list[dict] = field(default_factory=list)  # every identified item: {"file", "title_id", "type", "version"}
    files: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    @property
    def tag(self) -> str:
        return PLATFORMS.get(self.platform, ("", ""))[0]

    @property
    def platform_name(self) -> str:
        return PLATFORMS.get(self.platform, ("", self.platform))[1]

    def note(self, source: str) -> None:
        if source and source not in self.sources:
            self.sources.append(source)


# --- byte access ---------------------------------------------------------------------------------------------

class Blob:
    """Random access to a file on disk or to a prefix of it (a member streamed out of an archive)."""

    def __init__(self, path: Path | None = None, data: bytes | None = None, size: int | None = None):
        self.path = path
        self.data = data
        self.size = size if size is not None else (len(data) if data is not None else (path.stat().st_size if path else 0))

    def read(self, offset: int, size: int) -> bytes:
        if offset < 0 or size <= 0:
            return b""
        if self.data is not None:
            return self.data[offset:offset + size]
        try:
            with open(self.path, "rb") as handle:
                handle.seek(offset)
                return handle.read(size)
        except OSError:
            return b""


def _u16le(data: bytes, offset: int) -> int:
    return struct.unpack_from("<H", data, offset)[0] if len(data) >= offset + 2 else 0


def _u32le(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0] if len(data) >= offset + 4 else 0


def _u32be(data: bytes, offset: int) -> int:
    return struct.unpack_from(">I", data, offset)[0] if len(data) >= offset + 4 else 0


def _cstr(data: bytes, encoding: str = "utf-8") -> str:
    return data.split(b"\0", 1)[0].decode(encoding, "replace").strip()


# --- regions -------------------------------------------------------------------------------------------------

def region_label(code: str) -> str:
    return REGION_LABEL.get(code, "")


def region_warning(code: str) -> str:
    if code in {"JPN", "ASIA"}:
        return (f"{'japán' if code == 'JPN' else 'ázsiai'} kiadás: az nCore régiómezője csak NTSC (USA) / PAL (EUR) / "
                f"Régiófüggetlen (RF) értéket ismer — „{REGION_LABEL[code]}” címkét adtam, ellenőrizd")
    return ""


_SERIAL_REGION = {"U": "USA", "E": "EUR", "P": "JPN", "J": "JPN", "K": "ASIA", "A": "ASIA", "H": "ASIA", "C": "ASIA"}
_VITA_REGION = {"PCSA": "USA", "PCSE": "USA", "PCSB": "EUR", "PCSF": "EUR", "PCSG": "JPN", "PCSC": "JPN", "PCSH": "ASIA", "PCSD": "ASIA"}


def region_from_serial(serial: str) -> str:
    """SLUS/SCUS/BLUS/NPUB/ULUS → USA, SLES/BCES/NPEB/ULES → EUR, SLPS/SLPM/BLJM/ULJS → JPN, SLKA/BLAS/NPHB → ASIA."""

    prefix = re.sub(r"[^A-Z]", "", (serial or "").upper())[:4]
    if len(prefix) < 4:
        return ""
    if prefix in _VITA_REGION:
        return _VITA_REGION[prefix]
    if prefix in {"SCED", "SLED"}:
        return "EUR"
    if prefix.startswith(("CUSA", "PPSA")):
        return ""  # PS4/PS5 title ids carry no region; the content id prefix does
    return _SERIAL_REGION.get(prefix[2], "")


def region_from_content_id(content_id: str) -> str:
    """PS3/PS4/PS5 content id prefix: UP → USA, EP → EUR, JP → JPN, HP/KP → ASIA."""

    return {"UP": "USA", "EP": "EUR", "JP": "JPN", "HP": "ASIA", "KP": "ASIA"}.get((content_id or "")[:2].upper(), "")


def region_from_id6(id6: str) -> str:
    """Wii/GC region letter (4th character of the ID6)."""

    if len(id6 or "") < 4:
        return ""
    letter = id6[3].upper()
    if letter in "EN":
        return "USA"
    if letter == "J":
        return "JPN"
    if letter in "KQTW":
        return "ASIA"
    if letter == "A":
        return "RF"
    if letter in "PDFSIXYZULMHRV":
        return "EUR"
    return ""


def region_from_product_code(code: str) -> str:
    """3DS/Switch product code ("CTR-P-AREP", "HAC-P-AAACA"): the last letter is the region (A = all regions)."""

    code = re.sub(r"[^A-Z0-9]", "", (code or "").upper())
    if not code:
        return ""
    letter = code[-1]
    if letter == "A":
        return "RF"
    if letter == "E":
        return "USA"
    if letter == "J":
        return "JPN"
    if letter in "KCWT":
        return "ASIA"
    if letter in "PDFSIXYZUVHR":
        return "EUR"
    return ""


_DAT_EUR = {"europe", "uk", "germany", "france", "spain", "italy", "netherlands", "sweden", "scandinavia", "australia",
            "russia", "poland", "portugal", "greece", "austria", "switzerland", "belgium", "denmark", "finland", "norway",
            "ireland", "croatia", "czech", "hungary", "new zealand", "pal"}
_DAT_USA = {"usa", "canada", "brazil", "latin america", "mexico", "ntsc-u"}
_DAT_ASIA = {"asia", "korea", "china", "taiwan", "hong kong", "singapore", "thailand"}


def region_from_dat(text: str) -> str:
    """Redump/No-Intro/GameTDB region words ("Europe, Australia", "USA", "Japan", "World", "PAL", "NTSC-U", "ALL")."""

    words = [w.strip().lower() for w in re.split(r"[,/]", text or "") if w.strip()]
    found = set()
    for word in words:
        if word in {"world", "all", "region free", "rf"}:
            return "RF"
        if word in _DAT_EUR:
            found.add("EUR")
        elif word in _DAT_USA:
            found.add("USA")
        elif word in {"japan", "ntsc-j", "jpn"}:
            found.add("JPN")
        elif word in _DAT_ASIA or word == "ntsc-k":
            found.add("ASIA")
        elif word in {"eur"}:
            found.add("EUR")
    if len(found) > 1 and {"USA", "EUR"} <= found:
        return "RF"
    return sorted(found, key=["EUR", "USA", "JPN", "ASIA"].index)[0] if found else ""


def region_from_3ds_lockout(mask: int) -> str:
    if mask in (0x7FFFFFFF, 0xFFFFFFFF) or (mask & 0x7) == 0x7:
        return "RF"
    if mask & 0x4 or mask & 0x8:
        return "EUR" if not mask & 0x2 else "RF"
    if mask & 0x2:
        return "USA"
    if mask & 0x1:
        return "JPN"
    if mask & 0x70:
        return "ASIA"
    return ""


def region_from_xex(flags: int) -> str:
    """Xbox 360 XEX region flags: NTSC-U 0x000000FF, NTSC-J 0x0000FF00, PAL 0x00FF0000, all 0xFFFFFFFF."""

    groups = [g for g, mask in (("USA", 0xFF), ("JPN", 0xFF00), ("EUR", 0xFF0000)) if flags & mask]
    if flags == 0xFFFFFFFF or len(groups) == 3:
        return "RF"
    if len(groups) == 1:
        return groups[0]
    return "RF" if groups else ""


# --- release names -------------------------------------------------------------------------------------------

_PLATFORM_TOKENS = {
    "NSW": "switch", "SWITCH": "switch", "PS5": "ps5", "PS4": "ps4", "PS3": "ps3", "PS2": "ps2", "PS2DVD": "ps2", "PS2CD": "ps2",
    "PSX": "ps1", "PS1": "ps1", "PSP": "psp", "PSV": "vita", "VITA": "vita", "PSVITA": "vita", "XBOX360": "xbox360", "X360": "xbox360",
    "XBOX": "xbox", "WII": "wii", "WIIU": "wiiu", "NGC": "gc", "GAMECUBE": "gc", "GC": "gc", "3DS": "3ds",
}
_PLATFORM_UPPER_ONLY = {"SWITCH", "XBOX", "GC", "VITA"}  # ordinary words in titles unless written in capitals
_REGION_TOKENS = {
    "PAL": "EUR", "EUR": "EUR", "EUROPE": "EUR", "NTSC": "USA", "USA": "USA", "NTSCU": "USA", "JPN": "JPN", "JAP": "JPN",
    "JAPAN": "JPN", "NTSCJ": "JPN", "RF": "RF", "REGIONFREE": "RF", "ASIA": "ASIA", "KOR": "ASIA",
}
# Tags that end the title part of a scene name: any case / only when written in capitals.
_CUT_ANY_CASE = {"UPDATE", "UPD", "PATCH", "DLC", "PROPER", "REPACK", "INTERNAL", "READNFO", "DIRFIX", "NSFIX", "MULTI", "BACKPORT"}
_CUT_UPPER = {
    "HUN", "HUNGARIAN", "MAGYAR", "ENG", "FAKE", "PKG", "NSP", "NSZ", "XCI", "ISO", "CIA", "WBFS", "RVZ", "JB",
    "HEN", "CFW", "GOLDHEN", "EBOOT", "DVD", "DVD5", "DVD9", "XGD2", "XGD3", "RGH", "JTAG", "GOD", "XBLA", "NKIT", "REGION",
} | set(_PLATFORM_TOKENS) | set(_REGION_TOKENS)


def _is_cut(word: str) -> bool:
    bare = re.sub(r"[^A-Za-z0-9]", "", word)
    upper = bare.upper()
    if re.fullmatch(r"(?i)multi\d*", bare) or upper in _CUT_ANY_CASE:
        return True
    return upper in _CUT_UPPER and bare.isupper()

SWITCH_ID_RE = re.compile(r"(?i)(?<![0-9a-f])(01[0-9a-f]{14})(?![0-9a-f])")
SWITCH_VER_RE = re.compile(r"(?i)\[v(\d+)\]")
DISPLAY_VER_RE = re.compile(r"(?i)(?<![a-z0-9])v(\d{1,3}(?:\.\d{1,4}){1,3})(?![0-9])")
PS_TITLE_RE = re.compile(r"(?<![A-Z])((?:CUSA|PPSA|CUSB|PCAS|PLAS|BLES|BLUS|BCES|BCUS|BLJM|BLJS|BCJS|BLAS|BCAS|BLKS|NPEB|NPUB|NPEA|NPUA|NPJB|NPHB)\d{5})(?!\d)")
CONTENT_ID_RE = re.compile(r"(?<![A-Z0-9])([A-Z]{2}\d{4}-[A-Z]{4}\d{5}_\d{2}-[A-Z0-9]{16})(?![A-Z0-9])")  # "_" may border it
SERIAL_RE = re.compile(r"(?i)(?<![a-z])(S[LC][UEPKAC][SMDA]|U[LC][UEJAK][SM]|BL[UEJAK]S|BC[UEJAK]S|PBP[XA]|PAPX)[-_ ]?(\d{3})[._]?(\d{2})(?!\d)")
ID6_RE = re.compile(r"\[([RSGDWHE][A-Z0-9]{2}[A-Z][0-9A-Z]{2})\]")
PS4_APPVER_RE = re.compile(r"(?i)-A(\d{2})(\d{2})-V(\d{2})(\d{2})")


def normalize_serial(serial: str) -> str:
    """"SLES_503.30", "SLES-50330/EURO", "sles50330" → "SLES-50330"."""

    match = re.match(r"(?i)\s*([A-Z]{4})[-_ ]?(\d{3})[._]?(\d{2})", serial or "")
    return f"{match.group(1).upper()}-{match.group(2)}{match.group(3)}" if match else ""


def parse_name(name: str) -> dict:
    """Identifiers and tags in a release/file name: {"title", "platform", "region", "title_id", "version_int",
    "version", "content_id", "serial", "id6", "content_type", "hungarian"} (only the keys found)."""

    out: dict = {}
    text = name or ""
    raw_tokens = [t for t in re.split(r"[ ._\-()\[\]{}+,]+", text) if t]
    tokens = [t.upper() for t in raw_tokens]
    for raw, token in zip(raw_tokens, tokens):
        if token in _PLATFORM_TOKENS and "platform" not in out and (raw.isupper() or token not in _PLATFORM_UPPER_ONLY):
            out["platform"] = _PLATFORM_TOKENS[token]
        if token in _REGION_TOKENS and "region" not in out and raw.isupper():
            out["region"] = _REGION_TOKENS[token]
    if re.search(r"(?i)(?<![a-z])(ntsc[-_. ]?j)(?![a-z])", text):
        out["region"] = "JPN"
    if re.search(r"(?i)(?<![a-z])(region[-_. ]?free)(?![a-z])", text):
        out["region"] = "RF"
    if match := SWITCH_ID_RE.search(text):
        out["title_id"] = match.group(1).upper()
    if match := SWITCH_VER_RE.search(text):
        out["version_int"] = int(match.group(1))
    if match := PS4_APPVER_RE.search(text):
        out["version"] = f"{int(match.group(1))}.{match.group(2)}"
    elif match := DISPLAY_VER_RE.search(text):
        out["version"] = match.group(1)
    if match := CONTENT_ID_RE.search(text.upper()):
        out["content_id"] = match.group(1)
        out["title_id"] = match.group(1)[7:16]
    elif match := PS_TITLE_RE.search(text.upper()):
        out.setdefault("title_id", match.group(1))
    if match := SERIAL_RE.search(text):
        out["serial"] = f"{match.group(1).upper()}-{match.group(2)}{match.group(3)}"
    if match := ID6_RE.search(text):
        out["id6"] = match.group(1)
    upper = set(tokens)
    if upper & {"DLC", "ADDON"}:
        out["content_type"] = "dlc"
    elif upper & {"UPDATE", "UPD", "PATCH"}:
        out["content_type"] = "update"
    if "HUN" in raw_tokens or upper & {"HUNGARIAN", "MAGYAR"}:  # "Hun" alone is an ordinary word ("Attila the Hun")
        out["hungarian"] = True
    out["title"] = clean_title(text)
    return out


def clean_title(name: str) -> str:
    """Readable game title from a scene/file name: "Super_Mario_Odyssey_NSW-BigBlueBox" → "Super Mario Odyssey"."""

    text = re.sub(r"\.(nsp|nsz|xci|xcz|pkg|iso|cia|3ds|wbfs|rvz|wia|chd|bin|cue|apk|ipa)$", "", name or "", flags=re.I)
    text = re.sub(r"\[[^\]]*\]|\{[^}]*\}", " ", text)
    text = re.sub(r"\((?:[^)]*(?:v\d|Rev|Disc|En,|Europe|USA|Japan|World|Asia|Korea|Germany|France|Spain|Italy|Australia|UK)[^)]*)\)",
                  " ", text, flags=re.I)
    text = re.sub(r"(?i)\(\s*\d+(?:[.,]\d+)?\s*[KMGT]i?B\s*\)", " ", text)  # file size: "(16.95 GB)"
    group = re.search(r"[._ ]([A-Za-z0-9]+)-[A-Za-z0-9]{2,15}$", text)
    if group and (_is_cut(group.group(1)) or DISPLAY_VER_RE.fullmatch(group.group(1)) or re.fullmatch(r"\d{4}", group.group(1))):
        text = text[:text.rfind("-")]  # scene group suffix after the tags: "..._PS4-DUPLEX"
    text = CONTENT_ID_RE.sub(" ", text)
    text = PS_TITLE_RE.sub(" ", text)
    text = SERIAL_RE.sub(" ", text)
    text = PS4_APPVER_RE.sub(" ", text)
    text = DISPLAY_VER_RE.sub(" ", text)
    words: list[str] = []
    for word in re.split(r"[ ._]+", text):
        if not word:
            continue
        if words and _is_cut(word):
            break
        words.append(word)
    return re.sub(r"\s+", " ", " ".join(words)).strip(" -")


# --- Switch ---------------------------------------------------------------------------------------------------

def parse_pfs0(data: bytes) -> list[tuple[str, int, int]]:
    """[(name, absolute offset, size)] of a PFS0 container (NSP/NSZ), [] when it is not one or the header is cut."""

    if data[:4] != b"PFS0" or len(data) < 0x10:
        return []
    count, table_size = _u32le(data, 4), _u32le(data, 8)
    entries_end = 0x10 + count * 0x18
    if count > 10000 or len(data) < entries_end + table_size:
        return []
    strings = data[entries_end:entries_end + table_size]
    data_start = entries_end + table_size
    files = []
    for index in range(count):
        offset, size, name_offset = struct.unpack_from("<QQI", data, 0x10 + index * 0x18)
        name = _cstr(strings[name_offset:], "utf-8")
        files.append((name, data_start + offset, size))
    return files


def parse_hfs0(data: bytes, base: int = 0) -> list[tuple[str, int, int]]:
    """[(name, absolute offset, size)] of an HFS0 partition (XCI) whose header starts at ``base`` in the file."""

    if data[:4] != b"HFS0" or len(data) < 0x10:
        return []
    count, table_size = _u32le(data, 4), _u32le(data, 8)
    entries_end = 0x10 + count * 0x40
    if count > 10000 or len(data) < entries_end + table_size:
        return []
    strings = data[entries_end:entries_end + table_size]
    data_start = base + entries_end + table_size
    files = []
    for index in range(count):
        offset, size, name_offset = struct.unpack_from("<QQI", data, 0x10 + index * 0x40)
        files.append((_cstr(strings[name_offset:]), data_start + offset, size))
    return files


XCI_SIZES = {0xFA: "1 GB", 0xF8: "2 GB", 0xF0: "4 GB", 0xE0: "8 GB", 0xE1: "16 GB", 0xE2: "32 GB"}


def parse_xci_header(data: bytes) -> dict:
    """{"key_area": bool, "rom_size", "hfs0_offset", "hfs0_size", "package_id"} of an XCI cart image header."""

    for base in (0, 0x1000):
        if data[base + 0x100:base + 0x104] == b"HEAD":
            head = data[base:base + 0x200]
            return {
                "key_area": base == 0x1000,
                "rom_size": XCI_SIZES.get(head[0x10D], ""),
                "hfs0_offset": base + struct.unpack_from("<Q", head, 0x130)[0],
                "hfs0_size": struct.unpack_from("<Q", head, 0x138)[0],
            }
    return {}


def switch_kind(title_id: str) -> tuple[str, str]:
    """(content type, base title id): …000 base, …800 update, anything else DLC of (id − 0x1000) & ~0xFFF."""

    try:
        value = int(title_id, 16)
    except (TypeError, ValueError):
        return "", ""
    low = value & 0xFFF
    if low == 0:
        return "base", f"{value:016X}"
    if low == 0x800:
        return "update", f"{value & ~0xFFF:016X}"
    return "dlc", f"{(value - 0x1000) & ~0xFFF:016X}"


def switch_version(version: int | None) -> str:
    """Update number in the usual name form: 0 → "", 65536 → "v65536"."""

    return f"v{version}" if version else ""


def parse_cnmt_xml(text: str) -> dict:
    """{"type", "title_id", "version", "required_system_version"} from an authoring-tool ``*.cnmt.xml``."""

    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return {}

    def value(tag: str) -> str:
        node = root.find(tag)
        return (node.text or "").strip() if node is not None else ""

    title_id = value("Id").lower().removeprefix("0x").upper()
    version = value("Version")
    return {
        "type": value("Type"),
        "title_id": title_id,
        "version": int(version) if version.isdigit() else None,
        "required_system_version": value("RequiredSystemVersion"),
    }


def parse_nacp_xml(text: str) -> dict:
    """{"title", "publisher", "display_version", "languages"} from an authoring-tool ``*.nacp.xml``."""

    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return {}
    titles = {}
    for node in root.findall("Title"):
        language = (node.findtext("Language") or "").strip()
        titles[language] = ((node.findtext("Name") or "").strip(), (node.findtext("Publisher") or "").strip())
    name, publisher = titles.get("AmericanEnglish") or titles.get("BritishEnglish") or next(iter(titles.values()), ("", ""))
    return {
        "title": name,
        "publisher": publisher,
        "display_version": (root.findtext("DisplayVersion") or "").strip(),
        "languages": [x.text.strip() for x in root.findall("SupportedLanguage") if x.text],
    }


def read_switch(blob: Blob, filename: str) -> dict:
    """Ids of one NSP/NSZ/XCI/XCZ: {"format", "items": [{"title_id", "type", "version", "base"}], "title", "publisher",
    "display_version", "rom_size"}; the name tags fill what the container does not hold."""

    ext = Path(filename).suffix.lower()
    out: dict = {"format": ext.lstrip(".").upper(), "items": []}
    head = blob.read(0, 0x40000)
    files = parse_pfs0(head)
    if ext in {".xci", ".xcz"} or (not files and parse_xci_header(head)):
        header = parse_xci_header(head)
        out["format"] = "XCZ" if ext == ".xcz" else "XCI"
        out["rom_size"] = header.get("rom_size", "")
        files = []
    by_id: dict[str, dict] = {}
    for name, offset, size in files:
        lower = name.lower()
        if lower.endswith((".tik", ".cert")) and re.fullmatch(r"[0-9a-f]{32}", lower.rsplit(".", 1)[0]):
            title_id = lower[:16].upper()
            by_id.setdefault(title_id, {"title_id": title_id})
        elif lower.endswith(".cnmt.xml") and size < 1 << 20:
            meta = parse_cnmt_xml(blob.read(offset, size).decode("utf-8", "replace"))
            if meta.get("title_id"):
                item = by_id.setdefault(meta["title_id"], {"title_id": meta["title_id"]})
                item["version"] = meta.get("version")
                item["meta_type"] = meta.get("type", "")
        elif lower.endswith(".nacp.xml") and size < 1 << 20:
            nacp = parse_nacp_xml(blob.read(offset, size).decode("utf-8", "replace"))
            out.update({k: v for k, v in nacp.items() if v and k in {"title", "publisher", "display_version"}})
    if files:
        out["files"] = [name for name, _o, _s in files]
    tags = parse_name(filename)
    if not by_id and tags.get("title_id"):
        by_id[tags["title_id"]] = {"title_id": tags["title_id"], "from_name": True}
    for item in by_id.values():
        if len(by_id) == 1:
            if item.get("version") is None and tags.get("version_int") is not None:
                item["version"] = tags["version_int"]
            item["display_version"] = out.get("display_version") or tags.get("version", "")
        item["type"], item["base"] = switch_kind(item["title_id"])
        out["items"].append(item)
    return out


# --- PlayStation ----------------------------------------------------------------------------------------------

def parse_sfo(data: bytes) -> dict:
    """PARAM.SFO (PS3/PS4/PSP/Vita) as {KEY: str|int}."""

    if data[:4] != b"\0PSF" or len(data) < 0x14:
        return {}
    key_table, data_table, count = struct.unpack_from("<III", data, 8)
    out: dict = {}
    for index in range(min(count, 512)):
        base = 0x14 + index * 16
        if len(data) < base + 16:
            break
        key_offset, fmt, length, _max, data_offset = struct.unpack_from("<HHIII", data, base)
        start = key_table + key_offset
        end = data.find(b"\0", start)
        if end < 0:
            break
        key = data[start:end].decode("ascii", "replace")
        raw = data[data_table + data_offset:data_table + data_offset + length]
        if fmt == 0x0404:
            out[key] = struct.unpack("<I", raw[:4])[0] if len(raw) >= 4 else 0
        else:
            out[key] = _cstr(raw)
    return out


PS4_MAGIC = b"\x7fCNT"
PS3_MAGIC = b"\x7fPKG"
PS4_ENTRY_SFO = 0x1000
PS4_ENTRY_ICON0 = 0x1200
PS3_PKG_TYPES = {
    0x4: "GameData", 0x5: "GameExec", 0x6: "PS1emu", 0x7: "PSP", 0x9: "Theme", 0xA: "Widget", 0xB: "License",
    0xD: "Avatar", 0xE: "PSPgo", 0xF: "minis", 0x10: "NEOGEO", 0x12: "PS2classic", 0x14: "PSPremastered",
    0x15: "VitaApp", 0x16: "VitaDLC", 0x17: "VitaLiveArea",
}


def parse_ps4_pkg_header(data: bytes) -> dict:
    """{"content_id", "entry_count", "table_offset", "content_type", "content_flags", "body_size"} of a PS4/PS5 PKG."""

    if data[:4] != PS4_MAGIC or len(data) < 0x80:
        return {}
    return {
        "entry_count": _u32be(data, 0x10),
        "table_offset": _u32be(data, 0x18),
        "body_size": struct.unpack_from(">Q", data, 0x28)[0],
        "content_id": _cstr(data[0x40:0x64], "ascii"),
        "drm_type": _u32be(data, 0x70),
        "content_type": _u32be(data, 0x74),
        "content_flags": _u32be(data, 0x78),
    }


def parse_ps4_entries(table: bytes, count: int) -> list[dict]:
    entries = []
    for index in range(min(count, 4096)):
        base = index * 32
        if len(table) < base + 32:
            break
        entry_id, name_offset, flags1, flags2, offset, size = struct.unpack_from(">IIIIII", table, base)
        entries.append({"id": entry_id, "name_offset": name_offset, "flags1": flags1, "flags2": flags2, "offset": offset, "size": size})
    return entries


def read_ps4_pkg(blob: Blob) -> dict:
    """{"content_id", "sfo", "icon"} of a PS4/PS5 PKG; the param.sfo and icon0.png entries are not encrypted."""

    header = parse_ps4_pkg_header(blob.read(0, 0x1000))
    if not header:
        return {}
    out = {"content_id": header["content_id"], "sfo": {}, "icon": b""}
    count = min(header["entry_count"], 4096)  # a damaged header must not ask for gigabytes
    entries = parse_ps4_entries(blob.read(header["table_offset"], count * 32), count)
    for entry in entries:
        if entry["id"] == PS4_ENTRY_SFO and 0 < entry["size"] < 1 << 20:
            out["sfo"] = parse_sfo(blob.read(entry["offset"], entry["size"]))
        elif entry["id"] == PS4_ENTRY_ICON0 and 0 < entry["size"] < 4 << 20:
            icon = blob.read(entry["offset"], entry["size"])
            out["icon"] = icon if icon[:8] == b"\x89PNG\r\n\x1a\n" else b""
    return out


def parse_ps3_pkg_header(data: bytes) -> dict:
    """{"content_id", "pkg_type", "retail", "total_size", "content_type"} of a PS3/PSP/Vita PKG (plain header part)."""

    if data[:4] != PS3_MAGIC or len(data) < 0x60:
        return {}
    revision, pkg_type = struct.unpack_from(">HH", data, 4)
    meta_offset, meta_count = _u32be(data, 8), _u32be(data, 0x0C)
    content_type = 0
    pos = meta_offset
    for _ in range(min(meta_count, 64)):
        if len(data) < pos + 8:
            break
        meta_id, size = struct.unpack_from(">II", data, pos)
        if meta_id == 2 and size >= 4:
            content_type = _u32be(data, pos + 8)
        pos += 8 + size
    return {
        "content_id": _cstr(data[0x30:0x54], "ascii"),
        "pkg_type": pkg_type,
        "retail": revision == 0x8000,
        "total_size": struct.unpack_from(">Q", data, 0x18)[0],
        "content_type": PS3_PKG_TYPES.get(content_type, str(content_type) if content_type else ""),
    }


def parse_param_json(text: str) -> dict:
    """PS5 ``sce_sys/param.json``: {"title_id", "content_id", "version", "title", "category"}."""

    import json

    try:
        data = json.loads(text)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    localized = data.get("localizedParameters") if isinstance(data.get("localizedParameters"), dict) else {}
    default = str(localized.get("defaultLanguage") or "en-US")
    entry = localized.get(default) or localized.get("en-US") or {}
    title = str((entry.get("titleName") if isinstance(entry, dict) else "") or "").strip()
    version = str(data.get("contentVersion") or data.get("masterVersion") or "")
    return {
        "title_id": str(data.get("titleId") or ""),
        "content_id": str(data.get("contentId") or ""),
        "version": version,
        "title": title,
        "category": str(data.get("applicationCategoryType", "")),
        "languages": [k for k in localized if k != "defaultLanguage"],
    }


def ps_version(value: str) -> str:
    """"01.05" → "1.05", "01.000.000" → "1.000.000"."""

    match = re.match(r"0*(\d+)\.(\d+(?:\.\d+)?)", value or "")
    return f"{match.group(1)}.{match.group(2)}" if match else ""


def sfo_kind(category: str) -> str:
    category = (category or "").lower()
    if category in {"gp", "patch"}:
        return "update"
    if category in {"ac", "dlc"}:
        return "dlc"
    return "base" if category else ""


# SFO TITLE_xx language indices (psdevwiki): 00 Japanese, 01 English (US), 02 French, 03 Spanish, 04 German,
# 05 Italian, 06 Dutch, 07 Portuguese, 08 Russian, 09 Korean, 10 Trad. Chinese, 11 Simpl. Chinese, 12 Finnish,
# 13 Swedish, 14 Danish, 15 Norwegian, 16 Polish, 17 Portuguese (BR), 18 English (UK), 19 Turkish, 20 Spanish (LA),
# 21 Arabic, 22 French (CA), 23 Czech, 24 Hungarian, 25 Greek, 26 Romanian, 27 Thai, 28 Vietnamese, 29 Indonesian.
SFO_LANGUAGES = ["ja", "en", "fr", "es", "de", "it", "nl", "pt", "ru", "ko", "zh", "zh", "fi", "sv", "da", "no", "pl", "pt",
                 "en", "tr", "es", "ar", "fr", "cs", "hu", "el", "ro", "th", "vi", "id"]


def sfo_languages(sfo: dict) -> list[str]:
    """Languages with a localized TITLE_xx in a PARAM.SFO (a hint only: a localized title is not a translated game)."""

    found = []
    for key in sfo:
        match = re.fullmatch(r"TITLE_(\d\d)", key)
        if match and int(match.group(1)) < len(SFO_LANGUAGES) and sfo[key]:
            code = SFO_LANGUAGES[int(match.group(1))]
            if code not in found:
                found.append(code)
    return found


# --- ISO9660 (PS1/PS2/PS3/PSP discs) ---------------------------------------------------------------------------

SYNC = b"\x00" + b"\xff" * 10 + b"\x00"


class Iso9660:
    """Minimal ISO9660 reader over 2048-byte images and raw 2352-byte BIN images (Mode 1 / Mode 2 Form 1)."""

    def __init__(self, blob: Blob):
        self.blob = blob
        first = blob.read(0, 16)
        if first[:12] == SYNC:
            mode = blob.read(15, 1)
            self.stride, self.skip = 2352, (24 if mode == b"\x02" else 16)
        else:
            self.stride, self.skip = 2048, 0
        self.root: tuple[int, int] | None = None
        pvd = self.sector(16)
        if pvd[1:6] == b"CD001" and pvd[0] == 1:
            self.root = (_u32le(pvd, 156 + 2), _u32le(pvd, 156 + 10))
            self.volume_id = _cstr(pvd[40:72], "ascii")

    @property
    def valid(self) -> bool:
        return self.root is not None

    def sector(self, lba: int, count: int = 1) -> bytes:
        if self.stride == 2048:
            return self.blob.read(lba * 2048, count * 2048)
        return b"".join(self.blob.read((lba + i) * self.stride + self.skip, 2048) for i in range(count))

    def listdir(self, extent: tuple[int, int]) -> list[tuple[str, int, int, bool]]:
        lba, size = extent
        data = self.sector(lba, max(1, min((size + 2047) // 2048, 64)))
        entries, pos = [], 0
        while pos < min(len(data), size):
            length = data[pos]
            if length == 0:
                pos = (pos // 2048 + 1) * 2048
                continue
            record = data[pos:pos + length]
            if len(record) < 34:
                break
            name_len = record[32]
            raw = record[33:33 + name_len]
            if raw not in (b"\x00", b"\x01"):
                name = raw.decode("ascii", "replace").split(";")[0].rstrip(".")
                entries.append((name, _u32le(record, 2), _u32le(record, 10), bool(record[25] & 2)))
            pos += length
        return entries

    def find(self, path: str) -> tuple[int, int, bool] | None:
        if not self.root:
            return None
        current: tuple[int, int, bool] = (*self.root, True)
        for part in [p for p in path.replace("\\", "/").split("/") if p]:
            if not current[2]:
                return None
            match = [e for e in self.listdir(current[:2]) if e[0].upper() == part.upper()]
            if not match:
                return None
            current = match[0][1:]
        return current

    def read_file(self, path: str, limit: int = 1 << 20) -> bytes:
        found = self.find(path)
        if not found or found[2]:
            return b""
        lba, size, _ = found
        size = min(size, limit)
        return self.sector(lba, (size + 2047) // 2048)[:size]


def system_cnf_serial(text: str) -> tuple[str, str]:
    """(serial, platform) from SYSTEM.CNF: "BOOT2 = cdrom0:\\SLUS_200.62;1" → ("SLUS-20062", "ps2")."""

    match = re.search(r"(?im)^\s*BOOT(2?)\s*=\s*cdrom0?:\\*([A-Z]{4})[_-](\d{3})\.(\d{2})", text or "")
    if not match:
        return "", ""
    return f"{match.group(2).upper()}-{match.group(3)}{match.group(4)}", ("ps2" if match.group(1) else "ps1")


def _seven_zip_member(archive: Path, member: str, limit: int = 1 << 20) -> bytes:
    """Up to ``limit`` bytes of one member of an image or archive, streamed by 7z (never executed)."""

    tool = shutil.which("7z") or shutil.which("7za")
    if not tool:
        return b""
    try:
        result = safeio.run([tool, "e", "-so", "-pnone", "-spd", "--", str(archive), member],
                            timeout=120, stdout_limit=limit, prefix_only=True)
        return result.stdout
    except (OSError, subprocess.SubprocessError):
        return b""


def seven_zip_list(archive: Path) -> list[tuple[str, int]]:
    """[(member path, size)] of an archive or image via ``7z l -slt``; [] without 7z."""

    tool = shutil.which("7z") or shutil.which("7za")
    if not tool:
        return []
    try:
        run = safeio.run([tool, "l", "-slt", "-ba", "--", str(archive)], timeout=120)
    except (OSError, subprocess.SubprocessError):
        return []
    return parse_7z_slt(run.stdout.decode("utf-8", "replace"))


def parse_7z_slt(text: str) -> list[tuple[str, int]]:
    members, path, size, folder = [], None, 0, False
    for line in text.splitlines() + [""]:
        if not line.strip():
            if path is not None and not folder:
                members.append((path, size))
            path, size, folder = None, 0, False
            continue
        key, _, value = line.partition(" = ")
        if key == "Path":
            path = value.strip()
        elif key == "Size":
            size = int(value) if value.strip().isdigit() else 0
        elif key == "Folder":
            folder = value.strip() == "+"
        elif key == "Attributes" and value.strip().startswith("D"):
            folder = True
    return members


def read_disc_iso(blob: Blob, path: Path | None = None) -> dict:
    """PlayStation disc identity from an ISO/BIN: {"platform", "serial", "sfo", "volume_id"}."""

    iso = Iso9660(blob)
    out: dict = {}
    if iso.valid:
        out["volume_id"] = getattr(iso, "volume_id", "")
        cnf = iso.read_file("SYSTEM.CNF", 4096)
        if cnf:
            out["serial"], out["platform"] = system_cnf_serial(cnf.decode("latin-1"))
        for folder, platform in (("PS3_GAME", "ps3"), ("PSP_GAME", "psp")):
            sfo = parse_sfo(iso.read_file(f"{folder}/PARAM.SFO", 1 << 16))
            if sfo:
                out.update({"platform": platform, "sfo": sfo})
                serial = str(sfo.get("TITLE_ID") or sfo.get("DISC_ID") or "")
                out["serial"] = f"{serial[:4]}-{serial[4:]}" if re.fullmatch(r"[A-Z]{4}\d{5}", serial) else serial
                break
    if not out.get("serial") and path is not None and blob.data is None:  # UDF-only images: let 7z read the file system
        cnf = _seven_zip_member(path, "SYSTEM.CNF", 4096)
        if cnf:
            out["serial"], out["platform"] = system_cnf_serial(cnf.decode("latin-1"))
        else:
            for folder, platform in (("PS3_GAME", "ps3"), ("PSP_GAME", "psp")):
                sfo = parse_sfo(_seven_zip_member(path, f"{folder}/PARAM.SFO", 1 << 16))
                if sfo:
                    serial = str(sfo.get("TITLE_ID") or sfo.get("DISC_ID") or "")
                    out.update({"platform": platform, "sfo": sfo,
                                "serial": f"{serial[:4]}-{serial[4:]}" if re.fullmatch(r"[A-Z]{4}\d{5}", serial) else serial})
                    break
    return {k: v for k, v in out.items() if v}


# --- Wii / GameCube ---------------------------------------------------------------------------------------------

WII_MAGIC = 0x5D1C9EA3
GC_MAGIC = 0xC2339F3D


def parse_disc_header(data: bytes) -> dict:
    """Wii/GC disc header: {"id6", "platform" (wii/gc), "title", "disc", "revision"}; {} when the magic is missing."""

    if len(data) < 0x60:
        return {}
    if _u32be(data, 0x18) == WII_MAGIC:
        platform = "wii"
    elif _u32be(data, 0x1C) == GC_MAGIC:
        platform = "gc"
    else:
        return {}
    id6 = data[:6].decode("ascii", "replace")
    if not re.fullmatch(r"[A-Z0-9]{6}", id6):
        return {}
    title = data[0x20:0x60].split(b"\0", 1)[0]
    try:
        text = title.decode("utf-8")
    except UnicodeDecodeError:
        text = title.decode("shift_jis", "replace")
    return {"id6": id6, "platform": platform, "title": text.strip(), "disc": data[6], "revision": data[7]}


def wii_header_offset(head: bytes) -> tuple[int, str]:
    """(offset of the disc header copy, container format) for ISO/GCM, WBFS, WIA, RVZ and CISO images."""

    if head[:4] == b"WBFS" and len(head) > 9:
        return 1 << head[8], "WBFS"
    if head[:4] in (b"WIA\x01", b"RVZ\x01"):
        return 0x58, head[:3].decode()
    if head[:4] == b"CISO":
        return 0x8000, "CISO"
    if head[:4] == b"\x01\xc0\x0b\xb1":
        return -1, "GCZ"
    return 0, "ISO"


def read_wii(blob: Blob) -> dict:
    head = blob.read(0, 0x100)
    offset, fmt = wii_header_offset(head)
    if offset < 0:
        return {"format": fmt}
    info = parse_disc_header(blob.read(offset, 0x100))
    if info:
        info["format"] = fmt if fmt != "ISO" else ("GCM" if info["platform"] == "gc" else "ISO")
    return info


def wit_id6(path: Path) -> str:
    """ID6 via ``wit ID6`` for formats the Python reader does not cover (e.g. WDF)."""

    tool = shutil.which("wit")
    if not tool:
        return ""
    try:
        run = safeio.run([tool, "ID6", "--", str(path)], timeout=120)
    except (OSError, subprocess.SubprocessError):
        return ""
    match = re.search(r"\b([A-Z0-9]{6})\b", run.stdout.decode("ascii", "replace"))
    return match.group(1) if match else ""


# --- 3DS ---------------------------------------------------------------------------------------------------------

SMDH_LANGUAGES = ["ja", "en", "fr", "de", "it", "es", "zh", "ko", "nl", "pt", "ru", "zh"]
_TMD_SIG = {0x10000: 0x200 + 0x3C, 0x10001: 0x100 + 0x3C, 0x10002: 0x3C + 0x40, 0x10003: 0x200 + 0x3C,
            0x10004: 0x100 + 0x3C, 0x10005: 0x3C + 0x40}


def _align(value: int, to: int = 64) -> int:
    return (value + to - 1) // to * to


def parse_cia_header(data: bytes) -> dict:
    """Section offsets of a CIA: {"tmd_offset", "tmd_size", "content_offset", "content_size", "meta_offset", "meta_size"}."""

    if len(data) < 0x20:
        return {}
    header_size, _type, _version, cert_size, ticket_size, tmd_size, meta_size = struct.unpack_from("<IHHIIII", data, 0)
    content_size = struct.unpack_from("<Q", data, 0x18)[0]
    if header_size != 0x2020 or not tmd_size:
        return {}
    cert = _align(header_size)
    ticket = cert + _align(cert_size)
    tmd = ticket + _align(ticket_size)
    content = tmd + _align(tmd_size)
    meta = content + _align(content_size)
    return {"tmd_offset": tmd, "tmd_size": tmd_size, "content_offset": content, "content_size": content_size,
            "meta_offset": meta, "meta_size": meta_size}


def parse_tmd(data: bytes) -> dict:
    """{"title_id", "version", "content_count", "encrypted"} of a 3DS TMD (big-endian)."""

    sig_type = _u32be(data, 0)
    if sig_type not in _TMD_SIG:
        return {}
    header = 4 + _TMD_SIG[sig_type]
    if len(data) < header + 0xC4:
        return {}
    title_id = struct.unpack_from(">Q", data, header + 0x4C)[0]
    version = struct.unpack_from(">H", data, header + 0x9C)[0]
    count = struct.unpack_from(">H", data, header + 0x9E)[0]
    chunk = header + 0xC4 + 64 * 0x24
    encrypted = bool(struct.unpack_from(">H", data, chunk + 6)[0] & 1) if len(data) >= chunk + 8 else None
    return {"title_id": f"{title_id:016X}", "version": version, "content_count": count, "encrypted": encrypted}


def parse_smdh(data: bytes) -> dict:
    """{"titles": {lang: (short, long, publisher)}, "title", "publisher", "region_lockout"} of an SMDH icon file."""

    if data[:4] != b"SMDH" or len(data) < 0x2030:
        return {}
    titles = {}
    for index, lang in enumerate(SMDH_LANGUAGES):
        base = 8 + index * 0x200
        short = data[base:base + 0x80].decode("utf-16-le", "replace").split("\0")[0].strip()
        long = data[base + 0x80:base + 0x180].decode("utf-16-le", "replace").split("\0")[0].strip()
        publisher = data[base + 0x180:base + 0x200].decode("utf-16-le", "replace").split("\0")[0].strip()
        if short and lang not in titles:
            titles[lang] = (short, long.replace("\n", " "), publisher)
    english = titles.get("en") or next(iter(titles.values()), ("", "", ""))
    return {"titles": titles, "title": english[0], "publisher": english[2], "region_lockout": _u32le(data, 0x2018)}


def parse_ncch(data: bytes) -> dict:
    """{"title_id", "product_code", "maker", "ncch_version"} of an NCCH header (plain part). 0x112 is the NCCH format
    version (0-2), not the title version: a cart's title version sits in the encrypted ExHeader."""

    if data[0x100:0x104] != b"NCCH" or len(data) < 0x160:
        return {}
    return {
        "title_id": f"{struct.unpack_from('<Q', data, 0x118)[0]:016X}",
        "maker": data[0x110:0x112].decode("ascii", "replace"),
        "ncch_version": _u16le(data, 0x112),
        "product_code": _cstr(data[0x150:0x160], "ascii"),
    }


def n3ds_version(version: int) -> str:
    """3DS title version 1024 → "1.0.0" (major 6 bits, minor 6 bits, micro 4 bits)."""

    return f"{version >> 10}.{(version >> 4) & 0x3F}.{version & 0xF}" if version else ""


def n3ds_kind(title_id: str) -> tuple[str, str]:
    high, low = (title_id or "0" * 16)[:8].upper(), (title_id or "0" * 16)[8:].upper()
    kind = {"00040000": "base", "0004000E": "update", "0004008C": "dlc", "00040002": "demo"}.get(high, "")
    return kind, "00040000" + low if kind else ""


def read_3ds(blob: Blob) -> dict:
    """CIA or NCSD (.3ds/.cci): {"format", "title_id", "version", "product_code", "title", "publisher", "region"}."""

    head = blob.read(0, 0x4000)
    out: dict = {}
    if head[0x100:0x104] == b"NCSD" and len(head) >= 0x128:
        out["format"] = "3DS"
        out["title_id"] = f"{struct.unpack_from('<Q', head, 0x108)[0]:016X}"
        partition0 = _u32le(head, 0x120) * 0x200
        ncch = parse_ncch(blob.read(partition0, 0x200))
        if ncch.get("product_code"):  # the title version is not readable without keys (ExHeader)
            out["product_code"] = ncch["product_code"]
        return out
    sections = parse_cia_header(head)
    if not sections:
        return {}
    out["format"] = "CIA"
    tmd = parse_tmd(blob.read(sections["tmd_offset"], min(sections["tmd_size"], 0x4000)))
    out.update({k: v for k, v in tmd.items() if k in {"title_id", "version"}})
    if tmd.get("encrypted") is False:
        ncch = parse_ncch(blob.read(sections["content_offset"], 0x200))
        if ncch.get("product_code"):
            out["product_code"] = ncch["product_code"]
    if sections["meta_size"] >= 0x400 + 0x36C0:
        smdh = parse_smdh(blob.read(sections["meta_offset"] + 0x400, 0x36C0))
        if smdh:
            out.update({"title": smdh["title"], "publisher": smdh["publisher"], "region": region_from_3ds_lockout(smdh["region_lockout"])})
    return out


# --- Xbox / Xbox 360 -------------------------------------------------------------------------------------------

XDVDFS_MAGIC = b"MICROSOFT*XBOX*MEDIA"
XDVDFS_PARTITIONS = (0, 0x18300000, 0xFD90000, 0x89D80000, 0x2080000)


def xdvdfs_find(blob: Blob, wanted: tuple[str, ...] = ("default.xex", "default.xbe")) -> tuple[str, int, int, int] | None:
    """(name, absolute offset, size, partition) of default.xex/xbe in the root of an XDVDFS image."""

    for partition in XDVDFS_PARTITIONS:
        volume = blob.read(partition + 0x10000, 0x800)
        if volume[:20] != XDVDFS_MAGIC:
            continue
        root_sector, root_size = _u32le(volume, 0x14), _u32le(volume, 0x18)
        table = blob.read(partition + root_sector * 2048, min(root_size, 1 << 20))
        seen, stack = set(), [0]
        while stack:
            pos = stack.pop()
            if pos in seen or pos + 14 > len(table):
                continue
            seen.add(pos)
            left, right, sector, size, _attr, name_len = struct.unpack_from("<HHIIBB", table, pos)
            name = table[pos + 14:pos + 14 + name_len].decode("ascii", "replace")
            if name.lower() in wanted:
                return name, partition + sector * 2048, size, partition
            for child in (left, right):
                if child and child != 0xFFFF:
                    stack.append(child * 4)
    return None


def parse_xex(data: bytes) -> dict:
    """{"title_id", "media_id", "version", "base_version", "disc", "discs", "region"} from an XEX2 header."""

    if data[:4] != b"XEX2" or len(data) < 0x18:
        return {}
    security_offset, count = _u32be(data, 0x10), _u32be(data, 0x14)
    out: dict = {}
    for index in range(min(count, 64)):
        key, value = struct.unpack_from(">II", data, 0x18 + index * 8) if len(data) >= 0x20 + index * 8 else (0, 0)
        if key == 0x00040006 and len(data) >= value + 24:
            media, version, base, title, _platform, _type, disc, discs = struct.unpack_from(">IIIIBBBB", data, value)
            out.update({"media_id": f"{media:08X}", "version": version, "base_version": base, "title_id": f"{title:08X}",
                        "disc": disc, "discs": discs})
    if security_offset and len(data) >= security_offset + 0x17C:
        out["region"] = region_from_xex(_u32be(data, security_offset + 0x178))
    return out


def parse_xbe(data: bytes) -> dict:
    """Original Xbox default.xbe certificate: {"title_id", "title", "region", "version"}."""

    if data[:4] != b"XBEH" or len(data) < 0x178:
        return {}
    base, cert_address = _u32le(data, 0x104), _u32le(data, 0x118)
    cert = cert_address - base
    if cert <= 0 or len(data) < cert + 0xB0:
        return {}
    title_id = _u32le(data, cert + 0x8)
    name = data[cert + 0xC:cert + 0x5C].decode("utf-16-le", "replace").split("\0")[0].strip()
    flags = _u32le(data, cert + 0xA0)
    groups = [g for g, bit in (("USA", 1), ("JPN", 2), ("EUR", 4)) if flags & bit]
    publisher = bytes([(title_id >> 24) & 0xFF, (title_id >> 16) & 0xFF]).decode("ascii", "replace")
    return {
        "title_id": f"{title_id:08X}",
        "title_code": f"{publisher}-{title_id & 0xFFFF:03d}",
        "title": name,
        "region": "RF" if len(groups) >= 2 and {"USA", "EUR"} <= set(groups) else (groups[0] if groups else ""),
        "version": _u32le(data, cert + 0xAC),
    }


STFS_TYPES = {0x7000: "GOD", 0xD0000: "XBLA", 0xB0000: "update", 0x2: "dlc", 0x4000: "installer", 0x5000: "Xbox Original"}


def parse_stfs(data: bytes) -> dict:
    """{"kind", "title_id", "media_id", "version", "title", "display_name", "publisher", "content_type"} of a
    CON/LIVE/PIRS package header (Games on Demand, XBLA, title updates)."""

    if data[:4] not in (b"CON ", b"LIVE", b"PIRS") or len(data) < 0x1711:
        return {}

    def text(offset: int) -> str:
        return data[offset:offset + 0x80].decode("utf-16-be", "replace").split("\0")[0].strip()

    content_type = _u32be(data, 0x344)
    return {
        "kind": data[:4].decode().strip(),
        "content_type": STFS_TYPES.get(content_type, f"0x{content_type:X}"),
        "media_id": f"{_u32be(data, 0x354):08X}",
        "version": _u32be(data, 0x358),
        "title_id": f"{_u32be(data, 0x360):08X}",
        "disc": data[0x366],
        "discs": data[0x367],
        "display_name": text(0x411),
        "publisher": text(0x1611),
        "title": text(0x1691) or text(0x411),
    }


def xex_version(version: int) -> str:
    """Packed XEX version (major 4, minor 4, build 16, qfe 8 bits) → "1.0.1234.0"; "" for 0."""

    if not version:
        return ""
    return f"{version >> 28}.{(version >> 24) & 0xF}.{(version >> 8) & 0xFFFF}.{version & 0xFF}"


# --- the whole release ------------------------------------------------------------------------------------------

GOD_DIR_RE = re.compile(r"(?i)(^|/)[0-9a-f]{8}/(00007000|000d0000|000b0000|00000002)/")
GOD_HEADER_RE = re.compile(r"(?i)(^|[\\/])[0-9a-f]{8}[\\/](00007000|000d0000|000b0000|00000002)[\\/][0-9a-f]{16,42}$")


def _path(release, relpath: str) -> Path:
    return release.path if release.path.is_file() else release.path / relpath


def detect_platform(names: list[str], release_name: str = "") -> str:
    """Platform from file names/extensions and folder markers alone (no reading); '' when unsure."""

    lower = [n.replace("\\", "/").lower() for n in names]
    exts = {Path(n).suffix for n in lower}
    if any(n.endswith("ps3_game/param.sfo") for n in lower):
        return "ps3"
    if any(n.endswith("sce_sys/param.json") for n in lower):
        return "ps5"
    if any(n.endswith("sce_sys/param.sfo") for n in lower):
        return "ps4"
    if any(n.endswith("psp_game/param.sfo") for n in lower):
        return "psp"
    if any(n.endswith("default.xex") for n in lower) or any(GOD_DIR_RE.search(n) for n in lower):
        return "xbox360"
    if any(n.endswith("default.xbe") for n in lower):
        return "xbox"
    if exts & SWITCH_EXT:
        return "switch"
    if exts & N3DS_EXT:
        return "3ds"
    if exts & {".wbfs", ".rvz", ".wia", ".ciso", ".wdf"}:
        return "wii"
    if exts & {".gcm", ".gcz"}:
        return "gc"
    return parse_name(release_name).get("platform", "")


def _content_files(release) -> list:
    files = [f for f in release.files if f.ext in CONTENT_EXT or f.relpath.lower().endswith(("param.sfo", "param.json"))]
    return sorted(files, key=lambda f: -f.size)


def _first_volumes(release) -> list:
    out = []
    for item in release.files:
        lower = item.relpath.lower()
        if lower.endswith((".7z", ".zip")) or (lower.endswith(".rar") and not re.search(r"\.part(?!0*1\.rar)\d+\.rar$", lower)):
            out.append(item)
    return out


def identify(release, hints: dict | None = None) -> ConsoleInfo:
    """Everything the release's own files and name tell about the console game. Never raises on odd files."""

    hints = hints or {}
    info = ConsoleInfo()
    names = [f.relpath for f in release.files]
    tags = parse_name(release.name)
    forced = PLATFORM_ALIASES.get(str(hints.get("platform") or "").lower(), "")
    info.platform = forced or detect_platform(names, release.name)
    contents = _content_files(release)
    info.files = [f.relpath for f in contents[:20]]
    try:
        _read_files(release, info, contents)
        if not info.title_id and not info.serial and not info.id6:
            _read_archives(release, info)
    except Exception as exc:  # noqa: BLE001 - damaged, truncated or odd files: fall back to the name
        info.warnings.append(f"a fájlok fejlécét nem sikerült beolvasni ({type(exc).__name__}) — a névből azonosítok")
    if forced and info.platform != forced:
        info.warnings.append(f"a megadott platform ({forced}) eltér a fájlokból felismerttől ({info.platform}) — a megadottat használom")
        info.platform = forced
    _fill_from_name(info, tags)
    if not info.platform:
        info.warnings.append("a platformot nem sikerült felismerni — add meg: --platform (pl. ps4, switch, ps2)")
    _finish_region(info, tags)
    return info


def _read_files(release, info: ConsoleInfo, contents: list) -> None:
    for item in release.files:  # folder dumps first: their PARAM.SFO / param.json is the most exact source
        lower = item.relpath.replace("\\", "/").lower()
        if lower.endswith(("ps3_game/param.sfo", "psp_game/param.sfo", "sce_sys/param.sfo")):
            sfo = parse_sfo(Blob(_path(release, item.relpath)).read(0, 1 << 20))
            if sfo:
                _apply_sfo(info, sfo, "ps3" if "ps3_game" in lower else "psp" if "psp_game" in lower else "ps4")
                info.format = info.format or "mappa"
                info.note("PARAM.SFO")
                return
        if lower.endswith("sce_sys/param.json"):
            meta = parse_param_json(Blob(_path(release, item.relpath)).read(0, 1 << 20).decode("utf-8-sig", "replace"))
            if meta:
                info.platform, info.format = "ps5", "mappa"
                info.title, info.title_id, info.content_id = meta["title"], meta["title_id"], meta["content_id"]
                info.version = ps_version(meta["version"])
                info.note("param.json")
                return
    god = [f for f in release.files if GOD_HEADER_RE.search(f.relpath)]
    if god:
        _apply_stfs(info, parse_stfs(Blob(_path(release, god[0].relpath)).read(0, 0x2000)))
        if info.title_id:
            return
    xex = [f for f in release.files if f.relpath.lower().endswith("default.xex")]
    if xex:
        _apply_xex(info, parse_xex(Blob(_path(release, xex[0].relpath)).read(0, 0x10000)), "default.xex")
        info.platform, info.format = "xbox360", "mappa"
        if info.title_id:
            return
    xbe = [f for f in release.files if f.relpath.lower().endswith("default.xbe")]
    if xbe:
        _apply_xbe(info, parse_xbe(Blob(_path(release, xbe[0].relpath)).read(0, 0x10000)))
        info.format = "mappa"
        if info.title_id:
            return
    for item in contents:
        path = _path(release, item.relpath)
        if _read_one(info, Blob(path), item.relpath, path):
            if info.platform == "switch" or (info.platform in {"ps4", "ps5", "ps3"} and info.format == "PKG"):
                continue  # gather every NSP/PKG of the release (base + update + DLC)
            return


def _read_one(info: ConsoleInfo, blob: Blob, relpath: str, path: Path | None) -> bool:
    """Read one content file (or an archive member prefix); True when it gave an identifier."""

    ext = Path(relpath).suffix.lower()
    if ext in SWITCH_EXT:
        return _apply_switch(info, read_switch(blob, Path(relpath).name), relpath)
    head = blob.read(0, 0x1000)
    if ext in PKG_EXT or head[:4] in (PS4_MAGIC, PS3_MAGIC):
        return _apply_pkg(info, blob, head, relpath)
    if ext in N3DS_EXT:
        data = read_3ds(blob)
        if data.get("title_id"):
            _apply_3ds(info, data, relpath)
            return True
        return False
    if ext in WII_EXT or ext == ".iso":
        data = read_wii(blob)
        if data.get("id6"):
            info.platform, info.format, info.id6 = data["platform"], data["format"], data["id6"]
            info.title = info.title or data.get("title", "")
            if data.get("revision"):
                info.extra["revision"] = f"Rev {data['revision']}"
            if data.get("disc"):
                info.extra["discs"] = f"{data['disc'] + 1}. lemez"
            info.note("lemezfejléc")
            return True
        if ext in WII_EXT and path is not None and blob.data is None and (id6 := wit_id6(path)):
            info.platform = info.platform if info.platform in {"wii", "gc"} else "wii"
            info.id6, info.format = id6, ext.lstrip(".").upper()
            info.note("wit")
            return True
    if ext in {".iso", ".bin", ".img"}:
        found = xdvdfs_find(blob)
        if found:
            name, offset, _size, partition = found
            if name.lower() == "default.xex":
                _apply_xex(info, parse_xex(blob.read(offset, 0x10000)), "default.xex")
                info.platform = "xbox360"
            else:
                _apply_xbe(info, parse_xbe(blob.read(offset, 0x10000)))
            info.format = "ISO" if partition else "XISO"
            return bool(info.title_id)
        disc = read_disc_iso(blob, path)
        if disc.get("serial") or disc.get("sfo"):
            info.format = "BIN/CUE" if ext == ".bin" else "ISO"
            if disc.get("sfo"):
                _apply_sfo(info, disc["sfo"], disc["platform"])
            else:
                info.platform = disc.get("platform", info.platform)
                info.serial = disc["serial"]
                info.note("SYSTEM.CNF")
            return True
    if ext == ".chd":
        info.format = "CHD"
        info.warnings.append("CHD képfájl: a sorozatszámot nem olvasom ki belőle, a névből azonosítok")
    return False


def _apply_switch(info: ConsoleInfo, data: dict, relpath: str) -> bool:
    if not data.get("items"):
        return False
    info.platform = "switch"
    info.format = info.format or data["format"]
    info.title = info.title or data.get("title", "")
    if data.get("publisher"):
        info.extra.setdefault("publisher", data["publisher"])
    if data.get("rom_size"):
        info.extra["cart_size"] = data["rom_size"]
    for item in data["items"]:
        info.contents.append({"file": relpath, "title_id": item["title_id"], "type": item["type"], "version": item.get("version"),
                              "display_version": item.get("display_version", "")})
        if item.get("from_name"):
            info.note("fájlnév")
            if data["format"] in {"XCI", "XCZ"}:
                info.extra["xci_note"] = "XCI: a title ID a fájlnévből származik (a kártyakép titkosított)"
        else:
            info.note("PFS0 (NSP)")
    _summarize_switch(info)
    return True


def _summarize_switch(info: ConsoleInfo) -> None:
    bases = [c for c in info.contents if c["type"] == "base"]
    updates = [c for c in info.contents if c["type"] == "update"]
    dlcs = [c for c in info.contents if c["type"] == "dlc"]
    main = bases[0] if bases else (updates or dlcs or info.contents)[0]
    info.title_id = main["title_id"]
    info.content_type = main["type"]
    info.base_title_id = switch_kind(main["title_id"])[1]
    best = max(updates, key=lambda c: c.get("version") or 0) if updates else main
    info.version_int = best.get("version")
    info.version = best.get("display_version") or ("" if best["type"] == "base" else switch_version(best.get("version")))
    parts = []
    if bases:
        parts.append("alapjáték")
    if updates:
        parts.append(f"frissítés ({switch_version(best.get('version')) or 'v?'})")
    if dlcs:
        parts.append(f"{len(dlcs)} DLC")
    info.extra["contents"] = " + ".join(parts)


def _apply_pkg(info: ConsoleInfo, blob: Blob, head: bytes, relpath: str) -> bool:
    if head[:4] == PS4_MAGIC:
        data = read_ps4_pkg(blob)
        if not data.get("content_id"):
            return False
        sfo = data.get("sfo") or {}
        kind = sfo_kind(str(sfo.get("CATEGORY", ""))) or parse_name(relpath).get("content_type", "base")
        platform = "ps5" if data["content_id"][7:11] == "PPSA" else "ps4"
        version = ps_version(str(sfo.get("APP_VER") or sfo.get("VERSION") or ""))
        info.contents.append({"file": relpath, "title_id": data["content_id"][7:16], "content_id": data["content_id"],
                              "type": kind, "version": version, "title": sfo.get("TITLE", "")})
        if not info.content_id or kind == "base" and info.content_type != "base":
            info.platform, info.format = platform, "PKG"
            info.content_id, info.title_id, info.content_type = data["content_id"], data["content_id"][7:16], kind
            info.title = str(sfo.get("TITLE") or info.title)
            info.sfo = sfo or info.sfo
            info.icon = data.get("icon") or info.icon
            info.title_languages = sfo_languages(sfo) or info.title_languages
            if isinstance(sfo.get("SYSTEM_VER"), int) and sfo["SYSTEM_VER"]:
                info.extra["system_ver"] = _ps4_firmware(sfo["SYSTEM_VER"])
        updates = [c for c in info.contents if c["type"] == "update" and c.get("version")]
        info.version = max((c["version"] for c in updates), key=_version_key) if updates else (version if kind == "base" else info.version or version)
        info.note("PKG fejléc + param.sfo" if sfo else "PKG fejléc")
        _summarize_ps(info)
        return True
    data = parse_ps3_pkg_header(head)
    if not data.get("content_id"):
        return False
    title_id = data["content_id"][7:16]
    platform = "vita" if title_id.startswith("PCS") or data["content_type"].startswith("Vita") else "ps3" if data["pkg_type"] == 1 else "psp"
    info.contents.append({"file": relpath, "title_id": title_id, "content_id": data["content_id"], "type": "base", "version": ""})
    if not info.content_id:
        info.platform, info.format, info.content_id, info.title_id = platform, "PKG", data["content_id"], title_id
        info.serial = f"{title_id[:4]}-{title_id[4:]}"
        info.extra["pkg_type"] = data["content_type"]
        if not data["retail"]:
            info.warnings.append("a PKG nem retail (debug/fake) csomag — HEN/CFW-vel telepíthető")
    info.note("PKG fejléc")
    return True


def _ps4_firmware(value: int) -> str:
    """SYSTEM_VER 0x05050000 → "5.05"."""

    major, minor = (value >> 24) & 0xFF, (value >> 16) & 0xFF
    return f"{major:x}.{minor:02x}" if value else ""


def _version_key(text: str) -> tuple:
    return tuple(int(p) for p in re.findall(r"\d+", text or "0"))


def _summarize_ps(info: ConsoleInfo) -> None:
    kinds = [c["type"] for c in info.contents]
    parts = []
    if "base" in kinds:
        parts.append("alapjáték")
    if "update" in kinds:
        parts.append(f"frissítés (v{info.version})" if info.version else "frissítés")
    if kinds.count("dlc"):
        parts.append(f"{kinds.count('dlc')} DLC")
    info.extra["contents"] = " + ".join(parts)


def _apply_sfo(info: ConsoleInfo, sfo: dict, platform: str) -> None:
    info.platform = platform
    info.sfo = sfo
    info.title = str(sfo.get("TITLE") or info.title)
    title_id = str(sfo.get("TITLE_ID") or sfo.get("DISC_ID") or "")
    info.title_id = title_id
    if re.fullmatch(r"[A-Z]{4}\d{5}", title_id):
        info.serial = f"{title_id[:4]}-{title_id[4:]}"
    info.content_id = str(sfo.get("CONTENT_ID") or info.content_id)
    info.version = ps_version(str(sfo.get("APP_VER") or sfo.get("DISC_VERSION") or sfo.get("VERSION") or ""))
    info.content_type = sfo_kind(str(sfo.get("CATEGORY", ""))) if platform == "ps4" else "base"
    info.title_languages = sfo_languages(sfo) or info.title_languages
    info.note("PARAM.SFO")


def _apply_3ds(info: ConsoleInfo, data: dict, relpath: str) -> None:
    info.platform, info.format = "3ds", data["format"]
    info.title_id = data["title_id"]
    info.content_type, info.base_title_id = n3ds_kind(data["title_id"])
    info.version_int = data.get("version")
    info.version = n3ds_version(data.get("version") or 0)
    info.product_code = data.get("product_code", "")
    info.title = data.get("title") or info.title
    if data.get("publisher"):
        info.extra.setdefault("publisher", data["publisher"])
    if data.get("region"):
        info.region = data["region"]
    info.contents.append({"file": relpath, "title_id": info.title_id, "type": info.content_type, "version": info.version_int})
    info.note("CIA TMD/SMDH" if data["format"] == "CIA" else "NCSD/NCCH fejléc")


def _apply_xex(info: ConsoleInfo, data: dict, source: str) -> None:
    if not data.get("title_id"):
        return
    info.title_id, info.media_id = data["title_id"], data.get("media_id", "")
    info.version_int = data.get("version")
    if data.get("region"):
        info.region = data["region"]
    if data.get("discs", 0) > 1:
        info.extra["discs"] = f"{data['disc']}/{data['discs']}"
    info.note(source)


def _apply_xbe(info: ConsoleInfo, data: dict) -> None:
    info.platform = "xbox"
    if not data.get("title_id"):
        return
    info.title_id, info.title = data["title_id"], data["title"] or info.title
    info.extra["title_code"] = data["title_code"]
    info.version_int = data.get("version")
    if data.get("region"):
        info.region = data["region"]
    info.note("default.xbe")


def _apply_stfs(info: ConsoleInfo, data: dict) -> None:
    if not data.get("title_id"):
        return
    info.platform = "xbox360"
    info.format = data["content_type"] if data["content_type"] in {"GOD", "XBLA"} else "STFS"
    info.title_id, info.media_id = data["title_id"], data["media_id"]
    info.title = data["title"] or info.title
    if data.get("publisher"):
        info.extra.setdefault("publisher", data["publisher"])
    info.content_type = {"update": "update", "dlc": "dlc"}.get(data["content_type"], "base")
    info.note("STFS fejléc")


def _read_archives(release, info: ConsoleInfo) -> None:
    """Scene releases are packed: list the archive and read the first bytes of the content file through 7z."""

    for item in _first_volumes(release)[:2]:
        archive = _path(release, item.relpath)
        members = [(m, s) for m, s in seven_zip_list(archive) if Path(m).suffix.lower() in CONTENT_EXT]
        if not members:
            continue
        member, size = max(members, key=lambda m: m[1])
        info.extra["packed"] = f"{item.relpath} → {member}"
        data = _seven_zip_member(archive, member, HEAD_LIMIT)
        if data and _read_one(info, Blob(data=data, size=size), member, None):
            info.note("archívum (7z)")
            return
        tags = parse_name(Path(member).name)
        for key in ("title_id", "serial", "id6", "content_id"):
            if tags.get(key) and not getattr(info, key):
                setattr(info, key, tags[key])
        if info.platform == "" and Path(member).suffix.lower() in SWITCH_EXT:
            info.platform = "switch"


def _fill_from_name(info: ConsoleInfo, tags: dict) -> None:
    used = False
    if not info.platform and tags.get("platform"):
        info.platform = tags["platform"]
    if not info.title_id and tags.get("title_id"):
        info.title_id, used = tags["title_id"], True
        if info.platform == "switch" or (not info.platform and len(info.title_id) == 16):
            info.platform = info.platform or "switch"
            info.content_type, info.base_title_id = switch_kind(info.title_id)
    if not info.content_id and tags.get("content_id"):
        info.content_id, used = tags["content_id"], True
    if not info.serial and tags.get("serial"):
        info.serial, used = tags["serial"], True
    if not info.id6 and tags.get("id6"):
        info.id6, used = tags["id6"], True
    if info.version_int is None and tags.get("version_int") is not None:
        info.version_int, used = tags["version_int"], True
    if not info.version and tags.get("version"):
        info.version, used = tags["version"], True
    if not info.version and info.platform == "switch" and info.version_int:
        info.version = switch_version(info.version_int)
    if not info.content_type:
        info.content_type = tags.get("content_type", "")
    if tags.get("hungarian"):
        info.hungarian = True
    if "hu" in info.languages:
        info.hungarian = True
    elif "hu" in info.title_languages and not info.hungarian:
        info.warnings.append("a PARAM.SFO-ban magyar cím is van (TITLE_24): lehet, hogy a játék magyar, de ez nem biztos — "
                             "ellenőrizd, és ha magyar, írd a névbe: HUN")
    if not info.title:
        info.title = tags.get("title", "")
        info.title_from_name = bool(info.title)
    if info.platform == "ps3" and not info.serial and re.fullmatch(r"[A-Z]{4}\d{5}", info.title_id):
        info.serial = f"{info.title_id[:4]}-{info.title_id[4:]}"
    if used:
        info.note("név")


def _finish_region(info: ConsoleInfo, tags: dict) -> None:
    region = info.region
    if not region:
        if info.platform == "switch":
            region = "RF"  # the Switch has no region lock
        elif info.content_id:
            region = region_from_content_id(info.content_id)
        elif info.serial:
            region = region_from_serial(info.serial)
        elif info.id6:
            region = region_from_id6(info.id6)
        elif info.product_code:
            region = region_from_product_code(info.product_code)
    if not region and tags.get("region"):
        region = tags["region"]
        info.note("név (régió)")
    info.region = region
    if region and (warning := region_warning(region)):
        info.warnings.append(warning)
    if not region and info.platform:
        info.warnings.append("a régiót nem sikerült megállapítani — NTSC (USA) / PAL (EUR) / Régiófüggetlen (RF)?")
