from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ncore_up import release as rel
from ncore_up.local import gamefiles as gf

FIX = Path(__file__).parent / "fixtures" / "pcgames"
GOG_FILES = [("setup_tank_squad_1.3.10_16733_(64bit)_(85512).exe", 2097152), ("setup_tank_squad_1.3.10_16733_(64bit)_(85512)-1.bin", 4291821568),
             ("setup_tank_squad_1.3.10_16733_(64bit)_(85512)-2.bin", 4293918720), ("setup_tank_squad_1.3.10_16733_(64bit)_(85512)-3.bin", 3547332608),
             ("setup_tank_squad_-_ponyri_dlc_1.3.10_(64bit)_(85513).exe", 1000)]
FITGIRL_FILES = ["setup.exe", "fg-01.bin", "fg-02.bin", "fg-optional-bonus-content.bin", "fg-selective-english.bin", "MD5/QuickSFV.EXE",
                 "MD5/QuickSFV.ini", "MD5/fitgirl-bins.md5", "Verify BIN files before installation.bat"]


class ClassifyTests(unittest.TestCase):
    def test_gog_offline_installer(self):
        info = gf.classify(GOG_FILES)
        self.assertEqual(info.layout, "gog")
        self.assertEqual(info.setup, GOG_FILES[0][0])
        self.assertEqual(len(info.gog_parts), 3)
        self.assertEqual(info.dlc, ["setup_tank_squad_-_ponyri_dlc_1.3.10_(64bit)_(85513).exe"])
        self.assertIn("GOG offline telepítő: setup_tank_squad_1.3.10_16733_(64bit)_(85512).exe + 3 .bin rész", info.facts)
        self.assertEqual(info.gog_parsed["slug"], "tank_squad")

    def test_gog_version_words_are_not_updates(self):
        info = gf.classify(["setup_the_witcher_3_wild_hunt_goty_4.04a_redkit_update_2_(64bit)_(74397).exe",
                            "setup_the_witcher_3_wild_hunt_goty_4.04a_redkit_update_2_(64bit)_(74397)-1.bin",
                            "patch_the_witcher_3_wild_hunt_goty_4.04_(70000)_to_4.04a_(74397).exe"])
        self.assertEqual(info.updates, ["patch_the_witcher_3_wild_hunt_goty_4.04_(70000)_to_4.04a_(74397).exe"])
        self.assertEqual(info.dlc, [])
        self.assertEqual(len(info.gog_parts), 1)

    def test_fitgirl_repack(self):
        info = gf.classify(FITGIRL_FILES)
        self.assertEqual((info.layout, info.setup), ("fitgirl", "setup.exe"))
        self.assertEqual(info.optional, ["fg-optional-bonus-content.bin"])
        self.assertEqual(info.selective, ["fg-selective-english.bin"])
        self.assertIn("Verify BIN files before installation.bat", info.verify)
        self.assertIn("MD5/QuickSFV.EXE", info.verify)

    def test_scene_layouts(self):
        packed = gf.classify(["rune.nfo", "rune-enshrouded.sfv", "rune-enshrouded.rar"] + [f"rune-enshrouded.r{i:02d}" for i in range(12)])
        self.assertEqual((packed.layout, packed.archives, packed.nfo_files), ("packed", ["rune-enshrouded.rar"], ["rune.nfo"]))
        parts = gf.classify(["game-elamigos.part01.rar", "game-elamigos.part02.rar"])
        self.assertEqual(parts.archives, ["game-elamigos.part01.rar"])
        iso = gf.classify([("tenoke-total.chaos.iso", 15 * 2**30), ("tenoke-total.chaos.nfo", 2000)])
        self.assertEqual((iso.layout, iso.disc_images), ("disc_image", ["tenoke-total.chaos.iso"]))
        cue = gf.classify(["Game (Track 1).bin", "Game (Track 2).bin", "Game.cue"])
        self.assertEqual(cue.layout, "disc_image")
        self.assertEqual(len(cue.disc_images), 3)

    def test_disc_content_and_portable(self):
        inner = gf.classify(["setup.exe", "setup-1.bin", "TENOKE/steam_api64.dll", "_CommonRedist/DirectX/DXSETUP.exe", "autorun.inf"])
        self.assertEqual((inner.layout, inner.setup, inner.crack_dirs, inner.redist_dirs), ("installer", "setup.exe", ["TENOKE"], ["_CommonRedist"]))
        portable = gf.classify(["Game/Game.exe", "Game/data.pak", "Game/steam_api64.dll"])
        self.assertEqual(portable.layout, "portable")
        self.assertEqual(gf.classify([]).layout, "unknown")


class ToolOutputTests(unittest.TestCase):
    def test_7z_listing(self):
        files = gf.parse_7z_listing((FIX / "7z_listing.txt").read_text(encoding="utf-8"))
        names = [name for name, _size in files]
        self.assertIn("TENOKE/steam_api64.dll", names)
        self.assertIn("setup.exe", names)
        self.assertNotIn("TENOKE", names)  # folders are skipped
        info = gf.classify(files)
        self.assertEqual(info.crack_dirs, ["TENOKE"])

    def test_innoextract_and_exiftool(self):
        self.assertEqual(gf.parse_innoextract_gog_id("GOG.com game ID is 1207658787\n"), "1207658787")
        self.assertEqual(gf.parse_innoextract_gog_id("1962416882"), "1962416882")
        self.assertEqual(gf.parse_innoextract_gog_id("No GOG.com game ID found!\n"), "")
        info = gf.parse_innoextract_info('Listing "Tank Squad" - setup data version 6.0.0 (unicode)\n - "app/goggame-1962416882.info"\n')
        self.assertEqual((info["app_name"], info["inno_version"], info["gog_id"]), ("Tank Squad", "6.0.0", "1962416882"))
        self.assertEqual(gf.parse_innoextract_languages(" - english: English\n - hungarian: Magyar\n"), ["english", "hungarian"])
        exif = gf.parse_exiftool_json('[{"SourceFile": "x.exe", "ProductName": "Tank Squad", "ProductVersion": "1.3.10 16733"}]')
        self.assertEqual(exif, {"product_name": "Tank Squad", "product_version": "1.3.10 16733"})

    def test_missing_tools_do_not_raise(self):
        with mock.patch.object(gf.shutil, "which", return_value=None):
            self.assertEqual(gf.list_archive(Path("nope.iso")), [])
            info = gf.gog_installer_info(Path("nope.exe"))
        self.assertIn("innoextract", info.get("error", ""))

    @unittest.skipUnless(shutil.which("innoextract"), "innoextract not installed")
    def test_innoextract_on_a_non_installer(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "setup_fake_1.0_(1).exe"
            fake.write_bytes(b"MZ not an installer")
            info = gf.gog_installer_info(fake)
        self.assertFalse(info.get("gog_id"))
        self.assertTrue(info.get("error"))


class NfoTests(unittest.TestCase):
    def nfo(self, name: str) -> dict:
        return gf.parse_nfo(gf.read_nfo(FIX / name))

    def test_tenoke_style(self):
        info = self.nfo("tenoke_style.nfo")
        self.assertEqual((info["title"], info["version"], info["release_date"], info["protection"]), ("Total Chaos", "1.0.342", "2025-11-20", "Steam"))
        self.assertEqual(info["install"], ["Extract and burn or mount the .iso", "Run SETUP.exe and install the game", "Copy crack to install dir", "Play"])
        self.assertEqual(info["notes"], ["Block the game exe in your firewall to prevent the game from trying to go online."])
        self.assertFalse(info["already_cracked"])

    def test_rune_style_and_update(self):
        info = self.nfo("rune_style.nfo")
        self.assertEqual(info["install"][2], "Copy crack from RUNE dir to installdir")
        self.assertEqual(info["disks"], "12 x 2GB")
        self.assertEqual(len(info["notes"]), 2)
        update = self.nfo("update_style.nfo")
        self.assertEqual((update["update_from"], update["update_to"], update["version"], update["requires"]), ("1.0.9", "1.1.1", "1.1.1", "Tank.Squad-RUNE"))

    def test_drm_free_and_cp437(self):
        info = self.nfo("drmfree_style.nfo")
        self.assertTrue(info["already_cracked"])
        self.assertEqual(info["install"][:2], ["Extract", "Run Setup.exe"])
        self.assertIn("░▒▓█", gf.read_nfo(FIX / "drmfree_style.nfo"))

    def test_inspect_release_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Total.Chaos.v1.0.342-TENOKE"
            root.mkdir()
            (root / "tenoke-total.chaos.v1.0.342.iso").write_bytes(b"")
            shutil.copy(FIX / "tenoke_style.nfo", root / "tenoke-total.chaos.v1.0.342.nfo")
            with mock.patch.object(rel, "hungarian_audio", return_value=None):
                release = rel.inspect(root)
            info = gf.inspect(release, deep=False)
        self.assertEqual(info.layout, "disc_image")
        self.assertEqual(info.nfo["version"], "1.0.342")
        self.assertIn("Lemezkép: tenoke-total.chaos.v1.0.342.iso", info.facts)


if __name__ == "__main__":
    unittest.main()
