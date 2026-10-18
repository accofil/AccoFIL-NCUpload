from __future__ import annotations

import gzip
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ncore_up import keys, net
from ncore_up.sources import anilist, comicvine, googlebooks, mangaupdates, moly, openlibrary

FIX = Path(__file__).parent / "fixtures" / "books"
HAS_BS4 = importlib.util.find_spec("bs4") is not None


def fixture(name: str):
    text = (FIX / name).read_text(encoding="utf-8")
    return json.loads(text) if name.endswith(".json") else text


class GoogleBooksTests(unittest.TestCase):
    def test_parse(self):
        items = googlebooks.parse_search(fixture("googlebooks_search.json"))
        first = items[0]
        self.assertEqual((first["title"], first["subtitle"], first["authors"]), ("Network Effect", "A Murderbot Novel", ["Martha Wells"]))
        self.assertEqual((first["year"], first["pages"], first["language"], first["isbn13"]), ("2020", 352, "en", "9781250229861"))
        self.assertEqual(first["description"].split("\n\n")[0], "Murderbot returns in its highly-anticipated, first, full-length standalone novel.")
        self.assertIn("Congratulations", first["description"])
        self.assertNotIn("<", first["description"])
        self.assertTrue(first["cover_url"].startswith("https://") and "edge=curl" not in first["cover_url"])
        self.assertEqual(first["url"], "https://books.google.com/books?id=XDdJEAAAQBAJ")
        self.assertEqual(items[1]["language"], "hu")

    def test_no_key_no_request(self):
        keys.load({})
        with mock.patch.object(net, "get_json") as get:
            self.assertEqual(googlebooks.by_isbn("9781250229861"), {})
            self.assertEqual(googlebooks.find("Az ajtó", "Szabó", lang="hu"), [])
        get.assert_not_called()

    def test_queries_with_key(self):
        with mock.patch.dict(keys._VALUES, {"GOOGLE_BOOKS_KEY": "k"}), \
                mock.patch.object(net, "get_json", return_value=fixture("googlebooks_search.json")) as get:
            self.assertEqual(googlebooks.by_isbn("978-1-250-22986-1")["id"], "XDdJEAAAQBAJ")
            self.assertEqual(get.call_args.kwargs["params"]["q"], "isbn:9781250229861")
            googlebooks.find("Az ajtó", "Szabó Magda", lang="hu")
            params = get.call_args.kwargs["params"]
            self.assertEqual((params["q"], params["langRestrict"], params["key"]), ('intitle:"Az ajtó" inauthor:"Szabó Magda"', "hu", "k"))
            self.assertEqual(params["printType"], "BOOKS")
            self.assertEqual(get.call_args.args[0], "https://books.googleapis.com/books/v1/volumes")

    def test_empty_operator_query_falls_back_with_language_filter(self):
        with mock.patch.object(googlebooks, "search", side_effect=[[], [{"title": "Az ajtó"}]]) as search:
            self.assertEqual(googlebooks.find("Az ajtó", "Szabó Magda", lang="hu", max_results=5), [{"title": "Az ajtó"}])
        self.assertEqual(search.call_args_list, [
            mock.call('intitle:"Az ajtó" inauthor:"Szabó Magda"', lang="hu", max_results=5),
            mock.call("Az ajtó Szabó Magda", lang="hu", max_results=5)])

    def test_successful_operator_search_does_not_make_a_second_request(self):
        with mock.patch.object(googlebooks, "search", return_value=[{"title": "Az ajtó"}]) as search:
            self.assertTrue(googlebooks.find("Az ajtó"))
        search.assert_called_once()

    def test_plain_isbn_fallback_still_requires_an_exact_identifier(self):
        good = {"isbn13": "9781250229861", "isbn10": "1250229863", "title": "Network Effect"}
        unrelated = {"isbn13": "9781000826289", "isbn10": "1000826287", "title": "A book citing this ISBN"}
        with mock.patch.object(googlebooks, "search", side_effect=[[], [unrelated, good]]) as search:
            self.assertEqual(googlebooks.by_isbn("978-1-250-22986-1"), good)
        self.assertEqual(search.call_args_list, [mock.call("isbn:9781250229861", max_results=5), mock.call("9781250229861", max_results=5)])
        with mock.patch.object(googlebooks, "search", side_effect=[[], [unrelated]]):
            self.assertEqual(googlebooks.by_isbn("9781250229861"), {})

    def test_blank_title_and_invalid_isbn_do_not_search(self):
        with mock.patch.object(googlebooks, "search") as search:
            self.assertEqual(googlebooks.find("   "), [])
            self.assertEqual(googlebooks.by_isbn("123"), {})
        search.assert_not_called()


class OpenLibraryTests(unittest.TestCase):
    def test_parsers(self):
        data = openlibrary.parse_edition_data(fixture("openlibrary_api_books.json"), "9781250229861")
        self.assertEqual((data["title"], data["authors"], data["publishers"], data["year"], data["pages"]),
                         ("Network Effect", ["Martha Wells"], ["Tor"], "2020", 350))
        self.assertTrue(data["cover_url"].endswith("-L.jpg"))
        edition = openlibrary.parse_edition(fixture("openlibrary_isbn.json"))
        self.assertEqual((edition["work"], edition["languages"], edition["series"]), ("/works/OL20735675W", ["eng"], ["The Murderbot Diaries #5"]))
        self.assertTrue(edition["description"].startswith("Murderbot returns") and edition["description"].endswith("Drastic action it is, then"))
        work = openlibrary.parse_work(fixture("openlibrary_work.json"))
        self.assertTrue(work["description"].startswith("Murderbot returns in its highly-anticipated"))
        self.assertNotIn("**", work["description"])
        self.assertEqual(work["url"], "https://openlibrary.org/works/OL20735675W")
        docs = openlibrary.parse_search(fixture("openlibrary_search.json"))
        self.assertEqual((docs[0]["title"], docs[0]["authors"], docs[0]["publishers"]), ("Az ajtó", ["Magda Szabó"], ["Magvető"]))
        self.assertEqual(docs[0]["isbns"], ["9789631411546"])

    def test_nfd_text_is_normalized(self):
        docs = openlibrary.parse_search({"docs": [{"key": "/works/OL1W", "title": "Az ajtó", "publisher": ["Magvető"]}]})
        self.assertEqual((docs[0]["title"], docs[0]["publishers"]), ("Az ajtó", ["Magvető"]))

    def test_by_isbn_combines_edition_and_work(self):
        responses = {
            "https://openlibrary.org/api/books": fixture("openlibrary_api_books.json"),
            "https://openlibrary.org/isbn/9781250229861.json": fixture("openlibrary_isbn.json"),
            "https://openlibrary.org/works/OL20735675W.json": fixture("openlibrary_work.json"),
        }
        with mock.patch.object(net, "get_json", side_effect=lambda url, **kw: responses.get(url)):
            data = openlibrary.by_isbn("9781250229861")
        self.assertEqual(data["url"], "https://openlibrary.org/works/OL20735675W")
        self.assertIn("Murderbot", data["description"])
        self.assertEqual(data["languages"], ["eng"])
        with mock.patch.object(net, "get_json", return_value=None):
            self.assertEqual(openlibrary.by_isbn("9781250229861"), {})


class MangaTests(unittest.TestCase):
    def test_anilist(self):
        hits = anilist.parse_search(fixture("anilist_nichijou.json"))
        first = hits[0]
        self.assertEqual((first["title_romaji"], first["title_native"], first["format"], first["year"]), ("Nichijou", "日常", "MANGA", "2006"))
        self.assertEqual(first["authors"], ["Keiichi Arawi"])  # the English translator is not an author
        self.assertEqual(first["genres"], ["Comedy", "Slice of Life"])
        self.assertTrue(first["description"].startswith("Nichijou primarily focuses"))
        self.assertNotIn("<", first["description"])
        self.assertEqual(first["url"], "https://anilist.co/manga/33082")
        self.assertIn("Everyday", anilist.titles(first))
        self.assertEqual(anilist.clean_description("Text.<br><br>(Source: Yen Press)"), "Text.")

    def test_anilist_request(self):
        with mock.patch.object(net, "post_json", return_value=fixture("anilist_nichijou.json")) as post:
            self.assertEqual(anilist.search("Nichijou")[0]["id"], 33082)
        self.assertEqual(post.call_args.kwargs["json_body"]["variables"], {"search": "Nichijou", "type": "MANGA"})
        with mock.patch.object(net, "post_json", return_value=None):
            self.assertEqual(anilist.search("Nichijou"), [])

    def test_mangaupdates(self):
        hits = mangaupdates.parse_search(fixture("mangaupdates_search.json"))
        self.assertEqual((hits[0]["id"], hits[0]["title"], hits[0]["year"]), (66804182011, "Nichijou", "2007"))
        self.assertIn("Slice of Life", hits[0]["genres"])
        series = mangaupdates.parse_record(fixture("mangaupdates_series.json"))
        self.assertEqual((series["authors"], series["artists"]), (["ARAWI Keiichi"], ["ARAWI Keiichi"]))
        self.assertEqual((series["publisher_original"], series["publisher_english"], series["volumes"]), ("Kadokawa Shoten", "Vertical", 12))
        self.assertTrue(series["url"].startswith("https://www.mangaupdates.com/series/"))


class ComicVineTests(unittest.TestCase):
    def test_parse(self):
        data = fixture("comicvine.json")
        volume = comicvine.parse_volume(data["search"]["results"][0])
        self.assertEqual((volume["name"], volume["year"], volume["publisher"], volume["issues"]), ("Saga", "2012", "Image", 72))
        self.assertEqual(volume["description"], "Saga is the story of Alana and Marko, lovers from long-warring extraterrestrial races.")
        issue = comicvine.parse_issue(data["issues"]["results"][0])
        self.assertEqual((issue["number"], issue["date"], issue["volume"]), ("1", "2012-03-31", "Saga"))
        self.assertEqual((comicvine.writers(issue), comicvine.artists(issue)), (["Brian K. Vaughan"], ["Fiona Staples"]))

    def test_without_key(self):
        keys.load({})
        with mock.patch.object(net, "get_json") as get:
            self.assertEqual(comicvine.search_volumes("Saga"), [])
            self.assertEqual(comicvine.issue(43489, "1"), {})
        get.assert_not_called()

    def test_with_key(self):
        data = fixture("comicvine.json")
        with mock.patch.dict(keys._VALUES, {"COMICVINE_KEY": "k"}), mock.patch.object(net, "get_json", side_effect=[data["search"], data["issues"]]) as get:
            self.assertEqual(comicvine.search_volumes("Saga")[0]["id"], 43489)
            self.assertEqual(comicvine.issue(43489, "1")["name"], "Chapter One")
        self.assertEqual(get.call_args.kwargs["params"]["filter"], "volume:43489,issue_number:1")


SITEMAP = """<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://moly.hu</loc></url>
<url><loc>https://moly.hu/konyvek/szabo-magda-az-ajto</loc><lastmod>2026-10-16T06:46:40+02:00</lastmod></url>
<url><loc>https://moly.hu/konyvek/szabo-magda-az-ajto-2</loc></url>
<url><loc>https://moly.hu/konyvek/szabo-magda-abigel</loc></url>
<url><loc>https://moly.hu/konyvek/stephen-king-a-ragyogas</loc></url>
<url><loc>https://moly.hu/konyvek/j-k-rowling-harry-potter-es-a-bolcsek-kove</loc></url>
<url><loc>https://moly.hu/konyvek/frank-herbert-a-dune</loc></url>
<url><loc>https://moly.hu/konyvek/cugumi-oba-death-note-1</loc></url>
<url><loc>https://moly.hu/konyvek/tsugumi-ohba-death-note-1-olasz</loc></url>
""" + "".join(f"<url><loc>https://moly.hu/konyvek/filler-author-book-{n}</loc></url>\n" for n in range(3000)) + "</urlset>"
AUTHORS = '<urlset><url><loc>https://moly.hu/alkotok/szabo-magda</loc></url></urlset>'
INDEX = ('<sitemapindex><sitemap><loc>https://moly.hu/sitemap1.xml.gz</loc></sitemap><sitemap><loc>https://moly.hu/sitemap2.xml.gz</loc>'
         '</sitemap><sitemap><loc>https://moly.hu/sitemap3.xml.gz</loc></sitemap></sitemapindex>')


class MolySlugTests(unittest.TestCase):
    def test_slugify(self):
        self.assertEqual(moly.slugify("Szabó Magda Az ​ajtó"), "szabo-magda-az-ajto")
        self.assertEqual(moly.slugify("J. K. Rowling Harry Potter and the Philosopher's Stone"), "j-k-rowling-harry-potter-and-the-philosophers-stone")
        self.assertEqual(moly.slugify("Molnár Ferenc: A Pál utcai fiúk"), "molnar-ferenc-a-pal-utcai-fiuk")
        self.assertEqual(moly.slugify("Kiss Imre (szerk.) – Meteorológiai bibliográfia"), "kiss-imre-szerk-meteorologiai-bibliografia")

    def test_candidates(self):
        slugs = moly.candidates("Az ajtó: regény", ["Magda Szabó"])
        self.assertEqual(slugs[:4], ["magda-szabo-az-ajto-regeny", "magda-szabo-az-ajto", "szabo-magda-az-ajto-regeny", "szabo-magda-az-ajto"])
        self.assertIn("rejto-jeno-p-howard-csontbrigad", moly.candidates("Csontbrigád", ["Rejtő Jenő", "P. Howard"]))

    def test_price_prefix(self):
        self.assertEqual(moly.strip_price_prefix("Árak és vélemények egy helyen! · Legolcsóbb ár: 5024 Ft · A ragyogás pompás"), "A ragyogás pompás")


class MolyIndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "moly.sqlite"

    def tearDown(self):
        self.tmp.cleanup()

    def test_sitemap_slugs_stream(self):
        slugs = list(moly.sitemap_slugs(gzip.compress(SITEMAP.encode())))
        self.assertEqual(slugs[:2], ["szabo-magda-az-ajto", "szabo-magda-az-ajto-2"])
        self.assertEqual(len(slugs), 3008)
        self.assertEqual(list(moly.sitemap_slugs(SITEMAP.encode()))[-1], "filler-author-book-2999")
        self.assertEqual(moly.parse_sitemap_index(INDEX)[0], "https://moly.hu/sitemap1.xml.gz")

    def build(self):
        pages = {moly.SITEMAP_INDEX: gzip.compress(INDEX.encode()), "https://moly.hu/sitemap1.xml.gz": gzip.compress(SITEMAP.encode()),
                 "https://moly.hu/sitemap2.xml.gz": gzip.compress(AUTHORS.encode())}
        calls = []

        def request(method, url, **kwargs):
            calls.append(url)
            return mock.Mock(content=pages[url], text="", url=url) if url in pages else None

        with mock.patch.object(net, "request", side_effect=request):
            count = moly.build_index(self.path)
        return count, calls

    def test_build_and_match(self):
        count, calls = self.build()
        self.assertEqual(count, 3008)
        self.assertNotIn("https://moly.hu/sitemap3.xml.gz", calls)  # stops after the book sitemaps
        self.assertLess(moly.index_age(self.path), 0.01)
        self.assertEqual(moly.known(["szabo-magda-az-ajto", "nope"], self.path), ["szabo-magda-az-ajto"])
        self.assertEqual(moly.index_matches("Az ajtó", ["Magda Szabó"], self.path), ["szabo-magda-az-ajto", "szabo-magda-az-ajto-2"])
        self.assertEqual(moly.index_matches("Abigél (regény)", ["Szabó Magda"], self.path)[0], "szabo-magda-abigel")
        self.assertEqual(moly.index_matches("Abigeel", ["Szabó Magda"], self.path), ["szabo-magda-abigel"])  # fuzzy title
        self.assertEqual(moly.index_matches("Valami más", ["Szabó Magda"], self.path), [])
        self.assertEqual(moly.index_matches("Dűne", ["Frank Herbert"], self.path), ["frank-herbert-a-dune"])  # article "A"
        self.assertEqual(moly.index_matches("Death Note 1.", ["Ohba Cugumi"], self.path), ["cugumi-oba-death-note-1"])  # name order
        self.assertEqual(moly.index_matches("Death Note 1.", [], self.path), ["cugumi-oba-death-note-1"])  # unique by title

    def test_failed_download_keeps_nothing(self):
        with mock.patch.object(net, "request", return_value=None):
            self.assertEqual(moly.build_index(self.path), 0)
        self.assertFalse(self.path.exists())
        self.assertIsNone(moly.index_age(self.path))

    def test_failed_build_is_not_retried_for_every_book(self):
        with mock.patch.object(moly, "index_path", return_value=self.path), mock.patch.object(net, "request", return_value=None) as request:
            self.assertIsNone(moly.ensure_index())
            self.assertIsNone(moly.ensure_index())
        self.assertEqual(request.call_count, 1)
        self.assertTrue(self.path.with_suffix(".failed").exists())


@unittest.skipUnless(HAS_BS4, "beautifulsoup4 not installed")
class MolyPageTests(unittest.TestCase):
    def test_parse_az_ajto(self):
        info = moly.parse_book(fixture("moly_az_ajto.html"))
        self.assertEqual((info["slug"], info["title"], info["authors"]), ("szabo-magda-az-ajto", "Az ajtó", ["Szabó Magda"]))
        self.assertEqual(info["url"], "https://moly.hu/konyvek/szabo-magda-az-ajto")
        self.assertTrue(info["description"].startswith("Álmában nem, de életében egyszer kinyílik az ajtó"))
        self.assertNotIn("Vigyázat", info["description"])
        self.assertIn("\nAz ajtó a ki- és bezártság", info["description"])
        self.assertIn("magyar szerző", info["tags"])
        self.assertEqual(info["genres"], ["regény"])
        self.assertEqual(info["original_year"], "1987")
        self.assertEqual(info["series"], "")
        self.assertIn("Szabó Magda-életmű", info["publisher_series"])
        self.assertTrue(info["cover_url"].startswith("https://assets.moly.hu/system/covers/big/"))
        first = info["editions"][0]
        self.assertEqual((first["publisher"], first["city"], first["year"], first["date"], first["pages"], first["isbn"], first["ebook"]),
                         ("Jaffa", "Budapest", "2025", "2025-02-19", 282, "9786155715433", True))
        self.assertEqual(info["editions"][1]["binding"], "puhatáblás")

    def test_parse_series_original_and_translator(self):
        info = moly.parse_book(fixture("moly_a_ragyogas.html"))
        self.assertEqual((info["title"], info["series"], info["series_index"]), ("A ragyogás", "A ragyogás", "1"))
        self.assertEqual((info["original_title"], info["original_year"]), ("The Shining", "1977"))
        self.assertIn("Prekop Gabriella", info["translators"])
        self.assertTrue(info["description"].startswith("A ragyogás pompás rémtörténet"))
        self.assertEqual(moly.parse_book("<html><head></head><body>Moly</body></html>"), {})

    def test_find_with_isbn_and_guessing(self):
        page = fixture("moly_az_ajto.html")

        def get(url, **kwargs):
            if url.endswith("/szabo-magda-az-ajto"):
                return mock.Mock(url=url, text=page)
            return mock.Mock(url="https://moly.hu/", text="<html>home</html>")  # unknown slugs redirect home

        with mock.patch.object(net, "get", side_effect=get) as fetch:
            info = moly.find("Az ajtó", ["Magda Szabó"], isbn="9789636870478", use_index=False)
        self.assertEqual((info["match"], info["confidence"]), ("isbn", 0.95))
        self.assertLessEqual(fetch.call_count, 2)
        score = moly.score(moly.parse_book(page), "Az ajtó", ["Szabó Magda"])
        self.assertEqual(score, ("title", 0.85))
        self.assertEqual(moly.score(moly.parse_book(page), "Ajtó", ["Szabó Magda"]), ("title", 0.85))  # article dropped
        self.assertEqual(moly.score(moly.parse_book(page), "Abigél", ["Szabó Magda"]), ("", 0.0))
        self.assertEqual(moly.score(moly.parse_book(page), "Az ajtó", ["Stephen King"]), ("", 0.0))


if __name__ == "__main__":
    unittest.main()
