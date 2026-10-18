"""Windows installation media: editions, build, version, languages and architecture from sources/install.wim|esd.

Only the WIM header (208 bytes) and its XML metadata resource are read, never the multi-GB image itself:

1. UDF (the file system of Microsoft ISOs; their ISO 9660 part only holds a README) is walked directly to find the
   extents of sources/install.wim|esd, then the header and the XML are read at their offsets — a few KB of I/O.
2. If that fails, ``7z e -so`` streams the WIM out of the ISO and only the header and the XML part are kept
   (no temporary file, no free space needed, but the whole WIM is read once).
3. A folder release (an unpacked ISO) or a bare .wim/.esd is read in place (``wiminfo`` of wimtools as fallback).

``inspect(path)`` returns
    {"label": "CCCOMA_X64FRE_HU-HU_DV9", "wim": "sources/install.esd", "wim_size", "method": "udf" | "7z" | "file" | "wiminfo",
     "images": [{"index", "name", "display_name", "edition", "arch", "build": "26100.1742", "version": "10.0.26100.1742",
                 "languages": ["hu-HU"], "default_language", "installation_type", "size"}],
     "editions": ["Windows 11 Pro", ...], "product": "Windows 11", "version": "24H2", "build": "26100.1742",
     "arch": "x64", "language_codes": ["hu-HU"], "languages": ["magyar"], "server": False, "sha256": "" | hex}
{} when the ISO holds no Windows image (or nothing can be read).
"""

from __future__ import annotations

import hashlib
import re
import shutil
import struct
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

from .. import safeio
from typing import Callable

from . import installer

SECTOR = 2048
WIM_NAMES = ("sources/install.wim", "sources/install.esd", "sources/install.swm", "x64/sources/install.wim", "x64/sources/install.esd",
             "x86/sources/install.wim", "x86/sources/install.esd")
WIM_ARCH = {0: "x86", 5: "arm", 6: "ia64", 9: "x64", 12: "arm64"}
CULTURES = {
    "en-gb": "angol (nemzetközi)", "en-us": "angol", "pt-br": "portugál (brazil)", "pt-pt": "portugál", "zh-cn": "kínai (egyszerűsített)",
    "zh-tw": "kínai (hagyományos)", "zh-hk": "kínai (hagyományos)", "es-mx": "spanyol (Mexikó)", "fr-ca": "francia (Kanada)",
    "sr-latn-rs": "szerb (latin betűs)", "sr-latn-cs": "szerb (latin betűs)",
}
Reader = Callable[[int, int], bytes]


def culture_name(code: str) -> str:
    from ..describe import language

    code = (code or "").lower()
    return CULTURES.get(code) or language(code.split("-")[0])


def file_reader(path: str | Path) -> Reader:
    """read(offset, size) over a local file (opened per call: no handle is kept)."""

    def read(offset: int, size: int) -> bytes:
        with open(path, "rb") as fh:
            fh.seek(offset)
            return fh.read(size)

    return read


# --- ISO 9660 / UDF ---------------------------------------------------------------------------------------------

def iso_label(read: Reader) -> str:
    """Volume label: ISO 9660 primary volume descriptor, else the UDF logical volume identifier."""

    pvd = read(16 * SECTOR, SECTOR)
    if pvd[1:6] == b"CD001":
        label = pvd[40:72].decode("ascii", errors="replace").strip()
        if label:
            return label
    try:
        return _udf_volume(read)["label"]
    except (ValueError, struct.error, IndexError):
        return ""


def parse_label(label: str) -> dict:
    """'CCCOMA_X64FRE_HU-HU_DV9' -> {"arch": "x64", "language": "hu-HU", "edition": "consumer"}."""

    out = {"arch": "", "language": "", "edition": ""}
    match = re.search(r"(?i)(?:^|_)(X64|X86|A64|ARM64|AMD64)F[A-Z]*_([A-Z]{2}-[A-Z]{2,4})(?:_|$)", label or "")
    if match:
        out["arch"] = {"x64": "x64", "amd64": "x64", "x86": "x86", "a64": "arm64", "arm64": "arm64"}[match.group(1).lower()]
        parts = match.group(2).split("-")
        out["language"] = parts[0].lower() + "-" + parts[1].upper()
    prefix = (label or "").upper().split("_")
    first = prefix[1] if prefix and prefix[0] in {"J", "K"} and len(prefix) > 1 else (prefix[0] if prefix else "")
    out["edition"] = {"CCCOMA": "consumer", "CPBA": "consumer", "CCSA": "business", "CPRA": "business", "CENA": "enterprise",
                      "SSS": "server", "CES": "enterprise"}.get(first, "")
    return out


def _tag(data: bytes) -> int:
    return struct.unpack_from("<H", data, 0)[0] if len(data) >= 16 else -1


def _udf_volume(read: Reader) -> dict:
    anchor = read(256 * SECTOR, SECTOR)
    if _tag(anchor) != 2:
        raise ValueError("no UDF anchor")
    length, location = struct.unpack_from("<II", anchor, 16)
    volume: dict = {"label": "", "partition": None, "block": SECTOR, "fsd": None}
    for index in range(min(length // SECTOR, 64)):
        block = read((location + index) * SECTOR, SECTOR)
        tag = _tag(block)
        if tag == 5 and volume["partition"] is None:
            volume["partition"] = struct.unpack_from("<I", block, 188)[0]
        elif tag == 6 and volume["fsd"] is None:
            raw = block[84:84 + 128]
            volume["label"] = _dstring(raw[: raw[127]] if 0 < raw[127] < 128 else raw.rstrip(b"\0"))
            volume["block"] = struct.unpack_from("<I", block, 212)[0] or SECTOR
            volume["fsd"] = struct.unpack_from("<I", block, 252)[0]
        elif tag == 8:
            break
    if volume["partition"] is None or volume["fsd"] is None:
        raise ValueError("incomplete UDF volume")
    return volume


def _dstring(raw: bytes) -> str:
    if not raw:
        return ""
    if raw[0] == 16:
        return raw[1:len(raw) - (len(raw) - 1) % 2].decode("utf-16-be", errors="replace").rstrip("\0")
    return raw[1:].decode("latin-1").rstrip("\0") if raw[0] == 8 else raw.decode("latin-1").rstrip("\0")


def _file_entry(read: Reader, volume: dict, lbn: int) -> tuple[int, list[tuple[int, int]], bytes]:
    """(information length, [(absolute offset, length)], embedded data) of a UDF (extended) file entry."""

    block, start = volume["block"], volume["partition"]
    data = read((start + lbn) * block, block)
    tag = _tag(data)
    if tag == 261:
        info, ea, ad, base = struct.unpack_from("<Q", data, 56)[0], *struct.unpack_from("<II", data, 168), 176
    elif tag == 266:
        info, ea, ad, base = struct.unpack_from("<Q", data, 56)[0], *struct.unpack_from("<II", data, 208), 216
    else:
        raise ValueError(f"no file entry at block {lbn}")
    kind = struct.unpack_from("<H", data, 16 + 18)[0] & 7
    descriptors = data[base + ea: base + ea + ad]
    if kind == 3:
        return info, [], descriptors[:info]
    extents: list[tuple[int, int]] = []
    size = 8 if kind == 0 else 16
    for _hop in range(64):
        next_block = None
        for pos in range(0, len(descriptors) - size + 1, size):
            raw_length, position = struct.unpack_from("<II", descriptors, pos)
            length, extent_type = raw_length & 0x3FFFFFFF, raw_length >> 30
            if not length:
                break
            if extent_type == 3:
                next_block = position
                break
            if extent_type == 0:
                extents.append(((start + position) * block, length))
        if next_block is None:
            break
        more = read((start + next_block) * block, block)  # allocation extent descriptor (tag 258)
        if _tag(more) != 258:
            break
        descriptors = more[24: 24 + struct.unpack_from("<I", more, 20)[0]]
    return info, extents, b""


def _read_extents(read: Reader, extents: list[tuple[int, int]], offset: int, size: int) -> bytes:
    """Bytes [offset, offset+size) of a file stored in the given extents."""

    out = b""
    position = 0
    for start, length in extents:
        if offset < position + length and offset + size > position:
            begin = max(offset, position)
            end = min(offset + size, position + length)
            out += read(start + begin - position, end - begin)
        position += length
        if position >= offset + size:
            break
    return out


def _directory(read: Reader, volume: dict, lbn: int) -> list[tuple[str, bool, int]]:
    info, extents, embedded = _file_entry(read, volume, lbn)
    data = embedded or _read_extents(read, extents, 0, min(info, 4 * 1024 * 1024))
    out = []
    pos = 0
    while pos + 38 <= len(data):
        if _tag(data[pos: pos + 16]) != 257:
            break
        characteristics, name_length = data[pos + 18], data[pos + 19]
        icb_lbn = struct.unpack_from("<I", data, pos + 24)[0]
        use_length = struct.unpack_from("<H", data, pos + 36)[0]
        raw = data[pos + 38 + use_length: pos + 38 + use_length + name_length]
        if not characteristics & 0x0C:  # neither the parent entry nor a deleted one
            out.append((_dstring(raw), bool(characteristics & 0x02), icb_lbn))
        pos += (38 + use_length + name_length + 3) & ~3
    return out


def udf_find(read: Reader, path: str) -> tuple[int, list[tuple[int, int]]] | None:
    """(size, extents) of a file in a UDF image, case-insensitive path ("sources/install.wim"); None when missing."""

    try:
        volume = _udf_volume(read)
        fsd = read((volume["partition"] + volume["fsd"]) * volume["block"], volume["block"])
        if _tag(fsd) != 256:
            return None
        lbn = struct.unpack_from("<I", fsd, 404)[0]
        parts = [p for p in path.replace("\\", "/").split("/") if p]
        for index, part in enumerate(parts):
            entries = {name.lower(): (is_dir, icb) for name, is_dir, icb in _directory(read, volume, lbn)}
            if part.lower() not in entries:
                return None
            is_dir, lbn = entries[part.lower()]
            if index < len(parts) - 1 and not is_dir:
                return None
        info, extents, _embedded = _file_entry(read, volume, lbn)
        return info, extents
    except (ValueError, struct.error, IndexError, OSError):
        return None


# --- WIM ----------------------------------------------------------------------------------------------------------

def parse_wim_header(data: bytes) -> dict:
    """{"version", "flags", "compression", "parts", "part", "images", "boot_index", "xml_offset", "xml_size"} or {}."""

    if len(data) < 208 or data[:8] not in (b"MSWIM\0\0\0", b"WLPWM\0\0\0"):
        return {}
    _size, version, flags, _chunk = struct.unpack_from("<IIII", data, 8)
    part, parts, images = struct.unpack_from("<HHI", data, 40)
    xml_size = int.from_bytes(data[72:79], "little")
    xml_offset, xml_original = struct.unpack_from("<QQ", data, 80)
    compression = "lzms" if flags & 0x80000 else "lzx" if flags & 0x40000 else "xpress" if flags & 0x20000 else "none"
    return {"version": version, "flags": flags, "compression": compression, "part": part, "parts": parts, "images": images,
            "boot_index": struct.unpack_from("<I", data, 120)[0], "xml_offset": xml_offset, "xml_size": xml_original or xml_size,
            "pipable": data[:8] == b"WLPWM\0\0\0"}


def _decode_xml(raw: bytes) -> str:
    if raw.startswith(b"\xff\xfe") or (len(raw) > 1 and raw[1:2] == b"\0"):
        return raw.decode("utf-16-le", errors="replace").lstrip("﻿")
    return raw.decode("utf-8", errors="replace").lstrip("﻿")


def parse_wim_xml(text: str) -> list[dict]:
    """The images described by a WIM's XML metadata."""

    text = (text or "").strip("\0 \r\n﻿")
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    images = []
    for image in root.findall("IMAGE"):
        windows = image.find("WINDOWS")

        def value(node, path: str) -> str:
            found = node.find(path) if node is not None else None
            return (found.text or "").strip() if found is not None and found.text else ""

        major, minor = value(windows, "VERSION/MAJOR"), value(windows, "VERSION/MINOR")
        build, spbuild = value(windows, "VERSION/BUILD"), value(windows, "VERSION/SPBUILD")
        languages = [(n.text or "").strip() for n in (windows.findall("LANGUAGES/LANGUAGE") if windows is not None else []) if n.text]
        arch = value(windows, "ARCH")
        size = value(image, "TOTALBYTES")
        images.append({
            "index": int(image.get("INDEX", "0") or 0),
            "name": value(image, "NAME"),
            "display_name": value(image, "DISPLAYNAME") or value(image, "NAME"),
            "description": value(image, "DESCRIPTION"),
            "edition": value(windows, "EDITIONID") or value(image, "FLAGS"),
            "installation_type": value(windows, "INSTALLATIONTYPE"),
            "product_type": value(windows, "PRODUCTTYPE"),
            "arch": WIM_ARCH.get(int(arch), arch) if arch.isdigit() else arch,
            "build": f"{build}.{spbuild}" if build and spbuild else build,
            "version": ".".join(x for x in (major, minor, build, spbuild) if x),
            "languages": languages,
            "default_language": value(windows, "LANGUAGES/DEFAULT"),
            "size": int(size) if size.isdigit() else 0,
        })
    return images


def read_wim(read: Reader) -> dict:
    """{"header": parse_wim_header(), "images": [...]} from a reader positioned at the WIM's first byte."""

    header = parse_wim_header(read(0, 208))
    if not header or not header["xml_offset"] or not 0 < header["xml_size"] < 64 * 1024 * 1024:
        return {}
    return {"header": header, "images": parse_wim_xml(_decode_xml(read(header["xml_offset"], header["xml_size"])))}


WIMINFO_ARCH = {"x86": "x86", "x86_64": "x64", "amd64": "x64", "arm": "arm", "arm64": "arm64", "aarch64": "arm64", "ia64": "ia64"}


def parse_wiminfo(text: str) -> list[dict]:
    """The images of a ``wiminfo`` listing, in the shape of parse_wim_xml() (fallback reader for local WIM files)."""

    images: list[dict] = []
    current: dict | None = None
    for line in (text or "").splitlines():
        match = re.match(r"^([A-Za-z][\w ]*?):\s+(.*)$", line.strip())
        if not match:
            continue
        key, value = match.group(1).strip(), match.group(2).strip()
        if key == "Index" and value.isdigit():
            current = {"Index": value}
            images.append(current)
        elif current is not None:
            current[key] = value
    out = []
    for raw in images:
        build, spbuild = raw.get("Build", ""), raw.get("Service Pack Build", "")
        out.append({
            "index": int(raw["Index"]), "name": raw.get("Name", ""), "display_name": raw.get("Display Name") or raw.get("Name", ""),
            "description": raw.get("Description", ""), "edition": raw.get("Edition ID") or raw.get("Flags", ""),
            "installation_type": raw.get("Installation Type", ""), "product_type": raw.get("Product Type", ""),
            "arch": WIMINFO_ARCH.get(raw.get("Architecture", "").lower(), raw.get("Architecture", "").lower()),
            "build": f"{build}.{spbuild}" if build and spbuild else build,
            "version": ".".join(x for x in (raw.get("Major Version", ""), raw.get("Minor Version", ""), build, spbuild) if x),
            "languages": raw.get("Languages", "").split(), "default_language": raw.get("Default Language", ""),
            "size": int(raw["Total Bytes"]) if raw.get("Total Bytes", "").isdigit() else 0,
        })
    return out


def wiminfo_images(path: str | Path) -> list[dict]:
    """parse_wiminfo() of ``wiminfo <file>`` (wimtools), [] when the tool is missing or fails."""

    if not shutil.which("wiminfo"):
        return []
    try:
        run = safeio.run(["wiminfo", str(path)], timeout=120)
    except (OSError, subprocess.SubprocessError):
        return []
    return parse_wiminfo(run.stdout.decode("utf-8", errors="replace")) if run.returncode == 0 else []


def _local_wim(path: Path, name: str) -> dict:
    """read_wim() of a WIM/ESD file on disk, with wiminfo as the fallback reader."""

    wim = read_wim(file_reader(path))
    images = wim.get("images") or wiminfo_images(path)
    if not images:
        return {}
    return {"label": "", "wim": name, "wim_size": path.stat().st_size, "method": "file" if wim.get("images") else "wiminfo",
            "header": wim.get("header", {}), "images": images, **summarize(images)}


def _wim_from_stream(stream) -> dict:
    """read_wim() over a forward-only stream (7z -so): the header, then skip to the XML resource."""

    head = stream.read(208)
    header = parse_wim_header(head)
    if not header or not 0 < header["xml_size"] <= safeio.META_LIMIT or header["xml_offset"] < 208:
        return {}
    remaining = header["xml_offset"] - 208
    while remaining > 0:
        chunk = stream.read(min(remaining, 8 * 1024 * 1024))
        if not chunk:
            return {}
        remaining -= len(chunk)
    raw = b""
    while len(raw) < header["xml_size"]:
        chunk = stream.read(header["xml_size"] - len(raw))
        if not chunk:
            break
        raw += chunk
    return {"header": header, "images": parse_wim_xml(_decode_xml(raw))}


def _wim_via_7z(iso: Path, member: str) -> dict:
    tool = installer._sevenzip()
    if not tool:
        return {}
    try:
        with safeio.pipe([tool, "e", "-so", "-pnone", "-spd", "--", str(iso), member], timeout=120) as stream:
            return _wim_from_stream(stream)
    except (OSError, subprocess.SubprocessError):
        return {}


# --- summary ------------------------------------------------------------------------------------------------------

def summarize(images: list[dict]) -> dict:
    """Product, version name, build, architecture and languages of a set of WIM images."""

    from ..sources import msdownload

    client = [i for i in images if i.get("installation_type", "").lower() != "windows pe" and i.get("build")]
    images = client or images
    if not images:
        return {}
    first = images[0]
    build = max((i["build"] for i in images if i.get("build")), key=lambda b: [int(x) for x in re.findall(r"\d+", b)], default="")
    server = any("server" in (i.get("installation_type", "") + i.get("edition", "") + i.get("name", "")).lower() for i in images)
    known = msdownload.version_for_build(build, server=server, refresh=False) if build else {}
    names = [i.get("name") or i.get("display_name") for i in images]
    product = known.get("product", "")
    match = next((re.match(r"(Windows (?:Server )?\d+(?:\.\d+)?)", n or "") for n in names if n and n.startswith("Windows")), None)
    if match and (not product or product.split()[-1] != match.group(1).split()[-1]):
        product = match.group(1)
    codes = list(dict.fromkeys(code for i in images for code in i.get("languages", [])))
    archs = list(dict.fromkeys(i.get("arch", "") for i in images if i.get("arch")))
    return {
        "editions": list(dict.fromkeys(n for n in names if n)),
        "product": product,
        "version": known.get("version", ""),
        "build": build,
        "arch": "x86-x64" if set(archs) >= {"x86", "x64"} else (archs[0] if archs else first.get("arch", "")),
        "language_codes": codes,
        "languages": list(dict.fromkeys(culture_name(c) for c in codes)),
        "server": server,
    }


def sha256(path: str | Path, chunk: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            while True:
                block = fh.read(chunk)
                if not block:
                    break
                digest.update(block)
    except OSError:
        return ""
    return digest.hexdigest()


def inspect_reader(read: Reader) -> dict:
    """inspect() over any random-access reader of an ISO image (local file, HTTP range reader in probes)."""

    label = iso_label(read)
    for name in WIM_NAMES:
        found = udf_find(read, name)
        if not found:
            continue
        size, extents = found
        wim = read_wim(lambda offset, length: _read_extents(read, extents, offset, length))
        if wim.get("images"):
            return {"label": label, "wim": name, "wim_size": size, "method": "udf", "header": wim["header"], "images": wim["images"],
                    **summarize(wim["images"])}
    return {"label": label} if label else {}


def inspect(path: str | Path, *, hash_iso: bool = False) -> dict:
    """Windows media facts of an ISO file, an unpacked ISO folder or a bare .wim/.esd (see the module docstring)."""

    path = Path(path)
    result: dict = {}
    if path.is_dir():
        # the media may sit one level down ("Win11_25H2/sources/install.esd") and names may be upper case
        found = {p.relative_to(path).as_posix().lower(): p for p in path.rglob("*") if p.name.lower().startswith("install.") and p.is_file()}
        ranked = sorted((rel.count("/"), WIM_NAMES.index(wim), rel) for rel in found for wim in WIM_NAMES
                        if rel == wim or rel.endswith("/" + wim))
        if not ranked:
            return {}
        rel = ranked[0][2]
        return _local_wim(found[rel], rel)
    if path.suffix.lower() in {".wim", ".esd", ".swm"}:
        return _local_wim(path, path.name)
    result = inspect_reader(file_reader(path))
    if not result.get("images"):
        listing = installer.list_archive(path)
        members = {e["path"].lower(): e for e in listing.get("entries") or []}
        for name in WIM_NAMES:
            if name in members:
                wim = _wim_via_7z(path, members[name]["path"])
                if wim.get("images"):
                    result.update({"wim": name, "wim_size": members[name]["size"], "method": "7z", "header": wim["header"],
                                   "images": wim["images"], **summarize(wim["images"])})
                break
    if not result.get("images"):
        return {}
    if not result.get("arch") or not result.get("language_codes"):
        hint = parse_label(result.get("label", ""))
        result["arch"] = result.get("arch") or hint["arch"]
        if not result.get("language_codes") and hint["language"]:
            result["language_codes"], result["languages"] = [hint["language"]], [culture_name(hint["language"])]
    result["sha256"] = sha256(path) if hash_iso else ""
    return result


def is_windows_media(paths: list[str]) -> bool:
    """True when a file list (of a folder release or an ISO listing) holds a Windows install image."""

    return any(p.replace("\\", "/").lower().endswith(tuple(WIM_NAMES)) for p in paths)
