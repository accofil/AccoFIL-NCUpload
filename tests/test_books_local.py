from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from ncore_up.local import ebookmeta as em

sys.path.insert(0, str(Path(__file__).parent))
import test_books_samples as samples  # noqa: E402

FIX = Path(__file__).parent / "fixtures" / "books"
HAS_POPPLER = all(shutil.which(t) for t in ("pdfinfo", "pdftotext", "pdftoppm", "pdfimages"))


def section(name: str) -> str:
    text = (FIX / "pdftools.txt").read_text(encoding="utf-8")
    return text.split(f"=== {name}\n", 1)[1].split("\n=== ", 1)[0]


class IsbnTests(unittest.TestCase):
    def test_checksums_and_conversion(self):
        self.assertEqual(em.isbn13("978-615-5715-43-3"), "9786155715433")
        self.assertEqual(em.isbn13("9631411540"), "9789631411546")
        self.assertEqual(em.isbn13("9786155715434"), "")
        self.assertEqual(em.isbn13("12345"), "")
        self.assertTrue(em.is_hungarian_isbn("9789631427967"))
        self.assertFalse(em.is_hungarian_isbn("9781250229861"))

    def test_find_in_text(self):
        text = "Jaffa Kiadó 2025\nISBN 978-615-5715-43-3 (epub)\nISBN: 963 14 1154 0\ntel: 06 1 234 5678"
        self.assertEqual(em.find_isbns(text, labeled_only=True), ["9786155715433", "9789631411546"])
        self.assertEqual(em.find_isbns("no isbn here 2025"), [])


class OpfTests(unittest.TestCase):
    def test_epub2_with_calibre_series(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = samples.make_epub(Path(tmp) / "a.epub", series="Szabó Magda-életmű", series_index="12.0")
            data = em.read(path)
            self.assertEqual(data["format"], "epub")
            self.assertEqual(data["title"], "Az ajtó")
            self.assertEqual(data["creators"], ["Szabó Magda"])
            self.assertEqual((data["language"], data["publisher"], data["year"]), ("hu", "Jaffa Kiadó", "2025"))
            self.assertEqual(data["isbns"], ["9786155715433"])
            self.assertEqual((data["series"], data["series_index"]), ("Szabó Magda-életmű", "12"))
            self.assertEqual(data["description"], "Álmában nem, de életében egyszer kinyílik az ajtó.")
            self.assertIn("regény", data["subjects"])
            self.assertFalse(data["drm"])
            self.assertIn("Emerenc", data["text"])
            self.assertTrue(em.cover(path).startswith(b"\x89PNG"))

    def test_epub3_collection_and_urn_isbn(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = samples.make_epub(Path(tmp) / "b.epub", title="Network Effect: A Murderbot Novel", author="Martha Wells", language="en-US",
                                     isbn="9781250229861", series="The Murderbot Diaries", series_index="5", epub3=True)
            data = em.read(path)
            self.assertEqual((data["title"], data["subtitle"]), ("Network Effect", "A Murderbot Novel"))
            self.assertEqual(data["language"], "en")
            self.assertEqual(data["isbns"], ["9781250229861"])
            self.assertEqual((data["series"], data["series_index"]), ("The Murderbot Diaries", "5"))
            self.assertTrue(em.cover(path).startswith(b"\x89PNG"))

    def test_drm_epub(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = samples.make_epub(Path(tmp) / "c.epub")
            with zipfile.ZipFile(path, "a") as archive:
                archive.writestr("META-INF/encryption.xml", '<encryption><EncryptedData><CipherData><CipherReference URI="OEBPS/text/ch1.xhtml"/></CipherData></EncryptedData></encryption>')
            self.assertTrue(em.read(path)["drm"])

    def test_broken_files_give_blank(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "x.epub"
            bad.write_bytes(b"not a zip")
            self.assertEqual(em.read(bad)["title"], "")
            self.assertEqual(em.cover(bad), b"")
            self.assertEqual(em.parse_opf("<oops")["title"], "")


class ExiftoolTests(unittest.TestCase):
    def test_pdf(self):
        data = em.parse_exiftool(json.loads((FIX / "exiftool_pdf.json").read_text(encoding="utf-8"))[0])
        self.assertEqual((data["format"], data["title"], data["pages"]), ("pdf", "Az ajtó", 20))
        self.assertEqual(data["creators"], ["Szabó Magda"])  # PDF:Creator (the Word program) is not an author
        self.assertEqual(data["isbns"], ["9789631427967"])
        self.assertEqual(data["language"], "hu")
        self.assertEqual(data["subjects"], ["regény", "magyar irodalom"])

    def test_mobi(self):
        data = em.parse_exiftool(json.loads((FIX / "exiftool_mobi.json").read_text(encoding="utf-8"))[0])
        self.assertEqual(data["format"], "azw3")
        self.assertEqual(data["title"], "Network Effect: A Murderbot Novel")
        self.assertEqual((data["creators"], data["publisher"], data["year"]), (["Martha Wells"], "Tom Doherty Associates", "2020"))
        self.assertEqual(data["isbns"], ["9781250229861"])
        self.assertEqual(data["identifiers"]["asin"], "B07ZWH1NJN")
        self.assertTrue(data["description"].startswith("Murderbot returns"))
        self.assertFalse(data["drm"])
        self.assertEqual(len(data["subjects"]), 2)

    def test_pdfinfo_pdfimages_and_djvu_meta(self):
        info = em.parse_pdfinfo(section("pdfinfo -f 3 -l 4 book.pdf"))
        self.assertEqual((info["title"], info["author"], info["pages"], info["encrypted"]), ("Az ajtó", "Szabó Magda", 20, False))
        self.assertEqual(em.page_sizes(section("pdfinfo -f 3 -l 4 book.pdf")), {3: (595.0, 842.0), 4: (595.0, 842.0)})
        rows = em.parse_pdfimages(section("pdfimages -list -f 11 -l 11 book.pdf"))
        self.assertEqual(len(rows), 3)
        self.assertGreater(em.image_coverage(rows, (595.0, 842.0)), 0.95)
        self.assertLess(em.image_coverage(rows[2:], (595.0, 842.0)), 0.01)
        meta = em.parse_djvu_meta(section("djvused -e print-meta book.djvu"))
        self.assertEqual((meta["title"], meta["author"]), ("Az ajtó", "Szabó Magda"))


class LayerAndPageChoiceTests(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(em.classify_layer([(2000, 0.0)] * 8 + [(0, 1.0)] * 2), "TEXT")
        self.assertEqual(em.classify_layer([(1500, 1.0)] * 9 + [(0, 1.0)]), "OCR")
        self.assertEqual(em.classify_layer([(0, 1.0)] * 9 + [(30, 1.0)]), "SCAN")
        self.assertEqual(em.classify_layer([]), "")
        self.assertEqual(em.format_label(".pdf", "TEXT"), "pdf - (OCR)")
        self.assertEqual(em.format_label("djvu", "SCAN"), "djvu - (SCAN)")
        self.assertEqual(em.format_label("epub", ""), "epub")

    def test_toc_detection(self):
        toc = "Tartalom\n" + "\n".join(f"{i}. fejezet ........ {i * 9}" for i in range(1, 20))
        self.assertTrue(em.is_toc_like(toc))
        self.assertTrue(em.is_toc_like("\n".join(f"Fejezet {i}      {i * 12}" for i in range(1, 20))))
        self.assertTrue(em.is_toc_like("ISBN 978-615-5715-43-3\nMinden jog fenntartva"))
        self.assertFalse(em.is_toc_like(samples.LOREM * 5))

    def test_choose_pages_in_the_body(self):
        picks = em.choose_pages(200, lambda p: True)
        self.assertEqual(len(picks), 3)
        self.assertTrue(all(30 <= p <= 170 for p in picks))
        picks = em.choose_pages(200, lambda p: p % 10 == 0)  # only every 10th page is a text page
        self.assertEqual(len(picks), 3)
        self.assertTrue(all(p % 10 == 0 for p in picks))
        self.assertEqual(em.choose_pages(0, lambda p: True), [])


class ComicTests(unittest.TestCase):
    def test_comicinfo(self):
        data = em.parse_comicinfo(samples.COMICINFO)
        self.assertEqual((data["series"], data["series_index"], data["title"]), ("Nichijou", "1", "The Girls"))
        self.assertEqual(data["creators"], ["Keiichi Arawi"])
        self.assertEqual((data["publisher"], data["year"], data["date"], data["language"]), ("Vertical", "2016", "2016-11", "en"))
        self.assertEqual(data["isbns"], ["9781942993681"])
        self.assertEqual(data["comic"]["Manga"], "YesAndRightToLeft")
        self.assertIn("Slice of Life", data["subjects"])

    def test_cbz_read_cover_and_pages(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = samples.make_cbz(Path(tmp) / "Nichijou v01.cbz", pages=24)
            entries = em.comic_entries(path)
            self.assertEqual(len(entries), 24)
            self.assertTrue(entries[0].endswith("page001.png") and entries[-1].endswith("page024.png"))
            data = em.read(path)
            self.assertEqual((data["format"], data["series"], data["pages"]), ("cbz", "Nichijou", 24))
            self.assertTrue(em.cover(path).startswith(b"\x89PNG"))
            warnings: list[str] = []
            pages = em.proof_pages(path, Path(tmp) / "out", warnings=warnings)
            self.assertEqual([p.name for p in pages], ["oldal1.png", "oldal2.png", "oldal3.png"])
            self.assertEqual(warnings, [])
            self.assertNotEqual(pages[0].read_bytes(), pages[1].read_bytes())

    def test_natural_sort_and_junk(self):
        names = ["p10.jpg", "p2.jpg", "p1.jpg", "__MACOSX/._p1.jpg", "credits.jpg", "Thumbs.db", "scanlation_team.png"]
        self.assertEqual(sorted((n for n in names if em.IMAGE_RE.search(n) and not em.JUNK_RE.search(n)), key=em.natural_key),
                         ["p1.jpg", "p2.jpg", "p10.jpg"])

    @unittest.skipUnless(shutil.which("7z"), "7z not installed")
    def test_cb7_through_7z(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src"
            src.mkdir()
            (src / "ComicInfo.xml").write_text(samples.COMICINFO, encoding="utf-8")
            for number in range(1, 13):
                (src / f"{number:02d}.png").write_bytes(samples.png(30 + number, 40))
            archive = Path(tmp) / "Nichijou v01.cb7"
            subprocess.run(["7z", "a", "-t7z", "-bd", str(archive), str(src / "*")], capture_output=True, check=True)
            data = em.read(archive)
            self.assertEqual((data["series"], data["pages"]), ("Nichijou", 24))  # PageCount from ComicInfo
            self.assertEqual(len(em.comic_entries(archive)), 12)
            pages = em.proof_pages(archive, Path(tmp) / "out")
            self.assertEqual(len(pages), 3)
            self.assertTrue(pages[0].read_bytes().startswith(b"\x89PNG"))


class FormatsWithoutRendererTests(unittest.TestCase):
    def test_epub_pages_need_calibre(self):
        if shutil.which("ebook-convert") or shutil.which("mutool"):
            self.skipTest("a converter is installed")
        with tempfile.TemporaryDirectory() as tmp:
            warnings: list[str] = []
            path = samples.make_epub(Path(tmp) / "a.epub")
            self.assertEqual(em.proof_pages(path, Path(tmp) / "out", warnings=warnings), [])
            self.assertIn("calibre", warnings[0])


@unittest.skipUnless(HAS_POPPLER, "poppler-utils not installed")
class PdfToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_read_pdf_and_isbn_from_text(self):
        path = samples.make_pdf(self.dir / "book.pdf", samples.book_pages(20), title="Az ajtó", author="Szabó Magda")
        data = em.read(path)
        self.assertEqual((data["title"], data["creators"], data["pages"]), ("Az ajtó", ["Szabó Magda"], 20))
        self.assertEqual(data["isbns"], ["9786155715433"])  # from the imprint page text
        self.assertIn("Emerenc", data["text"])
        self.assertGreater(len(em.cover(path)), 500)

    def test_text_layer(self):
        text = samples.make_pdf(self.dir / "t.pdf", samples.book_pages(20))
        scan = samples.make_pdf(self.dir / "s.pdf", [{"image": True} for _ in range(12)])
        ocr = samples.make_pdf(self.dir / "o.pdf", [{"image": True, "invisible": True, "lines": [samples.LOREM] * 20} for _ in range(12)])
        self.assertEqual(em.detect_text_layer(text), "TEXT")
        self.assertEqual(em.detect_text_layer(scan), "SCAN")
        self.assertEqual(em.detect_text_layer(ocr), "OCR")
        self.assertEqual(em.detect_text_layer(self.dir / "missing.epub"), "")

    def test_proof_pages_skip_toc_and_pictures(self):
        pages = samples.book_pages(40)
        pages[6] = {"lines": ["Tartalom"] + [f"{i}. fejezet ........ {i}" for i in range(40)]}  # a TOC in the body
        path = samples.make_pdf(self.dir / "book.pdf", pages)
        warnings: list[str] = []
        images = em.proof_pages(path, self.dir / "out", warnings=warnings)
        self.assertEqual([p.name for p in images], ["oldal1.jpg", "oldal2.jpg", "oldal3.jpg"])
        self.assertEqual(warnings, [])
        self.assertTrue(images[0].read_bytes().startswith(b"\xff\xd8"))

    def test_scan_pages_are_still_rendered(self):
        path = samples.make_pdf(self.dir / "scan.pdf", [{"image": True} for _ in range(30)])
        images = em.proof_pages(path, self.dir / "out")
        self.assertEqual(len(images), 3)

    @unittest.skipUnless(shutil.which("pdf2djvu") and shutil.which("ddjvu") and shutil.which("djvutxt"), "djvulibre/pdf2djvu missing")
    def test_djvu(self):
        pdf = samples.make_pdf(self.dir / "book.pdf", samples.book_pages(20), title="Az ajtó", author="Szabó Magda")
        djvu = self.dir / "book.djvu"
        subprocess.run(["pdf2djvu", "-q", "-o", str(djvu), str(pdf)], capture_output=True, check=True)
        data = em.read(djvu)
        self.assertEqual((data["title"], data["pages"]), ("Az ajtó", 20))
        self.assertEqual(em.detect_text_layer(djvu), "OCR")
        images = em.proof_pages(djvu, self.dir / "out")
        self.assertEqual(len(images), 3)
        self.assertTrue(images[0].read_bytes().startswith(b"\xff\xd8"))


if __name__ == "__main__":
    unittest.main()
