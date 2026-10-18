"""Offline tests of the console/mobile file readers (ncore_up.local.consoleid / apk / ipa) on small synthetic files
built here byte by byte from the public format descriptions."""

from __future__ import annotations

import json
import plistlib
import shutil
import struct
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from ncore_up import release as rel
from ncore_up.local import apk, consoleid, ipa

FIXTURES = Path(__file__).parent / "fixtures" / "console_mobile"


# --- builders -----------------------------------------------------------------------------------------------------

def pfs0(files: list[tuple[str, bytes]]) -> bytes:
    names, offsets = b"", []
    for name, _ in files:
        offsets.append(len(names))
        names += name.encode() + b"\0"
    names += b"\0" * (-(0x10 + len(files) * 0x18 + len(names)) % 0x20)
    header = b"PFS0" + struct.pack("<III", len(files), len(names), 0)
    data, entries = b"", b""
    for (name, content), name_offset in zip(files, offsets):
        entries += struct.pack("<QQII", len(data), len(content), name_offset, 0)
        data += content
    return header + entries + names + data


def sfo(items: dict) -> bytes:
    keys = sorted(items)
    key_table = b"".join(k.encode() + b"\0" for k in keys)
    key_table += b"\0" * (-len(key_table) % 4)
    entries, data, key_offset = b"", b"", 0
    for key in keys:
        value = items[key]
        if isinstance(value, int):
            raw, fmt, length, max_len = struct.pack("<I", value), 0x0404, 4, 4
        else:
            raw = value.encode() + b"\0"
            fmt, length = 0x0204, len(raw)
            max_len = length + (-length % 4)
            raw += b"\0" * (max_len - length)
        entries += struct.pack("<HHIII", key_offset, fmt, length, max_len, len(data))
        data += raw
        key_offset += len(key.encode()) + 1
    key_start = 0x14 + len(entries)
    return b"\0PSF" + struct.pack("<IIII", 0x0101, key_start, key_start + len(key_table), len(keys)) + entries + key_table + data


PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 24


def ps4_pkg(content_id: str, param: bytes, icon: bytes = PNG) -> bytes:
    header = bytearray(0x1000)
    header[0:4] = b"\x7fCNT"
    struct.pack_into(">I", header, 0x10, 2)
    struct.pack_into(">I", header, 0x18, 0x1000)
    header[0x40:0x40 + 36] = content_id.encode()
    sfo_offset = 0x1000 + 0x40
    table = struct.pack(">IIIIII8x", 0x1000, 0, 0, 0, sfo_offset, len(param))
    table += struct.pack(">IIIIII8x", 0x1200, 0, 0, 0, sfo_offset + len(param), len(icon))
    return bytes(header) + table + param + icon


def ps3_pkg(content_id: str, pkg_type: int = 1) -> bytes:
    header = bytearray(0x100)
    header[0:4] = b"\x7fPKG"
    struct.pack_into(">HH", header, 4, 0x8000, pkg_type)
    struct.pack_into(">II", header, 8, 0xC0, 1)
    struct.pack_into(">Q", header, 0x18, 0x100)
    header[0x30:0x30 + 36] = content_id.encode()
    struct.pack_into(">II I", header, 0xC0, 2, 4, 5)
    return bytes(header)


def _dir_record(name: bytes, lba: int, size: int, directory: bool) -> bytes:
    body = struct.pack("<B", 0) + struct.pack("<I", lba) + struct.pack(">I", lba) + struct.pack("<I", size) + struct.pack(">I", size)
    body += b"\0" * 7 + bytes([2 if directory else 0, 0, 0]) + struct.pack("<H", 1) + struct.pack(">H", 1) + bytes([len(name)]) + name
    record = bytes([len(body) + 1]) + body
    return record + (b"\0" if len(record) % 2 else b"")


def iso9660(files: dict[str, bytes]) -> bytes:
    """2048-byte ISO with root files and one level of folders ("PS3_GAME/PARAM.SFO")."""

    folders = sorted({p.split("/")[0] for p in files if "/" in p})
    root_lba, next_lba = 18, 19 + len(folders)
    layout, data = {}, b""
    for path, content in files.items():
        layout[path] = (next_lba + len(data) // 2048, len(content))
        data += content + b"\0" * (-len(content) % 2048)

    def directory(own: int, parent: int, entries: list[tuple[bytes, int, int, bool]]) -> bytes:
        out = _dir_record(b"\0", own, 2048, True) + _dir_record(b"\1", parent, 2048, True)
        for name, lba, size, is_dir in entries:
            out += _dir_record(name, lba, size, is_dir)
        return out + b"\0" * (2048 - len(out))

    root_entries = [(f.encode(), 19 + i, 2048, True) for i, f in enumerate(folders)]
    root_entries += [(p.encode() + b";1", *layout[p], False) for p in files if "/" not in p]
    sectors = [b"\0" * 2048] * 16
    pvd = bytearray(2048)
    pvd[0], pvd[1:6], pvd[6] = 1, b"CD001", 1
    pvd[40:72] = b"TESTDISC".ljust(32)
    pvd[156:156 + 34] = _dir_record(b"\0", root_lba, 2048, True)
    terminator = bytearray(2048)
    terminator[0], terminator[1:6] = 255, b"CD001"
    sectors += [bytes(pvd), bytes(terminator), directory(root_lba, root_lba, root_entries)]
    for index, folder in enumerate(folders):
        entries = [(p.split("/")[1].encode() + b";1", *layout[p], False) for p in files if p.startswith(folder + "/")]
        sectors.append(directory(19 + index, root_lba, entries))
    return b"".join(sectors) + data


def raw_bin(iso: bytes) -> bytes:
    """Mode 2 Form 1 raw image (2352-byte sectors) of a 2048-byte ISO."""

    out = []
    for lba in range(len(iso) // 2048):
        sector = b"\x00" + b"\xff" * 10 + b"\x00" + bytes([0, 2, lba % 75, 2]) + b"\0" * 8 + iso[lba * 2048:(lba + 1) * 2048] + b"\0" * 280
        out.append(sector)
    return b"".join(out)


def wii_header(id6: str, title: str, gamecube: bool = False) -> bytes:
    header = bytearray(0x440)
    header[0:6] = id6.encode()
    header[7] = 1
    if gamecube:
        struct.pack_into(">I", header, 0x1C, consoleid.GC_MAGIC)
    else:
        struct.pack_into(">I", header, 0x18, consoleid.WII_MAGIC)
    header[0x20:0x20 + len(title)] = title.encode()
    return bytes(header)


def tmd(title_id: int, version: int, encrypted: bool = False) -> bytes:
    body = bytearray(4 + 0x100 + 0x3C + 0xC4 + 64 * 0x24 + 0x30)
    struct.pack_into(">I", body, 0, 0x10004)
    head = 4 + 0x100 + 0x3C
    struct.pack_into(">Q", body, head + 0x4C, title_id)
    struct.pack_into(">HH", body, head + 0x9C, version, 1)
    struct.pack_into(">IHHQ", body, head + 0xC4 + 64 * 0x24, 0, 0, 1 if encrypted else 0, 0x400)
    return bytes(body)


def ncch(title_id: int, product_code: str) -> bytes:
    header = bytearray(0x200)
    header[0x100:0x104] = b"NCCH"
    struct.pack_into("<Q", header, 0x108, title_id)
    header[0x110:0x112] = b"01"
    struct.pack_into("<Q", header, 0x118, title_id)
    header[0x150:0x150 + len(product_code)] = product_code.encode()
    return bytes(header)


def smdh(title: str, publisher: str, lockout: int) -> bytes:
    data = bytearray(0x36C0)
    data[0:4] = b"SMDH"
    base = 8 + 1 * 0x200  # English
    short = title.encode("utf-16-le")
    data[base:base + len(short)] = short
    pub = publisher.encode("utf-16-le")
    data[base + 0x180:base + 0x180 + len(pub)] = pub
    struct.pack_into("<I", data, 0x2018, lockout)
    return bytes(data)


def _pad64(data: bytes) -> bytes:
    return data + b"\0" * (-len(data) % 64)


def cia(title_id: int, version: int, product_code: str, title: str, lockout: int = 0x4) -> bytes:
    cert, ticket, tmd_data = b"\0" * 0xA00, b"\0" * 0x350, tmd(title_id, version)
    content = ncch(title_id, product_code) + b"\0" * 0x200
    meta = b"\0" * 0x400 + smdh(title, "Nintendo", lockout)
    header = bytearray(0x2020)
    struct.pack_into("<IHHIIII", header, 0, 0x2020, 0, 0, len(cert), len(ticket), len(tmd_data), len(meta))
    struct.pack_into("<Q", header, 0x18, len(content))
    return b"".join(_pad64(x) for x in (bytes(header), cert, ticket, tmd_data, content, meta))


def ncsd(title_id: int, product_code: str) -> bytes:
    header = bytearray(0x4000)
    header[0x100:0x104] = b"NCSD"
    struct.pack_into("<Q", header, 0x108, title_id)
    struct.pack_into("<II", header, 0x120, 0x4000 // 0x200, 0x10)
    return bytes(header) + ncch(title_id, product_code)


def xex(title_id: int, media_id: int, region: int) -> bytes:
    data = bytearray(0x300)
    data[0:4] = b"XEX2"
    struct.pack_into(">IIIII", data, 4, 0, 0x2000, 0, 0x100, 1)
    struct.pack_into(">II", data, 0x18, 0x00040006, 0x60)
    struct.pack_into(">IIIIBBBBI", data, 0x60, media_id, 0x10000000, 0, title_id, 0, 0, 1, 2, 0)
    struct.pack_into(">I", data, 0x100 + 0x178, region)
    return bytes(data)


def xiso(files: dict[str, bytes]) -> bytes:
    """Stripped XDVDFS image (game partition at 0) with files in the root directory."""

    image = bytearray(0x10000)
    volume = bytearray(0x800)
    volume[0:20] = consoleid.XDVDFS_MAGIC
    struct.pack_into("<II", volume, 0x14, 33, 0x800)
    volume[0x7EC:0x7EC + 20] = consoleid.XDVDFS_MAGIC
    image += volume
    table, data, sector = bytearray(), b"", 34
    names = list(files)
    for index, name in enumerate(names):
        right = 0
        entry = struct.pack("<HHIIBB", 0, 0, sector + len(data) // 2048, len(files[name]), 0x20, len(name)) + name.encode()
        entry += b"\xff" * (-len(entry) % 4)
        if index + 1 < len(names):
            right = (len(table) + len(entry)) // 4
            entry = struct.pack("<HH", 0, right) + entry[4:]
        table += entry
        data += files[name] + b"\0" * (-len(files[name]) % 2048)
    image += bytes(table) + b"\xff" * (0x800 - len(table))
    return bytes(image) + data


def stfs(title_id: int, name: str, publisher: str) -> bytes:
    data = bytearray(0x2000)
    data[0:4] = b"CON "
    struct.pack_into(">I", data, 0x344, 0x7000)
    struct.pack_into(">III", data, 0x354, 0x1B3DCDA5, 0, 0)
    struct.pack_into(">I", data, 0x360, title_id)
    data[0x366], data[0x367] = 1, 1
    for offset, text in ((0x411, name), (0x1611, publisher), (0x1691, name)):
        raw = text.encode("utf-16-be")
        data[offset:offset + len(raw)] = raw
    return bytes(data)


def xbe(title_id: int, title: str, regions: int) -> bytes:
    data = bytearray(0x400)
    data[0:4] = b"XBEH"
    struct.pack_into("<I", data, 0x104, 0x10000)
    struct.pack_into("<I", data, 0x118, 0x10000 + 0x200)
    struct.pack_into("<III", data, 0x200, 0x1D0, 0, title_id)
    raw = title.encode("utf-16-le")
    data[0x20C:0x20C + len(raw)] = raw
    struct.pack_into("<I", data, 0x200 + 0xA0, regions)
    struct.pack_into("<I", data, 0x200 + 0xAC, 3)
    return bytes(data)


def macho(cryptid: int, fat: bool = False) -> bytes:
    commands = struct.pack("<IIIII", 0x2C, 24, 0x4000, 0x1000, cryptid) + b"\0" * 4
    thin = struct.pack("<IiiIIII", 0xFEEDFACF, 0x0100000C, 0, 2, 1, len(commands), 0) + b"\0" * 4 + commands
    if not fat:
        return thin
    offset = 0x4000
    head = struct.pack(">II", 0xCAFEBABE, 1) + struct.pack(">iiIII", 0x0100000C, 0, offset, len(thin), 14)
    return head + b"\0" * (offset - len(head)) + thin


def ipa_file(path: Path, *, plist: dict, binary: bytes, hungarian: bool = True, itunes_meta: dict | None = None) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("Payload/Test.app/Info.plist", plistlib.dumps(plist, fmt=plistlib.FMT_BINARY))
        archive.writestr(f"Payload/Test.app/{plist.get('CFBundleExecutable', 'Test')}", binary)
        archive.writestr("Payload/Test.app/en.lproj/Localizable.strings", b"x")
        if hungarian:
            archive.writestr("Payload/Test.app/hu.lproj/Localizable.strings", b"x")
        if itunes_meta is not None:
            archive.writestr("iTunesMetadata.plist", plistlib.dumps(itunes_meta))
    return path


ANDROID_NS = "http://schemas.android.com/apk/res/android"


def axml(package: str, version_code: int, version_name: str, min_sdk: int, target_sdk: int, label: str, utf8: bool = False) -> bytes:
    """Binary AndroidManifest.xml (manifest + uses-sdk + application) as aapt would compile it."""

    strings = ["versionCode", "versionName", "minSdkVersion", "targetSdkVersion", "label", "android", ANDROID_NS, "package",
               "manifest", "uses-sdk", "application", package, version_name, label]
    index = {s: i for i, s in enumerate(strings)}
    resource_ids = [0x0101021B, 0x0101021C, 0x0101020C, 0x01010270, 0x01010001]
    pool_strings, offsets = b"", []
    for text in strings:
        offsets.append(len(pool_strings))
        if utf8:
            raw = text.encode("utf-8")
            pool_strings += bytes([len(text), len(raw)]) + raw + b"\0"
        else:
            pool_strings += struct.pack("<H", len(text)) + text.encode("utf-16-le") + b"\0\0"
    pool_strings += b"\0" * (-len(pool_strings) % 4)
    header_size = 28
    strings_start = header_size + 4 * len(strings)
    pool = struct.pack("<HHIIIIII", 0x0001, header_size, strings_start + len(pool_strings), len(strings), 0, 0x100 if utf8 else 0,
                       strings_start, 0)
    pool += struct.pack(f"<{len(strings)}I", *offsets) + pool_strings
    res_map = struct.pack("<HHI", 0x0180, 8, 8 + 4 * len(resource_ids)) + struct.pack(f"<{len(resource_ids)}I", *resource_ids)
    none = 0xFFFFFFFF
    ns = index[ANDROID_NS]

    def attr(name: str, *, string: str | None = None, integer: int | None = None, namespace: int = ns) -> bytes:
        if string is not None:
            return struct.pack("<IIIHBBI", namespace, index[name], index[string], 8, 0, 0x03, index[string])
        return struct.pack("<IIIHBBI", namespace, index[name], none, 8, 0, 0x10, integer)

    def start(tag: str, attrs: list[bytes]) -> bytes:
        body = struct.pack("<IIIIHHHHHH", 1, none, none, index[tag], 0x14, 0x14, len(attrs), 0, 0, 0) + b"".join(attrs)
        return struct.pack("<HHI", 0x0102, 0x10, 8 + len(body)) + body

    def end(tag: str) -> bytes:
        return struct.pack("<HHI", 0x0103, 0x10, 0x18) + struct.pack("<IIII", 1, none, none, index[tag])

    namespace_start = struct.pack("<HHI", 0x0100, 0x10, 0x18) + struct.pack("<IIII", 1, none, index["android"], ns)
    namespace_end = struct.pack("<HHI", 0x0101, 0x10, 0x18) + struct.pack("<IIII", 1, none, index["android"], ns)
    body = (namespace_start
            + start("manifest", [attr("versionCode", integer=version_code), attr("versionName", string=version_name),
                                 attr("package", string=package, namespace=none)])
            + start("uses-sdk", [attr("minSdkVersion", integer=min_sdk), attr("targetSdkVersion", integer=target_sdk)]) + end("uses-sdk")
            + start("application", [attr("label", string=label)]) + end("application")
            + end("manifest") + namespace_end)
    content = pool + res_map + body
    return struct.pack("<HHI", 0x0003, 8, 8 + len(content)) + content


def apk_file(path: Path, manifest: bytes) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("AndroidManifest.xml", manifest)
        archive.writestr("classes.dex", b"dex\n035\0" + b"\0" * 104)
    return path


def make_release(root: Path, files: dict[str, bytes]) -> rel.Release:
    for name, data in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    files_list = sorted((rel.ReleaseFile(str(p.relative_to(root)), p.stat().st_size) for p in root.rglob("*") if p.is_file()),
                        key=lambda f: f.relpath)
    return rel.Release(path=root, name=root.name, files=files_list, nfo=None, has_bdmv=False, has_video_ts=False)


# --- tests --------------------------------------------------------------------------------------------------------

class NameTests(unittest.TestCase):
    def test_scene_and_tagged_names(self):
        cases = {
            "Super_Mario_Odyssey_NSW-BigBlueBox": ("switch", "Super Mario Odyssey"),
            "Ghost.of.Tsushima.Update.v1.05.PS4-DUPLEX": ("ps4", "Ghost of Tsushima"),
            "The.Last.of.Us.PS3-DUPLEX": ("ps3", "The Last of Us"),
            "Grand_Theft_Auto_V_PAL_PS3-ABSTRAKT": ("ps3", "Grand Theft Auto V"),
            "Halo_3_RF_XBOX360-iMARS": ("xbox360", "Halo 3"),
            "Need.for.Speed.Hot.Pursuit.USA.PS2DVD-STRANGE": ("ps2", "Need for Speed Hot Pursuit"),
        }
        for name, (platform, title) in cases.items():
            tags = consoleid.parse_name(name)
            self.assertEqual(tags.get("platform"), platform, name)
            self.assertEqual(tags["title"], title, name)
        tags = consoleid.parse_name("Super Mario Odyssey [0100000000010800][v393216] (Update).nsp")
        self.assertEqual((tags["title_id"], tags["version_int"], tags["content_type"]), ("0100000000010800", 393216, "update"))
        tags = consoleid.parse_name("EP9000-CUSA00917_00-UNCHARTED4000000-A0105-V0100.pkg")
        self.assertEqual((tags["content_id"], tags["title_id"], tags["version"]), ("EP9000-CUSA00917_00-UNCHARTED4000000", "CUSA00917", "1.05"))
        self.assertEqual(consoleid.parse_name("Mario Kart Wii [RMCP01]")["id6"], "RMCP01")
        self.assertEqual(consoleid.parse_name("Grand Theft Auto III (SLES_503.30)")["serial"], "SLES-50330")
        self.assertTrue(consoleid.parse_name("FIFA.21.HUN.PS4-GRP")["hungarian"])
        self.assertNotIn("region", consoleid.parse_name("The.Last.of.Us"))

    def test_regions(self):
        self.assertEqual(consoleid.region_from_serial("SLES-50330"), "EUR")
        self.assertEqual(consoleid.region_from_serial("SLUS-20062"), "USA")
        self.assertEqual(consoleid.region_from_serial("SLPM-65002"), "JPN")
        self.assertEqual(consoleid.region_from_serial("BLES-01807"), "EUR")
        self.assertEqual(consoleid.region_from_serial("NPUB31857"), "USA")
        self.assertEqual(consoleid.region_from_serial("PCSB00245"), "EUR")
        self.assertEqual(consoleid.region_from_content_id("EP9000-CUSA00917_00-UNCHARTED4000000"), "EUR")
        self.assertEqual(consoleid.region_from_content_id("UP9000-CUSA00341_00-UNCHARTED4000000"), "USA")
        self.assertEqual(consoleid.region_from_content_id("JP9000-CUSA00912_00-UNCHARTED4000000"), "JPN")
        self.assertEqual(consoleid.region_from_id6("RMCP01"), "EUR")
        self.assertEqual(consoleid.region_from_id6("RMCE01"), "USA")
        self.assertEqual(consoleid.region_from_id6("RMCJ01"), "JPN")
        self.assertEqual(consoleid.region_from_product_code("CTR-P-AREP"), "EUR")
        self.assertEqual(consoleid.region_from_product_code("HAC-P-AAACA"), "RF")
        self.assertEqual(consoleid.region_from_dat("Europe, Australia"), "EUR")
        self.assertEqual(consoleid.region_from_dat("USA, Europe"), "RF")
        self.assertEqual(consoleid.region_from_dat("NTSC-U"), "USA")
        self.assertEqual(consoleid.region_from_xex(0x00FF0000), "EUR")
        self.assertEqual(consoleid.region_from_xex(0xFFFFFFFF), "RF")
        self.assertEqual(consoleid.region_from_3ds_lockout(0x7FFFFFFF), "RF")
        self.assertEqual(consoleid.region_label("JPN"), "NTSC-J (JPN)")
        self.assertIn("NTSC-J", consoleid.region_warning("JPN"))
        self.assertEqual(consoleid.region_warning("EUR"), "")


class SwitchTests(unittest.TestCase):
    def test_pfs0_tickets_and_authoring_xml(self):
        cnmt = (b'<?xml version="1.0" encoding="utf-8"?><ContentMeta><Type>Patch</Type><Id>0x0100000000010800</Id>'
                b"<Version>393216</Version><RequiredSystemVersion>0</RequiredSystemVersion></ContentMeta>")
        nacp = (b'<?xml version="1.0"?><Application><Title><Language>AmericanEnglish</Language><Name>Super Mario Odyssey</Name>'
                b"<Publisher>Nintendo</Publisher></Title><DisplayVersion>1.3.0</DisplayVersion></Application>")
        data = pfs0([("0123456789abcdef0123456789abcdef.nca", b"\0" * 64), ("01000000000108000000000000000005.tik", b"\0" * 0x2C0),
                     ("01000000000108000000000000000005.cert", b"\0" * 0x700), ("fedcba9876543210fedcba9876543210.cnmt.xml", cnmt),
                     ("aaaabbbbccccddddaaaabbbbccccdddd.nacp.xml", nacp)])
        files = consoleid.parse_pfs0(data)
        self.assertEqual(len(files), 5)
        name, offset, size = files[3]
        self.assertEqual(data[offset:offset + size], cnmt)
        info = consoleid.read_switch(consoleid.Blob(data=data), "Super Mario Odyssey [UPD].nsp")
        self.assertEqual(info["format"], "NSP")
        self.assertEqual(info["items"][0]["title_id"], "0100000000010800")
        self.assertEqual(info["items"][0]["type"], "update")
        self.assertEqual(info["items"][0]["base"], "0100000000010000")
        self.assertEqual(info["items"][0]["version"], 393216)
        self.assertEqual(info["items"][0]["display_version"], "1.3.0")
        self.assertEqual(info["title"], "Super Mario Odyssey")

    def test_kinds(self):
        self.assertEqual(consoleid.switch_kind("0100000000010000"), ("base", "0100000000010000"))
        self.assertEqual(consoleid.switch_kind("0100000000010800"), ("update", "0100000000010000"))
        self.assertEqual(consoleid.switch_kind("01007EF00011F001"), ("dlc", "01007EF00011E000"))

    @unittest.skipUnless(shutil.which("7z") or shutil.which("7za"), "7z not installed")
    def test_packed_release_is_read_through_7z(self):
        with tempfile.TemporaryDirectory() as tmp:
            packed = Path(tmp) / "src.zip"
            with zipfile.ZipFile(packed, "w") as archive:
                archive.writestr("Super.Mario.Odyssey.NSW-GRP/smo.nsp", pfs0([("01000000000100000000000000000003.tik", b"\0" * 64)]))
            release = make_release(Path(tmp) / "Super.Mario.Odyssey.NSW-GRP", {"grp-smo.zip": packed.read_bytes()})
            info = consoleid.identify(release, {})
        self.assertEqual((info.platform, info.title_id, info.content_type), ("switch", "0100000000010000", "base"))
        self.assertIn("archívum (7z)", info.sources)
        self.assertIn("smo.nsp", info.extra["packed"])

    def test_xci_uses_the_file_name(self):
        head = bytearray(0x200)
        head[0x100:0x104] = b"HEAD"
        head[0x10D] = 0xF8
        info = consoleid.read_switch(consoleid.Blob(data=bytes(head)), "Super Mario Odyssey [0100000000010000][v0].xci")
        self.assertEqual(info["format"], "XCI")
        self.assertEqual(info["rom_size"], "2 GB")
        self.assertEqual(info["items"][0]["title_id"], "0100000000010000")
        self.assertTrue(info["items"][0]["from_name"])

    def test_release_with_base_update_and_dlc(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Super.Mario.Odyssey.NSW-GRP"
            release = make_release(root, {
                "base.nsp": pfs0([("01000000000100000000000000000003.tik", b"\0" * 16)]),
                "update.nsp": pfs0([("01000000000108000000000000000005.tik", b"\0" * 16)]),
                "dlc.nsp": pfs0([("01000000000110010000000000000005.tik", b"\0" * 16)]),
            })
            info = consoleid.identify(release, {})
        self.assertEqual(info.platform, "switch")
        self.assertEqual(info.title_id, "0100000000010000")
        self.assertEqual(info.content_type, "base")
        self.assertEqual(info.region, "RF")
        self.assertEqual(info.extra["contents"], "alapjáték + frissítés (v?) + 1 DLC")
        self.assertEqual(info.title, "Super Mario Odyssey")


class PlayStationTests(unittest.TestCase):
    def test_sfo_roundtrip(self):
        data = sfo({"TITLE": "Bloodborne", "TITLE_ID": "CUSA00207", "APP_VER": "01.09", "CATEGORY": "gd", "SYSTEM_VER": 0x05050000,
                    "TITLE_24": "Bloodborne"})
        parsed = consoleid.parse_sfo(data)
        self.assertEqual(parsed["TITLE"], "Bloodborne")
        self.assertEqual(parsed["SYSTEM_VER"], 0x05050000)
        self.assertEqual(consoleid.sfo_languages(parsed), ["hu"])
        self.assertEqual(consoleid.ps_version("01.09"), "1.09")
        self.assertEqual(consoleid.sfo_kind("gp"), "update")

    def test_ps4_pkg(self):
        param = sfo({"TITLE": "Bloodborne", "TITLE_ID": "CUSA00207", "APP_VER": "01.09", "CATEGORY": "gp", "SYSTEM_VER": 0x05050000,
                     "CONTENT_ID": "EP9000-CUSA00207_00-BLOODBORNE0000EU"})
        data = ps4_pkg("EP9000-CUSA00207_00-BLOODBORNE0000EU", param)
        header = consoleid.parse_ps4_pkg_header(data)
        self.assertEqual(header["content_id"], "EP9000-CUSA00207_00-BLOODBORNE0000EU")
        read = consoleid.read_ps4_pkg(consoleid.Blob(data=data))
        self.assertEqual(read["sfo"]["APP_VER"], "01.09")
        self.assertTrue(read["icon"].startswith(b"\x89PNG"))
        with tempfile.TemporaryDirectory() as tmp:
            release = make_release(Path(tmp) / "Bloodborne.Update.v1.09.PS4-GRP", {"bb-update.pkg": data})
            info = consoleid.identify(release, {})
        self.assertEqual((info.platform, info.format, info.title_id, info.content_type), ("ps4", "PKG", "CUSA00207", "update"))
        self.assertEqual(info.version, "1.09")
        self.assertEqual(info.region, "EUR")
        self.assertEqual(info.extra["system_ver"], "5.05")
        self.assertTrue(info.icon)

    def test_ps3_pkg_and_folder(self):
        header = consoleid.parse_ps3_pkg_header(ps3_pkg("EP0001-NPEB00435_00-0000000000000000"))
        self.assertEqual(header["content_id"], "EP0001-NPEB00435_00-0000000000000000")
        self.assertEqual(header["content_type"], "GameExec")
        with tempfile.TemporaryDirectory() as tmp:
            release = make_release(Path(tmp) / "Grand_Theft_Auto_V_PAL_PS3-GRP", {
                "PS3_GAME/PARAM.SFO": sfo({"TITLE": "Grand Theft Auto V", "TITLE_ID": "BLES01807", "APP_VER": "01.00", "CATEGORY": "DG"}),
                "PS3_DISC.SFB": b"\0" * 16,
            })
            info = consoleid.identify(release, {})
        self.assertEqual((info.platform, info.serial, info.title, info.region), ("ps3", "BLES-01807", "Grand Theft Auto V", "EUR"))

    def test_ps2_iso_and_ps1_bin(self):
        iso = iso9660({"SYSTEM.CNF": b"BOOT2 = cdrom0:\\SLES_503.30;1\r\nVER = 1.40\r\nVMODE = PAL\r\n", "SLES_503.30": b"\0" * 100})
        disc = consoleid.read_disc_iso(consoleid.Blob(data=iso))
        self.assertEqual((disc["serial"], disc["platform"]), ("SLES-50330", "ps2"))
        bin_image = raw_bin(iso9660({"SYSTEM.CNF": b"BOOT = cdrom:\\SLUS_005.94;1\r\nTCB = 4\r\n"}))
        self.assertEqual(consoleid.Iso9660(consoleid.Blob(data=bin_image)).stride, 2352)
        disc = consoleid.read_disc_iso(consoleid.Blob(data=bin_image))
        self.assertEqual((disc["serial"], disc["platform"]), ("SLUS-00594", "ps1"))
        ps3 = iso9660({"PS3_GAME/PARAM.SFO": sfo({"TITLE": "Demo", "TITLE_ID": "BLUS30443"})})
        self.assertEqual(consoleid.read_disc_iso(consoleid.Blob(data=ps3))["serial"], "BLUS-30443")
        with tempfile.TemporaryDirectory() as tmp:
            release = make_release(Path(tmp) / "Metal Gear Solid (USA)", {"mgs.bin": bin_image, "mgs.cue": b'FILE "mgs.bin" BINARY\n'})
            info = consoleid.identify(release, {})
        self.assertEqual((info.platform, info.serial, info.region, info.format), ("ps1", "SLUS-00594", "USA", "BIN/CUE"))

    def test_system_cnf_variants(self):
        self.assertEqual(consoleid.system_cnf_serial("boot2 = cdrom0:\\\\SCUS_971.13;1"), ("SCUS-97113", "ps2"))
        self.assertEqual(consoleid.system_cnf_serial("BOOT=cdrom:SCES_003.44;1"), ("SCES-00344", "ps1"))
        self.assertEqual(consoleid.system_cnf_serial("nothing"), ("", ""))

    def test_ps5_param_json(self):
        meta = consoleid.parse_param_json(json.dumps({"titleId": "PPSA01411", "contentId": "UP9000-PPSA01411_00-MARVELSSMMORALES",
                                                      "contentVersion": "01.004.000", "localizedParameters": {
                                                          "defaultLanguage": "en-US", "en-US": {"titleName": "Marvel's Spider-Man: Miles Morales"}}}))
        self.assertEqual(meta["title"], "Marvel's Spider-Man: Miles Morales")
        self.assertEqual(consoleid.ps_version(meta["version"]), "1.004.000")


class NintendoDiscTests(unittest.TestCase):
    def test_wii_iso_wbfs_rvz(self):
        header = wii_header("RMCP01", "Mario Kart Wii")
        self.assertEqual(consoleid.parse_disc_header(header)["platform"], "wii")
        wbfs = b"WBFS" + b"\0" * 4 + bytes([9, 21]) + b"\0" * (0x200 - 10) + header
        self.assertEqual(consoleid.read_wii(consoleid.Blob(data=wbfs))["id6"], "RMCP01")
        rvz = b"RVZ\x01" + b"\0" * (0x58 - 4) + header[:0x80] + b"\0" * 0x100
        self.assertEqual(consoleid.read_wii(consoleid.Blob(data=rvz))["format"], "RVZ")
        gc = consoleid.read_wii(consoleid.Blob(data=wii_header("GALE01", "Super Smash Bros Melee", gamecube=True)))
        self.assertEqual((gc["platform"], gc["format"]), ("gc", "GCM"))
        with tempfile.TemporaryDirectory() as tmp:
            release = make_release(Path(tmp) / "Mario_Kart_Wii_PAL_WII-GRP", {"mkw.wbfs": wbfs})
            info = consoleid.identify(release, {})
        self.assertEqual((info.platform, info.id6, info.region, info.format), ("wii", "RMCP01", "EUR", "WBFS"))

    def test_3ds_cia_and_cart(self):
        data = cia(0x0004000000053F00, 1024, "CTR-P-AREP", "Super Mario 3D Land")
        read = consoleid.read_3ds(consoleid.Blob(data=data))
        self.assertEqual(read["title_id"], "0004000000053F00")
        self.assertEqual(consoleid.n3ds_version(read["version"]), "1.0.0")
        self.assertEqual(read["product_code"], "CTR-P-AREP")
        self.assertEqual((read["title"], read["publisher"], read["region"]), ("Super Mario 3D Land", "Nintendo", "EUR"))
        cart = consoleid.read_3ds(consoleid.Blob(data=ncsd(0x0004000000053F00, "CTR-P-AREP")))
        self.assertEqual((cart["format"], cart["product_code"]), ("3DS", "CTR-P-AREP"))
        self.assertEqual(consoleid.n3ds_kind("0004000E00053F00"), ("update", "0004000000053F00"))


class XboxTests(unittest.TestCase):
    def test_xiso_xex(self):
        image = xiso({"default.xex": xex(0x4D5307E6, 0x1B3DCDA5, 0x00FF0000), "media.bin": b"\0" * 10})
        found = consoleid.xdvdfs_find(consoleid.Blob(data=image))
        self.assertEqual(found[0], "default.xex")
        parsed = consoleid.parse_xex(image[found[1]:found[1] + 0x400])
        self.assertEqual((parsed["title_id"], parsed["media_id"], parsed["region"], parsed["discs"]), ("4D5307E6", "1B3DCDA5", "EUR", 2))
        with tempfile.TemporaryDirectory() as tmp:
            release = make_release(Path(tmp) / "Halo_3_PAL_XBOX360-GRP", {"halo3.iso": image})
            info = consoleid.identify(release, {})
        self.assertEqual((info.platform, info.title_id, info.region, info.format), ("xbox360", "4D5307E6", "EUR", "XISO"))
        self.assertEqual(info.extra["discs"], "1/2")

    def test_god_container(self):
        with tempfile.TemporaryDirectory() as tmp:
            release = make_release(Path(tmp) / "Halo 3 GOD", {
                "4D5307E6/00007000/1B3DCDA5E0A12B34C56D78E9F0A1B2C3D4E5F6A7B8": stfs(0x4D5307E6, "Halo 3", "Microsoft Game Studios"),
                "4D5307E6/00007000/1B3DCDA5E0A12B34C56D78E9F0A1B2C3D4E5F6A7B8.data/Data0000": b"\0" * 32,
            })
            info = consoleid.identify(release, {})
        self.assertEqual((info.platform, info.format, info.title_id, info.title), ("xbox360", "GOD", "4D5307E6", "Halo 3"))

    def test_xbe(self):
        parsed = consoleid.parse_xbe(xbe(0x4D530004, "Halo", 7))
        self.assertEqual((parsed["title_id"], parsed["title_code"], parsed["title"], parsed["region"]), ("4D530004", "MS-004", "Halo", "RF"))


class AndroidTests(unittest.TestCase):
    def test_badging_fixture(self):
        data = apk.parse_badging((FIXTURES / "aapt_badging.txt").read_text(encoding="utf-8"))
        self.assertEqual(data["package"], "hu.ncoreup.teszt")
        self.assertEqual((data["version_code"], data["version_name"]), ("10203", "1.2.3"))
        self.assertEqual((data["min_sdk"], data["target_sdk"]), ("26", "34"))
        self.assertNotIn("label", data)  # aapt resolves labels through resources.arsc, which this test APK lacks
        self.assertEqual((data["locales"], data["abis"]), ([], []))

    def test_badging_full_listing(self):
        text = ("package: name='org.videolan.vlc' versionCode='13070108' versionName='3.7.1' platformBuildVersionName='15' compileSdkVersion='35'\n"
                "sdkVersion:'21'\ntargetSdkVersion:'35'\nuses-permission: name='android.permission.INTERNET'\n"
                "application-label:'VLC'\napplication-label-hu:'VLC lejátszó'\napplication-icon-160:'res/mipmap/icon.png'\n"
                "application: label='VLC' icon='res/mipmap/icon.png'\nlaunchable-activity: name='org.videolan.vlc.StartActivity'  label='' icon=''\n"
                "locales: '--_--' 'de' 'hu' 'it'\ndensities: '160' '240'\nnative-code: 'arm64-v8a'\n")
        data = apk.parse_badging(text)
        self.assertEqual(data["labels"], {"hu": "VLC lejátszó"})
        self.assertEqual(data["locales"], ["de", "hu", "it"])
        self.assertEqual(data["abis"], ["arm64-v8a"])
        self.assertEqual(data["permissions"], ["android.permission.INTERNET"])
        self.assertTrue(data["launchable"])
        self.assertEqual(apk.android_version(data["min_sdk"]), "Android 5.0 (API 21)")

    def test_certs_fixtures(self):
        good = apk.parse_certs((FIXTURES / "apksigner_signed.txt").read_text(encoding="utf-8"), 0)
        self.assertTrue(good["verified"])
        self.assertEqual(len(good["signers"][0]["sha256"]), 64)
        self.assertIn("CN=ncore-up teszt", good["signers"][0]["dn"])
        self.assertEqual(apk.signature_status(good)[0], "érvényes")
        bad = apk.parse_certs((FIXTURES / "apksigner_unsigned.txt").read_text(encoding="utf-8"), 1)
        self.assertFalse(bad["verified"])
        self.assertEqual(apk.signature_status(bad)[0], "módosított")
        debug = {"verified": True, "signers": [{"dn": "C=US, O=Android, CN=Android Debug", "sha256": "ab" * 32}], "errors": []}
        self.assertEqual(apk.signature_status(debug)[0], "módosított")
        self.assertEqual(apk.signature_status({})[0], "ismeretlen")

    def test_axml_manifest(self):
        for utf8 in (False, True):
            parsed = apk.parse_axml_manifest(axml("hu.ncoreup.teszt", 10203, "1.2.3", 26, 34, "Teszt App", utf8=utf8))
            self.assertEqual(parsed, {"package": "hu.ncoreup.teszt", "version_code": "10203", "version_name": "1.2.3",
                                      "min_sdk": "26", "target_sdk": "34"})
        self.assertEqual(apk.parse_axml_manifest(b"not xml"), {})

    def test_apk_and_xapk_without_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            single = apk_file(Path(tmp) / "app.apk", axml("hu.ncoreup.teszt", 10203, "1.2.3", 26, 34, "Teszt App"))
            with mock.patch.object(apk.shutil, "which", return_value=None):
                data = apk.inspect(single)
                self.assertEqual((data["package"], data["version_name"], data["min_sdk"]), ("hu.ncoreup.teszt", "1.2.3", "26"))
                self.assertEqual(data["signature"][0], "ismeretlen")
                bundle = Path(tmp) / "app.xapk"
                with zipfile.ZipFile(bundle, "w") as archive:
                    archive.writestr("manifest.json", json.dumps({"package_name": "hu.ncoreup.teszt", "version_name": "1.2.3",
                                                                  "version_code": "10203", "name": "Teszt App", "min_sdk_version": "26",
                                                                  "split_apks": [{"file": "hu.ncoreup.teszt.apk", "id": "base"},
                                                                                 {"file": "config.arm64_v8a.apk", "id": "config.arm64_v8a"}]}))
                    archive.write(single, "hu.ncoreup.teszt.apk")
                    archive.writestr("config.arm64_v8a.apk", b"PK\x05\x06" + b"\0" * 18)
                    archive.writestr("config.hu.apk", b"PK\x05\x06" + b"\0" * 18)
                    archive.writestr("Android/obb/hu.ncoreup.teszt/main.1.hu.ncoreup.teszt.obb", b"\0" * 10)
                data = apk.inspect(bundle)
        self.assertEqual(data["format"], "XAPK")
        self.assertEqual((data["package"], data["version_code"], data["label"]), ("hu.ncoreup.teszt", "10203", "Teszt App"))
        self.assertEqual(data["abis"], ["arm64-v8a"])
        self.assertFalse(data["hungarian"])  # a downloader's config.hu split is no translated app
        self.assertTrue(data["hungarian_resources"])
        self.assertEqual(len(data["obb"]), 1)

    def test_split_names(self):
        configs = apk.split_configs(["splits/base-master.apk", "splits/base-arm64_v8a.apk", "split_config.xxhdpi.apk", "config.de.apk"])
        self.assertEqual(configs, {"abis": ["arm64-v8a"], "languages": ["de"], "densities": ["xxhdpi"]})

    @unittest.skipUnless(shutil.which("aapt"), "aapt not installed")
    def test_real_aapt_on_synthetic_apk(self):
        with tempfile.TemporaryDirectory() as tmp:
            single = apk_file(Path(tmp) / "app.apk", axml("hu.ncoreup.teszt", 10203, "1.2.3", 26, 34, "Teszt App"))
            data = apk.badging(single)
        self.assertEqual(data["package"], "hu.ncoreup.teszt")
        self.assertEqual(data["version_name"], "1.2.3")

    @unittest.skipUnless(shutil.which("apksigner"), "apksigner not installed")
    def test_real_apksigner(self):
        data = apk.certs(FIXTURES / "signed_test.apk")
        self.assertTrue(data["verified"])
        self.assertEqual(data["signers"][0]["dn"], "CN=ncore-up teszt, O=ncore-up, C=HU")
        self.assertIn("v2", data["schemes"])
        with tempfile.TemporaryDirectory() as tmp:
            unsigned = apk_file(Path(tmp) / "app.apk", axml("hu.ncoreup.teszt", 10203, "1.2.3", 26, 34, "Teszt App"))
            self.assertEqual(apk.signature_status(apk.certs(unsigned))[0], "módosított")


class IosTests(unittest.TestCase):
    def test_macho_cryptid(self):
        self.assertEqual(ipa.macho_cryptid(macho(1)), 1)
        self.assertEqual(ipa.macho_cryptid(macho(0, fat=True)), 0)
        self.assertIsNone(ipa.macho_cryptid(b"\0" * 64))

    def test_ipa(self):
        plist = {"CFBundleIdentifier": "com.google.Maps", "CFBundleDisplayName": "Google Maps", "CFBundleShortVersionString": "26.42.0",
                 "CFBundleVersion": "26.42.0.123", "MinimumOSVersion": "16.0", "UIDeviceFamily": [1, 2], "CFBundleExecutable": "Maps"}
        with tempfile.TemporaryDirectory() as tmp:
            path = ipa_file(Path(tmp) / "maps.ipa", plist=plist, binary=macho(0, fat=True),
                            itunes_meta={"itemName": "Google Maps", "appleId": "buyer@example.invalid"})
            data = ipa.inspect(path)
            clean = ipa.inspect(ipa_file(Path(tmp) / "enc.ipa", plist=plist, binary=macho(1), hungarian=False))
        self.assertEqual((data["bundle_id"], data["version"], data["build"], data["min_os"]), ("com.google.Maps", "26.42.0", "26.42.0.123", "16.0"))
        self.assertEqual(data["devices"], ["iPhone", "iPad"])
        self.assertTrue(data["hungarian"])
        self.assertIs(data["encrypted"], False)
        self.assertTrue(any("iTunesMetadata" in w for w in data["warnings"]))
        self.assertFalse(any("example.invalid" in w for w in data["warnings"]))
        self.assertIs(clean["encrypted"], True)
        self.assertFalse(clean["hungarian"])


if __name__ == "__main__":
    unittest.main()
