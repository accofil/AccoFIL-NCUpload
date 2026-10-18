"""Static PE (EXE/DLL) inspection: machine/architecture, .NET AnyCPU, subsystem, minimum Windows, VersionInfo strings,
manifest, overlay, icon and the Authenticode signer.

Pure Python (struct and a small DER reader), so it needs neither pefile nor osslsigncode, reads only the headers,
the resource tree and the certificate table, and never runs anything. ``read(path)`` returns

    {"machine": 0x8664, "arch": "x64" | "x86" | "arm64" | "arm" | "anycpu", "pe32plus", "dll", "dotnet",
     "subsystem": "gui" | "console" | ..., "os_version": "6.1", "min_os": "Windows 7",
     "product_name", "company", "description", "product_version", "file_version", "copyright", "original_filename",
     "internal_name", "comments", "fixed_file_version", "fixed_product_version", "version" (best_version()),
     "language_ids": ["0409"], "languages": ["angol"],
     "manifest_level": "requireAdministrator", "supported_os": ["Windows 7", "Windows 10/11"],
     "overlay_offset", "overlay_size", "sections": [".text", ...], "resource_types": [3, 14, 16, 24],
     "signature": {see parse_pkcs7(); {} when unsigned}}

{} when the file is not a PE image.
"""

from __future__ import annotations

import hashlib
import io
import re
import struct
from pathlib import Path
from typing import BinaryIO, Iterable

MACHINES = {0x14C: "x86", 0x8664: "x64", 0xAA64: "arm64", 0xA641: "arm64", 0x1C0: "arm", 0x1C2: "arm", 0x1C4: "arm", 0x200: "ia64"}
SUBSYSTEMS = {1: "native", 2: "gui", 3: "console", 7: "posix", 9: "wince", 10: "efi", 11: "efi", 12: "efi", 13: "efi", 14: "xbox", 16: "boot"}
OS_VERSIONS = [((10, 0), "Windows 10"), ((6, 3), "Windows 8.1"), ((6, 2), "Windows 8"), ((6, 1), "Windows 7"), ((6, 0), "Windows Vista"),
               ((5, 2), "Windows XP x64"), ((5, 1), "Windows XP"), ((5, 0), "Windows 2000")]
SUPPORTED_OS = {
    "e2011457-1546-43c5-a5fe-008deee3d3f0": "Windows Vista", "35138b9a-5d96-4fbd-8e2d-a2440225f93a": "Windows 7",
    "4a2f28e3-53b9-4441-ba9c-d69d4a4a6e38": "Windows 8", "1f676c76-80e1-4239-95bb-83d0f6d0da78": "Windows 8.1",
    "8e0f7a12-bfb3-4fe8-b9a5-48fd50a15a9a": "Windows 10/11",
}
# Windows LCIDs -> Hungarian language names (VersionInfo translations, MSI templates).
LCIDS = {
    0x0409: "angol", 0x0809: "angol", 0x0C09: "angol", 0x1009: "angol", 0x040E: "magyar", 0x0407: "német", 0x0C07: "német",
    0x0807: "német", 0x040C: "francia", 0x0C0C: "francia", 0x0410: "olasz", 0x040A: "spanyol", 0x0C0A: "spanyol", 0x080A: "spanyol",
    0x0415: "lengyel", 0x0419: "orosz", 0x0405: "cseh", 0x041B: "szlovák", 0x0418: "román", 0x0411: "japán", 0x0804: "kínai",
    0x0404: "kínai", 0x0412: "koreai", 0x0416: "portugál", 0x0816: "portugál", 0x0413: "holland", 0x041D: "svéd", 0x0406: "dán",
    0x0414: "norvég", 0x040B: "finn", 0x0408: "görög", 0x041F: "török", 0x0422: "ukrán", 0x041A: "horvát", 0x081A: "szerb",
    0x0C1A: "szerb", 0x0424: "szlovén", 0x0402: "bolgár", 0x0401: "arab", 0x040D: "héber", 0x041E: "thai", 0x042A: "vietnámi",
    0x0425: "észt", 0x0426: "lett", 0x0427: "litván", 0x0403: "katalán",
}
RT_ICON, RT_GROUP_ICON, RT_VERSION, RT_MANIFEST = 3, 14, 16, 24
_MAX_RSRC = 64 * 1024 * 1024
_MAX_RESOURCES = 20000  # resource leaves read at most (real programs have a few thousand)


class PEError(ValueError):
    pass


def _u16(data: bytes, offset: int) -> int:
    return struct.unpack_from("<H", data, offset)[0]


def _u32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def _align4(value: int) -> int:
    return (value + 3) & ~3


def os_name(major: int, minor: int) -> str:
    """'Windows 7' for 6.1; '' for versions below Windows XP (stubs that say 4.0 do not mean Windows 95)."""

    for version, name in OS_VERSIONS:
        if (major, minor) >= version:
            return name if (major, minor) >= (5, 1) else ""
    return ""


def lcid_name(lcid: int) -> str:
    return LCIDS.get(lcid, LCIDS.get(0x0400 | (lcid & 0xFF), "")) if lcid else ""


def combine_arch(archs: Iterable[str]) -> str:
    """The nCore compatibility tag of a set of binaries: {"x86","x64"} -> "x86-x64"; AnyCPU .NET runs on both."""

    found = {a for a in archs if a}
    if "anycpu" in found:
        found.discard("anycpu")
        found |= {"x86", "x64"} if not found or found <= {"x86", "x64"} else set()
    if {"x86", "x64"} <= found:
        return "x86-x64"
    for arch in ("x64", "x86", "arm64", "arm", "ia64"):
        if arch in found:
            return arch
    return ""


class _Image:
    """Headers and section table of a PE file opened for reading."""

    def __init__(self, fh: BinaryIO, size: int):
        self.fh, self.size = fh, size
        head = self.read(0, 64)
        if len(head) < 64 or head[:2] != b"MZ":
            raise PEError("not an MZ file")
        lfanew = _u32(head, 0x3C)
        nt = self.read(lfanew, 24)
        if len(nt) < 24 or nt[:4] != b"PE\0\0":
            raise PEError("no PE header")
        self.machine, sections, _stamp, _symtab, _symbols, opt_size, self.characteristics = struct.unpack_from("<HHIIIHH", nt, 4)
        opt = self.read(lfanew + 24, opt_size)
        if len(opt) < 72:
            raise PEError("short optional header")
        self.pe32plus = _u16(opt, 0) == 0x20B
        self.os_version = (_u16(opt, 40), _u16(opt, 42))
        self.subsystem_version = (_u16(opt, 48), _u16(opt, 50))
        self.subsystem = _u16(opt, 68)
        dd = 112 if self.pe32plus else 96
        count = _u32(opt, dd - 4) if len(opt) >= dd else 0
        self.dirs = [(_u32(opt, dd + 8 * i), _u32(opt, dd + 8 * i + 4)) for i in range(min(count, 16)) if dd + 8 * i + 8 <= len(opt)]
        self.checksum_offset = lfanew + 24 + 64
        self.certdir_offset = lfanew + 24 + dd + 8 * 4
        table = self.read(lfanew + 24 + opt_size, 40 * min(sections, 96))
        self.sections = []
        for i in range(len(table) // 40):
            name, vsize, va, rawsize, rawptr = struct.unpack_from("<8sIIII", table, 40 * i)
            self.sections.append((name.rstrip(b"\0").decode("latin-1"), vsize, va, rawsize, rawptr))
        ends = [ptr + raw for _n, _vs, _va, raw, ptr in self.sections if raw]
        self.overlay_offset = max(ends) if ends else 0

    def read(self, offset: int, size: int) -> bytes:
        if offset < 0 or size <= 0 or offset >= self.size:
            return b""
        self.fh.seek(offset)
        return self.fh.read(min(size, self.size - offset))

    def directory(self, index: int) -> tuple[int, int]:
        return self.dirs[index] if index < len(self.dirs) else (0, 0)

    def offset(self, rva: int) -> int:
        for _name, vsize, va, rawsize, rawptr in self.sections:
            if va <= rva < va + max(vsize, rawsize):
                return rawptr + rva - va
        return rva if rva < 4096 else -1

    def resources(self) -> list[tuple[list, int, int]]:
        """[([type, name, lang], data_rva, size)] of the resource tree (ids are ints, names strings)."""

        rva, size = self.directory(2)
        base = self.offset(rva) if rva else -1
        if base < 0 or not size:
            return []
        blob = self.read(base, min(size, _MAX_RSRC))
        out: list[tuple[list, int, int]] = []
        seen: set[int] = set()  # directory offsets: a crafted tree may loop or fan out without end
        budget = [_MAX_RESOURCES * 4]  # directory entries visited at most

        def name_of(value: int):
            if not value & 0x80000000:
                return value
            pos = value & 0x7FFFFFFF
            if pos + 2 > len(blob):
                return ""
            length = _u16(blob, pos)
            return blob[pos + 2: pos + 2 + 2 * length].decode("utf-16-le", errors="replace")

        def walk(pos: int, depth: int, path: list) -> None:
            if pos + 16 > len(blob) or depth > 2 or pos in seen or len(out) >= _MAX_RESOURCES:
                return
            seen.add(pos)
            entries = _u16(blob, pos + 12) + _u16(blob, pos + 14)
            for i in range(min(entries, 4096)):
                entry = pos + 16 + 8 * i
                budget[0] -= 1
                if entry + 8 > len(blob) or len(out) >= _MAX_RESOURCES or budget[0] < 0:
                    return
                name, target = _u32(blob, entry), _u32(blob, entry + 4)
                key = name_of(name)
                if target & 0x80000000:
                    walk(target & 0x7FFFFFFF, depth + 1, path + [key])
                elif target + 8 <= len(blob):
                    out.append((path + [key], _u32(blob, target), _u32(blob, target + 4)))

        walk(0, 0, [])
        return out

    def resource_data(self, rva: int, size: int, limit: int = 16 * 1024 * 1024) -> bytes:
        offset = self.offset(rva)
        return self.read(offset, min(size, limit)) if offset >= 0 else b""


# --- VersionInfo ---------------------------------------------------------------------------------------------------

def _block(data: bytes, pos: int) -> tuple[str, int, int, int, int]:
    """(key, value_start, value_bytes, children_start, end) of one VS_VERSIONINFO-style block."""

    length, value_length, value_type = struct.unpack_from("<HHH", data, pos)
    end = min(pos + length, len(data)) if length else len(data)
    key_end = pos + 6
    while key_end + 1 < end and data[key_end: key_end + 2] != b"\0\0":
        key_end += 2
    key = data[pos + 6: key_end].decode("utf-16-le", errors="replace")
    value_start = _align4(key_end + 2)
    value_bytes = value_length * 2 if value_type == 1 else value_length
    return key, value_start, value_bytes, _align4(value_start + value_bytes), end


def _children(data: bytes, start: int, end: int):
    pos = start
    while pos + 6 <= end:
        length = _u16(data, pos)
        if not length:
            return
        yield pos
        pos = _align4(pos + length)


def _text(data: bytes, start: int, end: int) -> str:
    raw = data[start:end]
    cut = next((i for i in range(0, len(raw) - 1, 2) if raw[i: i + 2] == b"\0\0"), len(raw) - len(raw) % 2)
    return raw[:cut].decode("utf-16-le", errors="replace").strip(" \0\t\r\n")


def parse_version_info(data: bytes) -> dict:
    """{"strings": {table_lang: {name: value}}, "translations": [(lang, codepage)], "file_version", "product_version"}"""

    out: dict = {"strings": {}, "translations": [], "file_version": "", "product_version": ""}
    try:
        key, value_start, value_bytes, children, end = _block(data, 0)
        if value_bytes >= 52 and _u32(data, value_start) == 0xFEEF04BD:
            fms, fls, pms, pls = struct.unpack_from("<IIII", data, value_start + 8)
            out["file_version"] = f"{fms >> 16}.{fms & 0xFFFF}.{fls >> 16}.{fls & 0xFFFF}"
            out["product_version"] = f"{pms >> 16}.{pms & 0xFFFF}.{pls >> 16}.{pls & 0xFFFF}"
        for pos in _children(data, children, end):
            name, _vs, _vb, sub, sub_end = _block(data, pos)
            if name == "StringFileInfo":
                for table in _children(data, sub, sub_end):
                    lang, _ts, _tb, entries, table_end = _block(data, table)
                    strings = out["strings"].setdefault(lang.lower(), {})
                    for entry in _children(data, entries, table_end):
                        label, start, _length, _c, entry_end = _block(data, entry)
                        value = _text(data, start, entry_end)
                        if label and value:
                            strings.setdefault(label, value)
            elif name == "VarFileInfo":
                for var in _children(data, sub, sub_end):
                    label, start, length, _c, _e = _block(data, var)
                    if label == "Translation":
                        for i in range(start, min(start + length, len(data)) - 3, 4):
                            out["translations"].append((_u16(data, i), _u16(data, i + 2)))
    except (struct.error, IndexError):
        pass
    return out


STRING_KEYS = {"ProductName": "product_name", "CompanyName": "company", "FileDescription": "description", "ProductVersion": "product_version",
               "FileVersion": "file_version", "LegalCopyright": "copyright", "OriginalFilename": "original_filename",
               "OriginalFileName": "original_filename", "InternalName": "internal_name", "Comments": "comments"}


def _pick_strings(info: dict) -> dict:
    tables = info.get("strings") or {}
    order = sorted(tables, key=lambda lang: (not lang.startswith("0409"), not lang.startswith("0000"), lang))
    out = {}
    for lang in order:
        for name, value in tables[lang].items():
            key = STRING_KEYS.get(name)
            if key and value and key not in out:
                out[key] = re.sub(r"\s{2,}", " ", value).strip()
    return out


# --- certificates (Authenticode PKCS#7) ------------------------------------------------------------------------------

def _element(data: bytes, pos: int) -> tuple[int, int, int, int]:
    """(tag, content_start, content_end, next) of the DER/BER element at ``pos``."""

    tag = data[pos]
    p = pos + 1
    if tag & 0x1F == 0x1F:
        while data[p] & 0x80:
            p += 1
        p += 1
    first = data[p]
    p += 1
    if first < 0x80:
        return tag, p, p + first, p + first
    count = first & 0x7F
    if count:
        length = int.from_bytes(data[p: p + count], "big")
        p += count
        if p + length > len(data):
            raise ValueError("truncated DER")
        return tag, p, p + length, p + length
    q = p  # BER indefinite length: children up to end-of-contents
    while data[q: q + 2] != b"\0\0":
        q = _element(data, q)[3]
    return tag, p, q, q + 2


def _items(data: bytes, start: int, end: int) -> list[tuple[int, int, int, int]]:
    out = []
    pos = start
    while pos < end and len(out) < 5000:
        if data[pos: pos + 2] == b"\0\0":
            break
        item = _element(data, pos)
        out.append(item)
        pos = item[3]
    return out


def _oid(raw: bytes) -> str:
    if not raw:
        return ""
    parts = [min(raw[0] // 40, 2), raw[0] - 40 * min(raw[0] // 40, 2)]
    value = 0
    for byte in raw[1:]:
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            parts.append(value)
            value = 0
    return ".".join(str(p) for p in parts)


def _string(tag: int, raw: bytes) -> str:
    if tag == 0x1E and len(raw) % 2 == 0:
        return raw.decode("utf-16-be", errors="replace")
    if tag == 0x1C:
        return raw.decode("utf-32-be", errors="replace")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


_NAME_OIDS = {"2.5.4.3": "CN", "2.5.4.10": "O", "2.5.4.6": "C", "2.5.4.7": "L", "2.5.4.8": "ST", "2.5.4.11": "OU"}
_DIGESTS = {"1.3.14.3.2.26": "sha1", "2.16.840.1.101.3.4.2.1": "sha256", "2.16.840.1.101.3.4.2.2": "sha384",
            "2.16.840.1.101.3.4.2.3": "sha512", "1.2.840.113549.2.5": "md5"}


def _name(data: bytes, item: tuple) -> dict:
    out: dict = {}
    for rdn in _items(data, item[1], item[2]):
        for pair in _items(data, rdn[1], rdn[2]):
            parts = _items(data, pair[1], pair[2])
            if len(parts) >= 2 and parts[0][0] == 0x06:
                label = _NAME_OIDS.get(_oid(data[parts[0][1]: parts[0][2]]))
                if label and label not in out:
                    out[label] = _string(parts[1][0], data[parts[1][1]: parts[1][2]])
    return out


def _time(text: str) -> str:
    """UTCTime (YYMMDDhhmmssZ) or GeneralizedTime (YYYYMMDDhhmmssZ) -> 'YYYY-MM-DD'."""

    digits = re.match(r"\d+", text)
    if not digits or len(digits.group(0)) not in (12, 14):
        return ""
    value = digits.group(0)
    if len(value) == 12:
        value = ("20" if int(value[:2]) < 50 else "19") + value
    return f"{value[:4]}-{value[4:6]}-{value[6:8]}"


def _numbers(version: str) -> tuple[int, ...]:
    parts = [int(p) for p in re.findall(r"\d+", version or "")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def best_version(info: dict) -> str:
    """The program version of a VersionInfo: the ProductVersion string when it agrees with the binary (fixed) one,
    else the fixed version ("8.981" vs 8.9.8.1 -> "8.9.8.1"), else whatever string there is."""

    text = (info.get("product_version") or info.get("file_version") or "").strip()
    fixed = info.get("fixed_product_version") or ""
    fixed = fixed if _numbers(fixed) not in {(), (0,)} else ""
    match = re.match(r"v?(\d+(?:[.,]\d+)*)", text)
    if match and (not fixed or _numbers(match.group(1)) == _numbers(fixed)):
        return match.group(1).replace(",", ".")
    if fixed:
        parts = list(_numbers(fixed))
        return ".".join(str(p) for p in parts + [0] * (2 - len(parts))) if parts else ""
    return text


def parse_pkcs7(blob: bytes) -> dict:
    """Signer of an Authenticode PKCS#7 SignedData blob:
    {"signed": True, "signer", "signer_org", "signer_country", "issuer", "program", "url", "digest_algorithm", "digest",
     "signing_time", "certificates"}; {} when the blob cannot be read."""

    try:
        top = _element(blob, 0)
        outer = _items(blob, top[1], top[2])
        if len(outer) < 2 or _oid(blob[outer[0][1]: outer[0][2]]) != "1.2.840.113549.1.7.2":
            return {}
        signed = _items(blob, outer[1][1], outer[1][2])[0]
        parts = _items(blob, signed[1], signed[2])
        content = parts[2]
        result: dict = {"signed": True, "signer": "", "signer_org": "", "signer_country": "", "issuer": "", "program": "", "url": "",
                        "digest_algorithm": "", "digest": "", "signing_time": "", "certificates": 0}
        inner = _items(blob, content[1], content[2])
        if len(inner) >= 2 and inner[1][0] == 0xA0:
            spc = _items(blob, inner[1][1], inner[1][2])[0]
            fields = _items(blob, spc[1], spc[2])
            digest_info = _items(blob, fields[1][1], fields[1][2]) if len(fields) > 1 else []
            if len(digest_info) == 2:
                alg = _items(blob, digest_info[0][1], digest_info[0][2])
                result["digest_algorithm"] = _DIGESTS.get(_oid(blob[alg[0][1]: alg[0][2]]), "") if alg else ""
                result["digest"] = blob[digest_info[1][1]: digest_info[1][2]].hex()
        certificates = []
        for part in parts[3:]:
            if part[0] != 0xA0:
                continue
            for cert in _items(blob, part[1], part[2]):
                tbs = _items(blob, cert[1], cert[2])[0]
                fields = _items(blob, tbs[1], tbs[2])
                if fields and fields[0][0] == 0xA0:
                    fields = fields[1:]
                if len(fields) >= 5:
                    certificates.append({"serial": blob[fields[0][1]: fields[0][2]], "issuer": _name(blob, fields[2]),
                                         "subject": _name(blob, fields[4])})
        result["certificates"] = len(certificates)
        signer_infos = _items(blob, parts[-1][1], parts[-1][2]) if parts[-1][0] == 0x31 else []
        leaf = None
        if signer_infos:
            info = _items(blob, signer_infos[0][1], signer_infos[0][2])
            issuer_serial = _items(blob, info[1][1], info[1][2])
            serial = blob[issuer_serial[1][1]: issuer_serial[1][2]] if len(issuer_serial) > 1 else b""
            leaf = next((c for c in certificates if c["serial"] == serial), None)
            for item in info:
                if item[0] != 0xA0:
                    continue
                for attribute in _items(blob, item[1], item[2]):
                    pair = _items(blob, attribute[1], attribute[2])
                    oid = _oid(blob[pair[0][1]: pair[0][2]])
                    if oid == "1.3.6.1.4.1.311.2.1.12":  # SpcSpOpusInfo: program name + more-info URL
                        opus = _items(blob, pair[1][1], pair[1][2])[0]
                        for field in _items(blob, opus[1], opus[2]):
                            choice = _items(blob, field[1], field[2])
                            if not choice:
                                continue
                            raw = blob[choice[0][1]: choice[0][2]]
                            if field[0] == 0xA0:  # SpcString: [0] BMPString or [1] IA5String
                                result["program"] = _string(0x1E if choice[0][0] == 0x80 else 0x16, raw).strip("\0 ")
                            elif choice[0][0] == 0x80:  # SpcLink: [0] IA5String URL
                                result["url"] = raw.decode("latin-1").strip("\0 ")
                    elif oid == "1.2.840.113549.1.9.5" and not result["signing_time"]:
                        value = _items(blob, pair[1][1], pair[1][2])[0]
                        result["signing_time"] = _time(blob[value[1]: value[2]].decode("ascii", errors="replace"))
        if leaf is None and certificates:
            issuers = {tuple(sorted(c["issuer"].items())) for c in certificates}
            leaf = next((c for c in certificates if tuple(sorted(c["subject"].items())) not in issuers), certificates[0])
        if leaf:
            result["signer"] = leaf["subject"].get("CN", "") or leaf["subject"].get("O", "")
            result["signer_org"] = leaf["subject"].get("O", "")
            result["signer_country"] = leaf["subject"].get("C", "")
            result["issuer"] = leaf["issuer"].get("CN", "") or leaf["issuer"].get("O", "")
        if not result["signing_time"]:
            stamp = re.search(rb"\x06\x09\x2a\x86\x48\x86\xf7\x0d\x01\x09\x05\x31.\x17.(\d{12})Z", blob, re.S) or \
                re.search(rb"\x18[\x0f-\x17](\d{14})(?:\.\d+)?Z", blob)
            if stamp:
                result["signing_time"] = _time(stamp.group(1).decode() + "Z")
        return result
    except (ValueError, IndexError, struct.error, RecursionError):  # truncated or crafted (deeply nested BER) blobs
        return {}


def _authenticode_digest(image: _Image, algorithm: str) -> str:
    """The Authenticode hash of the image (file without checksum, certificate directory entry and certificate table)."""

    digest = hashlib.new(algorithm)
    cert_offset, cert_size = image.directory(4)
    end = cert_offset if cert_offset and cert_offset <= image.size else image.size
    ranges = [(0, image.checksum_offset), (image.checksum_offset + 4, image.certdir_offset), (image.certdir_offset + 8, end)]
    if cert_offset and cert_offset + cert_size < image.size:
        ranges.append((cert_offset + cert_size, image.size))
    for start, stop in ranges:
        image.fh.seek(start)
        remaining = stop - start
        while remaining > 0:
            chunk = image.fh.read(min(remaining, 4 * 1024 * 1024))
            if not chunk:
                break
            digest.update(chunk)
            remaining -= len(chunk)
    return digest.hexdigest()


def _signature(image: _Image, verify: bool) -> dict:
    offset, size = image.directory(4)
    if not offset or size < 8 or offset + 8 > image.size:
        return {}
    table = image.read(offset, min(size, 4 * 1024 * 1024))
    length, _revision, cert_type = struct.unpack_from("<IHH", table, 0)
    if cert_type != 2:  # WIN_CERT_TYPE_PKCS_SIGNED_DATA
        return {}
    result = parse_pkcs7(table[8: max(8, min(length, len(table)))])
    if result and verify and result.get("digest_algorithm"):
        result["digest_ok"] = _authenticode_digest(image, result["digest_algorithm"]) == result["digest"]
    return result


# --- icon ------------------------------------------------------------------------------------------------------------

def _icon(image: _Image, resources: list) -> bytes:
    groups = [(path, rva, size) for path, rva, size in resources if path and path[0] == RT_GROUP_ICON]
    icons = {path[1]: (rva, size) for path, rva, size in resources if len(path) > 1 and path[0] == RT_ICON}
    if not groups:
        return b""
    data = image.resource_data(groups[0][1], groups[0][2], 64 * 1024)
    if len(data) < 6:
        return b""
    count = _u16(data, 4)
    best = None
    for i in range(count):
        pos = 6 + 14 * i
        if pos + 14 > len(data):
            break
        width, height, colors, _r, planes, bits, size, icon_id = struct.unpack_from("<BBBBHHIH", data, pos)
        rank = (width or 256, bits)
        if icon_id in icons and (best is None or rank > best[0]):
            best = (rank, width, height, colors, planes, bits, icon_id)
    if best is None:
        return b""
    _rank, width, height, colors, planes, bits, icon_id = best
    body = image.resource_data(*icons[icon_id], limit=4 * 1024 * 1024)
    if body.startswith(b"\x89PNG"):
        return body
    ico = struct.pack("<HHH", 0, 1, 1) + struct.pack("<BBBBHHII", width, height, colors, 0, planes, bits, len(body), 22) + body
    try:
        from PIL import Image  # optional dependency

        out = io.BytesIO()
        Image.open(io.BytesIO(ico)).save(out, format="PNG")
        return out.getvalue()
    except Exception:  # noqa: BLE001 - Pillow missing or an unusual DIB
        return ico


# --- public --------------------------------------------------------------------------------------------------------

def _inspect(fh: BinaryIO, size: int, *, signature: bool, verify: bool, icon: bool) -> dict:
    try:
        image = _Image(fh, size)
    except (PEError, struct.error):
        return {}
    arch = MACHINES.get(image.machine, f"0x{image.machine:x}")
    result: dict = {
        "machine": image.machine, "arch": arch, "pe32plus": image.pe32plus, "dll": bool(image.characteristics & 0x2000),
        "dotnet": False, "subsystem": SUBSYSTEMS.get(image.subsystem, str(image.subsystem)),
        "os_version": "%d.%d" % max(image.os_version, image.subsystem_version),
        "min_os": os_name(*max(image.os_version, image.subsystem_version)),
        "sections": [s[0] for s in image.sections], "overlay_offset": 0, "overlay_size": 0,
        "language_ids": [], "languages": [], "manifest_level": "", "supported_os": [], "resource_types": [], "signature": {},
    }
    if image.overlay_offset and image.overlay_offset < size:
        cert_offset, cert_size = image.directory(4)
        end = cert_offset if cert_offset >= image.overlay_offset else size
        result["overlay_offset"], result["overlay_size"] = image.overlay_offset, max(0, end - image.overlay_offset)
    clr_rva, clr_size = image.directory(14)
    if clr_rva and clr_size >= 20:
        header = image.read(image.offset(clr_rva), 24)
        if len(header) >= 20:
            flags = _u32(header, 16)
            result["dotnet"] = True
            if image.machine == 0x14C and flags & 0x1 and not flags & 0x2:
                result["arch"] = "anycpu"
    try:
        resources = image.resources()
    except (struct.error, RecursionError):
        resources = []
    result["resource_types"] = sorted({p[0] for p, _r, _s in resources if p and isinstance(p[0], int)})
    languages = []
    for path, rva, length in resources:
        if path and path[0] == RT_VERSION and not result.get("product_name"):
            info = parse_version_info(image.resource_data(rva, length, 256 * 1024))
            result.update({k: v for k, v in _pick_strings(info).items() if v})
            result["fixed_file_version"], result["fixed_product_version"] = info["file_version"], info["product_version"]
            languages += [lang for lang, _cp in info["translations"]]
            languages += [int(lang[:4], 16) for lang in info["strings"] if re.fullmatch(r"[0-9a-f]{8}", lang)]
        elif path and path[0] == RT_MANIFEST and not result["manifest_level"]:
            text = image.resource_data(rva, length, 256 * 1024).decode("utf-8", errors="replace")
            level = re.search(r"requestedExecutionLevel[^>]*level\s*=\s*[\"'](\w+)", text)
            result["manifest_level"] = level.group(1) if level else ""
            ids = re.findall(r"supportedOS\s+Id\s*=\s*[\"']\{([0-9a-fA-F-]{36})\}", text)
            result["supported_os"] = [SUPPORTED_OS[i.lower()] for i in ids if i.lower() in SUPPORTED_OS]
    result["language_ids"] = list(dict.fromkeys(f"{lang:04x}" for lang in languages if lang))
    result["languages"] = list(dict.fromkeys(n for n in (lcid_name(lang) for lang in languages) if n))
    if signature:
        result["signature"] = _signature(image, verify)
    if icon:
        result["icon"] = _icon(image, resources)
    result["version"] = best_version(result) if "fixed_product_version" in result else ""
    return result


def read(path: str | Path, *, signature: bool = True, verify: bool = False, icon: bool = False) -> dict:
    """Static facts of a PE file (see the module docstring); ``verify`` re-computes the Authenticode digest
    (reads the whole file), ``icon`` adds "icon": PNG bytes of the largest icon (ICO bytes without Pillow)."""

    try:
        with open(path, "rb") as fh:
            size = Path(path).stat().st_size
            return _inspect(fh, size, signature=signature, verify=verify, icon=icon)
    except OSError:
        return {}


def parse(data: bytes, *, signature: bool = True, verify: bool = False, icon: bool = False) -> dict:
    """read() for bytes already in memory."""

    return _inspect(io.BytesIO(data), len(data), signature=signature, verify=verify, icon=icon)


def overlay(path: str | Path, size: int = 64) -> bytes:
    """The first ``size`` bytes after the last section (installer payload signatures live there)."""

    info = read(path, signature=False)
    if not info.get("overlay_offset"):
        return b""
    try:
        with open(path, "rb") as fh:
            fh.seek(info["overlay_offset"])
            return fh.read(size)
    except OSError:
        return b""


def icon(path: str | Path) -> bytes:
    """PNG bytes of the file's largest icon ('' when it has none)."""

    return read(path, signature=False, icon=True).get("icon", b"")
