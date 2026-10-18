"""Offline tests of the program resolver: name parsing, torrent names, Hungarian texts and whole releases with the
network sources replaced by fixtures."""

from __future__ import annotations

import contextlib
import importlib.util
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ncore_up import release as rel
from ncore_up.resolve import software
from ncore_up.sources import winget

try:
    from test_software_local import FIXTURES, STRINGS, WIM_XML, build_pe, build_wim
except ImportError:  # run as tests.test_software_resolve from the repository root
    from tests.test_software_local import FIXTURES, STRINGS, WIM_XML, build_pe, build_wim

HAS_YAML = importlib.util.find_spec("yaml") is not None


class NameTests(unittest.TestCase):
    def test_scene_and_p2p_names(self):
        cases = {
            "APU.Software.TrueGain.v5.7.8.x64.WinALL.Incl.Keygen-BLiZZARD": ("APU Software TrueGain", "5.7.8", "x64", "Windows", "BLiZZARD"),
            "Microsoft Windows 11 Consumer Editions v26H2 HUN x64": ("Microsoft Windows 11 Consumer Editions", "26H2", "x64", "Windows", ""),
            "SciTools.Understand.v8.0.1264.x64.Incl.Keymaker-CORE": ("SciTools Understand", "8.0.1264", "x64", "Windows", "CORE"),
            "Adobe.Photoshop.2025.v26.1.0.121.x64.Multilingual-TCA": ("Adobe Photoshop 2025", "26.1.0.121", "x64", "Windows", "TCA"),
            "JetBrains.CLion.2025.2.3.Multilingual.x64-TRUMP": ("JetBrains CLion", "2025.2.3", "x64", "Windows", "TRUMP"),
            "Total Commander 11.58 x86-x64": ("Total Commander", "11.58", "x86-x64", "Windows", ""),
            "Corel.PaintShop.Pro.2023.v25.0.0.122.x86.x64.Multilingual-XYZ": ("Corel PaintShop Pro 2023", "25.0.0.122", "x86-x64", "Windows", "XYZ"),
            "VLC.Media.Player.v3.0.24.MacOS.U2B-TNT": ("VLC Media Player", "3.0.24", "", "macOS", "TNT"),
            "Notepad++ v8.9.8.1 Portable": ("Notepad++", "8.9.8.1", "", "", ""),
            "Tool.v1.2.Build.345.x86-GRP": ("Tool", "1.2.345", "x86", "Windows", "GRP"),
            "Windows.11.25H2.Build.26200.6584.x64.HUN": ("Windows 11", "25H2", "x64", "Windows", ""),
            "Some.App.x86_64.v2.0-GRP": ("Some App", "2.0", "x64", "Windows", "GRP"),
            "Mac.Cleaner.Pro.v3.1.MacOS-GRP": ("Mac Cleaner Pro", "3.1", "", "macOS", "GRP"),
        }
        for name, (title, version, arch, platform, group) in cases.items():
            parsed = software.parse_name(name)
            self.assertEqual((parsed["title"], parsed["version"], parsed["arch"], parsed["platform"], parsed["group"]),
                             (title, version, arch, platform, group), name)
        flags = software.parse_name("MAGIX.Sound.Forge.Pro.v18.0.0.40.x64.Incl.Patch.and.Keymaker-ZWT")
        self.assertEqual(flags["activation"], ["Patch", "Keymaker"])
        hun = software.parse_name("Microsoft Windows 11 Consumer Editions v26H2 HUN x64")
        self.assertTrue(hun["hungarian"])
        self.assertEqual(hun["languages"], ["magyar"])
        self.assertTrue(software.parse_name("Notepad++ v8.9.8.1 Portable")["portable"])

    def test_torrent_names(self):
        self.assertEqual(software.torrent_name("APU Software TrueGain", "5.7.8", "x64"), "APU Software TrueGain v5.7.8 x64")
        self.assertEqual(software.torrent_name("Microsoft Windows 11 Consumer Editions", "v26H2", "x64", hungarian=True),
                         "Microsoft Windows 11 Consumer Editions v26H2 HUN x64")
        self.assertEqual(software.torrent_name("Office: Pro Build", "2408 Build 17932", "x86-x64"), "Office - Pro v2408.17932 x86-x64")
        self.assertNotIn("build", software.torrent_name("Tool", "1.2 build 345", "x64").lower())


class TextTests(unittest.TestCase):
    def test_nfo_steps_translation(self):
        steps = ["Unpack and install the program", "Do not launch the program yet",
                 "Use our keygen to register the program (block it in your firewall)", "Enjoy!"]
        self.assertEqual(software.translate_steps(steps), [
            "Csomagold ki a fájlokat, majd telepítsd a programot.", "Még ne indítsd el a programot.",
            "Futtasd a mellékelt keygent, és regisztráld (aktiváld) vele a programot.", "Jó használatot!"])
        self.assertEqual(software.translate_steps(["Install", "Hack the planet with our special secret tool"]), [])
        self.assertEqual(software.translate_steps(["Copy the crack from the RUNE folder to the install dir"]),
                         ["Másold a mellékelt (crack) fájlokat a program telepítési mappájába, a meglévőket felülírva."])

    def test_install_guides(self):
        setup = software.install_guide("setup", {"setup": "TrueGain_Setup.exe", "tools": ["BLiZZARD/keygen.exe"], "tool_dirs": ["BLiZZARD"]})
        self.assertTrue(setup.startswith("1. Telepítsd a programot: futtasd a(z) TrueGain_Setup.exe fájlt"))
        self.assertIn("2. A telepítés végén még ne indítsd el a programot.", setup)
        self.assertIn("3. Futtasd a mellékelt keygent (keygen.exe) (mappa: BLiZZARD)", setup)
        patch = software.install_guide("msi", {"msi": "app.msi", "tools": ["Patch/patch.exe"], "activation": ["Patch"]})
        self.assertIn('msiexec /i "app.msi"', patch)
        self.assertIn("patch-et (patch.exe)", patch)
        portable = software.install_guide("portable", {"exe": "7zFM.exe", "archive": "app.rar"})
        self.assertTrue(portable.startswith("1. Csomagold ki a(z) app.rar archívumot"))
        self.assertIn("hordozható (portable)", portable)
        windows = software.install_guide("windows_iso", {"iso": "Win11_25H2_Hungarian_x64.iso", "editions": "Windows 11 Home, Windows 11 Pro"})
        self.assertIn("Rufus vagy a Ventoy", windows)
        self.assertIn("(Windows 11 Home, Windows 11 Pro)", windows)
        self.assertIn("Csatlakoztatás", software.install_guide("iso", {"iso": "disc.iso", "setup": "setup.exe"}))
        self.assertIn("Applications", software.install_guide("mac", {"mac": "VLC.dmg", "app": "VLC.app"}))
        self.assertIn("chmod +x", software.install_guide("linux", {"linux": "Tool.AppImage"}))
        crack = software.install_guide("setup", {"setup": "setup.exe", "tool_dirs": ["Crack"], "tools": ["Crack/app.dll"]})
        self.assertIn("Másold a(z) Crack mappa tartalmát", crack)

    def test_license_os_requirements(self):
        self.assertEqual(software.license_hu("LGPL-2.1"), "nyílt forráskódú (LGPL-2.1)")
        self.assertEqual(software.license_hu("Freeware"), "ingyenes (freeware)")
        self.assertEqual(software.license_hu("Proprietary"), "zárt forráskódú")
        self.assertEqual(software.license_hu("Inno Setup License"), "Inno Setup License")
        self.assertEqual(software.windows_name("10.0.17763.0"), "Windows 10 (1809)")
        self.assertEqual(software.windows_name("10.0.22000.0"), "Windows 11")
        self.assertEqual(software.windows_name("6.1.7600.0"), "Windows 7")
        req = software.requirements_text(os_name="Windows 10", arch="x64", nfo_text="2 GB RAM")
        self.assertEqual(req, "Operációs rendszer: Windows 10 vagy újabb\nArchitektúra: 64 bites (x64)\nAz NFO szerint:\n2 GB RAM")

    def test_arch_decision(self):
        stub = {"engine": "inno", "role": "setup", "pe": {"arch": "x86", "subsystem": "gui"}}
        arch, warnings = software.decide_arch({"arch": "x64"}, {"binaries": [stub]}, {})
        self.assertEqual((arch, warnings), ("x64", []))  # an x86 setup stub says nothing about the program
        arch, warnings = software.decide_arch({"arch": "x86"}, {"binaries": [dict(stub, payload_archs=["x64"])]}, {})
        self.assertEqual(arch, "x64")
        self.assertIn("a név szerint x86", warnings[0])
        arch, _w = software.decide_arch({"arch": ""}, {"msis_info": [{"arch": "x64"}], "binaries": []}, {})
        self.assertEqual(arch, "x64")
        official = {"installers": [{"arch": "x86", "sha256": "aa"}, {"arch": "x64", "sha256": "bb"}]}
        self.assertEqual(software.decide_arch({}, {"binaries": [stub], "sha256": {"setup.exe": "bb"}}, official)[0], "x64")
        arch, warnings = software.decide_arch({}, {"binaries": [stub]}, {})
        self.assertEqual(arch, "")
        self.assertIn("nem állapítható meg", warnings[0])


@contextlib.contextmanager
def offline(**overrides):
    """Every network-facing call of the resolver replaced (overrides: name -> return value)."""

    values = {"winget_lookup": {}, "choco": {}, "wikidata": {}, "summary": {}, "reachable": True, "brew": {}, "published": {}}
    values.update(overrides)
    with mock.patch.object(software.winget, "lookup", return_value=values["winget_lookup"]), \
            mock.patch.object(software.chocolatey, "best", return_value=values["choco"]), \
            mock.patch.object(software, "wikidata_software", return_value=values["wikidata"]), \
            mock.patch.object(software.wikipedia, "summary", return_value=values["summary"]), \
            mock.patch.object(software, "_reachable", return_value=values["reachable"]), \
            mock.patch.object(software.homebrew, "find", return_value=values["brew"]), \
            mock.patch.object(software.msdownload, "published", return_value=values["published"]), \
            mock.patch.object(software.translate, "to_hungarian", side_effect=lambda text, *a, **k: (text, False)):
        yield


class ResolveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_scene_release_with_keygen(self):
        folder = self.root / "APU.Software.TrueGain.v5.7.8.x64.WinALL.Incl.Keygen-BLiZZARD"
        (folder / "BLiZZARD").mkdir(parents=True)
        shutil.copy(FIXTURES / "blizzard.nfo", folder / "blizzard.nfo")
        shutil.copy(FIXTURES / "file_id.diz", folder / "file_id.diz")
        (folder / "TrueGain_Setup.exe").write_bytes(build_pe(0x14C, {"ProductName": "TrueGain", "CompanyName": "APU Software",
                                                                      "ProductVersion": "5.7.8", "Comments": "This installation was built with Inno Setup."},
                                                              overlay=b"zlb\x1a" + b"\0" * 64))
        (folder / "BLiZZARD" / "keygen.exe").write_bytes(build_pe(0x14C, {"ProductName": "keygen"}))
        with offline():
            meta = software.resolve(rel.inspect(folder), "misc", original=True, hints={})
        self.assertEqual((meta.kind, meta.title, meta.version, meta.publisher), ("software", "TrueGain", "5.7.8", "APU Software"))
        self.assertEqual(meta.extra["arch"], "x64")
        self.assertEqual(meta.extra["installer"], "Inno Setup telepítő")
        self.assertEqual(meta.extra["signer"], "nincs digitális aláírás")
        self.assertEqual(meta.extra["torrent_name"], folder.name)
        self.assertEqual(meta.extra["torrent_name_own"], "TrueGain v5.7.8 x64")
        self.assertEqual(meta.proof_links, ["https://apu.software/truegain/"])
        self.assertEqual(meta.date, "2026-10-14")
        self.assertIn("angol", meta.languages)
        self.assertTrue(meta.install_hu.startswith("1. Csomagold ki a fájlokat, majd telepítsd a programot."))
        self.assertIn("keygent", meta.install_hu)
        self.assertIn("loudness normalizer", meta.description_en)
        self.assertEqual(meta.description_source, "NFO")
        self.assertIn("Architektúra: 64 bites (x64)", meta.requirements)
        self.assertTrue(any("SAJÁT képet" in w for w in meta.warnings))
        self.assertTrue(any("nincs magyar leírás" in w for w in meta.warnings))

    @unittest.skipUnless(HAS_YAML, "PyYAML is not installed")
    def test_portable_release_with_winget_and_wikipedia(self):
        folder = self.root / "7-Zip 26.04 x64"
        folder.mkdir()
        (folder / "7zFM.exe").write_bytes(build_pe(0x8664, STRINGS))
        (folder / "7z.dll").write_bytes(build_pe(0x8664, {}))
        manifest = winget.parse_manifest((FIXTURES / "winget_7zip.7zip.yaml").read_text(encoding="utf-8"))
        manifest.update(matched_version=True, match={"method": "name", "score": 100.0, "publisher_match": True})
        wiki = {"qid": "Q215051", "huwiki": "7-Zip", "description_hu": "fájltömörítő program",
                "claims": {"P856": ["https://www.7-zip.org/"], "P154": ["7-zip Logo.svg"]}}
        summary = {"extract": "A 7-Zip egy nyílt forráskódú fájltömörítő program."}
        choco = {"id": "7zip", "project_url": "http://www.7-zip.org/", "icon_url": "https://example.org/7zip.svg", "description": "x"}
        with offline(winget_lookup=manifest, wikidata=wiki, summary=summary, choco=choco):
            meta = software.resolve(rel.inspect(folder), "misc", original=False, hints={})
        self.assertEqual((meta.title, meta.version, meta.publisher), ("7-Zip", "26.04", "Igor Pavlov"))
        self.assertEqual(meta.extra["arch"], "x64")
        self.assertEqual(meta.extra["license"], "nyílt forráskódú (LGPL-2.1)")
        self.assertEqual(meta.extra["torrent_name"], "7-Zip v26.04 x64")
        self.assertEqual(meta.proof_links, ["https://7-zip.org/download.html"])  # one link per site, never wiki
        self.assertEqual(meta.description_hu, summary["extract"])
        self.assertEqual(meta.description_source, "Wikipédia (CC BY-SA)")
        self.assertTrue(meta.cover_url.startswith("https://cdn.winget.microsoft.com/icons/"))
        self.assertIn("7zFM.exe", meta.install_hu)
        self.assertIn("hordozható", meta.install_hu)
        self.assertEqual(meta.identifiers["winget"], "7zip.7zip")
        self.assertEqual(meta.identifiers["wikidata"], "Q215051")
        self.assertEqual(meta.extra.get("min_os"), "Windows 7")  # the manifest has no MinimumOSVersion: the PE header tells
        self.assertGreaterEqual(meta.confidence, 0.7)

    def test_windows_install_media(self):
        folder = self.root / "Microsoft Windows 11 Consumer Editions v26H2 HUN x64"
        (folder / "sources").mkdir(parents=True)
        (folder / "sources" / "install.esd").write_bytes(build_wim(WIM_XML))
        (folder / "setup.exe").write_bytes(build_pe(0x8664, {"ProductName": "Microsoft Windows Operating System"}))
        with offline(summary={"extract": "A Windows 11 a Microsoft operációs rendszere."}):
            meta = software.resolve(rel.inspect(folder), "iso", original=False, hints={})
        self.assertEqual((meta.title, meta.version, meta.publisher), ("Microsoft Windows 11 Consumer Editions", "25H2", "Microsoft"))
        self.assertEqual(meta.extra["build"], "26200.6584")
        self.assertEqual(meta.extra["editions"], "Windows 11 Home, Windows 11 Pro")
        self.assertEqual(meta.extra["arch"], "x64")
        self.assertEqual(meta.languages, ["magyar"])
        self.assertEqual(meta.extra["torrent_name"], "Microsoft Windows 11 Consumer Editions v25H2 HUN x64")
        self.assertEqual(meta.proof_links, ["https://www.microsoft.com/hu-hu/software-download/windows11"])
        self.assertIn("TPM", meta.requirements)
        self.assertIn("Rufus", meta.install_hu)
        self.assertTrue(any("a név szerint 26H2" in w for w in meta.warnings))
        self.assertEqual(meta.description_source, "Wikipédia (CC BY-SA)")

    @unittest.skipUnless(HAS_YAML, "PyYAML is not installed")
    def test_winget_name_for_another_version_is_not_used(self):
        folder = self.root / "Inno Setup 5.6.1"
        folder.mkdir()
        (folder / "innosetup-5.6.1.exe").write_bytes(build_pe(0x14C, {"ProductName": "Inno Setup", "ProductVersion": "5.6.1",
                                                                      "Comments": "This installation was built with Inno Setup."}))
        manifest = winget.parse_manifest((FIXTURES / "winget_JRSoftware.InnoSetup.yaml").read_text(encoding="utf-8"))
        manifest.update(matched_version=False, latest_version="6.7.3", match={"method": "name", "score": 100.0, "publisher_match": True})
        with offline(winget_lookup=manifest):
            meta = software.resolve(rel.inspect(folder), "misc", original=False, hints={})
        self.assertEqual((meta.title, meta.version), ("Inno Setup", "5.6.1"))
        self.assertTrue(any("nem ismeri a(z) 5.6.1 verziót" in w for w in meta.warnings))
        self.assertEqual(sum("architektúra" in w or "kompatibilitást" in w for w in meta.warnings), 1)
        manifest.update(matched_version=True)
        (folder / "innosetup-5.6.1.exe").rename(folder / "innosetup-6.7.3.exe")
        with offline(winget_lookup=manifest):
            meta = software.resolve(rel.inspect(folder), "misc", original=False, hints={"version": "6.7.3"})
        self.assertEqual(meta.title, "Inno Setup")  # "Inno Setup 6" without the repeated major version

    def test_unreadable_files_never_raise(self):
        folder = self.root / "Broken.App.v1.0.x64-GRP"
        folder.mkdir()
        (folder / "setup.exe").write_bytes(b"MZ" + b"\xff" * 300)
        (folder / "grp.nfo").write_bytes(b"\xff\xfe\x00garbage")
        with offline(reachable=False):
            meta = software.resolve(rel.inspect(folder), "misc", original=True, hints={"title": "Broken App"})
        self.assertEqual((meta.title, meta.version, meta.extra["arch"]), ("Broken App", "1.0", "x64"))
        self.assertTrue(meta.install_hu.startswith("1. Telepítsd a programot"))


if __name__ == "__main__":
    unittest.main()
