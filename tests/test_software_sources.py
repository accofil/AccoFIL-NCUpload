"""Offline tests of the software sources: winget, Chocolatey, Homebrew, Microsoft release information."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ncore_up.sources import chocolatey, homebrew, msdownload, winget

FIXTURES = Path(__file__).parent / "fixtures" / "software"
HAS_YAML = importlib.util.find_spec("yaml") is not None


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def make_index(path: Path) -> Path:
    """A miniature winget index.db with the real schema (subset of tables)."""

    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE packages(rowid INTEGER PRIMARY KEY, id TEXT, name TEXT, moniker TEXT, latest_version TEXT, arp_min_version TEXT,
                              arp_max_version TEXT, hash BLOB);
        CREATE TABLE norm_names2(norm_name TEXT, package INT64);
        CREATE TABLE norm_publishers2(norm_publisher TEXT, package INT64);
        CREATE TABLE tags2(rowid INTEGER PRIMARY KEY, tag TEXT);
        CREATE TABLE tags2_map(tag INT64, package INT64);
        CREATE TABLE productcodes2(productcode TEXT, package INT64);
        CREATE TABLE upgradecodes2(upgradecode TEXT, package INT64);
    """)
    rows = [(83, "7zip.7zip", "7-Zip", "7zip", "26.04", bytes.fromhex("E6051F0C" + "00" * 28)),
            (14327, "mcmilk.7zip-zstd", "7-Zip ZS", "7zip-zstd", "26.02-v1.5.7-R2", bytes(32)),
            (8173, "Notepad++.Notepad++", "Notepad++", "notepad++", "8.9.8.1", bytes(32)),
            (13560, "flos-freeware.Notepad2", "Notepad2", None, "4.2.25", bytes(32)),
            (11809, "VideoLAN.VLC", "VLC media player", "vlc", "3.0.24", bytes.fromhex("8003EC63" + "00" * 28))]
    con.executemany("INSERT INTO packages VALUES (?, ?, ?, ?, ?, NULL, NULL, ?)", rows)
    con.executemany("INSERT INTO norm_names2 VALUES (?, ?)", [("7zip", 83), ("7zip(X64)", 83), ("7zipzs", 14327), ("notepad", 8173),
                                                             ("notepad2", 13560), ("vlcmediaplayer", 11809)])
    con.executemany("INSERT INTO norm_publishers2 VALUES (?, ?)", [("igorpavlov", 83), ("tinofoil", 14327), ("notepadteam", 8173),
                                                                  ("florianbalmer", 13560), ("videolan", 11809)])
    con.executemany("INSERT INTO tags2 VALUES (?, ?)", [(1, "archiver"), (2, "zip")])
    con.executemany("INSERT INTO tags2_map VALUES (?, ?)", [(1, 83), (2, 83)])
    con.execute("INSERT INTO productcodes2 VALUES (?, ?)", ("{23170f69-40c1-2702-2604-000001000000}", 83))
    con.commit()
    con.close()
    return path


class WingetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = make_index(Path(self.tmp.name) / "index.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_normalize_and_publishers(self):
        self.assertEqual(winget.normalize("Notepad++"), "notepad")
        self.assertEqual(winget.normalize("7-Zip (x64)"), "7zip")
        self.assertEqual(winget.normalize_publisher("Microsoft Corporation"), "microsoft")
        self.assertTrue(winget.same_publisher("igorpavlov", "Igor Pavlov"))
        self.assertTrue(winget.same_publisher("notepadteam", "NOTEPAD++"))
        self.assertTrue(winget.same_publisher("Ghisler Software GmbH", "ghisler"))
        self.assertFalse(winget.same_publisher("notepadteam", "Don HO"))

    def test_search_best_and_codes(self):
        hits = winget.search("7-Zip", ["Igor Pavlov"], db=self.db)
        self.assertEqual(hits[0]["id"], "7zip.7zip")
        self.assertTrue(hits[0]["publisher_match"])
        self.assertEqual(hits[0]["hash"][:8], "e6051f0c")
        self.assertEqual(hits[0]["tags"], ["archiver", "zip"])
        self.assertEqual(winget.best("Notepad++", ["NOTEPAD++"], db=self.db)["id"], "Notepad++.Notepad++")
        self.assertEqual(winget.best("VLC media player", db=self.db)["match"]["method"], "name")
        self.assertEqual(winget.best("APU Software TrueGain", ["APU Software"], db=self.db), {})
        self.assertEqual(winget.best("Notepad++", ["Some Other Vendor"], db=self.db), {})  # exact name, publisher disagrees
        by_code = winget.best("whatever", product_code="{23170F69-40C1-2702-2604-000001000000}", db=self.db)
        self.assertEqual((by_code["id"], by_code["match"]["method"]), ("7zip.7zip", "productcode"))

    def test_mszip_version_data(self):
        raw = (FIXTURES / "winget_vlc_versionData.mszyml").read_bytes()
        text = winget.mszip_decompress(raw).decode()
        self.assertIn("rP: manifests/v/VideoLAN/VLC/3.0.24/0fdc", text)
        if HAS_YAML:
            versions = winget.parse_version_data(raw)
            self.assertEqual(versions[0]["version"], "3.0.24")
            self.assertEqual(versions[0]["path"], "manifests/v/VideoLAN/VLC/3.0.24/0fdc")

    @unittest.skipUnless(HAS_YAML, "PyYAML is not installed")
    def test_parse_merged_manifests(self):
        seven = winget.parse_manifest(fixture("winget_7zip.7zip.yaml"))
        self.assertEqual((seven["name"], seven["publisher"], seven["version"]), ("7-Zip", "Igor Pavlov", "26.04"))
        self.assertEqual((seven["package_url"], seven["license"]), ("https://7-zip.org/download.html", "LGPL-2.1"))
        self.assertEqual(seven["archs"], ["x86", "x64", "arm", "arm64"])
        self.assertIn("d54bf805f9f3704d1e8db2fa3498ae7ef2df0312b40b558e7c71c734430a665d", seven["installer_sha256"])
        self.assertTrue(seven["icon_url"].startswith("https://cdn.winget.microsoft.com/icons/"))
        self.assertIn("zh-CN", seven["locales"])
        vlc = winget.parse_manifest(fixture("winget_VideoLAN.VLC.yaml"))
        self.assertEqual(vlc["min_os"], "6.1.7600.0")
        self.assertIn("nullsoft", vlc["installer_types"])
        self.assertTrue(vlc["description"].startswith("VLC is a free and open source"))
        inno = winget.parse_manifest(fixture("winget_JRSoftware.InnoSetup.yaml"))
        self.assertEqual(inno["installers"][0]["type"], "inno")  # root-level InstallerType applies to every installer

    def test_mini_yaml_fallback(self):
        data = winget._mini_yaml(fixture("winget_Notepad++.Notepad++.yaml"))
        self.assertEqual(data["Publisher"], "Notepad++ Team")
        self.assertEqual(data["PackageUrl"], "https://notepad-plus-plus.org/")
        self.assertIn("editor", data["Tags"] if isinstance(data["Tags"], list) else [])
        self.assertEqual(data["Installers"][0]["Architecture"], "x64")
        self.assertIn("free", data["Description"])

    def test_lookup_without_network(self):
        with mock.patch.object(winget, "index_db", return_value=None):
            self.assertEqual(winget.lookup("7-Zip"), {})


class ChocolateyTests(unittest.TestCase):
    def test_parse_feed_and_description(self):
        items = chocolatey.parse_feed(fixture("choco_search_7zip.xml"))
        self.assertEqual([i["id"] for i in items], ["7zip", "7zip.install"])
        first = items[0]
        self.assertEqual((first["title"], first["version"], first["project_url"]), ("7-Zip", "26.4.0", "http://www.7-zip.org/"))
        self.assertEqual(first["tags"][:3], ["7zip", "zip", "archiver"])
        self.assertTrue(first["icon_url"].endswith("7zip.svg"))
        self.assertEqual(first["authors"], "Igor Pavlov")
        self.assertTrue(first["latest"])
        text = chocolatey.clean_description(first["description"])
        self.assertIn("• High compression ratio in 7z format", text)
        self.assertNotIn("Version History", text)
        self.assertEqual(chocolatey.parse_feed("<html>"), [])

    def test_best_match(self):
        items = chocolatey.parse_feed(fixture("choco_search_7zip.xml"))
        with mock.patch.object(chocolatey, "search", return_value=items):
            self.assertEqual(chocolatey.best("7-Zip", ["Igor Pavlov"])["id"], "7zip")
            self.assertEqual(chocolatey.best("Totally Different Program"), {})


class HomebrewTests(unittest.TestCase):
    def test_parse_and_search(self):
        cask = homebrew.parse_cask(json.loads(fixture("brew_cask_vlc.json")))
        self.assertEqual((cask["name"], cask["homepage"], cask["apps"]), ("VLC media player", "https://www.videolan.org/vlc/", ["VLC.app"]))
        formula = homebrew.parse_formula(json.loads(fixture("brew_formula_p7zip.json")))
        self.assertEqual((formula["version"], formula["kind"]), ("17.06", "formula"))
        self.assertEqual(homebrew.token("VLC media player"), "vlc-media-player")
        self.assertEqual(homebrew.token("Notepad++"), "notepad-plus-plus")
        self.assertEqual(homebrew._min_macos({"macos": {">=": ["monterey"]}}), "macOS 12 (Monterey)")
        items = [{"token": "vlc", "names": ["VLC media player"]}, {"token": "iina", "names": ["IINA"]}]
        self.assertEqual(homebrew.search("VLC media player", items=items)[0]["token"], "vlc")
        self.assertEqual(homebrew.search("zzzz", items=items), [])


class MsDownloadTests(unittest.TestCase):
    def test_release_health(self):
        builds = msdownload.parse_release_health(fixture("release_health_win11.html"), "Windows 11")
        self.assertEqual(builds[26300], {"product": "Windows 11", "version": "26H2", "date": "2026-09-29", "latest": "26300.9711"})
        self.assertEqual(builds[22000]["version"], "21H2")
        ten = msdownload.parse_release_health(fixture("release_health_win10.html"), "Windows 10")
        self.assertEqual(ten[19045]["date"], "2022-10-18")
        self.assertEqual(ten[10240]["version"], "1507")
        for build, entry in list(builds.items()) + list(ten.items()):  # the static table agrees with Microsoft's page
            self.assertEqual(msdownload.BUILDS[build][1], entry["version"], build)

    def test_versions_hashes_and_current(self):
        self.assertEqual(msdownload.version_for_build("26100.1742")["version"], "24H2")
        self.assertEqual(msdownload.version_for_build("10.0.22631.2428")["version"], "23H2")
        self.assertEqual(msdownload.version_for_build(26100, server=True)["product"], "Windows Server 2025")
        self.assertEqual(msdownload.version_for_build("12345"), {})
        page = fixture("software_download_win11_hu.html")
        self.assertEqual(msdownload.parse_current_version(page), "26H2")
        hashes = msdownload.parse_hash_table(page)
        self.assertEqual(len(hashes), 38)
        self.assertEqual(hashes["614ACD776E654B26255F0CFCFAC509217E6F98B6FE949FA478AB5514A7697B5F"],
                         {"label": "magyar 64-bit", "language": "magyar", "arch": "x64"})
        with mock.patch.object(msdownload.net, "get_text", return_value=page):
            check = msdownload.check_iso("614acd776e654b26255f0cfcfac509217e6f98b6fe949fa478ab5514a7697b5f", "Windows 11")
            self.assertTrue(check["match"])
            self.assertEqual(check["language"], "magyar")
            self.assertFalse(msdownload.check_iso("00" * 32, "Windows 11")["match"])
        with mock.patch.object(msdownload.net, "get_text", return_value=None):
            self.assertEqual(msdownload.published("Windows 11"), {})


if __name__ == "__main__":
    unittest.main()
