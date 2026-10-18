"""Regression tests for the review fixes of the pcgames area (offline, fixtures + mocks)."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ncore_up import net
from ncore_up.local import gamefiles as gf
from ncore_up.local import gamename
from ncore_up.resolve import games
from ncore_up.sources import gog, gogdb, pcgw, steam

sys.path.insert(0, str(Path(__file__).parent))
from test_pcgames_resolve import Routed, fake_get_json  # noqa: E402


class NameFixTests(unittest.TestCase):
    def test_tag_words_that_start_a_title(self):
        for name, title, kind in (("Patch.Quest-TENOKE", "Patch Quest", "full"), ("Patch.Quest.Update.v1.2-TENOKE", "Patch Quest", "update"),
                                  ("DLC Quest-TENOKE", "DLC Quest", "full"), ("PC.Building.Simulator.2-RUNE", "PC Building Simulator 2", "full"),
                                  ("Win.That.War-TENOKE", "Win That War", "full"), ("Game.Name.v1.0.PC-RUNE", "Game Name", "full")):
            parsed = gamename.parse(name)
            self.assertEqual((parsed["title"], parsed["kind"]), (title, kind), name)

    def test_hun_tag_only_in_capitals(self):
        self.assertEqual(gamename.parse("Attila.the.Hun-RUNE")["title"], "Attila the Hun")
        self.assertEqual(gamename.parse("Attila.the.Hun-RUNE")["languages"], [])
        self.assertEqual(gamename.parse("Game.Name.HUN-RUNE")["languages"], ["hu"])
        self.assertEqual(gamename.parse("Game Name Magyarítás v1.0 GoG")["languages"], ["hu"])

    def test_the_final_cut(self):
        parsed = gamename.parse("Disco Elysium - The Final Cut v1.0 GoG")
        self.assertEqual((parsed["title"], parsed["subtitle"], parsed["edition"]), ("Disco Elysium", "", "The Final Cut"))


class MatchFixTests(unittest.TestCase):
    def test_numbers_must_agree(self):
        self.assertIsNone(steam.best_match([{"name": "Frostpunk 2"}], ["Frostpunk"]))
        self.assertIsNone(steam.best_match([{"name": "Cities: Skylines"}], ["Cities Skylines II"]))
        self.assertEqual(steam.best_match([{"name": "Cities: Skylines"}, {"name": "Cities: Skylines II"}], ["Cities Skylines II"])["name"],
                         "Cities: Skylines II")
        self.assertIsNotNone(steam.best_match([{"name": "Warhammer 40,000: Space Marine 2"}], ["Warhammer 40000 Space Marine 2"]))
        self.assertIsNotNone(steam.best_match([{"name": "Alone in the Dark"}], ["Alone in the Dark 2024"]))  # years do not count
        self.assertIsNone(steam.best_match([None, "x", {"name": None}], ["Game"]))

    def test_website_entities(self):
        hu = {"steam_appid": 5, "name": "X", "website": "https://example.com/?a=1&amp;b=2"}
        self.assertEqual(steam.parse_app(hu, None)["website"], "https://example.com/?a=1&b=2")


class OddShapeTests(unittest.TestCase):
    def test_parsers_never_raise(self):
        odd = [None, {}, [], "x", 5, {"items": [None]}, {"products": [None, {"id": 1, "genres": None}]}, {"response": {"store_items": {}}},
               {"_embedded": [], "id": 1}, {"idlookup": ["x"]}, {"query": {"search": [None]}}, {"id": 1, "languages": ["hu"], "downloads": []}]
        parsers = (steam.parse_search, steam.parse_items, steam.parse_taglist, gog.parse_catalog, gog.parse_product, gog.parse_game,
                   gog.parse_builds, gogdb.parse_product, pcgw.parse_idlookup, lambda d: pcgw.parse_search(d, "Game"),
                   lambda d: steam.parse_app(d, d))
        for parser in parsers:
            for value in odd:
                parser(value)  # must not raise
        self.assertEqual(gog.parse_product({"id": 1, "languages": ["hu"], "downloads": []})["languages"], [])

    def test_appdetails_odd_json(self):
        for value in (None, [], ["x"], {"5": None}, {"5": {"success": True, "data": []}}, {"5": {"success": False}}):
            with mock.patch.object(net, "get_json", return_value=value):
                self.assertEqual(steam.appdetails("5"), {})

    def test_innoextract_error_line(self):
        # innoextract 1.9 on an Inno Setup 6.7 installer (captured on nvme)
        stderr = ("Warning: Unexpected setup loader revision: 2\nWarning: Setup loader checksum mismatch!\n"
                  "Could not determine setup data version!\nIf you are sure the setup file is not corrupted, consider \n"
                  "filing a bug report at https://innoextract.constexpr.org/issues\nDone with 1 error and 2 warnings.\n")
        self.assertTrue(gf.innoextract_error(stderr).startswith("Could not determine setup data version!; valószínűleg újabb Inno Setup"))
        self.assertEqual(gf.innoextract_error("Not a supported Inno Setup installer!\n"), "Not a supported Inno Setup installer!")
        self.assertEqual(gf.innoextract_error(""), "innoextract hiba")
        info = gf.parse_innoextract_info('Inspecting "Inno Setup" - setup data version 5.6.0 (unicode)\n\nLanguages:\n - english: English\n')
        self.assertEqual((info["app_name"], info["inno_version"]), ("Inno Setup", "5.6.0"))
        self.assertEqual(gf.parse_innoextract_languages('Inspecting "Inno Setup" - setup data version 5.6.0 (unicode)\n - english: English\n'
                                                        ' - hungarian: Magyar\n'), ["english", "hungarian"])

    def test_huge_nfo_is_capped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "big.nfo"
            path.write_bytes(("é" * (gf.MAX_NFO)).encode("utf-8"))  # 2 bytes each: the cap cuts a character
            text = gf.read_nfo(path)
        self.assertEqual(len(text), gf.MAX_NFO // 2)
        self.assertEqual(set(text), {"é"})


class ResolveFixTests(Routed):
    def test_title_less_name_does_not_crash(self):
        meta = self.resolve("[FitGirl Repack]", {"setup.exe": b"", "fg-01.bin": b""})
        self.assertEqual(meta.kind, "game")
        self.assertTrue(any("nem derült ki a játék címe" in w for w in meta.warnings))
        self.assertEqual(meta.extra["torrent_name"], "[FitGirl Repack]")
        self.assertLess(meta.confidence, 0.5)

    def test_title_from_nfo_when_the_name_has_none(self):
        nfo = (Path(__file__).parent / "fixtures" / "pcgames" / "tenoke_style.nfo").read_bytes()
        meta = self.resolve("-TENOKE", {"tenoke-tc.iso": b"", "tenoke-tc.nfo": nfo})
        self.assertEqual((meta.title, meta.identifiers.get("steam")), ("Total Chaos", "2208350"))

    def test_version_hint_with_v_prefix(self):
        meta = self.resolve("Tank Squad GoG", {"setup_tank_squad_1.3.10_v4_(64bit)_(85000).exe": b""}, hints={"version": "v1.3.10 v4"})
        self.assertEqual(meta.version, "1.3.10 v4")
        self.assertEqual(meta.extra["version_proof"], "GOG build 1.3.10 v4 (2026-10-13)")
        self.assertFalse(any("vv" in w for w in meta.warnings))

    def test_gogdb_json_is_not_fetched(self):
        calls: list[str] = []

        def spy(url, *args, **kwargs):
            calls.append(url)
            return fake_get_json(url, *args, **kwargs)

        with mock.patch.object(net, "get_json", side_effect=spy):
            meta = self.resolve("MechWarrior 5 - Mercenaries - JumpShip Edition v1.15.398 GoG",
                                {"setup_mechwarrior_5_mercenaries_1.15.398_(64bit)_(90000).exe": b""})
        self.assertFalse([u for u in calls if "gogdb.org" in u])
        self.assertIn("https://www.gogdb.org/product/2142732028", meta.proof_links)  # the page stays a proof link

    def test_failed_steam_hint_is_not_trusted(self):
        meta = self.resolve("Unknown.Game.Nobody.Made.v1.0-TENOKE", {"tenoke-ugnm.iso": b""}, hints={"steam": "999999"})
        self.assertLess(meta.confidence, 0.5)


class GuideFixTests(unittest.TestCase):
    def test_common_scene_install_lines(self):
        cases = {
            "Burn or mount with your favorite tool.": "Csatold fel (mount)",
            "Copy everything from the FLT folder into the game installation.": "Másold a(z) FLT mappa tartalmát",
            "Copy over the cracked content from the /Crack directory on the image to your game install directory.": "Másold a(z) Crack mappa",
            'Copy the content of the "EMPRESS" folder to the game install directory': "Másold a(z) EMPRESS mappa",
            "Copy everything from the CODEX directory on the disc to the installation directory, overwriting the existing files":
                "Másold a(z) CODEX mappa",
            "Start the game.": "Játssz!",
        }
        for line, expected in cases.items():
            text, matched = games.translate_step(line)
            self.assertTrue(matched, line)
            self.assertIn(expected, text, line)
        self.assertFalse(games.translate_step("Run the game setup")[1])

    def test_gog_patch_only_release(self):
        files = gf.classify(["patch_tank_squad_1.3.9_(84000)_to_1.3.10_(85000).exe"])
        guide, _ = games.install_guide(files, gamename.parse("Tank Squad Patch v1.3.10 GoG"))
        self.assertIn("Futtasd a(z) patch_tank_squad_1.3.9_(84000)_to_1.3.10_(85000).exe", guide)
        self.assertNotIn("setup_", guide)

    def test_portable_skips_redist_and_helpers(self):
        files = gf.classify(["_CommonRedist/vcredist_x64.exe", "Game/unins000.exe", "Game/Game.exe"])
        guide, _ = games.install_guide(files, gamename.parse("Game.Name-P2P"))
        self.assertIn("Indítsd: Game.exe", guide)

    def test_fan_translation_is_not_counted_as_hungarian(self):
        data = {"steam": {"appid": "1", "hungarian": False}, "pcgw": {"hungarian": {"interface": True, "subtitles": True, "audio": False, "fan": True}}}
        flags = games._hungarian_flags(data)
        self.assertEqual((flags["interface"], flags["subtitles"]), (False, False))
        self.assertIn("rajongói magyarítás", games._flags_text(flags))

    def test_empty_title_torrent_name(self):
        self.assertEqual(games.torrent_name("", gamename.parse("[FitGirl Repack]")), "FitGirl Repack")


if __name__ == "__main__":
    unittest.main()
