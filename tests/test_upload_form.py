"""Upload form per category and mode, as the upload page's ncorev12.js ``lapoz`` shows the fields."""

from __future__ import annotations

import unittest

from ncore_up import categories as cats
from ncore_up import form as forms

BASE = {"getUnique", "tipus", "torrent_nev", "eredeti", "szoveg", "keresre", "keres_kodja", "mindent_tud1", "mindent_tud3"}
INFOBAR = {"imdb_id", "film_adatbazis", "megjelent", "dvd_megjelent", "orszag", "hossz", "film_magyar_cim", "film_angol_cim",
           "film_idegen_cim", "rendezo", "szereplok"}
DISC_EXTRA = {"film_felirat", "film_hangsav", "film_extra", "menu"}
SERIES_EXTRA = {"szezon", "epizod_szamok", "epizod_cim"}
MUSIC_OWN = {"eloado", "album", "album_megjelenes"}
EBOOK_OWN = {"konyv_cime", "konyvv_kiadva", "konyv_formatum"}


def data(code: str, original: bool, **kw) -> forms.UploadData:
    defaults = dict(infobar=forms.Infobar(imdb_id="tt1053424", title_hu="Végrehajtók", title_en="Repo Men"), menu=True, season=1,
                    episodes="1-6", artist="Omega", album="Gyöngyhajú lány", album_year="1969", book_title="Az ajtó", book_year="1987",
                    book_format="EPUB", styles=["rock"])
    defaults.update(kw)
    return forms.UploadData(category=code, name="Valami hosszabb név", original=original, **defaults)


def expected_fields(code: str, original: bool) -> set[str]:
    """The field map of the upload page (ncorev12.js lapoz), category by category."""

    fields = set(BASE)
    if code in cats.VIDEO_CODES:
        fields |= INFOBAR
        if not original and code in forms.DISC_FILM_CODES:
            fields |= DISC_EXTRA
        if not original and code in forms.SERIES_CODES:
            fields |= SERIES_EXTRA
    if code in forms.MUSIC_CODES:
        fields |= {"music_style"} | (set() if original else MUSIC_OWN)
    if code in forms.EBOOK_CODES:
        fields |= {"ebook_style"} | (set() if original else EBOOK_OWN)
    return fields


class FieldMapTests(unittest.TestCase):
    def test_every_category_and_mode(self):
        for code, category in cats.CATEGORIES.items():
            if category.group == "xxx":
                continue
            for original in (True, False):
                with self.subTest(code=code, original=original):
                    form = forms.build("tok", data(code, original))
                    self.assertEqual(set(form), expected_fields(code, original))
                    self.assertEqual(form["eredeti"], "igen" if original else "nem")
                    self.assertTrue(all(isinstance(v, str) for v in form.values()))

    def test_sd_films_have_no_menu_fields_but_hd_and_dvd_do(self):
        self.assertNotIn("film_felirat", forms.build("t", data("xvid_hun", False)))
        self.assertEqual(forms.build("t", data("hd", False, menu=False))["menu"], "nincs")
        self.assertEqual(forms.build("t", data("dvd9_hun", False))["menu"], "van")
        self.assertNotIn("menu", forms.build("t", data("hd", False, menu=None)))
        self.assertNotIn("film_felirat", forms.build("t", data("hdser", False)))

    def test_request_fields(self):
        form = forms.build("t", data("game_rip", False, request_id=" 12345 "))
        self.assertEqual((form["keresre"], form["keres_kodja"]), ("igen", "12345"))
        form = forms.build("t", data("game_rip", False))
        self.assertEqual((form["keresre"], form["keres_kodja"]), ("nem", ""))

    def test_infobar_values(self):
        bar = forms.Infobar(imdb_id="tt1053424", year="2010", countries=["United States of America", "England", "Canada"], runtime="111",
                            title_hu="Végrehajtók", title_en="Repo Men", title_other="Repo Men", directors=["Miguel Sapochnik"], cast=["Jude Law", "Forest Whitaker"])
        form = forms.build("t", data("hd_hun", True, infobar=bar))
        self.assertEqual(form["orszag"], "USA, UK, Canada")
        self.assertEqual((form["megjelent"], form["hossz"], form["szereplok"]), ("2010", "111", "Jude Law, Forest Whitaker"))


class StyleTagTests(unittest.TestCase):
    def test_words_joined_with_dots(self):
        self.assertEqual(forms.style_tags(["Hip Hop", "rock", "Pop/Rock", "Drum & Bass", "rock"]), "hip.hop, rock, pop.rock, drum.and.bass")

    def test_the_pages_own_vocabulary_is_kept(self):
        self.assertEqual(forms.style_tags(["sci-fi", "Sci Fi", "életmód.egészség", "szórakoztató irodalom"]), "sci-fi, életmód.egészség, szórakoztató.irodalom")

    def test_comma_separated_input_and_junk(self):
        self.assertEqual(forms.style_tags(["punk rock, ska", " ", "!!"]), "punk.rock, ska")


class MissingTests(unittest.TestCase):
    def test_season_must_fit_the_select(self):
        files = {"techinfo_fajl": 1, "kep1": 1, "kep2": 1, "kep3": 1}
        self.assertEqual(forms.missing(forms.build("t", data("hdser_hun", False, season=23)), files), [])
        problems = forms.missing(forms.build("t", data("hdser_hun", False, season=24)), files)
        self.assertEqual(len(problems), 1)
        self.assertIn("0–23", problems[0])

    def test_request_without_code(self):
        form = forms.build("t", data("misc", False))
        form["keresre"] = "igen"
        self.assertIn("kérés teljesítése: hiányzik a kérés kódja", forms.missing(form, {"kep1": 1, "kep2": 1, "kep3": 1}))

    def test_tracklist_forms(self):
        for text in ("Tracklista:\nA1. Intro", "[b]Fejezetek:[/b]\n1-01. Első", "1. Intro (3:21)\n2. Dal", "01) Intro", "1-01. Intro", "A1 Intro"):
            with self.subTest(text=text):
                self.assertTrue(forms.has_tracklist(text))
        for text in ("Előadó: X\nMegjelenés éve: 2001", "Méret: 1.5 GB", "10. Tizedik"):
            with self.subTest(text=text):
                self.assertFalse(forms.has_tracklist(text))

    def test_screenshots_by_category(self):
        self.assertFalse(forms.needs_screenshots("mp3", False))
        self.assertTrue(forms.needs_screenshots("ebook", False))
        self.assertTrue(forms.needs_screenshots("clip", False))
        self.assertFalse(forms.needs_screenshots("clip", True))
        self.assertFalse(forms.needs_screenshots("game_iso", True))
        self.assertTrue(forms.needs_screenshots("hd", True, has_sample=False))
        self.assertFalse(forms.needs_screenshots("hd", True, has_sample=True))

    def test_techinfo_by_category(self):
        self.assertTrue(forms.needs_techinfo("clip", False))
        self.assertTrue(forms.needs_techinfo("lossless_hun", False))
        self.assertFalse(forms.needs_techinfo("ebook", False))
        self.assertFalse(forms.needs_techinfo("hd", True))
        self.assertFalse(forms.needs_techinfo("game_rip", False))

    def test_bad_imdb_id(self):
        form = forms.build("t", data("hd", True, infobar=forms.Infobar(imdb_id="https://imdb.com/title/tt1", title_en="X")))
        self.assertTrue(any("hibás IMDb" in p for p in forms.missing(form, {"nfo_fajl": 1}, has_sample=True)))

    def test_own_clip_needs_artist_album_techinfo_and_images(self):
        problems = forms.missing(forms.build("t", data("clip", False, artist="", album="")), {})
        self.assertIn("zene: előadó és album kell", problems)
        self.assertIn("techinfo fájl kell (MediaInfo / BDInfo)", problems)
        self.assertIn("3 kép kell", problems)
        self.assertNotIn("zene: a tracklistát a leírásba kötelező beírni", problems)  # the page asks it for mp3/lossless only


if __name__ == "__main__":
    unittest.main()
