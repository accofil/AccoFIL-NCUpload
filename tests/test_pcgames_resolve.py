from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ncore_up import keys, net
from ncore_up import release as rel
from ncore_up.local import gamefiles as gf
from ncore_up.local import gamename
from ncore_up.resolve import games

FIX = Path(__file__).parent / "fixtures" / "pcgames"


def load(name: str):
    path = FIX / name
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


STEAM_SEARCH = {"total chaos": "total_chaos", "tank squad": "tank_squad", "heroes of might and magic 3": "heroes_of_might_and_magic_3",
                "tomb raider i-iii remastered starring lara croft": "tomb_raider_i-iii_remastered", "silver pines": "silver_pines"}
GOG_SEARCH = {"tank squad": "tank_squad", "heroes of might and magic 3 complete": "heroes_of_might_and_magic_3_complete",
              "heroes of might and magic 3": "heroes_of_might_and_magic_3", "tomb raider i-iii remastered starring lara croft": "tomb_raider_i-iii_remastered",
              "mechwarrior 5 mercenaries jumpship edition": "mechwarrior_5_mercenaries", "mechwarrior 5 mercenaries": "mechwarrior_5_mercenaries"}


def fake_get_json(url: str, params: dict | None = None, **_kwargs):
    params = params or {}
    if "storesearch" in url:
        name = STEAM_SEARCH.get(params.get("term", "").lower())
        return load(f"steam_search_{name}.json") if name else {"total": 0, "items": []}
    if "appdetails" in url:
        return load(f"steam_app_{params['appids']}_{params['l']}.json")
    if "GetItems" in url:
        appid = json.loads(params["input_json"])["ids"][0]["appid"]
        return load(f"steam_items_{appid}.json")
    if "GetTagList" in url:
        return load("steam_taglist_hu.json")
    if "catalog.gog.com" in url:
        name = GOG_SEARCH.get(params.get("query", "").replace("like:", "").lower())
        return load(f"gog_catalog_{name}.json") if name else {"products": []}
    match = re.search(r"api\.gog\.com/products/(\d+)", url)
    if match:
        return load(f"gog_product_{match.group(1)}.json")
    match = re.search(r"api\.gog\.com/v2/games/(\d+)", url)
    if match:
        return load(f"gog_game_{match.group(1)}.json")
    match = re.search(r"content-system\.gog\.com/products/(\d+)/", url)
    if match:
        return load(f"gog_builds_{match.group(1)}.json") or {"items": []}
    match = re.search(r"gogdb\.org/data/products/(\d+)/", url)
    if match:
        return load(f"gogdb_product_{match.group(1)}.json")
    if "pcgamingwiki" in url:
        if params.get("action") == "idlookup":
            return {"1203620": load("pcgw_idlookup_1203620.json"), "292030": {"idlookup": [{"title": {"Page": "The Witcher 3: Wild Hunt"}}]},
                    "1207658787": {"idlookup": [{"title": {"Page": "Heroes of Might and Magic III"}}]}}.get(params.get("value"), {"idlookup": []})
        if params.get("action") == "parse":
            page = {"Enshrouded": "enshrouded", "The Witcher 3: Wild Hunt": "witcher3", "Heroes of Might and Magic III": "homm3"}.get(params.get("page"))
            return load(f"pcgw_parse_{page}.json") if page else None
        if params.get("srsearch") == "The Witcher 3 Wild Hunt":
            return {"query": {"search": [{"title": "The Witcher 3: Wild Hunt"}]}}
        return {"query": {"search": []}}
    if "wikidata.org" in url:
        return {"results": {"bindings": []}}
    return None


def make_release(tmp: str, name: str, files: dict[str, bytes]) -> rel.Release:
    root = Path(tmp) / name
    root.mkdir()
    for relpath, content in files.items():
        path = root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    with mock.patch.object(rel, "hungarian_audio", return_value=None):
        return rel.inspect(root)


class Routed(unittest.TestCase):
    def setUp(self):
        patches = [mock.patch.object(net, "get_json", side_effect=fake_get_json),
                   mock.patch.object(net, "request", return_value=mock.Mock(status_code=200)),
                   mock.patch.object(keys, "get", return_value="")]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def resolve(self, name: str, files: dict[str, bytes], *, original: bool = False, code: str = "game_rip", hints: dict | None = None):
        with tempfile.TemporaryDirectory() as tmp:
            release = make_release(tmp, name, files)
            return games.resolve(release, code, original=original, hints={"deep": False, **(hints or {})})


class GogResolveTests(Routed):
    def test_tank_squad_gog(self):
        meta = self.resolve("Tank Squad v1.3.10 v4 GoG", {"setup_tank_squad_1.3.10_v4_(64bit)_(85000).exe": b"", "setup_tank_squad_1.3.10_v4_(64bit)_(85000)-1.bin": b""})
        self.assertEqual((meta.kind, meta.title, meta.version), ("game", "Tank Squad", "1.3.10 v4"))
        self.assertEqual(meta.identifiers["gog"], "1962416882")
        self.assertEqual(meta.extra["torrent_name"], "Tank Squad v1.3.10 v4 GoG")
        self.assertEqual(meta.extra["version_proof"], "GOG build 1.3.10 v4 (2026-10-13)")
        self.assertEqual(meta.proof_links[:2], ["https://www.gog.com/en/game/tank_squad", "https://www.gogdb.org/product/1962416882"])
        self.assertIn("magyar", meta.languages)
        self.assertEqual(meta.extra["hun_name_hint"], "HUN")
        self.assertEqual(meta.creators, ["DeGenerals"])
        self.assertIn("Telepítsd & Játssz!", meta.install_hu)
        self.assertIn("setup_tank_squad_1.3.10_v4_(64bit)_(85000).exe", meta.install_hu)
        self.assertIn("magyar is választható", meta.install_hu)
        self.assertIn("Op. rendszer: Windows 10 (64-bit)", meta.requirements)
        self.assertEqual(meta.extra["category_hint"], "game_rip")
        self.assertIn(games.SCREENSHOT_WARNING, meta.warnings)
        self.assertTrue(meta.description_en.startswith("Tank Squad is a tactical-combat action game"))
        self.assertEqual(meta.description_hu, "")  # no DeepL key in tests, no huwiki
        self.assertTrue(any("nincs magyar leírás" in w for w in meta.warnings))
        self.assertGreaterEqual(meta.confidence, 0.9)

    def test_gog_only_title_without_marker(self):
        meta = self.resolve("Heroes of Might and Magic 3 Complete", {"setup_heroes_of_might_and_magic_3_complete_4.0_(3.2)_gog_0.1_(77075).exe": b""})
        self.assertEqual(meta.title, "Heroes of Might and Magic 3: Complete")
        self.assertEqual(meta.identifiers["gog"], "1207658787")
        self.assertEqual(meta.year, "1999")
        self.assertEqual(meta.version, "4.0 (3.2) GOG 0.1")
        self.assertEqual(meta.extra["torrent_name"], "Heroes of Might and Magic 3 - Complete v4.0 (3.2) GOG 0.1 GoG")
        self.assertEqual(meta.extra["hungarian"], "felület: nem, felirat: nem, szinkron: nem")
        self.assertIn("Op. rendszer: Windows (10, 11)", meta.requirements)
        self.assertEqual(meta.extra["release_date"], "1999. június 1.")
        self.assertTrue(meta.cover_url.startswith("https://images.gog-statics.com/"))  # GOG box art for a GOG release
        self.assertEqual(meta.title_hu, "")

    def test_edition_pack_uses_base_game_builds(self):
        meta = self.resolve("MechWarrior 5 - Mercenaries - JumpShip Edition v1.15.398 GoG",
                            {"setup_mechwarrior_5_mercenaries_1.15.398_(64bit)_(90000).exe": b""})
        self.assertEqual(meta.identifiers["gog"], "2142732028")
        self.assertEqual(meta.extra["version_proof"], "GOG build 1.15.398 (2026-09-24)")
        self.assertEqual(meta.extra["torrent_name"], "MechWarrior 5 - Mercenaries - JumpShip Edition v1.15.398 GoG")


class SceneResolveTests(Routed):
    def test_total_chaos_scene_iso(self):
        nfo = (FIX / "tenoke_style.nfo").read_bytes()
        meta = self.resolve("Total.Chaos.v1.0.342-TENOKE", {"tenoke-total.chaos.v1.0.342.iso": b"", "tenoke-total.chaos.v1.0.342.nfo": nfo},
                            original=True, code="game_iso")
        self.assertEqual((meta.title, meta.version, meta.identifiers["steam"]), ("Total Chaos", "1.0.342", "2208350"))
        self.assertEqual(meta.extra["torrent_name"], "Total.Chaos.v1.0.342-TENOKE")  # original release keeps its name
        self.assertEqual(meta.extra["category_hint"], "game_iso")
        self.assertEqual(meta.genres, ["Akció", "Kaland", "Indie"])
        self.assertEqual(meta.publisher, "Apogee Entertainment")
        self.assertEqual(meta.date, "2025-11-20")
        self.assertEqual(meta.proof_links[0], "https://store.steampowered.com/app/2208350/")
        self.assertIn("http://www.totalchaosgame.com", meta.proof_links)
        self.assertIn("csatold fel (mount)", meta.install_hu)
        self.assertIn("Másold a crack fájljait", meta.install_hu)
        self.assertIn("tűzfalban", meta.install_hu)
        self.assertIn("Videokártya: GeForce GTX 970 or R9 390X", meta.requirements)
        self.assertEqual(meta.extra["hungarian"], "felület: nem, felirat: nem, szinkron: nem")
        self.assertNotIn(games.SCREENSHOT_WARNING, meta.warnings)
        self.assertEqual(meta.cover_url, "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/2208350/"
                                         "51c4460bc1d6608bc4f434b78b18efdd10af9a48/library_capsule.jpg?t=1784890778")
        self.assertEqual(meta.extra["steam_tags"].split(", ")[0], "Akció")

    def test_own_upload_name_and_hungarian_store_text(self):
        meta = self.resolve("Tomb.Raider.I-III.Remastered.Starring.Lara.Croft-RUNE", {"rune.nfo": (FIX / "rune_style.nfo").read_bytes(),
                                                                                     "rune-tr.rar": b"", "rune-tr.r00": b""})
        self.assertEqual(meta.identifiers["steam"], "2478970")
        self.assertEqual(meta.extra["torrent_name"], "Tomb Raider I-III Remastered Starring Lara Croft")
        self.assertEqual(meta.extra["hun_name_hint"], "HUN")
        self.assertIn("Másold a(z) RUNE mappa tartalmát", meta.install_hu)
        self.assertIn("rendszergazdaként", meta.install_hu)
        self.assertEqual(meta.extra["category_hint"], "game_iso")

    def test_witcher_hungarian_description_and_pcgw_flags(self):
        meta = self.resolve("The Witcher 3 Wild Hunt GOTY v4.04a", {"setup.exe": b""}, hints={"steam": "292030"})
        self.assertEqual(meta.description_source, "Steam")
        self.assertTrue(meta.description_hu.startswith("A The Witcher 3: Wild Hunt"))
        self.assertFalse(meta.machine_translated)
        self.assertEqual(meta.extra["hungarian"], "felület: igen, felirat: igen, szinkron: nem")
        self.assertEqual(meta.identifiers["pcgw"], "The Witcher 3: Wild Hunt")

    def test_renamed_store_title_found_through_pcgw(self):
        # Steam/GOG now say "The Witcher 3: Wild Hunt — Remastered"; the PCGW page title and infobox bridge it
        meta = self.resolve("The.Witcher.3.Wild.Hunt.v4.04a-RUNE", {"rune-tw3.rar": b""})
        self.assertEqual(meta.identifiers["steam"], "292030")
        self.assertEqual(meta.identifiers["pcgw"], "The Witcher 3: Wild Hunt")
        self.assertEqual(meta.description_source, "Steam")

    def test_dlc_does_not_match_the_base_game(self):
        meta = self.resolve("Tank Squad - Ponyri DLC", {"setup_tank_squad_-_ponyri_dlc_1.0_(1234).exe": b""})
        self.assertNotIn("gog", meta.identifiers)
        self.assertEqual(meta.extra["torrent_name"], "Tank Squad - Ponyri DLC v1.0 GoG")
        self.assertIn("telepítsd az alapjátékot", meta.install_hu)
        self.assertNotIn("A DLC-k telepítőit", meta.install_hu)

    def test_machine_translation_keeps_english(self):
        with mock.patch.object(games.translate, "to_hungarian", return_value=("Fort Oasis nem paradicsom.", True)) as deepl:
            meta = self.resolve("Total.Chaos.v1.0.342-TENOKE", {"tenoke-tc.iso": b""})
        self.assertTrue(deepl.call_args[0][0].startswith("Fort Oasis is no paradise"))  # the short description is translated
        self.assertEqual((meta.description_hu, meta.machine_translated, meta.description_source), ("Fort Oasis nem paradicsom.", True, "Steam"))
        self.assertTrue(meta.description_en.startswith("Descend into Total Chaos"))

    def test_igdb_fallback_with_keys(self):
        game = {"id": 7, "name": "Obscure Retro Game", "summary": "An old game.", "first_release_date": 946684800,
                "genres": [{"name": "Strategy"}], "involved_companies": [{"developer": True, "publisher": False, "company": {"name": "Old Studio"}}],
                "cover": {"image_id": "abc"}}
        igdb_keys = {"IGDB_CLIENT_ID": "id", "IGDB_CLIENT_SECRET": "secret"}
        with mock.patch.object(keys, "get", side_effect=lambda name: igdb_keys.get(name, "")), \
                mock.patch.object(games.igdb, "search", return_value=[game]):
            meta = self.resolve("Obscure.Retro.Game-TENOKE", {"tenoke-org.iso": b""})
        self.assertEqual((meta.title, meta.year, meta.creators, meta.identifiers["igdb"]), ("Obscure Retro Game", "2000", ["Old Studio"], "7"))
        self.assertEqual(meta.cover_url, "https://images.igdb.com/igdb/image/upload/t_cover_big/abc.jpg")
        self.assertEqual(meta.genres, ["Stratégia"])
        self.assertEqual(meta.description_en, "An old game.")

    def test_nothing_found(self):
        meta = self.resolve("Unknown.Game.Nobody.Made.v1.0-TENOKE", {"tenoke-ugnm.iso": b""})
        self.assertLess(meta.confidence, 0.5)
        self.assertTrue(any("egyik adatbázisban sem" in w for w in meta.warnings))
        self.assertTrue(any("IGDB kihagyva" in w for w in meta.warnings))
        self.assertEqual(meta.extra["torrent_name"], "Unknown Game Nobody Made v1.0")
        self.assertIn("Ha a képfájlon van TENOKE vagy Crack mappa", meta.install_hu)


class GuideAndNameTests(unittest.TestCase):
    def test_hungarian_text_check(self):
        self.assertFalse(games.looks_hungarian("Descend into Total Chaos, a gripping survival horror experience, a story and a game."))
        self.assertTrue(games.looks_hungarian("A The Witcher 3: Wild Hunt – Remastered számos fejlesztéssel és bővítéssel modernizálja a "
                                              "népszerű szerepjátékot, és egy nyílt sötétfantasy-világba visz."))

    def test_install_templates(self):
        fitgirl = gf.classify(["setup.exe", "fg-01.bin", "fg-optional-bonus.bin", "MD5/QuickSFV.EXE", "Verify BIN files before installation.bat"])
        guide, _ = games.install_guide(fitgirl, gamename.parse("Game Name - FitGirl Repack"))
        self.assertIn("„Verify BIN files before installation.bat”", guide)
        self.assertIn("fg-optional-bonus.bin", guide)
        self.assertIn("Telepítsd & Játssz!", guide)
        update = gf.classify(["rune.nfo", "rune-update.rar"])
        update.nfo = gf.parse_nfo(gf.read_nfo(FIX / "update_style.nfo"))
        guide, warnings = games.install_guide(update, gamename.parse("Tank.Squad.Update.v1.1.1-RUNE"))
        self.assertTrue(guide.startswith("Előfeltétel: a játék telepített v1.0.9 verziója (Tank.Squad-RUNE)"))
        self.assertIn("telepítsd a frissítést", guide)
        self.assertEqual(warnings, [])
        drmfree = gf.classify(["setup.exe", "data.bin"])
        drmfree.nfo = gf.parse_nfo(gf.read_nfo(FIX / "drmfree_style.nfo"))
        guide, _ = games.install_guide(drmfree, gamename.parse("Silver Pines - Deluxe Edition v618.173902"))
        self.assertIn("már crackelt", guide)
        self.assertNotIn("vásárold", guide)
        image = gf.classify(["game.iso"])
        image.crack_dirs, image.redist_dirs = ["TENOKE"], ["_CommonRedist"]
        guide, _ = games.install_guide(image, gamename.parse("Game.Name-TENOKE"))
        self.assertIn("1. Csatold fel a képfájlt (game.iso)", guide)
        self.assertIn("3. Másold a(z) TENOKE mappa tartalmát", guide)
        self.assertIn("_CommonRedist", guide)
        crackfix, _ = games.install_guide(gf.classify(["TENOKE/steam_api64.dll"]), gamename.parse("Game.Name.Crackfix-TENOKE"))
        self.assertIn("Másold a(z) TENOKE mappa tartalmát a játék telepítési könyvtárába", crackfix)

    def test_unknown_nfo_line_is_kept_with_warning(self):
        files = gf.classify(["game.iso"])
        files.nfo = {"install": ["Mount", "Install", "Do the funky chicken dance"]}
        guide, warnings = games.install_guide(files, gamename.parse("Game.Name-RUNE"))
        self.assertIn("(Do the funky chicken dance)", guide)
        self.assertTrue(warnings)

    def test_torrent_names(self):
        self.assertEqual(games.torrent_name("Game Name", gamename.parse("Game Name v1.2 - MULTi8 - FitGirl Repack")), "Game Name v1.2 - MULTi8 - FitGirl Repack")
        self.assertEqual(games.torrent_name("Game: Sub", gamename.parse("Game.Sub.v1.2-RUNE")), "Game - Sub v1.2")
        self.assertEqual(games.torrent_name("Tank Squad", gamename.parse("Tank.Squad.Update.v1.1.1-TENOKE")), "Tank Squad Update v1.1.1")
        self.assertEqual(games.torrent_name("Pizza House Simulator", gamename.parse("Pizza.House.Simulator.Build.17390212-TENOKE")),
                         "Pizza House Simulator Build 17390212")
        self.assertEqual(games.torrent_name("Game Name", gamename.parse("Game Name HUN v1.2 GoG")), "Game Name v1.2 HUN GoG")
        self.assertEqual(games.hungarian_date("2025-11-20"), "2025. november 20.")
        self.assertEqual(games.hungarian_date("2025"), "2025")


if __name__ == "__main__":
    unittest.main()
