from __future__ import annotations

import unittest

from ncore_up import audio, describe, parse, plan as plans


class ParseTests(unittest.TestCase):
    def test_names(self):
        p = parse.parse("Repo.Men.2010.UNRATED.2160p.VUDU.WEB-DL.DDP5.1.H265.HuN.EnG-B9R")
        self.assertEqual((p.title, p.year, p.resolution, p.group), ("Repo Men", "2010", "2160p", "B9R"))
        s = parse.parse("Crystal.Lake.S01E01.2160p.PCOK.WEB-DL.DD5.1.HEVC-PlayWEB")
        self.assertEqual((s.title, s.season, s.episode, s.episodes), ("Crystal Lake", 1, 1, "1"))
        self.assertEqual(parse.parse("The.Diplomat.S04.2160p.NF.WEB-DL.DD+5.1.Atmos.H.265-ANARCHY").season, 4)
        self.assertEqual(parse.parse("Highlander.S01.COMPLETE.BLURAY-WRTB").title, "Highlander")
        self.assertEqual(parse.parse("Blade.Runner.2049.2017.1080p.BluRay.x264-GRP").title, "Blade Runner 2049")


class DescribeTests(unittest.TestCase):
    def test_channel_layouts_and_four_channel_fallback(self):
        for count, layout, expected in ((4, "", "4.0"), (4, "4.0", "4.0"), (4, "quad", "4.0"),
                                         (4, "3.1", "3.1"), (3, "3.0", "3.0"), (5, "5.0(side)", "5.0"),
                                         (6, "5.1(side)", "5.1"), (6, "6.0", "6.0"), (4, "7.1", "4.0")):
            with self.subTest(count=count, layout=layout):
                self.assertEqual(describe.audio_line([{"lang": "eng", "channels": count, "channel_layout": layout}]), "angol " + expected)

    def test_description_does_not_repeat_structured_details(self):
        text = describe.video_description(menu=False, audio=[{"lang": "eng", "channels": 4}], subtitles=[],
                                          external_subs=[], extras="nincs", plot="Tartalom.", source="G4MBiT", include_details=False)
        self.assertEqual(text, "Tartalom.\n\nForrás: G4MBiT\n")

    def test_tracks(self):
        audio_tracks = [{"lang": "hun", "channels": 6, "title": ""}, {"lang": "eng", "channels": 8, "title": "Atmos"}, {"lang": "eng", "channels": 2, "title": "Commentary"}]
        self.assertEqual(describe.audio_line(audio_tracks), "magyar 5.1, angol 7.1, angol 2.0 kommentár")
        subs = [{"lang": "hun", "forced": True, "title": ""}, {"lang": "hun", "forced": False, "title": ""}, {"lang": "eng", "forced": False, "title": "English SDH"}]
        self.assertEqual(describe.subtitle_line(subs), "magyar kiegészítő, magyar, angol")

    def test_names(self):
        self.assertEqual(describe.video_name("Az a bizonyos első év", "I Give It a Year", "I.Give.It.a.Year.2013.1080p.BluRay.REMUX.AVC-HiFi", hungarian=True), "Az a bizonyos első év 1080p REMUX")
        self.assertEqual(describe.video_name("Titkok tengere", "The Blue Exile", "x.S01E56.1080p.WEB-DL", hungarian=True, season=1, episodes="56"), "Titkok tengere S01E56 1080p")
        self.assertEqual(describe.video_name("", "Forbidden City", "La.Citta.Proibita.2025.720p.x264-FHC", hungarian=False), "Forbidden City 720p")
        self.assertEqual(describe.video_name("Robin Byrd: Szex, tévé, szabadság", "Bang My Box", "x.720p", hungarian=True), "Robin Byrd - Szex, tévé, szabadság 720p")
        self.assertEqual(describe.disc_label(45 * 1024**3, uhd=False), "BD50")

    def test_description(self):
        text = describe.video_description(menu=False, audio=[{"lang": "hun", "channels": 6, "title": ""}], subtitles=[], external_subs=[], extras="", plot="Tartalom.", source="X.2013.1080p-GRP")
        self.assertEqual(text, "Menü: nincs\nFelirat(ok): nincs\nHangsáv: magyar 5.1\n\nTartalom.\n\nForrás: X.2013.1080p-GRP\n")


class MusicTests(unittest.TestCase):
    def test_folder_and_styles(self):
        self.assertEqual(audio.parse_folder("Korinna-UTAZO-Vadvirag-WEB-MP3-2026-Killoszto"), ("Korinna", "UTAZO Vadvirag", "2026"))
        self.assertEqual(audio.parse_folder("Omega - XI (1982)"), ("Omega", "XI", "1982"))
        self.assertEqual(audio.ncore_styles(["Hip Hop", "Soundtrack", "Shoegaze"]), ["hip.hop", "ost", "shoegaze"])


class ModeTests(unittest.TestCase):
    def test_original_detection(self):
        class R:
            nfo = object()
            name = "Movie.2020.1080p.BluRay.x264-GRP"
        self.assertTrue(plans.looks_original(R))
        R.name = "Az a bizonyos első év 1080p REMUX"
        self.assertFalse(plans.looks_original(R))
        R.name, R.nfo = "Movie.2020.1080p.BluRay.x264-GRP", None
        self.assertFalse(plans.looks_original(R))


if __name__ == "__main__":
    unittest.main()
