"""Metadata embedded in ebooks, comics and magazines, the SCAN/OCR check and the 3 proof page images.

Readers: EPUB OPF (zipfile), PDF (pdfinfo + exiftool XMP, pdftotext of the first/last pages for ISBN and language),
MOBI/AZW/AZW3 (exiftool EXTH), CBZ/CBR/CB7 ComicInfo.xml (zipfile / 7z), DjVu (exiftool, djvused), FB2 (XML).
``read(path)`` returns one dict with the keys of ``_blank()``.

nCore wants "SCAN" or "OCR" in the description of every PDF/DJVU upload. ``detect_text_layer`` tells
TEXT (born-digital), OCR (page images with a text layer) and SCAN (page images only). Approved uploads write
"pdf - (OCR)" for born-digital PDFs too (the text is searchable), so ``format_label`` maps TEXT to "OCR".

``proof_pages`` renders text pages from the body of the book (15-85 %), skipping short, TOC-like and image-only
pages; EPUB/MOBI/AZW3 need calibre's ``ebook-convert`` (or MuPDF's ``mutool`` for EPUB/FB2) to get pages at all.

Everything is static inspection: nothing from a release is ever executed. Missing tools give empty results.
"""

from __future__ import annotations

import html
import json
import math
import posixpath
import re
import shutil
import subprocess
import tempfile
import unicodedata
import zipfile
import zlib
from pathlib import Path

from .. import safeio
from urllib.parse import unquote
from xml.etree import ElementTree

BOOK_EXT = {".epub", ".pdf", ".mobi", ".prc", ".azw", ".azw3", ".djvu", ".djv", ".fb2", ".cbz", ".cbr", ".cb7", ".doc", ".docx", ".rtf", ".lit"}
COMIC_EXT = {".cbz", ".cbr", ".cb7"}
IMAGE_RE = re.compile(r"(?i)\.(jpe?g|png|webp|gif|avif|bmp)$")
JUNK_RE = re.compile(r"(?i)(^|/)(__macosx/|\._|thumbs\.db$|\.ds_store$)|(^|[/_ .-])(credits?|scanlat\w*|recruit\w*|ads?)([_ .-]|\.\w+$)")
TOC_LINE = re.compile(r"(\.{3,}|…|\s{2,}|\t)\s*\d{1,4}\s*$")
TOC_HEAD = re.compile(r"(?i)^\s*(tartalom(jegyz[ée]k)?|contents|table of contents|index|t[áa]rgymutat[óo]|n[ée]vmutat[óo]|jegyzetek|"
                      r"irodalomjegyz[ée]k|bibliography|impresszum|copyright|minden jog fenntartva|isbn)\b")
ISBN_RE = re.compile(r"(?i)(?:ISBN(?:-?1[03])?[:\s]*)?\b((?:97[89][\s-]?)?\d{1,5}[\s-]?\d{1,7}[\s-]?\d{1,7}[\s-]?[\dX])\b")
ISBN_LABELED = re.compile(r"(?i)ISBN(?:-?1[03])?[:\s#]*((?:97[89][\s-]?)?\d{1,5}[\s-]?\d{1,7}[\s-]?\d{1,7}[\s-]?[\dX])\b")
NS = {"opf": "http://www.idpf.org/2007/opf", "dc": "http://purl.org/dc/elements/1.1/",
      "c": "urn:oasis:names:tc:opendocument:xmlns:container"}
MIN_TEXT = 400  # characters on a page that counts as a text page
# what reading a damaged, password-protected or Deflate64 ZIP member can raise
ZIP_ERRORS = (OSError, KeyError, EOFError, RuntimeError, ValueError, NotImplementedError, zipfile.BadZipFile, zlib.error)


def _blank() -> dict:
    return {
        "format": "", "title": "", "subtitle": "", "creators": [], "description": "", "language": "", "publisher": "",
        "date": "", "year": "", "isbns": [], "series": "", "series_index": "", "subjects": [], "pages": 0, "drm": False,
        "identifiers": {}, "text": "", "comic": {},
    }


# --- small helpers ---------------------------------------------------------------------------------------------

def _run(cmd: list[str], timeout: int = 120) -> subprocess.CompletedProcess | None:
    if not shutil.which(cmd[0]):
        return None
    try:
        return safeio.run(cmd, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


def _out(cmd: list[str], timeout: int = 120) -> str:
    run = _run(cmd, timeout)
    return run.stdout.decode("utf-8", errors="replace") if run is not None and run.returncode == 0 else ""


def _nfc(text) -> str:
    return unicodedata.normalize("NFC", str(text or "")).strip()


def html_to_text(value: str) -> str:
    """Plain text of an HTML blurb (paragraphs kept as blank lines)."""

    text = re.sub(r"(?i)</p>|</div>|</h\d>", "\n\n", value or "")
    text = re.sub(r"(?i)<br\s*/?>|</li>", "\n", text)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", re.sub(r" *\n *", "\n", text)).strip()


def _year(date: str) -> str:
    match = re.search(r"\b(1[5-9]\d\d|20\d\d)\b", date or "")
    return match.group(1) if match else ""


def _lang(code: str) -> str:
    """'hu' from 'hu', 'hun', 'hu-HU', 'Hungarian', 'magyar'; '' when unknown."""

    code = (code or "").strip().lower().replace("_", "-")
    names = {"hungarian": "hu", "magyar": "hu", "english": "en", "angol": "en", "german": "de", "deutsch": "de", "french": "fr"}
    if code in names:
        return names[code]
    three = {"hun": "hu", "eng": "en", "ger": "de", "deu": "de", "fre": "fr", "fra": "fr", "ita": "it", "spa": "es", "rus": "ru",
             "pol": "pl", "cze": "cs", "ces": "cs", "slo": "sk", "slk": "sk", "rum": "ro", "ron": "ro", "jpn": "ja", "dut": "nl", "nld": "nl"}
    base = code.split("-")[0]
    if len(base) == 3:
        return three.get(base, "")
    return base if re.fullmatch(r"[a-z]{2}", base) else ""


def natural_key(name: str) -> list:
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", name)]


# --- ISBN ------------------------------------------------------------------------------------------------------

def isbn13(raw: str) -> str:
    """Canonical ISBN-13 of an ISBN-10/13 string with a valid checksum; '' otherwise."""

    digits = re.sub(r"[^0-9Xx]", "", raw or "").upper()
    if len(digits) == 10 and digits[:9].isdigit():
        total = sum((10 - i) * (10 if c == "X" else int(c)) for i, c in enumerate(digits))
        if total % 11:
            return ""
        digits = "978" + digits[:9]
        check = (10 - sum((1 if i % 2 == 0 else 3) * int(c) for i, c in enumerate(digits)) % 10) % 10
        return digits + str(check)
    if len(digits) == 13 and digits.isdigit() and digits[:3] in {"978", "979"}:
        if sum((1 if i % 2 == 0 else 3) * int(c) for i, c in enumerate(digits)) % 10 == 0:
            return digits
    return ""


def find_isbns(text: str, labeled_only: bool = False) -> list[str]:
    """Valid ISBN-13s in a text, in order of appearance. ``labeled_only``: only numbers written after "ISBN"."""

    found = []
    for match in (ISBN_LABELED if labeled_only else ISBN_RE).finditer(text or ""):
        value = isbn13(match.group(1))
        if value and value not in found:
            found.append(value)
    return found


def is_hungarian_isbn(isbn: str) -> bool:
    return isbn13(isbn)[:6] in {"978963", "978615"}


# --- EPUB --------------------------------------------------------------------------------------------------------

def parse_opf(xml: str | bytes) -> dict:
    """Metadata of an OPF package document (EPUB 2 and 3)."""

    result = _blank()
    result["format"] = "epub"
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        return result
    meta = root.find("opf:metadata", NS)
    if meta is None:
        meta = root.find(".//{*}metadata")
    if meta is None:
        return result

    def texts(tag: str) -> list:
        return [el for el in meta.findall(f"dc:{tag}", NS) if (el.text or "").strip()]

    titles = texts("title")
    if titles:
        result["title"] = _nfc(titles[0].text)
    refines: dict[str, dict[str, str]] = {}
    for el in meta.findall("opf:meta", NS) + meta.findall("meta"):
        target = (el.get("refines") or "").lstrip("#")
        if target and el.get("property"):
            refines.setdefault(target, {})[el.get("property")] = _nfc(el.text)
    for el in titles[1:]:
        kind = refines.get(el.get("id") or "", {}).get("title-type", "")
        if kind == "subtitle" or not kind and not result["subtitle"]:
            result["subtitle"] = _nfc(el.text)
    if ":" in result["title"] and not result["subtitle"]:
        main, _, sub = result["title"].partition(":")
        if len(main.strip()) >= 3 and sub.strip():
            result["title"], result["subtitle"] = main.strip(), sub.strip()
    for el in texts("creator"):
        role = el.get(f"{{{NS['opf']}}}role") or refines.get(el.get("id") or "", {}).get("role", "")
        if role in ("", "aut") and _nfc(el.text) not in result["creators"]:
            result["creators"].append(_nfc(el.text))
    if not result["creators"]:
        result["creators"] = [_nfc(el.text) for el in texts("creator")][:3]
    langs = texts("language")
    result["language"] = _lang(langs[0].text) if langs else ""
    publishers = texts("publisher")
    result["publisher"] = _nfc(publishers[0].text) if publishers else ""
    dates = texts("date")
    for el in dates:  # prefer the publication date over modification/creation events
        if el.get(f"{{{NS['opf']}}}event") in (None, "publication"):
            result["date"] = _nfc(el.text)[:10]
            break
    result["year"] = _year(result["date"])
    for el in texts("identifier"):
        scheme = (el.get(f"{{{NS['opf']}}}scheme") or "").lower()
        value = _nfc(el.text)
        if scheme == "isbn" or value.lower().startswith(("urn:isbn:", "isbn:")) or re.fullmatch(r"[\d\- X]{10,17}", value):
            isbn = isbn13(value.split(":")[-1])
            if isbn and isbn not in result["isbns"]:
                result["isbns"].append(isbn)
        elif scheme in {"amazon", "asin", "mobi-asin"} or value.lower().startswith("urn:asin:"):
            result["identifiers"]["asin"] = value.split(":")[-1]
        elif scheme == "google" or value.lower().startswith("urn:google:"):
            result["identifiers"]["google"] = value.split(":")[-1]
    descriptions = texts("description")
    if descriptions:
        result["description"] = html_to_text(descriptions[0].text)
    result["subjects"] = [_nfc(el.text) for el in texts("subject")]
    for el in meta.findall("opf:meta", NS) + meta.findall("meta"):
        name, content = el.get("name") or "", el.get("content") or ""
        if name == "calibre:series":
            result["series"] = _nfc(content)
        elif name == "calibre:series_index":
            result["series_index"] = _index(content)
        elif el.get("property") == "belongs-to-collection" and not result["series"]:
            props = refines.get(el.get("id") or "", {})
            if props.get("collection-type", "series") == "series":
                result["series"] = _nfc(el.text)
                result["series_index"] = _index(props.get("group-position", ""))
    return result


def _index(value: str) -> str:
    """'2' from '2.0', '1.5' stays."""

    value = (value or "").strip()
    try:
        number = float(value)
    except ValueError:
        return value
    return str(int(number)) if number.is_integer() else str(number)


def _opf_path(archive: zipfile.ZipFile) -> str:
    try:
        root = ElementTree.fromstring(safeio.read_zip(archive, "META-INF/container.xml"))
        node = root.find(".//c:rootfile", NS)
        if node is not None and node.get("full-path"):
            return node.get("full-path")
    except (KeyError, ElementTree.ParseError):
        pass
    return next((n for n in archive.namelist() if n.lower().endswith(".opf")), "")


def read_epub(path: Path) -> dict:
    try:
        with zipfile.ZipFile(path) as archive:
            name = _opf_path(archive)
            if not name:
                return _blank()
            result = parse_opf(safeio.read_zip(archive, name))
            names = set(archive.namelist())
            # DRM encrypts the text documents; font obfuscation only lists font files in encryption.xml
            if "META-INF/encryption.xml" in names:
                text = safeio.read_zip(archive, "META-INF/encryption.xml").decode("utf-8", errors="replace")
                result["drm"] = bool(re.search(r'(?i)URI="[^"]+\.(x?html?|xml|ncx|opf)"', text)) or "META-INF/rights.xml" in names
            result["text"] = _epub_text(archive, name, limit=4000)
    except ZIP_ERRORS:
        return _blank()
    if not result["isbns"]:
        result["isbns"] = find_isbns(result["text"], labeled_only=True)
    return result


def _epub_spine(archive: zipfile.ZipFile, opf_name: str) -> list[str]:
    try:
        root = ElementTree.fromstring(safeio.read_zip(archive, opf_name))
    except ZIP_ERRORS + (ElementTree.ParseError,):
        return []
    items = {el.get("id"): el.get("href") for el in root.iter() if el.tag.endswith("item") and el.get("href")}
    spine = [items.get(el.get("idref")) for el in root.iter() if el.tag.endswith("itemref")]
    return [_member(opf_name, href) for href in spine if href]


def _member(opf_name: str, href: str) -> str:
    """Archive name of a manifest href (relative to the OPF, URL-encoded)."""

    base = posixpath.dirname(opf_name)
    return posixpath.normpath(posixpath.join(base, unquote(href.split("#")[0]))).lstrip("/")


def _epub_text(archive: zipfile.ZipFile, opf_name: str, limit: int = 4000) -> str:
    """Text of the first spine documents (enough for ISBN and language checks)."""

    parts, size = [], 0
    for name in _epub_spine(archive, opf_name)[:12]:
        try:
            data = safeio.read_zip(archive, name, limit=2_000_000)
            text = html_to_text(re.sub(r"(?is)<(script|style|head)\b.*?</\1>", "", data.decode("utf-8", errors="replace")))
        except ZIP_ERRORS:
            continue
        parts.append(text)
        size += len(text)
        if size >= limit:
            break
    return "\n".join(parts)[:limit * 2]


def epub_cover(path: Path) -> bytes:
    """Cover image of an EPUB: manifest properties="cover-image", <meta name="cover">, then a file named cover."""

    try:
        with zipfile.ZipFile(path) as archive:
            name = _opf_path(archive)
            names = archive.namelist()
            href = ""
            if name:
                root = ElementTree.fromstring(safeio.read_zip(archive, name))
                items = [el for el in root.iter() if el.tag.endswith("item")]
                cover_id = next((el.get("content") for el in root.iter() if el.tag.endswith("meta") and el.get("name") == "cover"), "")
                for el in items:
                    if "cover-image" in (el.get("properties") or "").split() or (cover_id and el.get("id") == cover_id):
                        if IMAGE_RE.search(el.get("href") or ""):
                            href = _member(name, el.get("href"))
                            break
            if href and href in names:
                return safeio.read_zip(archive, href, safeio.IMAGE_LIMIT)
            covers = sorted((n for n in names if IMAGE_RE.search(n) and "cover" in n.lower()), key=natural_key)
            return safeio.read_zip(archive, covers[0], safeio.IMAGE_LIMIT) if covers else b""
    except ZIP_ERRORS + (ElementTree.ParseError,):
        return b""


# --- FB2 ---------------------------------------------------------------------------------------------------------

def read_fb2(path: Path) -> dict:
    result = _blank()
    result["format"] = "fb2"
    try:
        root = ElementTree.parse(path).getroot()
    except (OSError, ElementTree.ParseError):
        return result
    info = root.find(".//{*}title-info")
    if info is None:
        return result

    def text(node, tag: str) -> str:
        found = node.find(f"{{*}}{tag}")
        return _nfc("".join(found.itertext())) if found is not None else ""

    result["title"] = text(info, "book-title")
    for author in info.findall("{*}author"):
        name = " ".join(x for x in (text(author, "first-name"), text(author, "middle-name"), text(author, "last-name")) if x)
        if name:
            result["creators"].append(name)
    result["language"] = _lang(text(info, "lang"))
    result["description"] = text(info, "annotation")
    result["date"] = text(info, "date")
    result["year"] = _year(result["date"])
    sequence = info.find("{*}sequence")
    if sequence is not None:
        result["series"], result["series_index"] = _nfc(sequence.get("name")), _index(sequence.get("number") or "")
    publish = root.find(".//{*}publish-info")
    if publish is not None:
        result["publisher"] = text(publish, "publisher")
        result["isbns"] = find_isbns(text(publish, "isbn"))
        result["year"] = result["year"] or text(publish, "year")
    return result


# --- PDF / MOBI / DjVu through exiftool and poppler ------------------------------------------------------------

def exiftool_json(path: Path) -> dict:
    data = _out(["exiftool", "-j", "-G", "-charset", "utf8", "--", str(path)], timeout=120)
    try:
        items = json.loads(data) if data else []
    except ValueError:
        return {}
    return items[0] if items else {}


def _matches(key: str, name: str) -> bool:
    """'PDF:Title' matches 'PDF:Title' and 'Title'; a bare name matches the tag in any group."""

    return key == name if ":" in name else key.split(":")[-1] == name


def parse_exiftool(data: dict) -> dict:
    """Metadata from ``exiftool -j -G`` output of a PDF, MOBI/AZW3 (MOBI/Palm groups), DjVu or EPUB."""

    result = _blank()

    def first(*names: str) -> str:
        for name in names:
            for key, value in data.items():
                if _matches(key, name):
                    value = ", ".join(str(v) for v in value) if isinstance(value, list) else value
                    if str(value).strip():
                        return _nfc(value)
        return ""

    def many(*names: str, split: str = r"\s*;\s*") -> list[str]:
        values: list[str] = []
        for name in names:
            for key, value in data.items():
                if _matches(key, name):
                    for item in value if isinstance(value, list) else re.split(split, str(value)):
                        if str(item).strip() and _nfc(item) not in values:
                            values.append(_nfc(item))
            if values:
                break
        return values

    file_type = first("File:FileType").lower()
    kind = file_type.split()[0] if file_type else ""
    result["format"] = kind if kind in {"mobi", "azw3", "azw", "pdf", "epub", "djvu"} else ""
    pdf = result["format"] == "pdf"
    result["title"] = first("UpdatedTitle", "XMP:Title", "PDF:Title", "DjVu:Title", "XML:Title", "BookName", "Title")
    # PDF:Creator is the authoring program, the dc:creator of XMP is the author
    authors = many("MOBI:Author", "Palm:Author", "XMP:Creator", "PDF:Author", "DjVu:Author", "XML:Creator", "Author")
    if len(authors) == 1 and re.search(r"\s(&|and|és)\s|,.*,", authors[0]):
        authors = [a.strip() for a in re.split(r"\s*(?:&|\band\b|\bés\b|,)\s*", authors[0]) if a.strip()]
    result["creators"] = [a for a in authors if not re.search(r"(?i)calibre|microsoft|adobe|acrobat|\bword\b|latex|pdf", a)]
    result["publisher"] = first("Publisher", "Imprint")
    result["description"] = html_to_text(first("Description") or (first("PDF:Subject") if pdf else ""))
    result["language"] = _lang(first("Language"))
    result["date"] = first("PublishDate", "XMP:Date", "XML:Date")[:10].replace(":", "-")
    result["year"] = _year(result["date"])
    isbns: list[str] = []
    for value in [first("ISBN"), first("Identifier"), first("XMP:Identifier")] + many("Source"):
        isbns += [i for i in find_isbns(value) if i not in isbns]
    result["isbns"] = isbns
    if first("ASIN") or first("ASIN2"):
        result["identifiers"]["asin"] = first("ASIN") or first("ASIN2")
    subjects = many("PDF:Keywords", "XMP:Subject", split=r"\s*[;,]\s*") if pdf else many("Subject", split=r"\s*;\s*")
    result["subjects"] = [s for s in subjects if len(s) < 80]
    pages = first("PageCount", "Pages")
    result["pages"] = int(pages) if pages.isdigit() else 0
    # MOBI "No encryption"/"Mobipocket"; PDF "Standard ..." only restricts printing/copying, other handlers are DRM
    encryption = first("Encryption").lower()
    result["drm"] = bool(encryption) and not re.match(r"(none|no\b|0$|standard)", encryption)
    return result


def parse_pdfinfo(text: str) -> dict:
    """{"title", "author", "subject", "keywords", "creator", "producer", "pages", "encrypted", "page_size"} from pdfinfo."""

    fields = {}
    for line in (text or "").splitlines():
        key, sep, value = line.partition(":")
        if sep:
            fields[key.strip().lower()] = value.strip()
    size = re.match(r"([\d.]+) x ([\d.]+)", fields.get("page size", ""))
    return {
        "title": _nfc(fields.get("title", "")), "author": _nfc(fields.get("author", "")), "subject": _nfc(fields.get("subject", "")),
        "keywords": _nfc(fields.get("keywords", "")), "creator": fields.get("creator", ""), "producer": fields.get("producer", ""),
        "pages": int(fields["pages"]) if fields.get("pages", "").isdigit() else 0,
        "encrypted": fields.get("encrypted", "no").startswith("yes"),
        "page_size": (float(size.group(1)), float(size.group(2))) if size else (0.0, 0.0),
    }


def page_text(path: Path, page: int, last: int = 0) -> str:
    """Text of one page (or the pages up to ``last``) of a PDF (pdftotext) or DjVu (djvutxt); '' when none.
    Paths are absolute, so they never look like options (djvulibre tools do not accept "--")."""

    path = Path(path).absolute()
    if path.suffix.lower() == ".djvu":
        return "\n".join(_out(["djvutxt", f"--page={p}", str(path)], timeout=60) for p in range(page, max(page, last) + 1))
    return _out(["pdftotext", "-f", str(page), "-l", str(max(page, last)), "-layout", "-enc", "UTF-8", "--", str(path), "-"], timeout=120)


def page_count(path: Path) -> int:
    path = Path(path).absolute()
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return parse_pdfinfo(_out(["pdfinfo", "--", str(path)]))["pages"]
    if suffix == ".djvu":
        value = _out(["djvused", "-e", "n", str(path)]).strip()
        return int(value) if value.isdigit() else 0
    return 0


def page_sizes(text: str) -> dict[int, tuple[float, float]]:
    """{page: (width, height)} from ``pdfinfo -f A -l B`` ("Page    3 size: 595 x 842 pts")."""

    return {int(m.group(1)): (float(m.group(2)), float(m.group(3)))
            for m in re.finditer(r"(?m)^Page\s+(\d+)\s+size:\s+([\d.]+) x ([\d.]+)", text or "")}


def read_pdf(path: Path) -> dict:
    path = Path(path).absolute()
    info = parse_pdfinfo(_out(["pdfinfo", "--", str(path)]))
    result = parse_exiftool(exiftool_json(path)) if shutil.which("exiftool") else _blank()
    result["format"] = "pdf"
    result["title"] = result["title"] or info["title"]
    if not result["creators"] and info["author"]:
        result["creators"] = [a.strip() for a in re.split(r"\s*(?:;|&|\band\b|\bés\b)\s*", info["author"]) if a.strip()]
    result["pages"] = info["pages"] or result["pages"]
    # junk titles written by converters ("Microsoft Word - x.doc", "untitled")
    if re.search(r"(?i)^(microsoft word|untitled|document\d*$)|\.(docx?|indd|qxd|pdf)$", result["title"]):
        result["title"] = ""
    pages = result["pages"]
    if pages:
        texts = [page_text(path, 1, min(pages, 8))]
        if pages > 8:
            texts.append(page_text(path, max(9, pages - 3), pages))
        result["text"] = "\n".join(texts)
    for isbn in find_isbns(result["text"], labeled_only=True):
        if isbn not in result["isbns"]:
            result["isbns"].append(isbn)
    return result


def read_mobi(path: Path) -> dict:
    result = parse_exiftool(exiftool_json(path))
    result["format"] = path.suffix.lower().lstrip(".")
    return result


def read_djvu(path: Path) -> dict:
    path = Path(path).absolute()
    result = parse_exiftool(exiftool_json(path)) if shutil.which("exiftool") else _blank()
    if not result["title"]:
        meta = parse_djvu_meta(_out(["djvused", "-e", "print-meta", str(path)]))
        result["title"], result["creators"] = meta.get("title", ""), [meta["author"]] if meta.get("author") else []
    result["format"] = "djvu"
    result["pages"] = page_count(path)
    if result["pages"]:
        result["text"] = page_text(path, 1, min(result["pages"], 6))
    result["isbns"] = result["isbns"] or find_isbns(result["text"], labeled_only=True)
    return result


def parse_djvu_meta(text: str) -> dict:
    """{"title", "author", ...} from ``djvused -e print-meta`` (values are C-escaped UTF-8)."""

    result = {}
    for line in (text or "").splitlines():
        match = re.match(r'(\w+)\s+"(.*)"\s*$', line)
        if match:
            try:
                raw = match.group(2).encode("latin-1", errors="replace").decode("unicode_escape").encode("latin-1", errors="replace")
            except UnicodeError:  # a broken escape sequence: keep the text as it is
                raw = match.group(2).encode("utf-8", errors="replace")
            result[match.group(1).lower()] = _nfc(raw.decode("utf-8", errors="replace"))
    return result


# --- comics ---------------------------------------------------------------------------------------------------------

def parse_comicinfo(xml: str | bytes) -> dict:
    """ComicInfo.xml (Anansi schema) as a read() dict; the raw fields are under "comic"."""

    result = _blank()
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        return result
    fields = {child.tag.split("}")[-1]: _nfc(child.text) for child in root if (child.text or "").strip()}
    result["comic"] = fields
    result["series"] = fields.get("Series", "")
    result["series_index"] = fields.get("Number") or fields.get("Volume", "")
    result["title"] = fields.get("Title") or fields.get("Series", "")
    creators = []
    for role in ("Writer", "Penciller", "Artist", "Inker", "CoverArtist"):
        for name in re.split(r"\s*[;,]\s*", fields.get(role, "")):
            if name and name not in creators:
                creators.append(name)
    result["creators"] = creators
    result["publisher"] = fields.get("Publisher", "")
    result["description"] = html_to_text(fields.get("Summary", ""))
    result["language"] = _lang(fields.get("LanguageISO", ""))
    if fields.get("Year"):
        result["year"] = fields["Year"]
        result["date"] = "-".join([fields["Year"]] + [f"{int(fields[k]):02d}" for k in ("Month", "Day") if fields.get(k, "").isdigit()])
    result["subjects"] = [g for g in re.split(r"\s*[;,]\s*", fields.get("Genre", "") + "," + fields.get("Tags", "")) if g]
    result["pages"] = int(fields["PageCount"]) if fields.get("PageCount", "").isdigit() else 0
    result["isbns"] = find_isbns(fields.get("GTIN", "") + " " + fields.get("ISBN", ""))
    return result


def comic_entries(path: Path) -> list[str]:
    """Image entries of a CBZ/CBR/CB7 in reading order (natural sort, junk dropped). The names are returned as
    stored (Windows tools may write backslashes), so ``comic_member`` can read them back."""

    names: list[str] = []
    if zipfile.is_zipfile(path):
        try:
            with zipfile.ZipFile(path) as archive:
                names = [i.filename for i in archive.infolist() if not i.is_dir()]
        except ZIP_ERRORS:
            names = []
    else:
        listing = _out(["7z", "l", "-slt", "-ba", "--", str(path)], timeout=120)
        names = [line[7:].strip() for line in listing.splitlines() if line.startswith("Path = ")]
    keep = [(n.replace("\\", "/"), n) for n in names]
    keep = [(norm, n) for norm, n in keep if IMAGE_RE.search(norm) and not JUNK_RE.search(norm)]
    return [n for _, n in sorted(keep, key=lambda item: natural_key(item[0]))]


def _basename(name: str) -> str:
    return re.split(r"[/\\]", name)[-1]


def comic_member(path: Path, name: str) -> bytes:
    """Bytes of one archive member (zipfile for CBZ, ``7z e -so`` otherwise)."""

    if zipfile.is_zipfile(path):
        try:
            with zipfile.ZipFile(path) as archive:
                return safeio.read_zip(archive, name, safeio.IMAGE_LIMIT)
        except ZIP_ERRORS:
            return b""
    from .installer import read_member
    return read_member(path, name, limit=safeio.IMAGE_LIMIT)


def read_comic(path: Path) -> dict:
    listing: list[str] = []
    if zipfile.is_zipfile(path):
        try:
            with zipfile.ZipFile(path) as archive:
                listing = archive.namelist()
        except ZIP_ERRORS:
            listing = []
    else:
        out = _out(["7z", "l", "-slt", "-ba", "--", str(path)], timeout=120)
        listing = [line[7:].strip() for line in out.splitlines() if line.startswith("Path = ")]
    info = next((n for n in listing if _basename(n).lower() == "comicinfo.xml"), "")
    result = parse_comicinfo(comic_member(path, info)) if info else _blank()
    result["format"] = path.suffix.lower().lstrip(".")
    result["pages"] = result["pages"] or len(comic_entries(path))
    return result


# --- dispatch ---------------------------------------------------------------------------------------------------------

def read(path: str | Path) -> dict:
    """Embedded metadata of one ebook/comic/magazine file; empty values when nothing can be read."""

    path = Path(path)
    suffix = path.suffix.lower()
    try:
        if suffix == ".epub":
            result = read_epub(path)
        elif suffix == ".pdf":
            result = read_pdf(path)
        elif suffix in {".mobi", ".azw", ".azw3", ".prc"}:
            result = read_mobi(path)
        elif suffix in COMIC_EXT:
            result = read_comic(path)
        elif suffix == ".djvu":
            result = read_djvu(path)
        elif suffix == ".fb2":
            result = read_fb2(path)
        else:
            result = _blank()
    except Exception:  # noqa: BLE001 - a broken file must never stop the upload
        result = _blank()
    result["format"] = result.get("format") or suffix.lstrip(".")
    return result


def cover(path: str | Path) -> bytes:
    """Embedded cover: EPUB cover image, first comic page, first PDF/DjVu page rendered; b'' when none."""

    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".epub":
        return epub_cover(path)
    if suffix in COMIC_EXT:
        # the first page is the cover unless a page is named so ("p000.jpg" often is the cover, "p001.jpg" is not)
        entries = comic_entries(path)
        front = [n for n in entries if not re.search(r"(?i)back", _basename(n))]
        covers = [n for n in front if re.search(r"(?i)cover", _basename(n))] or front or entries
        return comic_member(path, covers[0]) if covers else b""
    if suffix in {".pdf", ".djvu"}:
        with tempfile.TemporaryDirectory() as tmp:
            image = _render(path, 1, Path(tmp) / "cover", 600)
            return image.read_bytes() if image else b""
    if suffix in {".mobi", ".azw", ".azw3"} and shutil.which("ebook-meta"):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "cover.jpg"
            _run(["ebook-meta", str(path), f"--get-cover={target}"], timeout=120)
            return target.read_bytes() if target.exists() else b""
    return b""


# --- SCAN / OCR -------------------------------------------------------------------------------------------------------

def parse_pdfimages(text: str) -> list[dict]:
    """Rows of ``pdfimages -list``: {"page", "type", "width", "height", "x_ppi", "y_ppi"}."""

    rows = []
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) >= 14 and parts[0].isdigit():
            try:
                rows.append({"page": int(parts[0]), "type": parts[2], "width": int(parts[3]), "height": int(parts[4]),
                             "x_ppi": float(parts[12]), "y_ppi": float(parts[13])})
            except ValueError:
                continue
    return rows


def image_coverage(rows: list[dict], page_size: tuple[float, float]) -> float:
    """Largest share of the page (0..1) covered by one image of ``pdfimages -list`` rows."""

    width, height = page_size
    if not width or not height:
        return 0.0
    best = 0.0
    for row in rows:
        if row["type"] != "image" or row["x_ppi"] <= 0 or row["y_ppi"] <= 0:
            continue
        shown = (row["width"] * 72 / row["x_ppi"]) * (row["height"] * 72 / row["y_ppi"])
        best = max(best, min(1.0, shown / (width * height)))
    return best


def classify_layer(samples: list[tuple[int, float]]) -> str:
    """'TEXT' / 'OCR' / 'SCAN' from (characters, image coverage) of sampled pages."""

    if not samples:
        return ""
    scanned = [chars for chars, cover_share in samples if cover_share >= 0.6]
    if len(scanned) * 2 >= len(samples):
        with_text = sum(chars >= 50 for chars in scanned)
        return "OCR" if with_text * 2 >= len(scanned) else "SCAN"
    return "TEXT" if any(chars >= 50 for chars, _ in samples) else "SCAN"


def _sample_pages(pages: int, count: int = 10) -> list[int]:
    if pages <= count:
        return list(range(1, pages + 1))
    lo, hi = max(1, math.ceil(pages * 0.1)), max(1, math.floor(pages * 0.9))
    return sorted({lo + round((hi - lo) * k / (count - 1)) for k in range(count)})


def detect_text_layer(path: str | Path) -> str:
    """'TEXT' (born-digital), 'OCR' (page images + text layer) or 'SCAN' (page images only) for a PDF/DjVu; ''
    when it cannot be told (other format or missing poppler/djvulibre)."""

    path = Path(path).absolute()
    suffix = path.suffix.lower()
    if suffix == ".djvu":
        if not shutil.which("djvutxt"):
            return ""
        texts = [len(re.sub(r"\s+", "", page_text(path, p))) for p in _sample_pages(page_count(path))]
        return ("OCR" if sum(c >= 50 for c in texts) * 2 >= len(texts) else "SCAN") if texts else ""
    if suffix != ".pdf" or not (shutil.which("pdfinfo") and shutil.which("pdftotext") and shutil.which("pdfimages")):
        return ""
    info = parse_pdfinfo(_out(["pdfinfo", "--", str(path)]))
    pages = _sample_pages(info["pages"])
    if not pages:
        return ""
    sizes = page_sizes(_out(["pdfinfo", "-f", str(pages[0]), "-l", str(pages[-1]), "--", str(path)]))
    samples = []
    for page in pages:
        chars = len(re.sub(r"\s+", "", page_text(path, page)))
        rows = parse_pdfimages(_out(["pdfimages", "-list", "-f", str(page), "-l", str(page), "--", str(path)]))
        samples.append((chars, image_coverage(rows, sizes.get(page) or info["page_size"])))
    return classify_layer(samples)


def format_label(ext: str, layer: str = "") -> str:
    """Format text of the description: 'epub', 'pdf - (OCR)', 'djvu - (SCAN)' (TEXT counts as OCR, see module doc)."""

    ext = ext.lower().lstrip(".")
    if ext in {"pdf", "djvu"}:
        return f"{ext} - ({'SCAN' if layer == 'SCAN' else 'OCR'})"
    return ext


# --- proof pages -------------------------------------------------------------------------------------------------------

def is_toc_like(text: str) -> bool:
    """Table of contents, index, imprint: many dot leaders / trailing page numbers or a typical heading."""

    lines = [line for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return False
    if any(TOC_HEAD.match(line) for line in lines[:5]):
        return True
    return sum(bool(TOC_LINE.search(line)) for line in lines) >= max(3, len(lines) * 0.25)


def choose_pages(pages: int, accept, count: int = 3, spread: int = 3) -> list[int]:
    """Pick ``count`` pages between 15 % and 85 % of the book near evenly spaced targets; ``accept(page)`` filters."""

    if pages <= 0:
        return []
    lo = max(1, math.ceil(pages * 0.15))
    hi = max(lo, math.floor(pages * 0.85))
    if hi - lo + 1 < count:  # very short documents: any page will do
        lo, hi = 1, pages
    spread = max(1, min(spread, (hi - lo + 1) // count))  # short documents still give ``count`` pages
    targets = [lo + (hi - lo) * (k + 1) / (count + 1) for k in range(count)]
    chosen: list[int] = []
    for target in targets:
        start = int(round(target))
        for step in [0, 1, 2, 3, 4, 5, 6, -1, -2, -3, -4, -5, -6, 7, 8, -7, -8]:
            page = start + step
            if page < lo or page > hi or page in chosen or any(abs(page - c) < spread for c in chosen):
                continue
            if accept(page):
                chosen.append(page)
                break
    return sorted(chosen)


def _render(path: Path, page: int, stem: Path, size: int = 1400) -> Path | None:
    """One page as JPEG (long side ``size`` px); None when it fails."""

    stem.parent.mkdir(parents=True, exist_ok=True)
    path, stem = Path(path).absolute(), Path(stem).absolute()
    if path.suffix.lower() == ".djvu":
        pnm = stem.with_suffix(".pnm")
        _run(["ddjvu", "-format=pnm", f"-page={page}", f"-size={size}x{size}", str(path), str(pnm)], timeout=300)
        if not pnm.exists():
            return None
        target = stem.with_suffix(".jpg")
        if not _to_jpeg(pnm, target):
            return None
        pnm.unlink(missing_ok=True)
        return target
    _run(["pdftoppm", "-f", str(page), "-l", str(page), "-jpeg", "-jpegopt", "quality=88", "-scale-to", str(size), "-singlefile",
          "--", str(path), str(stem)], timeout=300)
    target = stem.with_suffix(".jpg")
    return target if target.exists() and target.stat().st_size else None


def _to_jpeg(source: Path, target: Path) -> bool:
    try:
        from PIL import Image  # optional dependency

        with Image.open(source) as image:
            image.convert("RGB").save(target, "JPEG", quality=88)
        return True
    except Exception:  # noqa: BLE001 - Pillow missing or unreadable image
        pass
    if shutil.which("ffmpeg"):
        _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source), str(target)], timeout=120)
    return target.exists() and target.stat().st_size > 0


def _ocr_chars(path: Path, page: int) -> int:
    """Characters tesseract reads on a page image (-1 when tesseract or the renderer is missing)."""

    if not shutil.which("tesseract"):
        return -1
    langs = _out(["tesseract", "--list-langs"], timeout=30).split()
    lang = "+".join(x for x in ("hun", "eng") if x in langs) or "eng"
    with tempfile.TemporaryDirectory() as tmp:
        image = _render(path, page, Path(tmp) / "ocr", 1200)
        if image is None:
            return -1
        text = _out(["tesseract", str(image), "stdout", "-l", lang, "--psm", "6"], timeout=180)
    return len(re.sub(r"\s+", "", text))


def _paged(path: Path, out_dir: Path, warnings: list[str]) -> Path | None:
    """A PDF version of a reflowable ebook; None without tools.

    MuPDF's ``mutool convert`` lays out EPUB/FB2 itself. MOBI/AZW3/other formats go through calibre's
    ``ebook-convert`` to EPUB first (calibre's own PDF output needs QtWebEngine, which a headless box lacks), and
    calibre's direct PDF output is the last resort.
    """

    suffix = path.suffix.lower()
    path, target = Path(path).absolute(), (out_dir / "_oldalak.pdf").absolute()
    source = path
    if suffix not in {".epub", ".fb2"} and shutil.which("ebook-convert") and shutil.which("mutool"):
        epub = (out_dir / "_atalakitott.epub").absolute()
        _run(["ebook-convert", str(path), str(epub)], timeout=900)
        if epub.exists() and epub.stat().st_size:
            source = epub
    if shutil.which("mutool") and source.suffix.lower() in {".epub", ".fb2"}:
        _run(["mutool", "convert", "-O", "width=420,height=595,em=12", "-o", str(target), str(source)], timeout=600)
        if not target.exists() or not target.stat().st_size:
            _run(["mutool", "convert", "-o", str(target), str(source)], timeout=600)
    if (not target.exists() or not target.stat().st_size) and shutil.which("ebook-convert"):
        _run(["ebook-convert", str(path), str(target), "--paper-size", "a5", "--pdf-default-font-size", "16"], timeout=900)
    if not shutil.which("mutool") and not shutil.which("ebook-convert"):
        warnings.append(f"{suffix.lstrip('.').upper()}: az oldalképekhez a mupdf-tools (mutool) vagy a calibre kell, de nincs telepítve — add meg a 3 képet kézzel (--images)")
        return None
    if not target.exists() or not target.stat().st_size:
        warnings.append(f"{path.name}: nem sikerült PDF-et készíteni az oldalképekhez")
        return None
    return target


def proof_pages(path: str | Path, out_dir: str | Path, count: int = 3, warnings: list[str] | None = None, layer: str = "") -> list[Path]:
    """``count`` page images (JPEG/PNG) from the body of the book for nCore's "3 kép" rule; [] when impossible.

    PDF/DjVu are rendered (pdftoppm/ddjvu); text pages need >= 400 characters and must not look like a TOC/index;
    scans are checked with tesseract when installed. Comics: pages at 25/50/75 % extracted as they are.
    ``layer``: a known detect_text_layer() result (Meta.extra["text_layer"]) saves the second check.
    Problems are appended to ``warnings`` in Hungarian.
    """

    path, out_dir = Path(path), Path(out_dir)
    warnings = warnings if warnings is not None else []
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = path.suffix.lower()
    if suffix in COMIC_EXT:
        return _comic_pages(path, out_dir, count, warnings)
    if suffix in {".epub", ".mobi", ".azw", ".azw3", ".fb2"}:
        paged = _paged(path, out_dir, warnings)
        if paged is None:
            return []
        result = _document_pages(paged, out_dir, count, warnings, layer="TEXT")
        paged.unlink(missing_ok=True)
        return result
    if suffix in {".pdf", ".djvu"}:
        needed = ("pdftoppm", "pdftotext", "pdfinfo") if suffix == ".pdf" else ("ddjvu", "djvused", "djvutxt")
        if not all(shutil.which(tool) for tool in needed):
            warnings.append(f"{suffix.lstrip('.').upper()}: hiányzik a renderelő ({', '.join(needed)})")
            return []
        return _document_pages(path, out_dir, count, warnings, layer=layer)
    warnings.append(f"{path.name}: ebből a formátumból nem tudok oldalképet készíteni")
    return []


def _document_pages(path: Path, out_dir: Path, count: int, warnings: list[str], layer: str = "") -> list[Path]:
    pages = page_count(path)
    if pages < count:
        warnings.append(f"{path.name}: túl kevés oldal ({pages}) a {count} oldalképhez")
        return []
    layer = layer or detect_text_layer(path)
    texts: dict[int, str] = {}

    def text(page: int) -> str:
        if page not in texts:
            texts[page] = page_text(path, page)
        return texts[page]

    def text_page(minimum: int):
        return lambda page: len(re.sub(r"\s+", " ", text(page)).strip()) >= minimum and not is_toc_like(text(page))

    if layer == "SCAN":
        budget = [8]  # tesseract runs (a few seconds each); afterwards pages are taken unchecked
        unchecked: list[int] = []

        def scanned(page: int) -> bool:
            if budget[0] <= 0:
                unchecked.append(page)
                return True
            budget[0] -= 1
            chars = _ocr_chars(path, page)
            if chars < 0:  # no tesseract (or no renderer): cannot tell
                unchecked.append(page)
            return chars < 0 or chars >= 300

        picks = choose_pages(pages, scanned, count)
        if set(picks) & set(unchecked):
            warnings.append(f"{path.name}: a beolvasott oldalakról nem tudtam szövegfelismeréssel eldönteni, hogy szövegoldalak-e — nézd át az oldalképeket")
    else:
        picks = choose_pages(pages, text_page(MIN_TEXT), count)
        if len(picks) < count:
            picks = choose_pages(pages, text_page(150), count)
            if len(picks) == count:
                warnings.append(f"{path.name}: kevés szöveg az oldalakon — ellenőrizd az oldalképeket")
    result = []
    for index, page in enumerate(picks, 1):
        image = _render(path, page, out_dir / f"oldal{index}")
        if image is not None:
            result.append(image)
    if len(result) < count:
        warnings.append(f"{path.name}: csak {len(result)} megfelelő oldalképet találtam a {count}-ból")
    return result


def _comic_pages(path: Path, out_dir: Path, count: int, warnings: list[str]) -> list[Path]:
    entries = comic_entries(path)
    if len(entries) > count + 3:
        entries = entries[2:-1]  # cover, credits/inner cover, back cover
    if len(entries) < count:
        warnings.append(f"{path.name}: túl kevés kép a képregényben ({len(entries)})")
        return []
    picks = [entries[min(len(entries) - 1, len(entries) * (k + 1) // (count + 1))] for k in range(count)]
    result = []
    for index, name in enumerate(dict.fromkeys(picks), 1):
        data = comic_member(path, name)
        if not data:
            continue
        suffix = Path(name).suffix.lower()
        target = out_dir / f"oldal{index}{suffix if suffix in {'.jpg', '.jpeg', '.png'} else '.src'}"
        target.write_bytes(data)
        if target.suffix == ".src":  # webp/avif/gif/bmp -> JPEG
            jpeg = target.with_suffix(".jpg")
            converted = _to_jpeg(target, jpeg)
            target.unlink(missing_ok=True)
            if not converted:
                continue
            target = jpeg
        result.append(target)
    if len(result) < count:
        warnings.append(f"{path.name}: csak {len(result)} oldalt sikerült kibontani")
    return result
