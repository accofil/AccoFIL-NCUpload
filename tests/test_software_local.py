"""Offline tests of the software readers: NFO, PE, MSI, installer detection, Windows ISO/WIM."""

from __future__ import annotations

import base64
import gzip
import io
import json
import re
import shutil
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

from ncore_up.local import installer, msi, nfo, pe, winiso

FIXTURES = Path(__file__).parent / "fixtures" / "software"


# --- a tiny PE builder (version info, manifest, icon, CLR header, overlay, certificate table) -----------------------

def _vs_block(key: str, value: bytes = b"", children: bytes = b"", text: bool = False) -> bytes:
    key_bytes = key.encode("utf-16-le") + b"\0\0"
    head = 6 + len(key_bytes)
    pad1 = (-head) % 4
    pad2 = (-(head + pad1 + len(value))) % 4
    body = key_bytes + b"\0" * pad1 + value + b"\0" * pad2 + children
    body += b"\0" * ((-(6 + len(body))) % 4)
    length = len(value) // 2 if text else len(value)
    return struct.pack("<HHH", 6 + len(body), length, 1 if text else 0) + body


def version_info(strings: dict, version=(26, 4, 0, 0), lang: str = "040904b0") -> bytes:
    a, b, c, d = version
    fixed = struct.pack("<13I", 0xFEEF04BD, 0x00010000, (a << 16) | b, (c << 16) | d, (a << 16) | b, (c << 16) | d, 0x3F, 0, 0x40004, 1, 0, 0, 0)
    entries = b"".join(_vs_block(k, (v + "\0").encode("utf-16-le"), text=True) for k, v in strings.items())
    table = _vs_block(lang, children=entries, text=True)
    sfi = _vs_block("StringFileInfo", children=table, text=True)
    vfi = _vs_block("VarFileInfo", children=_vs_block("Translation", struct.pack("<HH", int(lang[:4], 16), int(lang[4:], 16))), text=True)
    return _vs_block("VS_VERSION_INFO", fixed, sfi + vfi)


def _resources(items: list[tuple[int, int, int, bytes]], va: int) -> bytes:
    count = len(items)
    base = 16 + 8 * count
    data_start = base + 2 * 24 * count + 16 * count
    data_start += (-data_start) % 8
    out = bytearray(struct.pack("<IIHHHH", 0, 0, 0, 0, 0, count))
    for i, (type_id, _n, _l, _d) in enumerate(items):
        out += struct.pack("<II", type_id, 0x80000000 | (base + 24 * i))
    for i, (_t, name_id, _l, _d) in enumerate(items):
        out += struct.pack("<IIHHHH", 0, 0, 0, 0, 0, 1) + struct.pack("<II", name_id, 0x80000000 | (base + 24 * count + 24 * i))
    for i, (_t, _n, lang, _d) in enumerate(items):
        out += struct.pack("<IIHHHH", 0, 0, 0, 0, 0, 1) + struct.pack("<II", lang, base + 48 * count + 16 * i)
    blobs = bytearray()
    for i, (_t, _n, _l, data) in enumerate(items):
        out += struct.pack("<IIII", va + data_start + len(blobs), len(data), 0, 0)
        blobs += data + b"\0" * ((-len(data)) % 8)
    out += b"\0" * (data_start - len(out))
    return bytes(out + blobs)


def png_1x1() -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\0\xff\0\0\xff")) + chunk(b"IEND", b""))


def build_pe(machine: int = 0x8664, strings: dict | None = None, *, manifest: str = "", icon: bool = False, clr_flags: int | None = None,
             subsystem: int = 2, os_version=(6, 1), overlay: bytes = b"", signature: bytes = b"", sections: tuple[str, ...] = ()) -> bytes:
    plus = machine in (0x8664, 0xAA64)
    opt_size = 240 if plus else 224
    items = []
    if strings is not None:
        numbers = [int(x) for x in re.findall(r"\d+", strings.get("ProductVersion", "26.4"))][:4]
        items.append((16, 1, 0x409, version_info(strings, tuple(numbers + [0] * (4 - len(numbers))))))
    if manifest:
        items.append((24, 1, 0x409, manifest.encode()))
    if icon:
        image = png_1x1()
        items.append((3, 1, 0x409, image))
        items.append((14, 101, 0x409, struct.pack("<HHH", 0, 1, 1) + struct.pack("<BBBBHHIH", 0, 0, 0, 0, 1, 32, len(image), 1)))
    rsrc = _resources(items, 0x2000) if items else b""
    rsrc_raw = rsrc + b"\0" * ((-len(rsrc)) % 0x200)
    text = bytearray(0x200)
    if clr_flags is not None:
        text[0:20] = struct.pack("<IHHIII", 72, 2, 5, 0, 0, clr_flags)
    names = [".text", ".rsrc"] + list(sections)
    body_end = 0x400 + 0x200 + len(rsrc_raw) + 0x200 * len(sections)
    tail = overlay
    cert = b""
    if signature:
        tail += b"\0" * ((-(body_end + len(tail))) % 8)
        cert = struct.pack("<IHH", 8 + len(signature), 0x0200, 2) + signature
        cert += b"\0" * ((-len(cert)) % 8)
    cert_offset = body_end + len(tail) if cert else 0
    opt = bytearray(opt_size)
    struct.pack_into("<H", opt, 0, 0x20B if plus else 0x10B)
    struct.pack_into("<HH", opt, 40, *os_version)
    struct.pack_into("<HH", opt, 48, *os_version)
    struct.pack_into("<H", opt, 68, subsystem)
    dd = 112 if plus else 96
    struct.pack_into("<I", opt, dd - 4, 16)
    if rsrc:
        struct.pack_into("<II", opt, dd + 16, 0x2000, len(rsrc))
    if cert:
        struct.pack_into("<II", opt, dd + 32, cert_offset, len(cert))
    if clr_flags is not None:
        struct.pack_into("<II", opt, dd + 14 * 8, 0x1000, 72)
    table = struct.pack("<8sIIIIIIHHI", b".text", 0x1000, 0x1000, 0x200, 0x400, 0, 0, 0, 0, 0x60000020)
    table += struct.pack("<8sIIIIIIHHI", b".rsrc", max(len(rsrc), 1), 0x2000, len(rsrc_raw), 0x600, 0, 0, 0, 0, 0x40000040)
    for i, name in enumerate(sections):
        table += struct.pack("<8sIIIIIIHHI", name.encode(), 0x200, 0x3000 + 0x1000 * i, 0x200, 0x600 + len(rsrc_raw) + 0x200 * i, 0, 0, 0, 0, 0)
    head = bytearray(b"MZ" + b"\0" * 58 + struct.pack("<I", 0x80))
    head += b"\0" * (0x80 - len(head))
    head += b"PE\0\0" + struct.pack("<HHIIIHH", machine, len(names), 0, 0, 0, opt_size, 0x22) + opt + table
    head += b"\0" * (0x400 - len(head))
    return bytes(head) + bytes(text) + rsrc_raw + b"\0" * (0x200 * len(sections)) + tail + cert


STRINGS = {"CompanyName": "Igor Pavlov", "FileDescription": "7-Zip Installer", "FileVersion": "26.04", "ProductName": "7-Zip",
           "ProductVersion": "26.04", "LegalCopyright": "Copyright (c) 1999-2026 Igor Pavlov", "OriginalFilename": "7zipInstall.exe"}
MANIFEST = ('<assembly><trustInfo><security><requestedPrivileges><requestedExecutionLevel level="requireAdministrator"/>'
            '</requestedPrivileges></security></trustInfo><compatibility><application>'
            '<supportedOS Id="{35138b9a-5d96-4fbd-8e2d-a2440225f93a}"/><supportedOS Id="{8e0f7a12-bfb3-4fe8-b9a5-48fd50a15a9a}"/>'
            '</application></compatibility></assembly>')


class NfoTests(unittest.TestCase):
    def test_software_nfo_fields_sections_and_steps(self):
        data = nfo.read(FIXTURES / "blizzard.nfo")
        self.assertEqual(data["release"], "APU.Software.TrueGain.v5.7.8.x64.WinALL.Incl.Keygen-BLiZZARD")
        self.assertEqual(data["group"], "BLiZZARD")
        self.assertEqual((data["title"], data["version"], data["date"]), ("APU Software TrueGain", "5.7.8", "2026-10-14"))
        self.assertEqual(data["url"], "https://apu.software/truegain/")
        self.assertEqual(data["release_type"], "Keygen")
        self.assertEqual(data["os"], "Windows 10/11 x64")
        self.assertEqual(data["languages"], ["angol", "német"])
        self.assertTrue(data["description"].startswith("TrueGain is a loudness normalizer for audio files. It analyzes"))
        self.assertEqual(data["install_steps"], ["Unpack and install the program", "Do not launch the program yet",
                                                 "Use our keygen to register the program (block it in your firewall)", "Enjoy"])
        self.assertNotIn("▀", nfo.strip_art(nfo.decode((FIXTURES / "blizzard.nfo").read_bytes())))

    def test_game_nfo_with_spaced_headers_and_copyright_line(self):
        data = nfo.read(FIXTURES / "rune.nfo")
        self.assertEqual((data["release"], data["group"]), ("Starfall.Odyssey-RUNE", "RUNE"))
        self.assertEqual((data["title"], data["publisher"]), ("Starfall Odyssey", "Nebula Interactive"))
        self.assertEqual(data["protection"], "Steam")
        self.assertEqual(data["languages"], ["angol", "francia", "német", "magyar"])
        self.assertEqual(data["install_steps"][-1], "Play")
        self.assertEqual(len(data["install_steps"]), 4)
        self.assertIn("dying galaxy", data["notes"])
        self.assertEqual(data["url"], "https://www.nebula-interactive.example/starfall")

    def test_file_id_diz_and_merge(self):
        data = nfo.read(FIXTURES / "file_id.diz")
        self.assertEqual(data["publisher"], "APU Software")
        self.assertIn("Loudness normalizer", data["description"])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "APU.Software.TrueGain.v5.7.8.x64.WinALL.Incl.Keygen-BLiZZARD"
            root.mkdir()
            shutil.copy(FIXTURES / "blizzard.nfo", root / "blizzard.nfo")
            shutil.copy(FIXTURES / "file_id.diz", root / "file_id.diz")

            class Rel:
                path = root
                files = [type("F", (), {"relpath": n, "ext": Path(n).suffix.lower()})() for n in ("blizzard.nfo", "file_id.diz")]

            merged = nfo.read_release(Rel)
        self.assertEqual(merged["files"], ["blizzard.nfo", "file_id.diz"])
        self.assertEqual(merged["publisher"], "APU Software")
        self.assertEqual(merged["version"], "5.7.8")

    def test_helpers(self):
        self.assertEqual(nfo.parse_date("16.10.2026"), "2026-10-16")
        self.assertEqual(nfo.parse_date("Oct 14th, 2026"), "2026-10-14")
        self.assertEqual(nfo.parse_date("2026/10/03"), "2026-10-03")
        self.assertEqual(nfo.languages("English / Hungarian & MULTi5"), ["angol", "magyar", "többnyelvű"])
        self.assertEqual(nfo.decode("Árvíztűrő".encode("utf-8")), "Árvíztűrő")
        self.assertEqual(nfo.decode(b"\xdb\xdb caf\x82"), "██ café")
        self.assertEqual(nfo.read(FIXTURES / "missing.nfo"), {})


class PeTests(unittest.TestCase):
    def test_version_info_manifest_icon(self):
        info = pe.parse(build_pe(0x8664, STRINGS, manifest=MANIFEST, icon=True), icon=True)
        self.assertEqual((info["arch"], info["subsystem"], info["min_os"]), ("x64", "gui", "Windows 7"))
        self.assertEqual((info["product_name"], info["company"], info["version"]), ("7-Zip", "Igor Pavlov", "26.04"))
        self.assertEqual(info["fixed_product_version"], "26.4.0.0")
        self.assertEqual(info["languages"], ["angol"])
        self.assertEqual(info["manifest_level"], "requireAdministrator")
        self.assertEqual(info["supported_os"], ["Windows 7", "Windows 10/11"])
        self.assertTrue(info["icon"].startswith(b"\x89PNG"))
        self.assertEqual(info["signature"], {})

    def test_machines_dotnet_and_overlay(self):
        self.assertEqual(pe.parse(build_pe(0x14C, {}))["arch"], "x86")
        self.assertEqual(pe.parse(build_pe(0xAA64, {}))["arch"], "arm64")
        self.assertEqual(pe.parse(build_pe(0x14C, None, clr_flags=0x1))["arch"], "anycpu")
        self.assertEqual(pe.parse(build_pe(0x14C, None, clr_flags=0x3))["arch"], "x86")  # 32BITREQUIRED
        self.assertTrue(pe.parse(build_pe(0x8664, None, clr_flags=0x1))["dotnet"])
        info = pe.parse(build_pe(0x14C, None, overlay=b"\0\0\0\0\xef\xbe\xad\xdeNullsoftInst" + b"x" * 100, os_version=(4, 0)))
        self.assertEqual((info["overlay_offset"], info["overlay_size"]), (0x600, 120))
        self.assertEqual(info["min_os"], "")
        self.assertEqual(pe.parse(b"not a pe file"), {})
        self.assertEqual(pe.parse(b"MZ" + b"\0" * 100), {})

    def test_best_version(self):
        self.assertEqual(pe.best_version({"product_version": "8.981", "fixed_product_version": "8.9.8.1"}), "8.9.8.1")
        self.assertEqual(pe.best_version({"product_version": "26.04", "fixed_product_version": "26.4.0.0"}), "26.04")
        self.assertEqual(pe.best_version({"product_version": "6.7.3", "fixed_product_version": "0.0.0.0"}), "6.7.3")

    def test_authenticode_signer(self):
        blob = (FIXTURES / "npp_signature.p7b").read_bytes()
        sig = pe.parse_pkcs7(blob)
        self.assertEqual((sig["signer"], sig["signer_org"], sig["signer_country"]), ("NOTEPAD++", "NOTEPAD++", "FR"))
        self.assertEqual(sig["issuer"], "GlobalSign GCC R45 CodeSigning CA 2020")
        self.assertEqual((sig["program"], sig["url"]), ("Notepad++", "https://notepad-plus-plus.org"))
        self.assertEqual(sig["digest_algorithm"], "sha256")
        self.assertEqual(sig["digest"], "af47df48579c393e030a7f7e49b40483ea0b8b0f45f9345c29d2c9f144604db8")
        self.assertEqual(sig["signing_time"], "2026-09-24")
        self.assertEqual(sig["certificates"], 2)
        self.assertEqual(pe.parse_pkcs7(b"\x30\x03\x02\x01\x00"), {})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "signed.exe"
            path.write_bytes(build_pe(0x14C, STRINGS, signature=blob))
            info = pe.read(path, verify=True)
        self.assertEqual(info["signature"]["signer"], "NOTEPAD++")
        self.assertFalse(info["signature"]["digest_ok"])  # the blob belongs to another file

    def test_combine_arch_and_os(self):
        self.assertEqual(pe.combine_arch(["x86", "x64"]), "x86-x64")
        self.assertEqual(pe.combine_arch(["anycpu"]), "x86-x64")
        self.assertEqual(pe.combine_arch(["x64", "x64", ""]), "x64")
        self.assertEqual(pe.combine_arch(["arm64"]), "arm64")
        self.assertEqual(pe.os_name(10, 0), "Windows 10")
        self.assertEqual(pe.os_name(5, 1), "Windows XP")
        self.assertEqual(pe.lcid_name(0x040E), "magyar")


class MsiTests(unittest.TestCase):
    def test_property_table_and_summary(self):
        properties = msi.parse_property_table((FIXTURES / "7z_msi_property.txt").read_text(encoding="utf-8"))
        summary = msi.parse_suminfo((FIXTURES / "7z_msi_suminfo.txt").read_text(encoding="utf-8"))
        info = msi.summarize(properties, summary, [["VersionNT >= 601", "Windows 7 kell"]])
        self.assertEqual((info["name"], info["manufacturer"], info["version"]), ("7-Zip", "Igor Pavlov", "26.04.00.0"))
        self.assertEqual(info["product_code"], "{23170F69-40C1-2702-2604-000001000000}")
        self.assertEqual((info["arch"], info["languages"]), ("x64", ["angol"]))
        self.assertEqual(info["url"], "http://www.7-zip.org/")
        self.assertEqual(info["min_os"], "Windows 7")
        self.assertIn("Windows Installer XML", info["creating_app"])

    def test_clean_name(self):
        self.assertEqual(msi.clean_name("Microsoft Visual C++ 2015-2022 Redistributable (x64) - 14.38.33130", "14.38.33130.0"),
                         "Microsoft Visual C++ 2015-2022 Redistributable")
        self.assertEqual(msi.clean_name("Office 16 Click-to-Run Extensibility Component", "16.0.1"), "Office 16 Click-to-Run Extensibility Component")
        self.assertEqual(msi.parse_suminfo("Template: Intel;1038")["arch"], "x86")
        self.assertEqual(msi.summarize({"ProductLanguage": "1038"}, msi.parse_suminfo("Template: ;1038"))["languages"], ["magyar"])

    @unittest.skipUnless(shutil.which("msiinfo"), "msiinfo is not installed")
    def test_read_non_msi_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.msi"
            path.write_bytes(b"not an msi")
            self.assertEqual(msi.read(path), {})


SLT = """
7-Zip [64] 26.02 : Copyright (c) 1999-2026 Igor Pavlov : 2026-06-25

Listing archive: npp.8.9.8.1.Installer.x64.exe

--
Path = npp.8.9.8.1.Installer.x64.exe
Type = PE
Comment =
{
FileVersion: 8.9.8.1
index=2 name=RESOURCE VA=0x48000 Size=156128
}
----
Path = [0]
Size = 6760696
Offset = 190464
--
Path = [0]
Type = Nsis
Method = LZMA:23
SubType = NSIS-3 Unicode

----------
Path = $PLUGINSDIR/System.dll
Size = 12288
Packed Size = 6760685
Modified =
Attributes =

Path = notepad++.exe
Size = 8546288
Packed Size =
Modified = 2026-09-24 13:57:44.0000000
Attributes =

Path = localization\\hungarian.xml
Size = 99000

Path = uninstall.exe
Size =
"""

INNO_INFO = """Inspecting "Example Tool 2.1.3 (x64)" - setup data version 5.6.0 (unicode)

Languages:
 - english: English
 - hungarian: Magyar

No GOG.com game ID found!
"""
INNO_LIST = """Listing "Example Tool 2.1.3 (x64)" - setup data version 5.6.0 (unicode)
 - "tmp/logo.bmp" [temp] (4.19 KiB)
 - "app/ExampleTool.exe" (1.07 MiB)
 - "app/Languages/Hungarian.lng" (20.8 KiB)
"""


class InstallerTests(unittest.TestCase):
    def test_detect_engines_from_bytes(self):
        self.assertEqual(installer.detect_bytes(b"..Inno Setup Setup Data (6.7.0)..", b"zlb\x1a")["engine_version"], "6.7.0")
        nsis = installer.detect_bytes(b"Nullsoft Install System v3.12", b"\0\0\0\0\xef\xbe\xad\xdeNullsoftInst")
        self.assertEqual((nsis["type"], nsis["engine_version"], nsis["label"]), ("nsis", "3.12", "NSIS telepítő"))
        self.assertEqual(installer.detect_bytes(b"stub 7z\xbc\xaf\x27\x1c in code", b"7z\xbc\xaf\x27\x1c\0\x04")["type"], "7z-sfx")
        self.assertEqual(installer.detect_bytes(b"", b"Rar!\x1a\x07\x01\0")["type"], "rar-sfx")
        self.assertEqual(installer.detect_bytes(b"", b"", [".text", ".wixburn"])["type"], "wix-burn")
        self.assertEqual(installer.detect_bytes(b"InstallShield", b"ISSetupStream")["type"], "installshield")
        self.assertEqual(installer.detect_bytes(b"", b"", strings={"comments": "This installation was built with Inno Setup."})["type"], "inno")
        self.assertEqual(installer.detect_bytes(b"plain", b"")["type"], "exe")

    def test_detect_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            setup = Path(tmp) / "setup.exe"
            setup.write_bytes(build_pe(0x14C, {"ProductName": "Foo"}, overlay=b"\0\0\0\0\xef\xbe\xad\xdeNullsoftInst" + b"\0" * 64))
            self.assertEqual(installer.detect(setup)["type"], "nsis")
            package = Path(tmp) / "foo.msi"
            package.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 504)
            self.assertEqual(installer.detect(package)["type"], "msi")
            burn = Path(tmp) / "bundle.exe"
            burn.write_bytes(build_pe(0x14C, {}, sections=(".wixburn",)))
            self.assertEqual(installer.detect(burn)["type"], "wix-burn")
            self.assertEqual(installer.detect(Path(tmp) / "missing.exe"), {})

    def test_parse_7z_listing(self):
        listing = installer.parse_7z_slt(SLT)
        self.assertEqual(listing["type"], "Nsis")
        self.assertEqual([a.get("Type") for a in listing["archives"]], ["PE", "Nsis"])
        paths = [e["path"] for e in listing["entries"]]
        self.assertEqual(paths, ["$PLUGINSDIR/System.dll", "notepad++.exe", "localization/hungarian.xml", "uninstall.exe"])
        self.assertEqual(listing["entries"][1]["size"], 8546288)
        self.assertEqual([e["path"] for e in installer.main_programs(listing["entries"])], ["notepad++.exe"])
        self.assertTrue(installer.hungarian_ui(paths))

    def test_innoextract_output(self):
        info = installer.parse_innoextract(INNO_INFO, INNO_LIST)
        self.assertTrue(info["supported"])
        self.assertEqual((info["name"], info["data_version"]), ("Example Tool 2.1.3 (x64)", "5.6.0"))
        self.assertEqual(info["languages"], ["angol", "magyar"])
        self.assertEqual([f["path"] for f in info["files"] if not f["temp"]], ["app/ExampleTool.exe", "app/Languages/Hungarian.lng"])
        self.assertEqual(installer.split_app_name(info["name"]), ("Example Tool", "2.1.3"))
        self.assertFalse(installer.parse_innoextract("Not a supported Inno Setup installer!")["supported"])

    def test_classify_release_files(self):
        files = [("Setup/TrueGain_Setup_x64.exe", 30_000_000), ("BLiZZARD/keygen.exe", 300_000), ("blizzard.nfo", 2000),
                 ("file_id.diz", 100), ("Lang/hu.lng", 1000)]
        cls = installer.classify(files, "BLiZZARD")
        self.assertEqual(cls["kind"], "setup")
        self.assertEqual(cls["setups"], ["Setup/TrueGain_Setup_x64.exe"])
        self.assertEqual(cls["tools"], ["BLiZZARD/keygen.exe"])
        self.assertEqual(cls["tool_dirs"], ["BLiZZARD"])
        self.assertEqual(cls["hungarian_files"], ["Lang/hu.lng"])
        self.assertEqual(installer.classify([("app/App.exe", 9), ("app/dispatcher.exe", 5)])["kind"], "portable")
        self.assertEqual(installer.classify([("app/dispatcher.exe", 5)])["tools"], [])
        self.assertEqual(installer.classify([("x.msi", 9)])["kind"], "msi")
        rar = installer.classify([("blz.rar", 50), ("blz.r00", 50), ("blz.r01", 50), ("blizzard.nfo", 1)])
        self.assertEqual((rar["kind"], rar["archives"]), ("archive", ["blz.rar"]))
        self.assertEqual(installer.classify([("disc.iso", 600_000_000)])["kind"], "iso")
        self.assertTrue(installer.classify([("sources/install.wim", 9), ("setup.exe", 1)])["windows_iso"])
        self.assertEqual(installer.classify([("VLC.app/Contents/MacOS/VLC", 1), ("VLC.app/Contents/Info.plist", 1)])["mac"], ["VLC.app"])

    def test_ui_languages(self):
        many = [f"Lang/{code}.txt" for code in ("de", "fr", "it", "es", "hu", "pl")]
        self.assertEqual(installer.ui_languages(many), ["többnyelvű", "magyar"])
        self.assertEqual(installer.ui_languages(["app/Languages/Hungarian.lng", "app/Tool.exe"]), ["magyar"])
        self.assertEqual(installer.ui_languages(["locales/de.pak", "locales/fr.pak", "locales/it.pak", "locales/es.pak", "locales/pl.pak"]),
                         ["többnyelvű"])
        self.assertEqual(installer.ui_languages(["app/Tool.exe"]), [])

    @unittest.skipUnless(shutil.which("7z") or shutil.which("7zz"), "7z is not installed")
    def test_list_archive_of_a_zip(self):
        import zipfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.zip"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("app/Tool.exe", build_pe(0x8664, {"ProductName": "Tool"}))
                archive.writestr("readme.txt", "hello")
            listing = installer.list_archive(path)
            self.assertEqual(sorted(e["path"] for e in listing["entries"]), ["app/Tool.exe", "readme.txt"])
            self.assertEqual(installer.payload_archs(path, listing), ["x64"])
            self.assertEqual(installer.read_member(path, "readme.txt"), b"hello")


WIM_XML = """<WIM><TOTALBYTES>123</TOTALBYTES>
<IMAGE INDEX="1"><TOTALBYTES>20000</TOTALBYTES><WINDOWS><ARCH>9</ARCH><EDITIONID>Core</EDITIONID><INSTALLATIONTYPE>Client</INSTALLATIONTYPE>
<LANGUAGES><LANGUAGE>hu-HU</LANGUAGE><DEFAULT>hu-HU</DEFAULT></LANGUAGES>
<VERSION><MAJOR>10</MAJOR><MINOR>0</MINOR><BUILD>26200</BUILD><SPBUILD>6584</SPBUILD></VERSION></WINDOWS>
<NAME>Windows 11 Home</NAME><DISPLAYNAME>Windows 11 Home</DISPLAYNAME></IMAGE>
<IMAGE INDEX="2"><WINDOWS><ARCH>9</ARCH><EDITIONID>Professional</EDITIONID><INSTALLATIONTYPE>Client</INSTALLATIONTYPE>
<LANGUAGES><LANGUAGE>hu-HU</LANGUAGE></LANGUAGES><VERSION><MAJOR>10</MAJOR><MINOR>0</MINOR><BUILD>26200</BUILD><SPBUILD>6584</SPBUILD></VERSION>
</WINDOWS><NAME>Windows 11 Pro</NAME></IMAGE></WIM>"""


def build_wim(xml: str, gap: int = 4096) -> bytes:
    raw = b"\xff\xfe" + xml.encode("utf-16-le")
    header = bytearray(208)
    header[0:8] = b"MSWIM\0\0\0"
    struct.pack_into("<IIII", header, 8, 208, 0x10D00, 0x40002, 32768)
    struct.pack_into("<HHI", header, 40, 1, 1, 2)
    header[72:79] = len(raw).to_bytes(7, "little")
    struct.pack_into("<QQ", header, 80, 208 + gap, len(raw))
    return bytes(header) + b"\0" * gap + raw


class WinIsoTests(unittest.TestCase):
    def test_real_microsoft_iso_metadata(self):
        """UDF walk + WIM header/XML of a real 5 GB Microsoft ISO, from the 16 reads the parser made (sparse fixture)."""

        sparse = {int(k): base64.b64decode(v) for k, v in json.loads(gzip.decompress((FIXTURES / "win11_ltsc_eval_sparse.json.gz").read_bytes())).items()}

        def read(offset: int, size: int) -> bytes:
            for start, data in sparse.items():
                if start <= offset and offset + size <= start + len(data):
                    return data[offset - start: offset - start + size]
            raise AssertionError(f"unexpected read {offset}+{size}")

        info = winiso.inspect_reader(read)
        self.assertEqual(info["label"], "CESE_X64FREE_EN-US_DV9")
        self.assertEqual((info["wim"], info["method"], info["wim_size"]), ("sources/install.wim", "udf", 4300455560))
        self.assertEqual((info["product"], info["version"], info["build"], info["arch"]), ("Windows 11", "24H2", "26100.1742", "x64"))
        self.assertEqual(info["editions"], ["Windows 11 Enterprise LTSC 2024 Evaluation", "Windows 11 Enterprise N Evaluation"])
        self.assertEqual((info["language_codes"], info["languages"]), (["en-US"], ["angol"]))
        self.assertEqual(info["header"]["compression"], "lzx")

    def test_wim_file_and_stream(self):
        data = build_wim(WIM_XML)
        info = winiso.read_wim(lambda offset, size: data[offset: offset + size])
        self.assertEqual([i["name"] for i in info["images"]], ["Windows 11 Home", "Windows 11 Pro"])
        self.assertEqual(info["images"][0]["build"], "26200.6584")
        streamed = winiso._wim_from_stream(io.BytesIO(data))
        self.assertEqual(len(streamed["images"]), 2)
        summary = winiso.summarize(info["images"])
        self.assertEqual((summary["product"], summary["version"], summary["languages"]), ("Windows 11", "25H2", ["magyar"]))
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "Win11"
            (folder / "sources").mkdir(parents=True)
            (folder / "sources" / "install.esd").write_bytes(data)
            result = winiso.inspect(folder)
        self.assertEqual((result["method"], result["wim"], result["arch"]), ("file", "sources/install.esd", "x64"))
        self.assertEqual(winiso.parse_wim_header(b"nope" * 60), {})

    def test_real_wimlib_files(self):
        """Header + XML of a WIM (LZX) and a solid ESD (LZMS) written by wimlib-imagex capture."""

        wim = winiso.read_wim(winiso.file_reader(FIXTURES / "wimlib_capture.wim"))
        self.assertEqual((wim["header"]["compression"], wim["header"]["images"]), ("lzx", 1))
        self.assertEqual((wim["images"][0]["name"], wim["images"][0]["description"]), ("Test Image", "Test description"))
        esd = winiso.read_wim(winiso.file_reader(FIXTURES / "wimlib_capture_solid.esd"))
        self.assertEqual((esd["header"]["compression"], esd["images"][0]["name"]), ("lzms", "Solid Image"))

    def test_wiminfo_fallback_parser(self):
        text = """WIM Information:
----------------
Path:           install.wim
Image Count:    1
Compression:    LZX

Available Images:
-----------------
Index:                  1
Name:                   Windows 11 Pro
Description:            Windows 11 Pro
Display Name:           Windows 11 Pro
Total Bytes:            18299282863
Architecture:           x86_64
Edition ID:             Professional
Installation Type:      Client
Languages:              hu-HU
Default Language:       hu-HU
Major Version:          10
Minor Version:          0
Build:                  26100
Service Pack Build:     1742
WIMBoot compatible:     no
"""
        images = winiso.parse_wiminfo(text)
        self.assertEqual(len(images), 1)
        self.assertEqual((images[0]["arch"], images[0]["build"], images[0]["languages"]), ("x64", "26100.1742", ["hu-HU"]))
        self.assertEqual(winiso.summarize(images)["version"], "24H2")
        with mock.patch.object(winiso, "read_wim", return_value={}), mock.patch.object(winiso, "wiminfo_images", return_value=images):
            result = winiso.inspect(FIXTURES / "wimlib_capture.wim")
        self.assertEqual((result["method"], result["product"]), ("wiminfo", "Windows 11"))

    @unittest.skipUnless(shutil.which("wiminfo"), "wimtools is not installed")
    def test_wiminfo_tool(self):
        self.assertEqual(winiso.wiminfo_images(FIXTURES / "wimlib_capture.wim")[0]["name"], "Test Image")

    def test_label_and_cultures(self):
        self.assertEqual(winiso.parse_label("CCCOMA_X64FRE_HU-HU_DV9"), {"arch": "x64", "language": "hu-HU", "edition": "consumer"})
        self.assertEqual(winiso.parse_label("CPBA_A64FRE_EN-GB_DV9")["arch"], "arm64")
        self.assertEqual(winiso.culture_name("en-GB"), "angol (nemzetközi)")
        self.assertEqual(winiso.culture_name("de-DE"), "német")
        self.assertTrue(winiso.is_windows_media(["SOURCES/INSTALL.ESD", "setup.exe"]))
        self.assertEqual(winiso.inspect_reader(lambda o, s: b"\0" * s), {})


if __name__ == "__main__":
    unittest.main()
