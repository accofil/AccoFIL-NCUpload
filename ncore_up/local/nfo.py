"""Scene NFO / file_id.diz reader, generic enough for programs and games.

NFOs are CP437 text with ASCII/ANSI art around "Label....: value" fields and titled sections. The reader
decodes, strips the art and returns plain data:

    read(path) / parse(text) -> {
        "release", "group",              scene release name found in the text, its group
        "title", "version", "publisher", field values ("Title", "Program", "Company", ...)
        "date",                          ISO date when parsable ("2026-10-01"), else the raw value
        "url", "urls",                   official/program URL (first non-scene link), every link found
        "size", "release_type",          "12 x 15 MB", "Keygen" / "Retail" / "Patch" ...
        "protection", "os",              "Denuvo" / "Steam" ..., "Windows 10/11 x64"
        "languages",                     Hungarian language names ("angol", "magyar", "többnyelvű")
        "requirements",                  requirements block (raw English text)
        "install", "install_steps",      install section text, and its numbered steps
        "notes", "description",          notes and description/about sections
        "fields", "sections",            every "label: value" pair and every titled section (lowercase keys)
    }

``read_release(release)`` merges the release's NFO with its file_id.diz. Never raises; missing data is ''.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from .. import safeio

# Box drawing, block elements, geometric shapes and a few CP437 art characters.
ART_RE = re.compile("[─-◿▀-▟·•∙√ⁿ²°⌠⌡≈¦░-▓←-⇿☺-♫«»⌂]")
EDGE_RE = re.compile(r"^[\s|!*#=~<>\[\]{}:;^`\\/]+|[\s|!*#=~<>\[\]{}:;^`\\]+$")
URL_RE = re.compile(r"(?i)\b((?:https?://|www\.)[^\s<>\"'()\[\]{}|]+[^\s<>\"'()\[\]{}|.,;:!?])")
SCENE_RE = re.compile(r"(?<![\w./])([A-Za-z0-9][\w()&+'-]*(?:\.[\w()&+'-]+)+-[A-Za-z0-9]{2,15})(?![\w./-])")
FIELD_RE = re.compile(r"^(?P<label>[^\W\d_][\w /&()'+-]{0,32}?)\s*(?:(?:\s*[.:_·-]){2,}|\s*:)\s*(?P<value>\S.*)$")
COPYRIGHT_RE = re.compile(r"(?i)^(?:(.{2,60}?)\s+)?(?:\(c\)|©|copyright)\s+(?:\d{4}(?:\s*-\s*\d{4})?\s+)?(?:by\s+)?([^\d\s].{1,59})$")
STEP_RE = re.compile(r"^\s*(?:(?P<num>\d{1,2})\s*[.):\]-]|[-*o>]\s|step\s*\d+\s*[:.-])\s*(?P<text>\S.*)$", re.I)

LABELS = {
    "title": ("title", "name", "app", "app name", "application", "program", "program name", "product", "product name", "software",
              "game", "game name", "rls name", "package", "tool"),
    "release": ("release", "release name", "rlsname", "rls"),
    "publisher": ("publisher", "company", "developer", "developers", "manufacturer", "vendor", "author", "label", "developed by",
                  "published by", "maker"),
    "date": ("date", "release date", "rls date", "street date", "supplied date", "pre date", "released", "release day", "date released",
             "rel date"),
    "url": ("url", "program url", "homepage", "home page", "www", "web", "web site", "website", "site", "product url", "web page",
            "webpage", "link", "info url", "url info", "program www", "product page", "official site", "home"),
    "size": ("size", "rls size", "release size", "total size", "disks", "disk", "discs", "disc", "cds", "dvds", "files", "archives",
             "file size", "installed size"),
    "release_type": ("release type", "rls type", "type", "crack", "cracked by", "activation", "registration", "reg", "crack type",
                     "release info"),
    "protection": ("protection", "protected by", "drm", "copy protection"),
    "language": ("language", "languages", "lang", "langs", "interface", "ui language"),
    "os": ("os", "operating system", "platform", "system", "supported os", "os support", "target", "runs on", "supported systems",
           "os requirements"),
    "requirements": ("requirements", "system requirements", "requirement", "min requirements", "minimum requirements", "req", "reqs"),
    "genre": ("genre", "category", "genres"),
    "version": ("version", "ver", "build", "program version"),
    "format": ("format", "filetype"),
}
_LABEL_KEY = {label: key for key, labels in LABELS.items() for label in labels}

SECTIONS = {
    "install": ("install", "installation", "how to install", "install notes", "installation notes", "install info", "install instructions",
                "installing", "unpack", "unpacking", "setup", "install steps", "instructions", "how to", "usage", "install/usage"),
    "notes": ("notes", "release notes", "note", "game notes", "crack notes", "important", "rls notes", "nfo notes", "group notes", "info notes"),
    "description": ("description", "about", "info", "release info", "program info", "game info", "about the program", "about the game",
                    "product info", "release description", "software info", "app info", "features", "overview", "summary"),
    "requirements": ("requirements", "system requirements", "min requirements", "minimum requirements", "requirement", "recommended requirements"),
    "greetings": ("greetings", "greets", "contact", "news", "group news", "we need", "join us", "wanted", "affils", "respect", "thanks"),
}
_SECTION_KEY = {label: key for key, labels in SECTIONS.items() for label in labels}

# English language names (as NFOs write them) -> Hungarian.
LANG_NAMES = {
    "english": "angol", "hungarian": "magyar", "magyar": "magyar", "german": "német", "deutsch": "német", "french": "francia",
    "spanish": "spanyol", "italian": "olasz", "russian": "orosz", "polish": "lengyel", "czech": "cseh", "slovak": "szlovák",
    "japanese": "japán", "chinese": "kínai", "simplified chinese": "kínai", "traditional chinese": "kínai", "korean": "koreai",
    "portuguese": "portugál", "brazilian": "portugál (brazil)", "dutch": "holland", "swedish": "svéd", "norwegian": "norvég",
    "danish": "dán", "finnish": "finn", "turkish": "török", "ukrainian": "ukrán", "romanian": "román", "greek": "görög",
    "croatian": "horvát", "serbian": "szerb", "slovenian": "szlovén", "bulgarian": "bolgár", "arabic": "arab", "hebrew": "héber",
    "thai": "thai", "vietnamese": "vietnámi", "indonesian": "indonéz", "estonian": "észt", "latvian": "lett", "lithuanian": "litván",
    "catalan": "katalán", "corsican": "korzikai", "scottishgaelic": "skót gael", "armenian": "örmény", "nepali": "nepáli",
    "icelandic": "izlandi", "albanian": "albán", "basque": "baszk", "galician": "galíciai", "belarusian": "belarusz", "georgian": "grúz",
    "kazakh": "kazah", "persian": "perzsa", "farsi": "perzsa", "hindi": "hindi", "bengali": "bengáli", "tamil": "tamil",
    "macedonian": "macedón", "bosnian": "bosnyák", "welsh": "walesi", "irish": "ír", "luxembourgish": "luxemburgi", "maltese": "máltai",
    "uzbek": "üzbég", "azerbaijani": "azeri", "afrikaans": "afrikaans", "swahili": "szuahéli", "malay": "maláj", "filipino": "filippínó",
    "chinesesimplified": "kínai", "chinesetraditional": "kínai", "serbiancyrillic": "szerb", "serbianlatin": "szerb",
    "brazilianportuguese": "portugál (brazil)", "portuguesebrazil": "portugál (brazil)", "multilingual": "többnyelvű", "multilanguage": "többnyelvű", "multi": "többnyelvű", "multi-language": "többnyelvű",
    "multi language": "többnyelvű", "multilang": "többnyelvű",
}
_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
SCENE_HOSTS = ("srrdb", "predb", "nfo", "imdb.com", "t.me", "discord", "irc.", "scene", "torrent", "xrel", "corehalla", "nforce")


def decode(data: bytes) -> str:
    """UTF-8 when the bytes are valid UTF-8, otherwise CP437 (the NFO standard); LF line ends.

    CP437 art is practically never valid UTF-8 (runs of lead bytes like DB DB); valid UTF-8 that decodes to C1
    control characters (U+0080..U+009F, never used in text) is treated as CP437 anyway."""

    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = data.decode("utf-16", errors="replace")
    else:
        try:
            text = data.decode("utf-8")
            if re.search("[\x80-\x9f]", text):
                text = data.decode("cp437")
        except UnicodeDecodeError:
            text = data.decode("cp437", errors="replace")
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")


def _despace(line: str) -> str:
    """"I N S T A L L   N O T E S" -> "INSTALL NOTES" (letter-spaced headers)."""

    text = line.strip()
    if re.fullmatch(r"(?:\S +){2,}\S", text) and not re.search(r"\S\S", text):
        small = min(len(gap) for gap in re.findall(r" +", text))
        return " ".join(w.replace(" ", "") for w in re.split(" {%d,}" % (small + 1), text))
    return line


def clean_line(line: str) -> str:
    line = EDGE_RE.sub("", ART_RE.sub(" ", line))
    # dot leaders belong to "Label......: value" fields; other long runs are art
    line = re.sub(r"([-_=~*#])\1{3,}", " ", line) if FIELD_RE.match(line) else re.sub(r"([-_=~*#.])\1{3,}", " ", line)
    line = _despace(EDGE_RE.sub("", line))
    return re.sub(r"[ \t]{2,}", "  ", line).strip()


def strip_art(text: str) -> str:
    """The readable lines of an NFO: art characters removed, art-only lines dropped, paragraphs kept."""

    out: list[str] = []
    for raw in text.split("\n"):
        line = clean_line(raw)
        letters = len(re.findall(r"[^\W\d_]", line))
        visible = len(re.sub(r"\s", "", line))
        if not line or (not FIELD_RE.match(line) and (letters < 2 or (visible and letters / visible < 0.3 and not URL_RE.search(line)))):
            if out and out[-1]:
                out.append("")
            continue
        out.append(line)
    return "\n".join(out).strip()


def _header(line: str) -> str:
    """Section key ('install', 'notes', ...) when the line is a section title, else ''."""

    text = re.sub(r"[\[\](){}<>:=*#~|_]+", " ", line).strip(" .-").lower()
    text = re.sub(r"\s+", " ", text)
    if not text or len(text) > 40:
        return ""
    if text in _SECTION_KEY:
        return _SECTION_KEY[text]
    if text.startswith("how to install") or (line.isupper() and text.startswith("install") and len(text) <= 24):
        return "install"
    if line.isupper() and len(text) <= 30 and re.fullmatch(r"[a-z' /&-]+", text):
        return "other:" + text
    return ""


def _label(label: str) -> str:
    label = re.sub(r"\s+", " ", label.strip(" .:-_").lower())
    return _LABEL_KEY.get(label) or _LABEL_KEY.get(label.rstrip("s")) or ""


def parse_date(value: str) -> str:
    value = value.strip()
    match = re.search(r"(20\d\d|19\d\d)[-./](\d{1,2})[-./](\d{1,2})", value)
    if match:
        y, m, d = (int(x) for x in match.groups())
    else:
        match = re.search(r"(\d{1,2})[-./](\d{1,2})[-./]((?:19|20)?\d\d)\b", value)
        named = re.search(r"(?i)(\d{1,2})(?:st|nd|rd|th)?[-. /]*([a-z]{3})[a-z]*[-., /]*((?:19|20)\d\d)", value) or \
            re.search(r"(?i)([a-z]{3})[a-z]*[-. /]+(\d{1,2})(?:st|nd|rd|th)?[-., /]+((?:19|20)\d\d)", value)
        if named and not match:
            a, b, y = named.groups()
            if a.isdigit():
                d, mon = int(a), _MONTHS.get(b.lower()[:3], 0)
            else:
                mon, d = _MONTHS.get(a.lower()[:3], 0), int(b)
            y, m = int(y), mon
        elif match:
            a, b, y = int(match.group(1)), int(match.group(2)), int(match.group(3))
            y = y + 2000 if y < 100 else y
            # ambiguous dates are read as US (mm/dd), the scene default; 16.10.2026 can only be dd.mm
            m, d = (a, b) if a <= 12 else (b, a)
        else:
            return value
    try:
        return datetime(y, m, d).strftime("%Y-%m-%d")
    except ValueError:
        return value


def languages(value: str) -> list[str]:
    """'English, Hungarian / MULTi5' -> ['angol', 'magyar', 'többnyelvű'] (unknown names are kept as written)."""

    out: list[str] = []
    for part in re.split(r"[,/&;+|]| and |\s{2,}", value or "", flags=re.I):
        word = re.sub(r"\s+", " ", re.sub(r"\(.*?\)|\d+", "", part)).strip(" .-").lower()
        if not word:
            continue
        name = LANG_NAMES.get(word) or LANG_NAMES.get(word.split()[0], "")
        if not name and len(word) <= 3:
            from ..describe import LANG_HU

            name = LANG_HU.get(word, "")
        name = name or word
        if name not in out:
            out.append(name)
    return out


def _official_url(urls: list[str], field_url: str) -> str:
    if field_url:
        return field_url
    for url in urls:
        if not any(host in url.lower() for host in SCENE_HOSTS):
            return url
    return ""


def _norm_url(url: str) -> str:
    url = url.strip().rstrip(".,;")
    return "https://" + url if url.lower().startswith("www.") else url


def paragraphs(text: str) -> str:
    """Re-flow hard-wrapped NFO prose: lines of a paragraph are joined, list items and blank lines are kept."""

    out: list[str] = []
    for line in (text or "").split("\n"):
        line = line.strip()
        if not line:
            if out and out[-1]:
                out.append("")
        elif out and out[-1] and not STEP_RE.match(line) and not out[-1].endswith(":"):
            out[-1] += " " + line
        else:
            out.append(line)
    return "\n".join(out).strip()


def steps(text: str) -> list[str]:
    """Numbered / bulleted steps of an install section; unnumbered lines continue the previous step."""

    out: list[str] = []
    marked = gap = False
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            gap = True
            continue
        match = STEP_RE.match(line)
        if match:
            out.append(match.group("text").strip())
            marked, gap = True, False
        elif marked and gap:
            break  # text after the numbered list (signature lines, release name) is not a step
        elif out:
            out[-1] = (out[-1] + " " + line).strip()
        else:
            out.append(line)
    return out


def parse(text: str) -> dict:
    """Fields and sections of a decoded NFO text (see the module docstring for the keys)."""

    clean = strip_art(text or "")
    result: dict = {"release": "", "group": "", "title": "", "version": "", "publisher": "", "date": "", "url": "", "urls": [],
                    "size": "", "release_type": "", "protection": "", "os": "", "languages": [], "requirements": "", "install": "",
                    "install_steps": [], "notes": "", "description": "", "genre": "", "format": "", "fields": {}, "sections": {}}
    fields: dict[str, str] = {}
    sections: dict[str, list[str]] = {}
    owners: list[tuple[str, str]] = []  # "<title> (c) <publisher>" lines
    current = ""
    for line in clean.split("\n"):
        key = _header(line) if line else ""
        if key:
            current = key
            sections.setdefault(current, [])
            continue
        match = FIELD_RE.match(line) if line else None
        if match:
            label = re.sub(r"\s+", " ", match.group("label").strip(" .:-_").lower())
            fields.setdefault(label, match.group("value").strip(" .:"))
            if not current.startswith(("install", "notes", "description")):
                continue
        owner = COPYRIGHT_RE.match(line) if line and not current.startswith(("install", "greetings")) else None
        if owner:
            owners.append(((owner.group(1) or "").strip(), owner.group(2).strip()))
        if current and not SCENE_RE.fullmatch(line):
            sections[current].append(line)
    result["fields"] = fields
    result["sections"] = {k: "\n".join(v).strip() for k, v in sections.items()}
    for label, value in fields.items():
        key = _label(label)
        if key in {"title", "release", "publisher", "size", "release_type", "protection", "os", "genre", "format", "version"} and not result[key]:
            result[key] = value
        elif key == "date" and not result["date"]:
            result["date"] = parse_date(value)
        elif key == "url" and not result["url"]:
            found = URL_RE.search(value)
            result["url"] = _norm_url(found.group(1)) if found else ""
        elif key == "language" and not result["languages"]:
            result["languages"] = languages(value)
        elif key == "requirements" and not result["requirements"]:
            result["requirements"] = value
    if owners:
        result["title"] = result["title"] or owners[0][0]
        result["publisher"] = result["publisher"] or owners[0][1]
    result["install"] = result["sections"].get("install", "")
    for key in ("notes", "description"):
        result[key] = paragraphs(result["sections"].get(key, ""))
    if result["sections"].get("requirements"):
        block = result["sections"]["requirements"]
        result["requirements"] = (result["requirements"] + "\n" + block).strip() if result["requirements"] not in block else block
    result["install_steps"] = steps(result["install"])
    result["urls"] = list(dict.fromkeys(_norm_url(m.group(1)) for m in URL_RE.finditer(clean)))
    result["url"] = _official_url(result["urls"], result["url"])
    if not fields and not sections:  # file_id.diz and other free-form texts
        result["description"] = "\n".join(line for line in clean.split("\n") if not SCENE_RE.fullmatch(line.strip())).strip()
    names = SCENE_RE.findall(URL_RE.sub(" ", text or ""))
    release = result["release"] if SCENE_RE.fullmatch(result["release"] or "") else (names[0] if names else "")
    result["release"] = release
    result["group"] = release.rsplit("-", 1)[-1] if release else ""
    return result


def read(path: str | Path) -> dict:
    """parse() of an NFO / DIZ file; {} when it cannot be read."""

    try:
        data = safeio.read_file(path)
    except (OSError, safeio.LimitError):
        return {}
    if len(data) > 2_000_000:
        return {}
    return parse(decode(data))


def find(release) -> list[Path]:
    """All listed NFO/DIZ files, or basename-matched sidecars of a single input file.

    A sibling belonging to another torrent is never a metadata source. Sidecars
    are read only: they are not added to the torrent or selected as original NFO.
    """

    if not getattr(release, "path", None):
        return []
    root = Path(release.path)
    base = root if root.is_dir() else root.parent
    if root.is_dir():
        found = [root / f.relpath for f in release.files if f.ext in {".nfo", ".diz"}]
    else:
        found = [p for p in base.iterdir() if p.suffix.lower() in {".nfo", ".diz"} and p.stem.casefold() == root.stem.casefold()]
    safe = []
    for path in found:
        try:
            parts = path.relative_to(base).parts
            if ".." in parts or any((base.joinpath(*parts[:i])).is_symlink() for i in range(1, len(parts) + 1)):
                continue
            if path.is_file():
                safe.append(path)
        except (OSError, ValueError):
            continue
    return sorted(safe, key=lambda p: (len(p.relative_to(base).parts), p.suffix.lower() != ".nfo", str(p).casefold()))


def read_release(release) -> dict:
    """Bounded, cached metadata snapshot, independent of the upload mode.

    Conflicting fields are omitted rather than silently taking the first NFO.
    Raw records remain available to the category-specific parsers.
    """
    cached = getattr(release, "nfo_data", None)
    if cached is not None:
        return cached
    try:
        paths = find(release)
    except OSError:
        release.nfo_data = {**parse(""), "files": [], "records": [], "conflicts": [],
                            "warnings": ["az NFO-fájlokat nem sikerült felsorolni"]}
        return release.nfo_data
    if not paths:
        release.nfo_data = {}
        return {}
    merged = parse("")
    merged.update(files=[], records=[], warnings=[], conflicts=[])
    root = Path(release.path)
    base = root if root.is_dir() else root.parent
    total = 0
    blocked: set[tuple[str, str]] = set()
    for index, path in enumerate(paths):
        if index >= 64 or total >= 8 * 1024 * 1024:
            merged["warnings"].append("az NFO-feldolgozás összesített korlátja betelt; további NFO-k maradtak ki")
            break
        label = str(path.relative_to(base))
        try:
            blob = safeio.read_file(path, limit=min(2_000_000, 8 * 1024 * 1024 - total))
            total += len(blob)
            text = decode(blob)
            data = parse(text)
        except (OSError, ValueError):
            merged["warnings"].append(f"az NFO nem olvasható vagy túllépi a méretkorlátot: {label}")
            continue
        merged["files"].append(label)
        merged["records"].append({"file": label, "text": text, "data": data})
        for key, value in data.items():
            if key == "urls":
                merged[key] = list(dict.fromkeys(merged[key] + value))
                continue
            entries = value.items() if isinstance(value, dict) else [("", value)]
            target = merged[key] if isinstance(value, dict) else merged
            for sub, item in entries:
                dest, token = sub or key, (key, sub)
                if not item or token in blocked:
                    continue
                old = target.get(dest)
                if old and old != item:
                    if path.suffix.lower() == ".diz":
                        continue  # a brief file_id.diz only completes the NFO
                    # Different prose / installation sections need review too;
                    # do not splice instructions from unrelated releases.
                    target[dest] = {} if isinstance(item, dict) else [] if isinstance(item, list) else ""
                    blocked.add(token)
                    merged["conflicts"].append(key + (":" + sub if sub else ""))
                else:
                    target[dest] = item
    for label, pattern in (("IMDb", r"imdb\.com/(?:[a-z]{2}/)?title/(tt\d{5,10})"),
                           ("MusicBrainz", r"musicbrainz\.org/release/([0-9a-f-]{36})"),
                           ("Discogs", r"discogs\.com/(?:[^/\s]+/)?release/(\d+)"),
                           ("Steam", r"store\.steampowered\.com/app/(\d+)")):
        ids = {m.group(1).lower() for r in merged["records"] for m in re.finditer(pattern, r["text"], re.I)}
        if len(ids) > 1:
            merged["conflicts"].append(label)
    release.nfo_data = merged
    return merged


def text(release) -> str:
    return "\n".join(r["text"] for r in read_release(release).get("records", []))


def value(release, *labels: str) -> str:
    """Unambiguous labelled value (including aliases in different NFOs)."""
    wanted = {re.sub(r"[\W_]+", "", s.casefold()) for s in labels}
    records = read_release(release).get("records", [])
    found = {v.strip() for r in records if Path(r["file"]).suffix.lower() == ".nfo"
             for k, v in r["data"]["fields"].items() if re.sub(r"[\W_]+", "", k.casefold()) in wanted and v.strip()}
    if not found:
        found = {v.strip() for r in records for k, v in r["data"]["fields"].items()
                 if re.sub(r"[\W_]+", "", k.casefold()) in wanted and v.strip()}
    return next(iter(found)) if len(found) == 1 else ""


def unique_match(release, pattern: re.Pattern) -> str:
    found = {m.group(1).strip() for r in read_release(release).get("records", []) for m in pattern.finditer(r["text"])}
    return next(iter(found)) if len(found) == 1 else ""


def year(release) -> str:
    # A scene's release/pre/rip date is not the work's publication year.
    raw = value(release, "year", "original year", "publication year", "published year", "év", "megjelenés éve")
    return raw if re.fullmatch(r"(?:18|19|20)\d{2}", raw) else ""


def track_titles(release) -> list[str]:
    """An unambiguous numbered music list; file counts/positions are checked by the caller."""
    lists = set()
    pattern = re.compile(r"^\s*(\d{1,2})[.)]\s+(.+?)\s+(?:\.{2,}\s*)?\d{1,2}:\d{2}\s*$")
    for record in read_release(release).get("records", []):
        rows = [(int(m[1]), m[2].rstrip(" .")) for line in strip_art(record["text"]).splitlines() if (m := pattern.match(line))]
        if rows and [number for number, _ in rows] == list(range(1, len(rows) + 1)):
            lists.add(tuple(title for _, title in rows))
    return list(next(iter(lists))) if len(lists) == 1 else []


def annotate(meta, release) -> dict:
    """Expose processing/provenance and uncertainty, without raising confidence."""
    data = read_release(release)
    if not data:
        return data
    if data["files"]:
        meta.add_source("NFO")
    meta.extra["nfo_files"] = list(data["files"])
    for message in data["warnings"]:
        if message not in meta.warnings:
            meta.warnings.append(message)
    if data["conflicts"]:
        message = "eltérő NFO-adatok; az ütköző mezőket kihagytam: " + ", ".join(data["conflicts"])
        if message not in meta.warnings:
            meta.warnings.append(message)
        reasons = meta.extra.setdefault("review_reasons", [])
        if message not in reasons:
            reasons.append(message)
    return data


def description(meta, release) -> None:
    """Use an explicit NFO description only when other sources supplied none."""
    if meta.description:
        return
    data = read_release(release)
    prose = data.get("sections", {}).get("description", "") or value(release, "description", "summary", "fülszöveg")
    if len(prose) < 40:
        return
    from .. import translate
    if translate.is_hungarian(prose):
        meta.description_hu = paragraphs(prose)
    else:
        meta.description_en = paragraphs(prose)
        translated, done = translate.to_hungarian(meta.description_en)
        if done:
            meta.description_hu, meta.machine_translated = translated, True
        else:
            meta.warnings.append("nincs magyar leírás: az NFO angol szövegét használtam (a fordítás nem elérhető)")
    meta.description_source = "NFO"
