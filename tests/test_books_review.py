"""Regression tests for the books review: short documents, odd archives and metadata, volume numbers, manga
detection of Western comics, edition-level data and loose hints."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
import zipfile
from contextlib import closing
from pathlib import Path
from unittest import mock

from ncore_up import keys, net, translate
from ncore_up import release as rel
from ncore_up.local import ebookmeta as em
from ncore_up.resolve import books
from ncore_up.sources import anilist, moly

sys.path.insert(0, str(Path(__file__).parent))
import test_books_samples as samples  # noqa: E402

FIX = Path(__file__).parent / "fixtures" / "books"


def inspect(path: Path) -> rel.Release:
    with mock.patch.object(rel, "hungarian_audio", return_value=None):
        return rel.inspect(path)


def staff(*pairs: tuple[str, str]) -> dict:
    return {"edges": [{"role": role, "node": {"name": {"full": name}}} for role, name in pairs]}


SPIDER_MAN = {"data": {"Page": {"media": [{
    "id": 31629, "title": {"romaji": "Spider-Man", "english": None, "native": "スパイダーマン"}, "synonyms": [], "format": "MANGA",
    "countryOfOrigin": "JP", "startDate": {"year": 1970}, "genres": ["Action"], "siteUrl": "https://anilist.co/manga/31629",
    "description": "A high school student named Yu Komori is bitten by a radioactive spider, which gave him spider-like powers.",
    "staff": staff(("Story & Art", "Ryouichi Ikegami"), ("Original Creator", "Stan Lee")),
}]}}}


class LocalReviewTests(unittest.TestCase):
    def test_short_documents_still_give_three_pages(self):
        for pages in range(3, 40):
            self.assertEqual(len(em.choose_pages(pages, lambda p: True)), 3, pages)
        self.assertTrue(all(2 <= p <= 8 for p in em.choose_pages(10, lambda p: True)))  # still from the body
        self.assertEqual(len(em.choose_pages(2, lambda p: True)), 2)

    def test_djvu_meta_with_a_broken_escape(self):
        meta = em.parse_djvu_meta('title "Az ajt\\303\\263"\nauthor "abc\\x"\n')
        self.assertEqual((meta["title"], meta["author"]), ("Az ajtó", "abc\\x"))

    def test_comic_names_with_backslashes_are_read_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.cbz"
            with zipfile.ZipFile(path, "w") as archive:  # some Windows tools store "dir\\page.png"
                for number in range(1, 9):
                    archive.writestr(f"Vol 1\\p{number:03d}.png", samples.png(20 + number, 30))
            entries = em.comic_entries(path)
            self.assertEqual(len(entries), 8)
            self.assertTrue(all(em.comic_member(path, name) for name in entries))
            self.assertEqual(len(em.proof_pages(path, Path(tmp) / "out")), 3)

    def test_comic_cover(self):
        with tempfile.TemporaryDirectory() as tmp:
            first, named = samples.png(11, 11), samples.png(12, 12)
            path = Path(tmp) / "a.cbz"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("Back Cover.png", samples.png(13, 13))
                archive.writestr("p000.png", first)  # the cover of many scans is page 000, page 001 is inside
                for number in range(1, 6):
                    archive.writestr(f"p{number:03d}.png", samples.png(20 + number, 20))
            self.assertEqual(em.cover(path), first)
            path = Path(tmp) / "b.cbz"
            with zipfile.ZipFile(path, "w") as archive:
                for number in range(1, 6):
                    archive.writestr(f"p{number:03d}.png", samples.png(20 + number, 20))
                archive.writestr("zz_cover.png", named)
            self.assertEqual(em.cover(path), named)

    def test_scan_pages_taken_unchecked_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)

            def render(path, page, stem, size=1400):
                target = Path(stem).with_suffix(".jpg")
                target.write_bytes(b"\xff\xd8 page %d" % page)
                return target

            warnings: list[str] = []
            with mock.patch.object(em, "page_count", return_value=30), mock.patch.object(em, "_ocr_chars", return_value=-1), \
                    mock.patch.object(em, "_render", side_effect=render):
                images = em._document_pages(out / "scan.pdf", out, 3, warnings, layer="SCAN")
            self.assertEqual(len(images), 3)
            self.assertTrue(any("nézd át az oldalképeket" in w for w in warnings))


class SourceReviewTests(unittest.TestCase):
    def test_anilist_authors_are_the_creators_only(self):
        media = {"id": 30021, "staff": staff(("Art", "Takeshi Obata"), ("Story", "Tsugumi Ooba"), ("Assistant", "Ryou Ogawa"),
                                             ("Touch-up Art & Lettering (English)", "Gia Cam Luc"),
                                             ("Translator (English: vols 4-7)", "Alexis Kirsch"),
                                             ("Story & Art (vols 41- )", "Studio Gaga"), ("Supervisor (vols 41- )", "Kouji Mori"),
                                             ("Original Creator", "Stan Lee"))}
        self.assertEqual(anilist.parse_media(media)["authors"], ["Takeshi Obata", "Tsugumi Ooba", "Studio Gaga", "Stan Lee"])

    def test_moly_volume_numbers_must_agree(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "moly.sqlite"
            with closing(sqlite3.connect(path)) as db:
                db.execute("CREATE TABLE slugs (slug TEXT PRIMARY KEY) WITHOUT ROWID")
                db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
                db.executemany("INSERT INTO slugs VALUES (?)", [(s,) for s in ("cugumi-oba-death-note-1", "cugumi-oba-death-note-11",
                                                                                 "cugumi-oba-death-note-12", "szabo-magda-abigel")])
                db.commit()
            self.assertEqual(moly.index_matches("Death Note 13.", ["Cugumi Oba"], path), [])
            self.assertEqual(moly.index_matches("Death Note 1.", ["Cugumi Oba"], path)[0], "cugumi-oba-death-note-1")
            self.assertEqual(moly.index_matches("Abigeel", ["Szabó Magda"], path), ["szabo-magda-abigel"])
        page = {"title": "Death Note 11.", "authors": ["Cugumi Oba"], "editions": []}
        self.assertEqual(moly.score(page, "Death Note 1.", ["Cugumi Oba"]), ("", 0.0))
        self.assertEqual(moly.score({**page, "title": "Death Note 1."}, "Death Note 01", ["Cugumi Oba"]), ("title", 0.85))

    def test_open_library_odd_records(self):
        from ncore_up.sources import openlibrary

        self.assertEqual(openlibrary.parse_edition_data({"ISBN:1": {"title": "x", "number_of_pages": "xii, 282"}}, "1")["pages"], 0)
        self.assertEqual(openlibrary.parse_search({"docs": [{"key": "/works/OL1W", "isbn": [9789631411546, "9789631411546"]}]})[0]["isbns"],
                         ["9789631411546"])


class ResolveReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        keys.load({})
        patches = [mock.patch.object(moly, "ensure_index", return_value=None),
                   mock.patch.object(net, "request", side_effect=AssertionError("unexpected network call")),
                   mock.patch.object(translate, "to_hungarian", side_effect=lambda text: (text, False))]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def test_western_comic_is_not_taken_for_a_manga(self):
        path = samples.make_cbz(self.dir / "Spider-Man 001 (2022) (Digital).cbz", comicinfo=None)
        with mock.patch.object(net, "post_json", return_value=SPIDER_MAN), mock.patch.object(net, "get_json", return_value=None):
            meta = books.resolve(inspect(path), "ebook", original=False, hints={})
        self.assertEqual((meta.kind, meta.extra["torrent_name"]), ("comic", "Spider-Man #1"))
        self.assertNotIn("anilist", meta.identifiers)
        self.assertEqual(meta.creators, [])
        # ComicInfo writers that AniList does not know: still not that manga
        info = samples.COMICINFO.replace("Nichijou", "Spider-Man").replace("Keiichi Arawi", "Zeb Wells").replace(
            "<Manga>YesAndRightToLeft</Manga>", "").replace("<Year>2016</Year>", "<Year>2022</Year>")
        path = samples.make_cbz(self.dir / "Spider-Man 002.cbz", comicinfo=info)
        with mock.patch.object(net, "post_json", return_value=SPIDER_MAN), mock.patch.object(net, "get_json", return_value=None):
            meta = books.resolve(inspect(path), "ebook", original=False, hints={})
        self.assertEqual((meta.kind, meta.creators), ("comic", ["Zeb Wells"]))

    def test_volume_named_cbz_without_comicinfo_is_a_manga(self):
        path = samples.make_cbz(self.dir / "Nichijou v01.cbz", comicinfo=None)
        data = (FIX / "anilist_nichijou.json").read_text(encoding="utf-8")
        import json

        with mock.patch.object(net, "post_json", return_value=json.loads(data)), mock.patch.object(net, "get_json", return_value=None):
            meta = books.resolve(inspect(path), "ebook", original=False, hints={})
        self.assertEqual((meta.kind, meta.creators, meta.extra["torrent_name"]), ("manga", ["Keiichi Arawi"], "Keiichi Arawi - Nichijou Vol. 1"))

    def test_open_library_work_hit_gives_no_edition_data(self):
        path = samples.make_epub(self.dir / "Szabo Magda - Az ajto.epub", isbn="", publisher="", date="", description="")
        search = (FIX / "openlibrary_search.json").read_text(encoding="utf-8")
        import json

        def get_json(url, **kwargs):
            return json.loads(search) if url.endswith("/search.json") else None

        home = mock.Mock(url="https://moly.hu/", text="<html></html>")  # moly does not know the book
        with mock.patch.object(net, "get", return_value=home), mock.patch.object(net, "get_json", side_effect=get_json):
            meta = books.resolve(inspect(path), "ebook_hun", original=False, hints={})
        self.assertIn("Open Library", meta.sources)
        self.assertEqual((meta.publisher, meta.year, meta.cover_url), ("", "", ""))  # Magvető 1987 is another edition
        self.assertNotIn("pages", meta.extra)
        self.assertIn("https://openlibrary.org/works/OL940070W", meta.proof_links)

    def test_moly_outage_is_reported(self):
        path = samples.make_epub(self.dir / "Szabo Magda - Az ajto.epub")

        def get(url, **kwargs):  # what net.get does when Cloudflare or the server refuses
            net.FAILURES.append("moly.hu: HTTP 403")

        with mock.patch.object(net, "FAILURES", []), mock.patch.object(net, "get", side_effect=get), \
                mock.patch.object(net, "get_json", return_value=None):
            meta = books.resolve(inspect(path), "ebook_hun", original=False, hints={})
        self.assertTrue(any(w.startswith("moly.hu nem érhető el") for w in meta.warnings))
        self.assertEqual(meta.extra["torrent_name"], "Szabó Magda - Az ajtó")

    def test_book_names_keep_their_numbers(self):
        self.assertEqual(books.parse_name("Ray Bradbury - Fahrenheit 451", numbered=False)["title"], "Fahrenheit 451")
        info = books.parse_name("Brandon.Sanderson-The.Way.of.Kings.2010.RETAIL.EPUB.eBook-GRP")
        self.assertEqual((info["author"], info["title"], info["year"]), ("Brandon Sanderson", "The Way of Kings", "2010"))
        self.assertEqual(books.parse_name("Jean-Paul.Sartre-A.let.es.a.semmi.HUN.EPUB.eBook-GRP")["author"], "Jean-Paul Sartre")
        info = books.parse_name("Saga 001 (of 6) (2012)")
        self.assertEqual((info["title"], info["series"], info["issue"]), ("Saga", "", "1"))
        info = books.parse_name("One Piece v100 (2022) (Digital) (1r0n)")
        self.assertEqual((info["title"], info["volume"]), ("One Piece", "100"))
        path = samples.make_pdf(self.dir / "Ray Bradbury - Fahrenheit 451.pdf", [{"lines": [samples.LOREM]} for _ in range(4)])
        with mock.patch.object(net, "get_json", return_value=None):
            meta = books.resolve(inspect(path), "ebook", original=False, hints={})
        self.assertEqual(meta.extra["torrent_name"], "Ray Bradbury - Fahrenheit 451")

    def test_loose_hints(self):
        path = samples.make_epub(self.dir / "x.epub", title="Network Effect", author="Martha Wells", language="en", isbn="",
                                 description="", date="2020-05-05")
        with mock.patch.object(net, "get_json", return_value=None):
            meta = books.resolve(inspect(path), "ebook", original=False,
                                 hints={"year": 2021, "author": ["Martha Wells", "Someone Else"], "magazine_dir": str(self.dir)})
        self.assertEqual((meta.year, meta.creators), ("2021", ["Martha Wells", "Someone Else"]))
        self.assertEqual(meta.extra["torrent_name"], "Martha Wells, Someone Else - Network Effect")
        self.assertEqual(books.magazine_name({"name": "HVG", "year": "", "issue": "", "month": ""}), "HVG")
        settings, stub, created = books.magazine_config("HVG", "magyar", str(self.dir))
        self.assertTrue(created and stub.exists())


if __name__ == "__main__":
    unittest.main()
