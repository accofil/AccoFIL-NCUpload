from __future__ import annotations

import unittest

from ncore_up.local import gamename


def parse(name: str) -> dict:
    return gamename.parse(name)


class ReleaseNameTests(unittest.TestCase):
    def check(self, name: str, **expected):
        result = parse(name)
        for key, value in expected.items():
            self.assertEqual(result[key], value, f"{name!r}: {key}")
        return result

    def test_gog_names(self):
        self.check("Tank Squad v1.3.10 v4 GoG", title="Tank Squad", version="1.3.10 v4", source="gog", kind="full", group="")
        self.check("Tomb Raider I-III Remastered Starring Lara Croft vDay 90 GoG",
                   title="Tomb Raider I-III Remastered Starring Lara Croft", version="Day 90", source="gog")
        self.check("MechWarrior 5 - Mercenaries - JumpShip Edition v1.15.398 GoG",
                   title="MechWarrior 5", subtitle="Mercenaries", edition="JumpShip Edition", version="1.15.398", source="gog")
        self.check("The Witcher 3 Wild Hunt GOTY v4.04a GOG", title="The Witcher 3 Wild Hunt", edition="GOTY", version="4.04a")
        self.check("Alone in the Dark v1.0.5b (GOG)", title="Alone in the Dark", version="1.0.5b", source="gog")

    def test_scene_and_p2p(self):
        self.check("Enshrouded-RUNE", title="Enshrouded", group="RUNE", source="scene", version="", kind="full")
        self.check("Total.Chaos.v1.0.342-TENOKE", title="Total Chaos", version="1.0.342", group="TENOKE", source="scene")
        self.check("Frostpunk.2.Deluxe.Edition.v1.4.0-I_KnoW", title="Frostpunk 2", edition="Deluxe Edition", version="1.4.0",
                   group="I_KnoW", source="p2p")
        self.check("Pizza.House.Simulator.Build.17390212-TENOKE", title="Pizza House Simulator", build="17390212", version="")
        self.check("Some.Game.v20240512-P2P", title="Some Game", version="20240512", source="p2p")
        self.check("Half-Life 2", title="Half-Life 2", group="")

    def test_kinds(self):
        self.check("Game.Name.Update.v1.2.3-RUNE", title="Game Name", kind="update", version="1.2.3", group="RUNE")
        self.check("Game.Name.Update.v1.0.1.to.v1.0.2-RUNE", kind="update", update_from="1.0.1", version="1.0.2")
        self.check("Tank.Squad.Update.v1.1.1-TENOKE", title="Tank Squad", kind="update", version="1.1.1")
        self.check("Game.Name.Crackfix-TENOKE", title="Game Name", kind="crackfix")
        self.check("Tank Squad - Ponyri DLC", title="Tank Squad", subtitle="Ponyri", kind="dlc")

    def test_repacks_and_languages(self):
        self.check("Game Name - FitGirl Repack", title="Game Name", source="fitgirl", repack="FitGirl Repack")
        self.check("Game Name v1.2 - MULTi8 - FitGirl Repack", title="Game Name", version="1.2", multi=8, source="fitgirl")
        cyber = self.check("Cyberpunk 2077 Ultimate Edition (v2.21 + 3 DLCs + Bonus Content) [FitGirl Repack]",
                           title="Cyberpunk 2077", edition="Ultimate Edition", version="2.21", source="fitgirl")
        self.assertTrue(cyber["dlc_included"])
        self.assertEqual(cyber["kind"], "full")
        self.check("Baldurs.Gate.3.v4.1.1.6072089.incl.DLC.MULTi13-ElAmigos", title="Baldurs Gate 3", version="4.1.1.6072089",
                   multi=13, source="elamigos", dlc_included=True)
        self.check("Hades II [DODI Repack]", title="Hades II", source="dodi", repack="DODI Repack")
        self.check("Game Name HUN v1.2 GoG", title="Game Name", languages=["hu"], source="gog", version="1.2")
        self.check("Game.Name.Early.Access.v0.9-P2P", title="Game Name", early_access=True, version="0.9")
        self.check("Silver Pines - Deluxe Edition v618.173902", title="Silver Pines", edition="Deluxe Edition", version="618.173902", source="")

    def test_titles_for_search(self):
        parsed = parse("MechWarrior 5 - Mercenaries - JumpShip Edition v1.15.398 GoG")
        self.assertEqual(gamename.full_title(parsed), "MechWarrior 5 - Mercenaries - JumpShip Edition")
        self.assertEqual(gamename.search_titles(parsed), ["MechWarrior 5 - Mercenaries - JumpShip Edition", "MechWarrior 5 - Mercenaries", "MechWarrior 5"])
        self.assertEqual(gamename.search_titles(parse("Enshrouded-RUNE")), ["Enshrouded"])
        self.check("Heroes of Might and Magic 3 Complete", title="Heroes of Might and Magic 3", edition="Complete")


class GogInstallerNameTests(unittest.TestCase):
    def test_with_api_versions(self):
        info = gamename.parse_gog_installer("setup_tank_squad_1.3.10_16733_(64bit)_(85512).exe", versions=["1.3.10 16733"])
        self.assertEqual((info["slug"], info["version"], info["arch"], info["build"], info["part"]), ("tank_squad", "1.3.10 16733", "64", "85512", ""))
        info = gamename.parse_gog_installer("setup_heroes_of_might_and_magic_3_complete_4.0_(3.2)_gog_0.1_(77075).exe", versions=["4.0 (3.2) GOG 0.1"])
        self.assertEqual((info["slug"], info["version"]), ("heroes_of_might_and_magic_3_complete", "4.0 (3.2) GOG 0.1"))
        info = gamename.parse_gog_installer("setup_tomb_raider_i_to_iii_remastered_day_90_(64bit)_(81111).exe", versions=["1.1.2a", "Day 90"])
        self.assertEqual((info["slug"], info["version"]), ("tomb_raider_i_to_iii_remastered", "Day 90"))

    def test_heuristics_parts_and_slug(self):
        info = gamename.parse_gog_installer("setup_cyberpunk_2077_2.13_(64bit)_(76205).exe")
        self.assertEqual((info["slug"], info["version"], info["title"]), ("cyberpunk_2077", "2.13", "Cyberpunk 2077"))
        part = gamename.parse_gog_installer("setup_tank_squad_1.3.10_16733_(64bit)_(85512)-2.bin")
        self.assertEqual((part["part"], part["ext"], part["version"]), ("2", "bin", "1.3.10 16733"))
        known = gamename.parse_gog_installer("setup_the_witcher_3_wild_hunt_goty_4.04a_redkit_update_2_(64bit)_(74397).exe",
                                             slug="the_witcher_3_wild_hunt_goty")
        self.assertEqual(known["version"], "4.04a redkit update 2")
        self.assertEqual(gamename.parse_gog_installer("setup_game_1.0_(french)_(12345).exe")["language"], "french")
        self.assertEqual(gamename.parse_gog_installer("setup.exe"), {})
        self.assertTrue(gamename.is_gog_installer("Tank Squad/setup_tank_squad_1.3.10_16733_(64bit)_(85512).exe"))
        self.assertEqual(gamename.slug_title("heroes_of_might_and_magic_iii_horn_of_the_abyss"), "Heroes of Might and Magic III Horn of the Abyss")


if __name__ == "__main__":
    unittest.main()
