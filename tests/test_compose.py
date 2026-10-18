"""compose.*: description templates per content group, provenance notes, boilerplate filter, proof links."""

from __future__ import annotations

import unittest

from ncore_up import compose
from ncore_up.meta import Meta, Track


class TextTests(unittest.TestCase):
    def test_lines_drop_empty_values(self):
        self.assertEqual(compose._lines(("A", "1"), ("B", ""), ("C", "3")), ["A: 1", "C: 3"])

    def test_join_skips_empty_blocks(self):
        self.assertEqual(compose.join([["a"], [], ["b", "c"]]), "a\n\nb\nc\n")

    def test_boilerplate_paragraphs_are_dropped(self):
        text = "A játék története.\n\nAll rights reserved. Terms of Service apply.\n\nMásodik bekezdés.\n\nEgészségügyi figyelmeztetés: tartson szünetet."
        self.assertEqual(compose.clean_text(text), "A játék története.\n\nMásodik bekezdés.")

    def test_long_text_is_cut_at_paragraph_or_sentence(self):
        first, second = "Első mondat. " * 10, "Második bekezdés " * 20
        self.assertEqual(compose.clean_text(f"{first}\n\n{second}", limit=200), first.strip())
        cut = compose.clean_text("Egy mondat itt. " * 40, limit=100)
        self.assertLessEqual(len(cut), 100)
        self.assertTrue(cut.endswith("."))


class PlotBlockTests(unittest.TestCase):
    def test_native_hungarian_retains_source_credit(self):
        self.assertEqual(compose.plot_block(Meta(description_hu="Magyar tartalom.", description_source="PORT.hu")), ["Magyar tartalom.\n(Forrás: PORT.hu)"])

    def test_empty_or_boilerplate_only(self):
        self.assertEqual(compose.plot_block(Meta()), [])
        self.assertEqual(compose.plot_block(Meta(description_en="Privacy Policy and Terms of Service.")), [])

    def test_machine_translation_note(self):
        meta = Meta(description_hu="Gépi szöveg.", description_en="Text.", machine_translated=True, description_source="Steam")
        self.assertEqual(compose.plot_block(meta), ["Gépi szöveg.\n(gépi fordítás; Forrás: Steam)"])

    def test_cc_by_sa_attribution(self):
        meta = Meta(description_hu="Wikis szöveg.", description_source="Wikipédia (CC BY-SA)")
        self.assertEqual(compose.plot_block(meta), ["Wikis szöveg.\n(Forrás: Wikipédia (CC BY-SA))"])
        both = Meta(description_hu="Fordított.", machine_translated=True, description_source="Wikipedia (CC BY-SA)")
        self.assertEqual(compose.plot_block(both), ["Fordított.\n(gépi fordítás; Forrás: Wikipedia (CC BY-SA))"])

    def test_english_text_is_marked_even_with_attribution(self):
        self.assertEqual(compose.plot_block(Meta(description_en="English text.")), ["English text.\n(angol nyelvű leírás)"])
        meta = Meta(description_en="English wiki text.", description_source="Wikipedia (CC BY-SA)")
        self.assertEqual(compose.plot_block(meta), ["English wiki text.\n(angol nyelvű leírás; Forrás: Wikipedia (CC BY-SA))"])


class ProofTests(unittest.TestCase):
    def test_single_and_many(self):
        self.assertEqual(compose.proof_block(Meta(proof_links=["https://moly.hu/konyvek/x"])), ["Igazoló link: https://moly.hu/konyvek/x"])
        many = Meta(proof_links=[f"https://example.com/{i}" for i in range(5)])
        self.assertEqual(compose.proof_block(many, "Link"), ["Link:", "https://example.com/0", "https://example.com/1", "https://example.com/2"])
        self.assertEqual(compose.proof_block(Meta()), [])

    def test_never_wikipedia_youtube_videa(self):
        meta = Meta()
        for url in ("https://hu.wikipedia.org/wiki/X", "https://www.youtube.com/watch?v=1", "https://videa.hu/x", "https://www.gog.com/game/x"):
            meta.add_proof(url)
        self.assertEqual(meta.proof_links, ["https://www.gog.com/game/x"])
        # links set without add_proof (and in other letter case) are filtered by the template as well
        meta = Meta(proof_links=["https://EN.WIKIPEDIA.ORG/wiki/X", "https://youtu.be/x", "https://www.discogs.com/release/1", "https://www.discogs.com/release/1"])
        self.assertEqual(compose.proof_block(meta), ["Igazoló link: https://www.discogs.com/release/1"])
        self.assertNotIn("wikipedia", compose.book(meta).lower())
        self.assertEqual(compose.proof_block(Meta(proof_links=["https://hu.wikipedia.org/wiki/X"])), [])


class TemplateTests(unittest.TestCase):
    def test_music(self):
        meta = Meta(kind="album", title="Album", creators=["Artist"], year="2026", publisher="Label", extra={"catno": "LBL001", "format": "MP3 320 kbps"},
                    tracks=[Track("1", "One", "3:21"), Track("2", "Two", artist="Guest")], proof_links=["https://musicbrainz.org/release/x"])
        self.assertEqual(compose.music(meta), "Előadó: Artist\nAlbum címe: Album\nMegjelenés éve: 2026\nKiadó: Label (LBL001)\n\n"
                                              "Tracklista:\n1. One (3:21)\n2. Guest - Two\n\nIgazoló link: https://musicbrainz.org/release/x\n")

    def test_music_empty_fields_omitted(self):
        text = compose.music(Meta(title="Album", creators=["A"], extra={"catno": "CAT 9"}))
        self.assertEqual(text, "Előadó: A\nAlbum címe: Album\nKatalógusszám: CAT 9\n")
        self.assertNotIn("Kiadó", text)
        self.assertNotIn("Tracklista", text)
        self.assertEqual(compose.music(Meta()), "\n")

    def test_audiobook(self):
        meta = Meta(kind="audiobook", title="A könyv", creators=["Szerző Elek"], year="2020", publisher="Kiadó Kft.",
                    extra={"narrator": "Felolvasó Ferenc", "format": "MP3 64 kbps", "duration": "5:01:02"},
                    description_hu="Történet.", tracks=[Track("01", "Első fejezet", "30:00")])
        self.assertEqual(compose.audiobook(meta), "Szerző: Szerző Elek\nCím: A könyv\nElőadó: Felolvasó Ferenc\nKiadó: Kiadó Kft.\nMegjelenés éve: 2020\n"
                                                "Teljes hossz: 5:01:02\n\nTörténet.\n\nFejezetek:\n01. Első fejezet (30:00)\n")

    def test_clip(self):
        meta = Meta(kind="concert", title="Live at Venue", creators=["Artist"], year="2019", tracks=[Track("1", "Song")], proof_links=["https://www.setlist.fm/x"])
        text = compose.clip(meta, audio="angol 5.1", subtitles="nincs", menu=None, extras="nincs", chapters=True, sample=False, source="Artist.Live-GRP")
        self.assertEqual(text, "Előadó: Artist\nKiadvány címe: Live at Venue\nMegjelenés éve: 2019\n\nTracklista:\n1. Song\n\n"
                               "Hangsáv: angol 5.1\nFelirat: nincs\nExtra: nincs\nChapter: van\nSample: nincs\n\nForrás: Artist.Live-GRP\n\n"
                               "Igazoló link: https://www.setlist.fm/x\n")
        with_menu = compose.clip(meta, audio="", subtitles="", menu=False, extras="", chapters=False, sample=True, source="")
        self.assertIn("Menü: nincs\nChapter: nincs\nSample: van", with_menu)
        self.assertNotIn("Forrás", with_menu)

    def test_book(self):
        meta = Meta(kind="book", title="A könyv", subtitle="Alcím", title_original="The Book", date="2020-05-01", year="2020", publisher="Európa",
                    creators=["Szerző Elek"], languages=["magyar"], identifiers={"isbn": "9789630000000"},
                    extra={"format": "EPUB", "series": "Sorozat", "translator": "Fordító Ferenc", "pages": "320"},
                    proof_links=["https://moly.hu/konyvek/a-konyv"], description_hu="Fülszöveg.")
        self.assertEqual(compose.book(meta), "Könyv címe: A könyv\nAlcím: Alcím\nEredeti cím: The Book\nSorozat: Sorozat\nKiadás dátuma: 2020-05-01\n"
                                             "Kiadó: Európa\nFormátum: EPUB\n\nNyelv: magyar\nSzerző(k): Szerző Elek\nFordító: Fordító Ferenc\n"
                                             "Oldalak száma: 320\nISBN: 9789630000000\n\nIgazoló link: https://moly.hu/konyvek/a-konyv\n\nLeírás:\nFülszöveg.\n")
        self.assertEqual(compose.book(Meta(title="X", year="2001")), "Könyv címe: X\nKiadás dátuma: 2001\n")

    def test_game(self):
        meta = Meta(kind="game", title="Game", date="2024-03-01", creators=["Dev"], publisher="Pub", genres=["akció"], languages=["angol", "magyar"],
                    version="1.2", description_hu="Leírás.", proof_links=["https://www.gog.com/game/x"], requirements="OS: Windows 10",
                    install_hu="1. Futtasd a telepítőt.")
        self.assertEqual(compose.game(meta, "Game v1.2 GoG"), "Game v1.2 GoG\n\nMegjelenés dátuma: 2024-03-01\nFejlesztő: Dev\nKiadó: Pub\nMűfaj: akció\n"
                                                             "Nyelv: angol, magyar\nVerzió: 1.2\n\nLeírás.\n\nHivatalos oldal / igazoló link: "
                                                             "https://www.gog.com/game/x\n\nGépigény:\nOS: Windows 10\n\nTelepítés:\n1. Futtasd a telepítőt.\n")
        self.assertEqual(compose.game(Meta(), ""), "\n")

    def test_software(self):
        meta = Meta(kind="software", title="Tool", version="1.2", publisher="Tool Inc.", creators=["Ignored"], languages=["magyar"],
                    extra={"arch": "x64", "license": "freeware"}, requirements="Windows 10", install_hu="1. Telepítsd.")
        text = compose.software(meta, "Tool v1.2 x64")
        self.assertEqual(text, "Tool v1.2 x64\n\nProgram neve: Tool\nVerzió: 1.2\nFejlesztő: Tool Inc.\nArchitektúra: x64\nLicenc: freeware\nNyelv: magyar\n\n"
                               "Rendszerigény:\nWindows 10\n\nTelepítés:\n1. Telepítsd.\n")
        self.assertIn("Fejlesztő: Dev A, Dev B", compose.software(Meta(creators=["Dev A", "Dev B"]), ""))

    def test_console(self):
        meta = Meta(kind="console_game", title="Game", platform="PS4", publisher="Pub", creators=["Dev"], genres=["akció"], date="2020-01-01",
                    region="PAL (EUR)", languages=["magyar"], version="1.05", identifiers={"titleid": "CUSA00001", "serial": "SER"},
                    extra={"required_system": "PS4 firmware 9.00+"}, install_hu="1. Másold fel.")
        self.assertEqual(compose.console(meta, "[PS4] Game v1.05 HUN"), "[PS4] Game v1.05 HUN\n\nPlatform: PS4\nKiadó: Pub\nFejlesztő: Dev\nKategória: akció\n"
                                                                       "Megjelenés: 2020-01-01\nRégió: PAL (EUR)\nNyelv: magyar\nVerzió: 1.05\n"
                                                                       "Azonosító: CUSA00001\n\nSzükséges gép: PS4 firmware 9.00+\n\nTelepítés:\n1. Másold fel.\n")
        self.assertIn("Azonosító: ABCD01", compose.console(Meta(identifiers={"id6": "ABCD01"}), ""))
        self.assertNotIn("Azonosító", compose.console(Meta(platform="PS2"), ""))

    def test_app(self):
        meta = Meta(kind="app", version="2.0", creators=["Dev"], genres=["eszközök"], identifiers={"package": "com.example.app"},
                    extra={"min_os": "Android 8.0", "signature": "ellenőrizve"}, description_en="An app.", proof_links=["https://play.google.com/store/apps/details?id=com.example.app"])
        self.assertEqual(compose.app(meta, "[Android] App v2.0"), "[Android] App v2.0\n\nVerzió: 2.0\nFejlesztő: Dev\nKategória: eszközök\nMinimum rendszer: Android 8.0\n"
                                                                 "Csomagnév: com.example.app\nAláírás: ellenőrizve\n\nAn app.\n(angol nyelvű leírás)\n\n"
                                                                 "Igazoló link: https://play.google.com/store/apps/details?id=com.example.app\n")


if __name__ == "__main__":
    unittest.main()
