"""Regression tests of the software review: crafted files, ambiguous names, nested media, identity from the files."""

from __future__ import annotations

import contextlib
import hashlib
import sqlite3
import struct
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from ncore_up import release as rel
from ncore_up.local import installer, nfo, pe, winiso
from ncore_up.resolve import software
from ncore_up.sources import winget

try:
    from test_software_local import STRINGS, WIM_XML, build_pe, build_wim
    from test_software_sources import make_index
except ImportError:  # run as tests.test_software_review from the repository root
    from tests.test_software_local import STRINGS, WIM_XML, build_pe, build_wim
    from tests.test_software_sources import make_index


class NfoReviewTests(unittest.TestCase):
    def test_utf8_with_latin1_letters_is_not_read_as_cp437(self):
        self.assertEqual(nfo.decode("Café © 2026 APU Software".encode("utf-8")), "Café © 2026 APU Software")
        self.assertEqual(nfo.decode(b"\xc2\x80 art"), "┬Ç art")  # decodes to a C1 control: CP437 after all
        self.assertEqual(nfo.decode(b"\xdb\xdb caf\x82"), "██ café")

    def test_languages_with_odd_whitespace(self):
        self.assertEqual(nfo.languages("English, , German"), ["angol", "német"])
        self.assertEqual(nfo.parse("Language....: English,   \n")["languages"], ["angol"])


class PeReviewTests(unittest.TestCase):
    def test_deeply_nested_ber_blob(self):
        self.assertEqual(pe.parse_pkcs7(b"\x30\x80" * 100000 + b"\x00\x00" * 100000), {})

    def test_resource_tree_that_points_to_itself(self):
        data = bytearray(build_pe(0x8664, None, manifest="<a>" + "x" * 40000 + "</a>"))
        root = 0x600  # the builder's .rsrc section
        struct.pack_into("<IIHHHH", data, root, 0, 0, 0, 0, 0, 4096)
        for i in range(4096):
            struct.pack_into("<II", data, root + 16 + 8 * i, i + 1, 0x80000000)
        started = time.monotonic()
        info = pe.parse(bytes(data), icon=True)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(info["arch"], "x64")


class ClassifyReviewTests(unittest.TestCase):
    def test_inno_split_installer_is_no_disc_image(self):
        cls = installer.classify([("setup.exe", 2_000_000), ("setup-1.bin", 30_000_000), ("setup-2.bin", 30_000_000)])
        self.assertEqual((cls["kind"], cls["isos"], cls["setups"]), ("setup", [], ["setup.exe"]))
        disc = installer.classify([("Disc/image.bin", 600_000_000), ("Disc/image.cue", 200)])
        self.assertEqual((disc["kind"], disc["isos"]), ("iso", ["Disc/image.bin"]))

    def test_bundled_runtimes(self):
        cls = installer.classify([("App/App.exe", 9_000_000), ("Redist/vc_redist.x64.exe", 25_000_000), ("DirectX/DXSETUP.exe", 500_000)])
        self.assertEqual((cls["kind"], cls["setups"], cls["exes"]), ("portable", [], ["App/App.exe"]))
        self.assertEqual(len(cls["redist"]), 2)
        pack = installer.classify([("vcredist2015_x64.exe", 15_000_000), ("vcredist2015_x86.exe", 14_000_000)])  # the runtime itself
        self.assertEqual((pack["kind"], pack["setups"]), ("setup", ["vcredist2015_x64.exe", "vcredist2015_x86.exe"]))

    def test_language_folders(self):
        names = [f"$PLUGINSDIR/nppLocalization/{n}.xml" for n in ("german", "french", "italian", "polish", "hungarian")]
        self.assertEqual(installer.ui_languages(names), ["többnyelvű", "magyar"])
        self.assertEqual(installer.ui_languages([f"src/golang/{n}.go" for n in "abcdef"]), [])


class WinIsoReviewTests(unittest.TestCase):
    def test_media_one_level_down_with_upper_case_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            sources = Path(tmp) / "Win11_25H2" / "SOURCES"
            sources.mkdir(parents=True)
            (sources / "INSTALL.ESD").write_bytes(build_wim(WIM_XML))
            info = winiso.inspect(Path(tmp))
        self.assertEqual((info["wim"], info["version"], info["arch"]), ("win11_25h2/sources/install.esd", "25H2", "x64"))


class WingetReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = make_index(Path(self.tmp.name) / "index.db")
        con = sqlite3.connect(self.db)
        con.execute("INSERT INTO packages VALUES (?, ?, ?, ?, ?, NULL, NULL, ?)", (9001, "ndd.Notepad--", "Notepad--", None, "3.7.2", bytes(32)))
        con.execute("INSERT INTO norm_names2 VALUES (?, ?)", ("notepad", 9001))
        con.commit()
        con.close()
        winget._INDEX_FAILED[0] = 0.0

    def tearDown(self):
        self.tmp.cleanup()
        winget._INDEX_FAILED[0] = 0.0

    def test_versions_stay_strings(self):
        raw = b"sV: 1.0\nvD:\n- v: 26.00\n  rP: manifests/a/26.00/x\n  s256H: aa\n- v: 11.50\n  rP: manifests/a/11.50/y\n  s256H: bb\n"
        self.assertEqual([v["version"] for v in winget.parse_version_data(raw)], ["26.00", "11.50"])
        self.assertEqual(winget.parse_manifest("PackageIdentifier: Ghisler.TotalCommander\nPackageVersion: 11.50\nPackageName: Total Commander\n")
                         ["version"], "11.50")

    def test_name_only_match_needs_the_exact_name(self):
        self.assertEqual(winget.best("Notepad++", db=self.db)["id"], "Notepad++.Notepad++")  # not 'Notepad--' (same normalized name)
        self.assertEqual(winget.best("Notepad", db=self.db), {})  # ambiguous without a publisher
        self.assertEqual(winget.best("Notepad", ["Notepad++ Team"], db=self.db)["id"], "Notepad++.Notepad++")

    def test_homebrew_min_macos_as_numbers(self):
        from ncore_up.sources import homebrew

        self.assertEqual(homebrew._min_macos({"macos": {">=": ["12"]}}), "macOS 12 (Monterey)")  # formulae.brew.sh, 2026-10
        self.assertEqual(homebrew._min_macos({"macos": {">=": ["10.15"]}}), "macOS 10.15 (Catalina)")
        self.assertEqual(homebrew._min_macos({"macos": {}}), "")

    def test_failed_index_download_is_not_retried_at_once(self):
        with mock.patch.object(winget.net, "cache_dir", return_value=Path(self.tmp.name) / "cache"), \
                mock.patch.object(winget.net, "request", return_value=None) as request:
            (Path(self.tmp.name) / "cache").mkdir()
            self.assertIsNone(winget.index_db())
            self.assertIsNone(winget.index_db())
        self.assertEqual(request.call_count, 1)


@contextlib.contextmanager
def offline(lookup):
    """The resolver's network calls replaced; ``lookup`` is the side effect of winget.lookup."""

    with mock.patch.object(software.winget, "lookup", side_effect=lookup), \
            mock.patch.object(software.chocolatey, "best", return_value={}), \
            mock.patch.object(software, "wikidata_software", return_value={}), \
            mock.patch.object(software.wikipedia, "summary", return_value={}), \
            mock.patch.object(software, "_reachable", return_value=True), \
            mock.patch.object(software.homebrew, "find", return_value={}), \
            mock.patch.object(software.msdownload, "published", return_value={}), \
            mock.patch.object(software.translate, "to_hungarian", side_effect=lambda text, *a, **k: (text, False)):
        yield


class SoftwareReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_names(self):
        self.assertEqual(software.parse_name("Total Commander 11.58 (x86-x64)")["arch"], "x86-x64")
        fl = software.parse_name("FL Studio Producer Edition 21.2.3 Build 4004 (x64)")
        self.assertEqual((fl["title"], fl["version"], fl["arch"]), ("FL Studio Producer Edition", "21.2.3.4004", "x64"))
        self.assertEqual(software.parse_name("Unlocker.v1.9.2-GRP")["title"], "Unlocker")
        patch = software.parse_name("Patch.My.PC.v4.5.x64.Incl.Keygen-GRP")
        self.assertEqual((patch["title"], patch["activation"]), ("Patch My PC", ["Keygen"]))

    def test_activation_step_words(self):
        crack = software.activation_step({"tools": ["Crack/libpkg.dll"], "tool_dirs": ["Crack"]})
        self.assertIn("Másold a(z) Crack mappa tartalmát", crack[0])
        keygen = software.activation_step({"tools": ["GRP/readme.exe", "GRP/keygen.exe"], "tool_dirs": ["GRP"]})
        self.assertIn("(keygen.exe)", keygen[0])
        guide = software.install_guide("setup", {"setup": "setup.exe", "tools": ["GRP/keygen.exe"], "tool_dirs": ["GRP"],
                                                 "nfo_steps": ["Install the program", "Enjoy!"]})
        self.assertEqual(guide.split("\n")[-2:], ["2. Futtasd a mellékelt keygent (keygen.exe) (mappa: GRP), és a kapott adatokkal regisztráld "
                                                  "(aktiváld) a programot.", "3. Jó használatot!"])

    def test_license_texts(self):
        self.assertEqual(software.license_hu("GNU General Public License Version 2"), "nyílt forráskódú (GNU General Public License Version 2)")
        self.assertEqual(software.license_hu("GPL-2.0, Copyright (c) VideoLAN"), "nyílt forráskódú (GPL-2.0, Copyright (c) VideoLAN)")
        self.assertEqual(software.license_hu("Free for non-commercial use"), "magáncélra ingyenes")
        self.assertEqual(software.license_hu("Commercial"), "kereskedelmi")

    def test_only_public_links_are_proof_links(self):
        self.assertTrue(software._public_url("https://apu.software/truegain/"))
        for url in ("http://192.168.1.1/admin", "http://localhost:8080/", "https://nas.home.arpa/x", "ftp://example.org/", "http://[::1]/"):
            self.assertFalse(software._public_url(url), url)

    def test_lowest_os(self):
        binaries = [{"pe": {"os_version": "5.2"}}, {"pe": {"os_version": "5.1"}}]
        self.assertEqual(software._lowest_os(binaries), "Windows XP")
        self.assertEqual(software._lowest_os(binaries + [{"pe": {"os_version": "5.0"}}]), "")

    def test_single_installer_identified_by_its_product_name(self):
        path = self.root / "npp.8.9.8.1.Installer.x64.exe"
        path.write_bytes(build_pe(0x8664, {"ProductName": "Notepad++", "CompanyName": "Don HO", "ProductVersion": "8.9.8.1"}))
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        manifest = {"id": "Notepad++.Notepad++", "name": "Notepad++", "version": "8.9.8.1", "publisher": "Notepad++ Team",
                    "package_url": "https://notepad-plus-plus.org/", "installer_sha256": [digest],
                    "installers": [{"arch": "x64", "sha256": digest}], "archs": ["x64"], "matched_version": True,
                    "match": {"method": "name", "score": 100.0, "publisher_match": False}}
        with offline(lambda name, *a, **k: manifest if name == "Notepad++" else {}):
            meta = software.resolve(rel.inspect(path), "misc", original=False, hints={})
        self.assertEqual((meta.title, meta.version, meta.extra["arch"]), ("Notepad++", "8.9.8.1", "x64"))
        self.assertEqual(meta.extra["torrent_name"], "Notepad++ v8.9.8.1 x64")
        self.assertIn("official_installer", meta.extra)
        self.assertTrue(any("terméknevéből" in w for w in meta.warnings))
        self.assertFalse(any("terméknévként mást ír" in w for w in meta.warnings))

    def test_unconfirmed_product_match_is_not_used(self):
        folder = self.root / "Big.Program.v2.0.x64-GRP"
        folder.mkdir()
        (folder / "setup.exe").write_bytes(build_pe(0x14C, dict(STRINGS, ProductName="Other Tool", CompanyName="Other Vendor")))
        other = {"id": "Vendor.OtherTool", "name": "Other Tool", "version": "9.0", "matched_version": False, "installer_sha256": [],
                 "match": {"method": "name", "score": 100.0, "publisher_match": True}, "package_url": "https://other.example/"}
        with offline(lambda name, *a, **k: other if name == "Other Tool" else {}):
            meta = software.resolve(rel.inspect(folder), "misc", original=False, hints={})
        self.assertEqual(meta.title, "Big Program")
        self.assertNotIn("winget", meta.identifiers)
        self.assertNotIn("https://other.example/", meta.proof_links)
        self.assertTrue(any("nem használtam" in w for w in meta.warnings))

    def test_unpacked_windows_media_one_level_down(self):
        folder = self.root / "Win11.25H2.HUN.x64"
        (folder / "Win11_25H2" / "sources").mkdir(parents=True)
        (folder / "Win11_25H2" / "sources" / "install.esd").write_bytes(build_wim(WIM_XML))
        with offline(lambda *a, **k: {}):
            meta = software.resolve(rel.inspect(folder), "misc", original=False, hints={})
        self.assertEqual((meta.title, meta.version, meta.extra["build"]), ("Windows 11", "25H2", "26200.6584"))
        self.assertEqual(meta.extra["torrent_name"], "Windows 11 v25H2 HUN x64")
        self.assertIn("mappából bootolható pendrive-ot", meta.install_hu)
        self.assertFalse(any("nem találtam" in w for w in meta.warnings))
        self.assertTrue(any("Program (ISO) kategória illik" in w for w in meta.warnings))

    def test_program_disc_image_in_the_rip_category(self):
        folder = self.root / "Some.Suite.v3.0.x64-GRP"
        folder.mkdir()
        with open(folder / "suite.iso", "wb") as fh:
            fh.truncate(11 * 1024 * 1024)
        with offline(lambda *a, **k: {}):
            meta = software.resolve(rel.inspect(folder), "misc", original=False, hints={})
        self.assertTrue(any("Program (ISO) kategória illik" in w for w in meta.warnings))
        self.assertIn("Csatold fel a(z) suite.iso", meta.install_hu)


if __name__ == "__main__":
    unittest.main()
