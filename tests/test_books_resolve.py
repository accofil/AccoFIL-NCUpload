from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ncore_up import keys, net, translate
from ncore_up import release as rel
from ncore_up.resolve import books
from ncore_up.sources import moly

sys.path.insert(0, str(Path(__file__).parent))
import test_books_samples as samples  # noqa: E402

FIX = Path(__file__).parent / "fixtures" / "books"
HAS_BS4 = importlib.util.find_spec("bs4") is not None


def fixture(name: str):
    text = (FIX / name).read_text(encoding="utf-8")
    return json.loads(text) if name.endswith(".json") else text


def inspect(path: Path) -> rel.Release:
    with mock.patch.object(rel, "hungarian_audio", return_value=None):
        return rel.inspect(path)


class NameTests(unittest.TestCase):
    def test_book_names(self):
        info = books.parse_name("Szabó Magda - Az ajtó (Szabó Magda-életmű 12.) [Jaffa, 2016]")
        self.assertEqual((info["author"], info["title"], info["series"], info["index"], info["publisher"], info["year"]),
                         ("Szabó Magda", "Az ajtó", "Szabó Magda-életmű", "12", "Jaffa", "2016"))
        info = books.parse_name("Martha.Wells.-.Network.Effect.2020.RETAIL.EPUB.eBook-GRP")
        self.assertEqual((info["author"], info["title"], info["year"]), ("Martha Wells", "Network Effect", "2020"))
        info = books.parse_name("J.K.Rowling.-.Harry.Potter.es.a.bolcsek.kove.HUN.EPUB.eBook-GRP")
        self.assertEqual(info["author"], "J. K. Rowling")
        self.assertEqual(books.parse_name("Az ajtó (z-library)")["title"], "Az ajtó")

    def test_comic_names(self):
        info = books.parse_name("Saga 001 (2012) (Digital) (Minutemen-Excelsior)")
        self.assertEqual((info["title"], info["issue"], info["year"]), ("Saga", "1", "2012"))
        self.assertEqual(books.parse_name("Nichijou v01")["volume"], "1")
        self.assertEqual(books.parse_name("Death Note 3. kötet")["volume"], "3")
        self.assertEqual(books.parse_name("George Orwell - 1984")["title"], "1984")

    def test_magazines(self):
        self.assertEqual(books.parse_magazine("Nők Lapja 2024-05-12"), {"name": "Nők Lapja", "year": "2024", "month": "5", "day": "12", "issue": ""})
        self.assertEqual(books.parse_magazine("HVG 2024 - 12. szám")["issue"], "12")
        self.assertEqual(books.parse_magazine("Chip 2024.05")["month"], "5")
        self.assertEqual(books.parse_magazine("PC Guru 2023. december")["month"], "12")
        self.assertEqual(books.parse_magazine("National_Geographic_05-2024")["issue"], "5")
        self.assertEqual(books.parse_magazine("Szabó Magda - Az ajtó 2016"), {})
        self.assertEqual(books.parse_magazine("George Orwell - 1984"), {})
        self.assertEqual(books.parse_magazine("Martha.Wells.-.Network.Effect.2020.RETAIL.EPUB.eBook-GRP"), {})
        self.assertEqual(books.magazine_name({"name": "HVG", "year": "2024", "issue": "5"}), "HVG 2024 - 05.")
        self.assertEqual(books.magazine_name({"name": "Nők Lapja", "year": "2024", "month": "5", "day": "12"}), "Nők Lapja 2024.05.12.")
        self.assertEqual(books.magazine_name({"name": "National Geographic", "year": "2024", "month": "5"}, "HU"), "National Geographic HU 2024.05.")

    def test_authors(self):
        self.assertEqual(books.book_name(["A", "B", "C"], "Cím"), "A, B, C - Cím")
        self.assertEqual(books.book_name(["A", "B", "C", "D"], "Cím"), "Various Authors - Cím")
        self.assertEqual(books.book_name([], "Cím"), "Cím")


class StyleTests(unittest.TestCase):
    def test_moly_tags(self):
        self.assertEqual(books.map_styles(genre_tags=["regény"], theme_tags=["vallás", "klasszikus", "kutya"]), ["szépirodalom"])
        self.assertEqual(books.map_styles(genre_tags=["horror", "regény", "thriller"], theme_tags=["pszichothriller", "zene"]), ["szórakoztató.irodalom"])
        self.assertEqual(books.map_styles(genre_tags=["regény"], theme_tags=["fantasy", "ifjúsági"]), ["fantasy", "gyermek"])

    def test_categories(self):
        self.assertEqual(books.map_styles(topic_tags=["Fiction / Science Fiction / Space Opera"]), ["sci-fi"])
        self.assertEqual(books.map_styles(topic_tags=["Fiction / Historical"]), ["szépirodalom"])
        self.assertEqual(books.map_styles(topic_tags=["History / Europe / Hungary"]), ["történelem"])
        self.assertEqual(books.map_styles(topic_tags=["Computers / Programming / Python"]), ["számítástechnika"])
        self.assertEqual(books.map_styles(topic_tags=["Cooking / Regional & Ethnic / Hungarian"]), ["gasztronómia"])
        self.assertTrue(set(books.map_styles(genre_tags=["Comedy", "Romance", "Sci-Fi"])) <= set(books.EBOOK_STYLES))


class MagazineConfigTests(unittest.TestCase):
    def test_stub_then_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings, path, created = books.magazine_config("Nők Lapja", "magyar", Path(tmp))
            self.assertTrue(created and path.name == "nok-lapja.yaml" and settings == {})
            self.assertIn('name: "Nők Lapja"', path.read_text(encoding="utf-8"))
            path.write_text('name: "Nők Lapja"\npublisher: "Central Médiacsoport"  # kiadó\nmultiple_editions: false\nissn: "0029-0874"\n', encoding="utf-8")
            settings, _, created = books.magazine_config("Nők Lapja", "magyar", Path(tmp))
            self.assertFalse(created)
            self.assertEqual((settings["publisher"], settings["issn"], settings["multiple_editions"]), ("Central Médiacsoport", "0029-0874", False))


class ResolveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        keys.load({})
        # hermetic: no sitemap index, and any request a test does not mock fails loudly
        patches = [mock.patch.object(moly, "ensure_index", return_value=None),
                   mock.patch.object(net, "request", side_effect=AssertionError("unexpected network call"))]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def tearDown(self):
        self.tmp.cleanup()

    @unittest.skipUnless(HAS_BS4, "beautifulsoup4 not installed")
    def test_hungarian_epub_with_moly(self):
        folder = self.dir / "Szabo.Magda.-.Az.ajto.2025.HUN.EPUB.eBook-GRP"
        folder.mkdir()
        samples.make_epub(folder / "Szabo Magda - Az ajto.epub", description="<p>Rövid.</p>")
        page = fixture("moly_az_ajto.html")

        def get(url, **kwargs):
            if url == "https://moly.hu/konyvek/szabo-magda-az-ajto":
                return mock.Mock(url=url, text=page)
            return mock.Mock(url="https://moly.hu/", text="<html></html>")

        with mock.patch.object(moly, "ensure_index", return_value=None), mock.patch.object(net, "get", side_effect=get), \
                mock.patch.object(net, "get_json", return_value=None), mock.patch.object(translate, "to_hungarian", side_effect=AssertionError):
            meta = books.resolve(inspect(folder), "ebook_hun", original=False, hints={})
        self.assertEqual((meta.kind, meta.title, meta.creators), ("book", "Az ajtó", ["Szabó Magda"]))
        self.assertEqual(meta.extra["torrent_name"], "Szabó Magda - Az ajtó")
        self.assertEqual((meta.description_source, meta.machine_translated), ("moly.hu", False))
        self.assertTrue(meta.description_hu.startswith("Álmában nem"))
        self.assertEqual(meta.proof_links[0], "https://moly.hu/konyvek/szabo-magda-az-ajto")
        self.assertEqual((meta.identifiers["isbn"], meta.confidence), ("9786155715433", 0.95))
        self.assertEqual((meta.publisher, meta.year, meta.extra["pages"]), ("Jaffa Kiadó", "2025", "282"))
        self.assertEqual((meta.extra["format"], meta.languages, meta.tags), ("epub", ["magyar"], ["szépirodalom"]))
        self.assertTrue(meta.cover.startswith(b"\x89PNG"))
        self.assertEqual(meta.title_original, "")
        self.assertIn("moly.hu", meta.sources)

    @unittest.skipUnless(HAS_BS4, "beautifulsoup4 not installed")
    def test_english_blurb_on_moly_is_not_hungarian(self):
        page = fixture("moly_az_ajto.html")
        start = page.index('<div class="text shrinkable">')
        end = page.index("</div>", start)
        page = page[:start] + '<div class="text shrinkable"><p>Following the surreal adventures of the young inhabitants of a city, this book is a hit.</p>' + page[end:]
        path = samples.make_epub(self.dir / "Szabo Magda - Az ajto.epub", description="")
        with mock.patch.object(net, "get", side_effect=lambda url, **kw: mock.Mock(url=url, text=page)), \
                mock.patch.object(net, "get_json", return_value=None), mock.patch.object(translate, "to_hungarian", side_effect=lambda t: (t, False)):
            meta = books.resolve(inspect(path), "ebook_hun", original=False, hints={})
        self.assertEqual(meta.description_hu, "")
        self.assertTrue(meta.description_en.startswith("Following the surreal"))
        self.assertEqual(meta.description_source, "moly.hu")

    def test_english_epub_google_and_open_library(self):
        path = samples.make_epub(self.dir / "Network Effect.epub", title="Network Effect: A Murderbot Novel", author="Martha Wells",
                                 language="en", isbn="9781250229861", description="<p>Short.</p>")
        responses = {
            googlebooks_url(): fixture("googlebooks_search.json"),
            "https://openlibrary.org/api/books": fixture("openlibrary_api_books.json"),
            "https://openlibrary.org/isbn/9781250229861.json": fixture("openlibrary_isbn.json"),
            "https://openlibrary.org/works/OL20735675W.json": fixture("openlibrary_work.json"),
        }
        with mock.patch.dict(keys._VALUES, {"GOOGLE_BOOKS_KEY": "k"}), mock.patch.object(net, "get_json", side_effect=lambda url, **kw: responses.get(url)), \
                mock.patch.object(net, "get", side_effect=AssertionError("moly must not be asked")), \
                mock.patch.object(translate, "to_hungarian", return_value=("A Gyilkosbot visszatér.", True)):
            meta = books.resolve(inspect(path), "ebook", original=False, hints={})
        self.assertEqual((meta.title, meta.subtitle, meta.creators), ("Network Effect", "A Murderbot Novel", ["Martha Wells"]))
        self.assertEqual(meta.extra["torrent_name"], "Martha Wells - Network Effect")
        self.assertEqual((meta.description_hu, meta.machine_translated, meta.description_source), ("A Gyilkosbot visszatér.", True, "Google Books"))
        self.assertTrue(meta.description_en.startswith("Murderbot returns"))
        self.assertEqual(meta.proof_links, ["https://books.google.com/books?id=XDdJEAAAQBAJ", "https://openlibrary.org/works/OL20735675W"])
        self.assertEqual(meta.extra["series"], "The Murderbot Diaries #5")
        self.assertEqual((meta.languages, meta.tags[0], meta.confidence), (["angol"], "sci-fi", 0.9))
        self.assertEqual(meta.extra["pages"], "352")

    def test_manga_cbz_with_anilist(self):
        path = samples.make_cbz(self.dir / "Nichijou v01.cbz")
        with mock.patch.object(net, "post_json", return_value=fixture("anilist_nichijou.json")), \
                mock.patch.object(net, "get_json", return_value=None), mock.patch.object(translate, "to_hungarian", side_effect=lambda t: (t, False)):
            meta = books.resolve(inspect(path), "ebook", original=False, hints={})
        self.assertEqual((meta.kind, meta.title, meta.creators), ("manga", "Nichijou", ["Keiichi Arawi"]))
        self.assertEqual(meta.extra["torrent_name"], "Keiichi Arawi - Nichijou Vol. 1")
        self.assertEqual(meta.tags[0], "manga.képregény")
        self.assertIn("https://anilist.co/manga/33082", meta.proof_links)
        self.assertEqual((meta.publisher, meta.year, meta.extra["format"], meta.extra["pages"]), ("Vertical", "2016", "cbz", "24"))
        self.assertTrue(meta.description_en.startswith("Nichijou primarily focuses"))
        self.assertIn("vígjáték", meta.genres)
        self.assertTrue(any("angol szöveg" in w for w in meta.warnings))

    def test_magazine_pdf_creates_stub(self):
        folder = self.dir / "HVG 2024 - 12"
        folder.mkdir()
        samples.make_pdf(folder / "HVG 2024 - 12.pdf", [{"lines": [f"HVG {n}. oldal", samples.LOREM]} for n in range(1, 13)])
        magazines = self.dir / "magazines"
        meta = books.resolve(inspect(folder), "ebook_hun", original=False, hints={"magazine_dir": magazines})
        self.assertEqual((meta.kind, meta.title, meta.extra["torrent_name"]), ("magazine", "HVG", "HVG 2024 - 12."))
        self.assertTrue((magazines / "hvg.yaml").exists())
        self.assertTrue(any("új magazin" in w for w in meta.warnings))
        self.assertEqual(meta.tags, ["magazin"])
        self.assertTrue(meta.extra["format"].startswith("pdf - ("))
        (magazines / "hvg.yaml").write_text('name: "HVG"\npublisher: "HVG Kiadó Zrt."\nissn: "1217-9647"\ncountry: "HU"\nmultiple_editions: true\n', encoding="utf-8")
        meta = books.resolve(inspect(folder), "ebook_hun", original=False, hints={"magazine_dir": magazines})
        self.assertEqual((meta.publisher, meta.extra["torrent_name"]), ("HVG Kiadó Zrt.", "HVG HU 2024 - 12."))
        self.assertEqual(meta.proof_links, ["https://portal.issn.org/resource/ISSN/1217-9647"])

    def test_empty_release(self):
        folder = self.dir / "Empty"
        folder.mkdir()
        (folder / "readme.txt").write_text("x")
        meta = books.resolve(inspect(folder), "ebook_hun", original=False, hints={})
        self.assertTrue(meta.warnings and meta.confidence == 0)

    def test_sources_down_never_raise(self):
        path = samples.make_epub(self.dir / "Szabo Magda - Az ajto.epub")
        with mock.patch.object(moly, "ensure_index", return_value=None), mock.patch.object(net, "get", return_value=None), \
                mock.patch.object(net, "get_json", return_value=None), mock.patch.object(net, "post_json", return_value=None):
            meta = books.resolve(inspect(path), "ebook_hun", original=False, hints={"tags": "szépirodalom"})
        self.assertEqual((meta.title, meta.extra["torrent_name"], meta.tags), ("Az ajtó", "Szabó Magda - Az ajtó", ["szépirodalom"]))
        self.assertTrue(any("igazoló link" in w for w in meta.warnings))
        self.assertTrue(any("Google Books" in w for w in meta.warnings))
        from ncore_up.sources import googlebooks

        with mock.patch.dict(keys._VALUES, {"GOOGLE_BOOKS_KEY": "k"}), mock.patch.object(moly, "find", side_effect=KeyError("x")), \
                mock.patch.object(googlebooks, "by_isbn", side_effect=TypeError), mock.patch.object(googlebooks, "find", return_value=[]), \
                mock.patch.object(net, "get_json", return_value=None):
            meta = books.resolve(inspect(path), "ebook_hun", original=False, hints={})
        self.assertEqual(meta.extra["torrent_name"], "Szabó Magda - Az ajtó")
        self.assertTrue(any(w.startswith("moly.hu: váratlan válasz") for w in meta.warnings))
        self.assertTrue(any(w.startswith("Google Books: váratlan válasz") for w in meta.warnings))


def googlebooks_url() -> str:
    from ncore_up.sources import googlebooks

    return googlebooks.API


if __name__ == "__main__":
    unittest.main()
