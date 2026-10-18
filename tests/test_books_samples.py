"""Small, valid sample files built with the standard library only (PDF, EPUB, CBZ, PNG) for the books tests.

The generators are imported by the other ``test_books_*`` modules and by live probes; the tests here only check
that the generated files are well-formed.
"""

from __future__ import annotations

import struct
import tempfile
import unittest
import zipfile
import zlib
from pathlib import Path

LOREM = ("Az ajtó előtt állt, és sokáig nem mert kopogni. A ház csendes volt, csak a kutya mozgott a kertben. "
         "Emerenc mindig mindent tudott, mielőtt bárki szólt volna neki, és ezt senki sem értette igazán. ")


def png(width: int = 60, height: int = 80, shade: int = 200, stripes: bool = True) -> bytes:
    """An 8-bit grayscale PNG; with ``stripes`` it has dark rows that look like lines of text."""

    rows = []
    for y in range(height):
        dark = stripes and (y % 6 in (2, 3)) and 4 < y < height - 4
        value = 40 if dark else shade
        rows.append(b"\x00" + bytes([value]) * width)
    raw = zlib.compress(b"".join(rows))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)) + chunk(b"IDAT", raw) + chunk(b"IEND", b"")


def _pdf_string(text: str) -> bytes:
    data = text.encode("cp1252", errors="replace")
    return b"(" + data.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)") + b")"


def _utf16(text: str) -> bytes:
    return b"<FEFF" + text.encode("utf-16-be").hex().upper().encode() + b">"


def make_pdf(path: Path, pages: list[dict], *, title: str = "", author: str = "") -> Path:
    """Write a PDF. Each page: {"lines": [str], "image": bool (full-page grayscale image), "invisible": bool
    (text drawn with render mode 3, like an OCR layer)}. A4 portrait, Helvetica, WinAnsi encoding."""

    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    catalog = add(b"")  # placeholder, filled at the end
    pages_obj = add(b"")
    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    gray = zlib.compress(b"".join(bytes([200 if (y % 8) < 5 else 60]) * 150 for y in range(212)))
    kids = []
    for page in pages:
        resources = f"/Font << /F1 {font} 0 R >>".encode()
        ops = []
        if page.get("image"):
            image = add(b"<< /Type /XObject /Subtype /Image /Width 150 /Height 212 /ColorSpace /DeviceGray /BitsPerComponent 8 "
                        b"/Filter /FlateDecode /Length " + str(len(gray)).encode() + b" >>\nstream\n" + gray + b"\nendstream")
            resources += f" /XObject << /Im1 {image} 0 R >>".encode()
            ops.append(b"q 595 0 0 842 0 0 cm /Im1 Do Q")
        lines = page.get("lines") or []
        if lines:
            ops.append(b"BT /F1 10 Tf 13 TL 50 790 Td" + (b" 3 Tr" if page.get("invisible") else b""))
            for line in lines:
                ops.append(_pdf_string(line) + b" Tj T*")
            ops.append(b"ET")
        stream = b"\n".join(ops)
        contents = add(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream")
        kids.append(add(f"<< /Type /Page /Parent {pages_obj} 0 R /MediaBox [0 0 595 842] /Contents {contents} 0 R /Resources << ".encode()
                        + resources + b" >> >>"))
    objects[catalog - 1] = f"<< /Type /Catalog /Pages {pages_obj} 0 R >>".encode()
    objects[pages_obj - 1] = ("<< /Type /Pages /Kids [" + " ".join(f"{k} 0 R" for k in kids) + f"] /Count {len(kids)} >>").encode()
    info = add(b"<< " + (b"/Title " + _utf16(title) + b" " if title else b"") + (b"/Author " + _utf16(author) + b" " if author else b"")
               + b"/Producer (ncore-up tests) >>")
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root {catalog} 0 R /Info {info} 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    path.write_bytes(bytes(out))
    return path


def book_pages(count: int = 20) -> list[dict]:
    """A book-like page list: title page, TOC, short imprint page, body text pages, one picture page."""

    pages = [{"lines": ["SZABO MAGDA", "", "Az ajtó", "", "regény"]},
             {"lines": ["Tartalom"] + [f"{i}. fejezet ........................ {i * 9}" for i in range(1, 30)]},
             {"lines": ["ISBN 978-615-5715-43-3", "Minden jog fenntartva.", "Jaffa Kiadó, Budapest 2025"]}]
    for number in range(4, count + 1):
        if number == count // 2 + 1:
            pages.append({"image": True})
            continue
        pages.append({"lines": [f"{number}. oldal"] + [LOREM[i % 40:] + LOREM[: i % 40] for i in range(28)]})
    return pages


def make_epub(path: Path, *, title: str = "Az ajtó", author: str = "Szabó Magda", language: str = "hu",
              isbn: str = "9786155715433", description: str = "<p>Álmában nem, de életében egyszer kinyílik az ajtó.</p>",
              series: str = "", series_index: str = "", publisher: str = "Jaffa Kiadó", date: str = "2025-02-19",
              epub3: bool = False, chapters: int = 3) -> Path:
    opf_ns = 'xmlns:opf="http://www.idpf.org/2007/opf"'
    meta = [f"<dc:title>{title}</dc:title>",
            f'<dc:creator opf:role="aut" opf:file-as="{author}">{author}</dc:creator>' if not epub3 else f'<dc:creator id="cre">{author}</dc:creator>',
            f"<dc:language>{language}</dc:language>", f"<dc:publisher>{publisher}</dc:publisher>", f"<dc:date>{date}</dc:date>",
            f'<dc:identifier opf:scheme="ISBN">{isbn}</dc:identifier>' if not epub3 else f"<dc:identifier>urn:isbn:{isbn}</dc:identifier>",
            '<dc:identifier id="uid">urn:uuid:12345678-1234-1234-1234-123456789abc</dc:identifier>',
            "<dc:description>" + description.replace("<", "&lt;").replace(">", "&gt;") + "</dc:description>",
            "<dc:subject>Fiction</dc:subject>", "<dc:subject>regény</dc:subject>", '<meta name="cover" content="cover-img"/>']
    if series and not epub3:
        meta += [f'<meta name="calibre:series" content="{series}"/>', f'<meta name="calibre:series_index" content="{series_index}"/>']
    if series and epub3:
        meta += [f'<meta property="belongs-to-collection" id="c01">{series}</meta>',
                 '<meta refines="#c01" property="collection-type">series</meta>',
                 f'<meta refines="#c01" property="group-position">{series_index}</meta>']
    manifest = ['<item id="cover-img" href="images/cover.png" media-type="image/png"' + (' properties="cover-image"' if epub3 else "") + "/>",
                '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>']
    spine = []
    for number in range(1, chapters + 1):
        manifest.append(f'<item id="ch{number}" href="text/ch{number}.xhtml" media-type="application/xhtml+xml"/>')
        spine.append(f'<itemref idref="ch{number}"/>')
    opf = (f'<?xml version="1.0" encoding="utf-8"?>\n<package xmlns="http://www.idpf.org/2007/opf" version="{"3.0" if epub3 else "2.0"}" unique-identifier="uid">'
           f'<metadata xmlns:dc="http://purl.org/dc/elements/1.1/" {opf_ns}>' + "".join(meta) + "</metadata>"
           "<manifest>" + "".join(manifest) + '</manifest><spine toc="ncx">' + "".join(spine) + "</spine></package>")
    container = ('<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                 '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip")
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("OEBPS/content.opf", opf)
        archive.writestr("OEBPS/toc.ncx", '<?xml version="1.0"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1"><navMap/></ncx>')
        archive.writestr("OEBPS/images/cover.png", png(120, 180, stripes=False))
        for number in range(1, chapters + 1):
            body = "".join(f"<p>{LOREM}</p>" for _ in range(12))
            archive.writestr(f"OEBPS/text/ch{number}.xhtml", f'<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml"><head><title>{number}</title></head><body><h1>{number}. fejezet</h1>{body}</body></html>')
    return path


COMICINFO = """<?xml version="1.0" encoding="utf-8"?>
<ComicInfo xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema">
  <Title>The Girls</Title>
  <Series>Nichijou</Series>
  <Number>1</Number>
  <Volume>1</Volume>
  <Count>10</Count>
  <Summary>The daily antics of a trio of childhood friends.</Summary>
  <Year>2016</Year>
  <Month>11</Month>
  <Writer>Keiichi Arawi</Writer>
  <Penciller>Keiichi Arawi</Penciller>
  <Publisher>Vertical</Publisher>
  <Genre>Comedy, Slice of Life</Genre>
  <Tags>school, robots</Tags>
  <Web>https://anilist.co/manga/33082</Web>
  <PageCount>24</PageCount>
  <LanguageISO>en</LanguageISO>
  <GTIN>9781942993681</GTIN>
  <Manga>YesAndRightToLeft</Manga>
</ComicInfo>
"""


def make_cbz(path: Path, pages: int = 24, comicinfo: str | None = COMICINFO) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        if comicinfo is not None:
            archive.writestr("ComicInfo.xml", comicinfo)
        archive.writestr("__MACOSX/._page001.png", b"junk")
        for number in range(1, pages + 1):
            archive.writestr(f"Nichijou v01/page{number:03d}.png", png(40 + number, 60, shade=180 + number))
    return path


class SampleTests(unittest.TestCase):
    def test_generated_files_are_well_formed(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf = make_pdf(Path(tmp) / "a.pdf", book_pages(8), title="Az ajtó", author="Szabó Magda")
            data = pdf.read_bytes()
            self.assertTrue(data.startswith(b"%PDF-1.4") and data.rstrip().endswith(b"%%EOF"))
            self.assertIn(b"/Count 8", data)
            epub = make_epub(Path(tmp) / "a.epub")
            with zipfile.ZipFile(epub) as archive:
                self.assertEqual(archive.namelist()[0], "mimetype")
                self.assertIn("OEBPS/content.opf", archive.namelist())
            cbz = make_cbz(Path(tmp) / "a.cbz", pages=5)
            with zipfile.ZipFile(cbz) as archive:
                self.assertEqual(len([n for n in archive.namelist() if n.endswith(".png")]), 6)
        self.assertTrue(png().startswith(b"\x89PNG"))


if __name__ == "__main__":
    unittest.main()
