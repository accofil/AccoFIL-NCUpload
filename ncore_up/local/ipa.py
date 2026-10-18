"""iOS apps (.ipa) read statically: Payload/<App>.app/Info.plist through zipfile + plistlib (XML or binary plist),
the localizations (*.lproj folders), the FairPlay state of the main executable (LC_ENCRYPTION_INFO cryptid) and
whether an iTunesMetadata.plist with the buyer's Apple account is inside.
"""

from __future__ import annotations

import plistlib
import re
import struct
import zipfile
import zlib
from pathlib import Path
from .. import safeio

INFO_PLIST_RE = re.compile(r"^Payload/([^/]+)\.app/Info\.plist$")
DEVICE_FAMILY = {1: "iPhone", 2: "iPad", 3: "Apple TV", 4: "Apple Watch", 6: "Mac", 7: "Vision"}
ACCOUNT_KEYS = ("appleId", "AppleID", "accountInfo", "com.apple.iTunesStore.downloadInfo", "purchaseDate", "userName")


def parse_info_plist(data: bytes) -> dict:
    """{"bundle_id", "name", "version", "build", "min_os", "devices", "executable", "platforms"} of an Info.plist."""

    plist = _plist(data)
    if not isinstance(plist, dict):
        return {}
    families = plist.get("UIDeviceFamily") or []
    if isinstance(families, (int, str)):
        families = [families]
    if not isinstance(families, list):
        families = []
    return {
        "bundle_id": str(plist.get("CFBundleIdentifier") or ""),
        "name": str(plist.get("CFBundleDisplayName") or plist.get("CFBundleName") or ""),
        "version": str(plist.get("CFBundleShortVersionString") or plist.get("CFBundleVersion") or ""),
        "build": str(plist.get("CFBundleVersion") or ""),
        "min_os": str(plist.get("MinimumOSVersion") or plist.get("LSMinimumSystemVersion") or ""),
        "devices": [DEVICE_FAMILY.get(int(f), str(f)) for f in families if str(f).isdigit()],
        "executable": str(plist.get("CFBundleExecutable") or ""),
        "platforms": _strings(plist.get("CFBundleSupportedPlatforms")),
        "localizations": _strings(plist.get("CFBundleLocalizations")),
    }


def _plist(data: bytes):
    """plistlib.loads() that never raises (malformed XML raises ExpatError, not ValueError); None on failure."""

    try:
        return plistlib.loads(data)
    except Exception:  # noqa: BLE001 - InvalidFileException, ExpatError, ValueError, OverflowError ...
        return None


def _strings(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    return [str(x) for x in value] if isinstance(value, list) else []


def macho_cryptid(data: bytes) -> int | None:
    """cryptid of the first architecture of a Mach-O (thin or fat) executable: 1 FairPlay-encrypted, 0 decrypted,
    None when there is no LC_ENCRYPTION_INFO or the bytes are not a Mach-O."""

    if len(data) < 32:
        return None
    magic_be = struct.unpack_from(">I", data, 0)[0]
    if magic_be in (0xCAFEBABE, 0xCAFEBABF):
        count = struct.unpack_from(">I", data, 4)[0]
        if not 0 < count < 32:
            return None
        if magic_be == 0xCAFEBABE:
            offset = struct.unpack_from(">I", data, 8 + 8)[0]
        else:
            offset = struct.unpack_from(">Q", data, 8 + 8)[0]
        return macho_cryptid(data[offset:]) if offset < len(data) else None
    magic = struct.unpack_from("<I", data, 0)[0]
    if magic == 0xFEEDFACF:
        header = 32
    elif magic == 0xFEEDFACE:
        header = 28
    else:
        return None
    ncmds = struct.unpack_from("<I", data, 16)[0]
    pos = header
    for _ in range(min(ncmds, 512)):
        if pos + 8 > len(data):
            return None
        cmd, size = struct.unpack_from("<II", data, pos)
        if cmd in (0x21, 0x2C) and pos + 20 <= len(data):
            return struct.unpack_from("<I", data, pos + 16)[0]
        if size < 8:
            return None
        pos += size
    return None


def _fat_first_offset(head: bytes) -> int:
    if len(head) >= 24 and struct.unpack_from(">I", head, 0)[0] == 0xCAFEBABE:
        return struct.unpack_from(">I", head, 16)[0]
    if len(head) >= 32 and struct.unpack_from(">I", head, 0)[0] == 0xCAFEBABF:
        return struct.unpack_from(">Q", head, 16)[0]
    return 0


def inspect(path: Path) -> dict:
    """{"format": "IPA", "bundle_id", "name", "version", "build", "min_os", "devices", "localizations", "hungarian",
    "encrypted", "warnings"} of one .ipa file."""

    out: dict = {"format": "IPA", "warnings": [], "localizations": [], "devices": []}
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile):
        out["warnings"].append(f"az IPA nem zip fájl: {Path(path).name}")
        return out
    with archive:
        names = archive.namelist()
        plists = [n for n in names if INFO_PLIST_RE.match(n)]
        if not plists:
            out["warnings"].append("az IPA-ban nincs Payload/*.app/Info.plist")
            return out
        app_dir = plists[0][: -len("Info.plist")]
        try:
            parsed = parse_info_plist(safeio.read_zip(archive, plists[0]))
        except (KeyError, RuntimeError, ValueError, zipfile.BadZipFile, OSError, zlib.error, NotImplementedError):
            parsed = {}
        if parsed:
            out.update(parsed)
        else:
            out["warnings"].append("az Info.plist nem olvasható")
        lprojs = sorted({m.group(1) for n in names if (m := re.match(re.escape(app_dir) + r"([^/]+)\.lproj/", n))})
        out["localizations"] = list(dict.fromkeys(out.get("localizations", []) + lprojs))
        out["hungarian"] = any(x.lower().split("-")[0].split("_")[0] in {"hu", "hungarian"} for x in out["localizations"])
        out["encrypted"] = _encrypted(archive, app_dir + out.get("executable", "")) if out.get("executable") else None
        if out["encrypted"]:
            out["warnings"].append("a futtatható fájl FairPlay-titkosított (cryptid=1): csak a vásárló Apple ID-jával fut")
        if "iTunesMetadata.plist" in names:
            try:
                meta = _plist(safeio.read_zip(archive, "iTunesMetadata.plist"))
            except (KeyError, RuntimeError, ValueError, OSError, zipfile.BadZipFile, zlib.error, NotImplementedError):
                meta = {}
            if isinstance(meta, dict) and any(k in meta for k in ACCOUNT_KEYS):
                out["warnings"].append("az IPA tartalmazza az iTunesMetadata.plist-et a vásárló Apple-fiókjának adataival — "
                                       "feltöltés előtt töröld belőle (személyes adat)")
    return out


def _encrypted(archive: zipfile.ZipFile, member: str) -> bool | None:
    try:
        with archive.open(member) as handle:
            head = handle.read(64 * 1024)
            offset = _fat_first_offset(head)
            if offset:
                if offset > safeio.META_LIMIT:
                    return None
                rest = handle.read(offset + 64 * 1024 - len(head)) if offset + 64 * 1024 > len(head) else b""
                head += rest
    except (KeyError, RuntimeError, zipfile.BadZipFile, OSError, zlib.error, NotImplementedError):
        return None
    cryptid = macho_cryptid(head)
    return None if cryptid is None else bool(cryptid)
