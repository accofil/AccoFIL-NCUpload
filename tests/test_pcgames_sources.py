from __future__ import annotations

import json
import unittest
from pathlib import Path

from ncore_up.sources import gog, gogdb, pcgw, steam

FIX = Path(__file__).parent / "fixtures" / "pcgames"


def load(name: str):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def app(appid: str, lang: str) -> dict:
    return load(f"steam_app_{appid}_{lang}.json")[appid]["data"]


class SteamTests(unittest.TestCase):
    def test_search_and_match(self):
        items = steam.parse_search(load("steam_search_total_chaos.json"))
        self.assertEqual(items[0], {"appid": "2208350", "name": "Total Chaos", "type": "app"})
        self.assertEqual(steam.best_match(items, ["Total Chaos"])["appid"], "2208350")
        homm = steam.parse_search(load("steam_search_heroes_of_might_and_magic_3.json"))
        self.assertEqual(steam.best_match(homm, ["Heroes of Might and Magic 3 - Complete", "Heroes of Might and Magic 3"])["appid"], "4921760")
        tank = steam.parse_search(load("steam_search_tank_squad.json"))
        self.assertEqual(steam.best_match(tank, ["Tank Squad"])["appid"], "1498130")
        self.assertIsNone(steam.best_match(tank, ["Tank Squad - Ponyri"]))  # the DLC needs "DLC" in the wanted title
        self.assertEqual(steam.best_match(tank, ["Tank Squad - Ponyri DLC", "Tank Squad - Ponyri"])["appid"], "4746200")
        self.assertEqual(steam.parse_search(load("steam_search_heroes_of_might_and_magic_3_complete.json")), [])

    def test_app_total_chaos(self):
        info = steam.parse_app(app("2208350", "hungarian"), app("2208350", "english"))
        self.assertEqual((info["appid"], info["name"], info["date"]), ("2208350", "Total Chaos", "2025-11-20"))
        self.assertEqual(info["developers"], ["Trigger Happy Interactive"])
        self.assertEqual(info["genres_hu"], ["Akció", "Kaland", "Indie"])
        self.assertFalse(info["hungarian"])
        self.assertEqual(info["audio"], ["angol"])
        self.assertIn("német", info["languages"])
        minimum = dict(info["requirements"]["minimum"])
        self.assertEqual(minimum["Op. rendszer"], "Windows 10")
        self.assertEqual(minimum["Videokártya"], "GeForce GTX 970 or R9 390X")
        self.assertEqual(minimum["DirectX"], "10")
        self.assertEqual(minimum["Tárhely"], "30 GB szabad hely")
        self.assertEqual(info["url"], "https://store.steampowered.com/app/2208350/")
        self.assertTrue(info["cover_url"].endswith("/steam/apps/2208350/library_600x900.jpg"))
        self.assertNotIn("<", info["about_en"])
        self.assertTrue(info["short_en"].startswith("Fort Oasis is no paradise"))

    def test_hungarian_store_text_and_language_flags(self):
        witcher = steam.parse_app(app("292030", "hungarian"), app("292030", "english"))
        self.assertTrue(witcher["short_hu"].startswith("A The Witcher 3: Wild Hunt"))
        self.assertTrue(witcher["hungarian"])
        self.assertFalse(witcher["hungarian_audio"])
        self.assertEqual(witcher["date"], "2015-05-18")
        tomb = steam.parse_app(app("2478970", "hungarian"), app("2478970", "english"))
        self.assertTrue(tomb["hungarian"])
        self.assertEqual(tomb["date"], "2024-02-13")
        self.assertEqual(dict(tomb["requirements"]["minimum"])["Op. rendszer"], "Windows 7 or later")
        text = steam.requirements_text(tomb["requirements"])
        self.assertIn("Minimális gépigény:\n64 bites processzor és operációs rendszer szükséges\nOp. rendszer: Windows 7 or later", text)
        self.assertNotIn("Ajánlott", text)  # only an unlabelled line there

    def test_english_requirements_and_dates(self):
        rows = steam.parse_requirements(app("2478970", "english")["pc_requirements"]["minimum"])
        self.assertEqual(dict(rows)["Tárhely"], "5 GB szabad hely")
        self.assertEqual(dict(rows)["DirectX"], "12")
        self.assertEqual(steam.parse_date("13 Feb, 2024"), "2024-02-13")
        self.assertEqual(steam.parse_date("Feb 13, 2024"), "2024-02-13")
        self.assertEqual(steam.parse_date("2026. október 15."), "2026-10-15")
        self.assertEqual(steam.parse_date("Coming soon"), "")

    def test_tags_and_assets(self):
        item = steam.parse_items(load("steam_items_2208350.json"))
        self.assertEqual(item["tagids"][0], 19)
        names = steam.parse_taglist(load("steam_taglist_hu.json"))
        self.assertEqual(names[19], "Akció")
        self.assertTrue(steam.cover_url("2208350", item["assets"]).endswith("/library_capsule.jpg?t=1784890778"))

    def test_html_text(self):
        self.assertEqual(steam.html_text("<p>Egy</p><ul><li>a</li><li>b</li></ul><img src=x><p>Kettő&nbsp;!</p>"), "Egy\n\n• a\n• b\n\nKettő !")
        self.assertEqual(steam.norm_title("Heroes® of Might & Magic® III - HD Edition"), "heroes of might and magic 3 hd edition")


class GogTests(unittest.TestCase):
    def test_catalog(self):
        items = gog.parse_catalog(load("gog_catalog_tank_squad.json"))
        self.assertEqual(items[0]["id"], "1962416882")
        self.assertEqual(items[0]["release_date"], "2025-05-08")
        self.assertEqual(steam.best_match(items, ["Tank Squad"], key="title")["slug"], "tank_squad")
        mech = gog.parse_catalog(load("gog_catalog_mechwarrior_5_mercenaries.json"))
        titles = ["MechWarrior 5 - Mercenaries - JumpShip Edition", "MechWarrior 5 - Mercenaries", "MechWarrior 5"]
        self.assertEqual(steam.best_match(mech, titles, key="title")["id"], "2142732028")
        self.assertEqual(steam.best_match(mech, titles[1:], key="title")["id"], "2147483045")
        homm = gog.parse_catalog(load("gog_catalog_heroes_of_might_and_magic_3_complete.json"))
        self.assertEqual(steam.best_match(homm, ["Heroes of Might and Magic 3 - Complete"], key="title")["id"], "1207658787")

    def test_product(self):
        info = gog.parse_product(load("gog_product_1962416882.json"))
        self.assertEqual((info["title"], info["slug"], info["type"]), ("Tank Squad", "tank_squad", "game"))
        self.assertTrue(info["hungarian"])
        self.assertIn("magyar", info["languages"])
        self.assertIn("brazíliai portugál", info["languages"])
        self.assertEqual(info["installers"][0]["version"], "1.3.10 16733")
        self.assertEqual(info["url"], "https://www.gog.com/en/game/tank_squad")
        self.assertTrue(info["description_en"].startswith("Tank Squad is a tactical-combat action game"))
        pack = gog.parse_product(load("gog_product_2142732028.json"))
        self.assertEqual((pack["type"], pack["installers"]), ("pack", []))

    def test_game_v2(self):
        info = gog.parse_game(load("gog_game_1962416882.json"))
        self.assertTrue(info["hungarian"])
        self.assertFalse(info["hungarian_audio"])
        self.assertEqual(info["audio"], ["angol"])
        self.assertEqual(info["developers"], ["DeGenerals"])
        self.assertEqual(info["date"], "2025-05-08")
        minimum = dict(info["requirements"]["minimum"])
        self.assertEqual(minimum["Op. rendszer"], "Windows 10 (64-bit)")
        self.assertEqual(minimum["DirectX"], "11")
        homm = gog.parse_game(load("gog_game_1207658787.json"))
        self.assertFalse(homm["hungarian"])
        self.assertEqual(homm["date"], "1999-06-01")
        self.assertTrue(homm["cover_url"].startswith("https://images.gog-statics.com/"))

    def test_builds_and_version_match(self):
        builds = gog.parse_builds(load("gog_builds_1962416882.json"))
        self.assertEqual([b["version"] for b in builds], ["1.3.10 16733", "1.3.10 v5 staging", "1.3.10 v4"])
        self.assertEqual(gog.match_version("1.3.10 v4", builds)["date"], "2026-10-13")
        tomb = gog.parse_builds(load("gog_builds_1407750955.json"))
        self.assertEqual(gog.match_version("day 90", tomb)["version"], "Day 90")
        self.assertIsNone(gog.match_version("9.9", builds))
        self.assertEqual(gog.parse_builds(load("gog_builds_2142732028.json")), [])
        self.assertEqual(gog.genres_hu(["Action", "Simulation", "Tactical"]), ["Akció", "Szimuláció", "Taktikai"])


class GogdbTests(unittest.TestCase):
    def test_product(self):
        info = gogdb.parse_product(load("gogdb_product_1962416882.json"))
        self.assertEqual(info["builds"][0]["version"], "1.3.10 16733")
        self.assertIn("1.3.10 v4", [b["version"] for b in info["builds"]])
        self.assertEqual(info["url"], "https://www.gogdb.org/product/1962416882")
        self.assertTrue(any(loc["code"] == "hu" and loc["text"] for loc in info["localizations"]))
        self.assertEqual(gogdb.product_url("abc"), "")


class PcgwTests(unittest.TestCase):
    def test_idlookup_and_search(self):
        self.assertEqual(pcgw.parse_idlookup(load("pcgw_idlookup_1203620.json")), "Enshrouded")
        self.assertEqual(pcgw.parse_idlookup({"idlookup": []}), "")
        # "... Complete" is a redirect to the main page (parse follows it); the HD remaster must not win
        search = load("pcgw_search_homm3.json")
        self.assertEqual(pcgw.parse_search(search, "Heroes of Might and Magic 3 Complete"), "Heroes of Might and Magic III Complete")
        self.assertNotEqual(pcgw.parse_search(search, "Heroes of Might and Magic III"), "Heroes of Might and Magic III HD")
        self.assertEqual(pcgw.parse_search(search, "Tank Squad"), "")
        self.assertEqual(pcgw.parse_search({"query": {"search": [{"title": "Heroes of Might and Magic III"}]}}, "Heroes of Might and Magic 3"),
                         "Heroes of Might and Magic III")

    def test_witcher_languages_and_requirements(self):
        info = pcgw.parse_wikitext(load("pcgw_parse_witcher3.json")["parse"]["wikitext"])
        self.assertEqual(info["hungarian"]["interface"], True)
        self.assertEqual(info["hungarian"]["subtitles"], True)
        self.assertEqual(info["hungarian"]["audio"], False)
        minimum = dict(info["requirements"]["minimum"])
        self.assertEqual(minimum["Op. rendszer"], "Windows 11")
        self.assertEqual(minimum["Videokártya"], "Nvidia GeForce GTX 1660 / AMD Radeon RX 5500 XT 8GB / Intel Arc A580")
        self.assertEqual(minimum["Tárhely"], "60 GB")  # the {{note}} is dropped
        self.assertEqual(info["steam_appid"], "292030")

    def test_homm3_windows_block_only(self):
        info = pcgw.parse_wikitext(load("pcgw_parse_homm3.json")["parse"]["wikitext"])
        self.assertEqual(dict(info["requirements"]["minimum"])["Op. rendszer"], "Windows 95")
        self.assertEqual(info["gog_id"], "1207658787")
        self.assertEqual(info["hungarian"], {})
        self.assertEqual(pcgw.page_url("Heroes of Might and Magic III"), "https://www.pcgamingwiki.com/wiki/Heroes_of_Might_and_Magic_III")


if __name__ == "__main__":
    unittest.main()
