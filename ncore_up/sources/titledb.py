"""Nintendo Switch titles: blawar/titledb (GitHub, MIT) indexed locally, plus the Nintendo of Europe search (official
nintendo.com page as proof link, packshot, product code, languages).

``<REGION>.<lang>.json`` (~86 MB, keyed by nsuId) is downloaded at most once a day into ``net.cache_dir()/titledb/``,
streamed to disk and read item by item (never loaded whole) into an SQLite index: title id → name, publisher,
developer, description, intro, release date, region, icon/banner, screenshots, nsuId, latest version, languages,
category, rating. ``versions.json`` adds the update list (version → date). The HU eShop has no Hungarian text: its
descriptions are English. A title missing from HU.en is looked up in US.en (downloaded only then).

The helpers ``fetch_file`` (daily/weekly dumps with ETag revalidation, streamed, atomic) and ``JsonStream``
(item-by-item reading of huge JSON objects) are shared by the other dump-based sources (gametdb, libretro_dat, fdroid).
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sqlite3
import time
from pathlib import Path

from .. import net

RAW = "https://raw.githubusercontent.com/blawar/titledb/master/"
REGIONS = ("HU.en", "US.en")
NOE_SEARCH = "https://searching.nintendo-europe.com/en/select"
NOE_SITE = "https://www.nintendo.com"
DAY = 86400.0


# --- shared dump helpers ------------------------------------------------------------------------------------------

def fetch_file(url: str, dest: Path, *, max_age_days: float = 1.0, min_size: int = 1, timeout: int = 300) -> Path | None:
    """Keep ``dest`` a fresh copy of ``url``: re-download (ETag/Last-Modified revalidation) when older than
    ``max_age_days``. Streams to a temporary file and renames it atomically, bypassing the HTTP cache. A stale copy is
    returned when the refresh fails; None when there is no copy at all."""

    dest.parent.mkdir(parents=True, exist_ok=True)
    stamp = dest.with_name(dest.name + ".etag")
    if dest.exists() and time.time() - dest.stat().st_mtime < max_age_days * DAY and dest.stat().st_size >= min_size:
        return dest
    headers = {}
    if dest.exists() and stamp.exists():
        try:
            saved = json.loads(stamp.read_text(encoding="utf-8"))
            if saved.get("etag"):
                headers["If-None-Match"] = saved["etag"]
            if saved.get("modified"):
                headers["If-Modified-Since"] = saved["modified"]
        except (OSError, ValueError):
            pass
    session = net.session()
    disabled = session.cache_disabled() if hasattr(session, "cache_disabled") else contextlib.nullcontext()
    temp = dest.with_name(f"{dest.name}.{os.getpid()}.part")
    try:
        with disabled, session.get(url, headers=headers, stream=True, timeout=(net.TIMEOUT, timeout)) as response:
            if response.status_code == 304 and dest.exists():
                os.utime(dest)
                return dest
            if response.status_code != 200:
                raise OSError(f"HTTP {response.status_code}")
            with open(temp, "wb") as sink:
                for chunk in response.iter_content(1 << 20):
                    sink.write(chunk)
            if temp.stat().st_size < min_size:
                raise OSError("túl kicsi letöltés")
            os.replace(temp, dest)
            stamp.write_text(json.dumps({"etag": response.headers.get("ETag", ""), "modified": response.headers.get("Last-Modified", "")}),
                             encoding="utf-8")
            return dest
    except Exception as exc:  # noqa: BLE001 - network, HTTP or disk problem: keep the old copy
        net.FAILURES.append(f"{url.split('/')[2]}: letöltés sikertelen ({type(exc).__name__}: {exc})"[:200])
        with contextlib.suppress(OSError):
            temp.unlink()
        return dest if dest.exists() and dest.stat().st_size >= min_size else None


class JsonStream:
    """Reads the members of one (possibly nested) JSON object of a huge document one at a time:
    ``JsonStream(fp).items(("packages",))`` yields (key, value) of ``document["packages"]``."""

    def __init__(self, fp, chunk: int = 1 << 20):
        self.fp, self.chunk = fp, chunk
        self.buf, self.pos, self.eof = "", 0, False
        self.decoder = json.JSONDecoder()

    def _fill(self) -> bool:
        if self.eof:
            return False
        data = self.fp.read(self.chunk)
        if not data:
            self.eof = True
            return False
        self.buf = self.buf[self.pos:] + data
        self.pos = 0
        return True

    def _peek(self) -> str:
        while True:
            while self.pos < len(self.buf) and self.buf[self.pos] in " \t\r\n":
                self.pos += 1
            if self.pos < len(self.buf):
                return self.buf[self.pos]
            if not self._fill():
                return ""

    def _expect(self, char: str) -> None:
        if self._peek() != char:
            raise ValueError(f"JSON: '{char}' expected at {self.pos}")
        self.pos += 1

    def value(self):
        while True:
            self._peek()
            try:
                obj, end = self.decoder.raw_decode(self.buf, self.pos)
            except json.JSONDecodeError:
                if not self._fill():
                    raise ValueError("JSON: truncated document") from None
                continue
            if end >= len(self.buf) and not self.eof and self._fill():
                continue  # a number or literal may continue in the next chunk
            self.pos = end
            return obj

    def items(self, path: tuple[str, ...] = ()):
        self._expect("{")
        if self._peek() == "}":
            self.pos += 1
            return
        while True:
            key = self.value()
            self._expect(":")
            if path and key == path[0]:
                yield from self.items(path[1:])
                return
            if path:
                self.value()
            else:
                yield key, self.value()
            following = self._peek()
            self.pos += 1
            if following == "}":
                return
            if following != ",":
                raise ValueError(f"JSON: ',' expected at {self.pos}")


def open_db(path: Path, schema: str) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=60)
    db.row_factory = sqlite3.Row
    db.executescript(schema)
    return db


def norm(text: str) -> str:
    return re.sub(r"[^0-9a-z]+", " ", (text or "").lower().replace("&", " and ")).strip()


# --- titledb ------------------------------------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS titles (titleid TEXT, region TEXT, nsuid TEXT, name TEXT, name_norm TEXT, publisher TEXT,
  developer TEXT, description TEXT, intro TEXT, release_date TEXT, icon_url TEXT, banner_url TEXT, screenshots TEXT,
  languages TEXT, category TEXT, rating TEXT, size INTEGER, version INTEGER, is_demo INTEGER, PRIMARY KEY (titleid, region));
CREATE INDEX IF NOT EXISTS titles_name ON titles (name_norm);
CREATE TABLE IF NOT EXISTS versions (titleid TEXT PRIMARY KEY, data TEXT);
CREATE TABLE IF NOT EXISTS built (name TEXT PRIMARY KEY, mtime REAL);
"""


def _dir() -> Path:
    return net.cache_dir() / "titledb"


def parse_entry(entry: dict) -> dict:
    """One titledb record → {"title_id", "nsu_id", "name", "publisher", "developer", "description", "intro",
    "release_date" (YYYY-MM-DD), "icon_url", "banner_url", "screenshots", "languages", "category", "rating", "size",
    "version", "is_demo"}; {} for records without a title id."""

    title_id = str(entry.get("id") or "").upper()
    if not re.fullmatch(r"[0-9A-F]{16}", title_id):
        return {}
    date = str(entry.get("releaseDate") or "")
    if re.fullmatch(r"\d{8}", date):
        date = f"{date[:4]}-{date[4:6]}-{date[6:]}"
    rating = entry.get("rating")
    size = entry.get("size")

    def text(key: str) -> str:  # one odd record must not break the whole index build
        value = entry.get(key)
        return value.strip() if isinstance(value, str) else ""

    def strings(key: str) -> list:
        value = entry.get(key)
        return [x for x in value if isinstance(x, (str, int))] if isinstance(value, list) else []

    return {
        "title_id": title_id,
        "nsu_id": str(entry.get("nsuId") or ""),
        "name": text("name"),
        "publisher": text("publisher"),
        "developer": text("developer"),
        "description": text("description"),
        "intro": text("intro"),
        "release_date": date,
        "icon_url": text("iconUrl"),
        "banner_url": text("bannerUrl"),
        "screenshots": [s for s in strings("screenshots") if isinstance(s, str)],
        "languages": [str(x).lower() for x in strings("languages")],
        "category": [str(x) for x in strings("category")],
        "rating": f"{rating}+" if isinstance(rating, int) and rating else "",
        "size": size if isinstance(size, int) and not isinstance(size, bool) and 0 <= size < 1 << 62 else 0,
        "version": entry.get("version") if isinstance(entry.get("version"), int) else None,
        "is_demo": bool(entry.get("isDemo")),
    }


def build_index(db: sqlite3.Connection, json_path: Path, region: str) -> int:
    """(Re)load one region file into the index, streaming it; returns the number of titles."""

    count = 0
    with db:
        db.execute("DELETE FROM titles WHERE region = ?", (region,))
        with open(json_path, encoding="utf-8") as handle:
            for _key, entry in JsonStream(handle).items():
                item = parse_entry(entry) if isinstance(entry, dict) else {}
                if not item:
                    continue
                db.execute(
                    "INSERT OR REPLACE INTO titles VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (item["title_id"], region, item["nsu_id"], item["name"], norm(item["name"]), item["publisher"], item["developer"],
                     item["description"], item["intro"], item["release_date"], item["icon_url"], item["banner_url"],
                     json.dumps(item["screenshots"]), json.dumps(item["languages"]), json.dumps(item["category"]), item["rating"],
                     item["size"], item["version"], int(item["is_demo"])),
                )
                count += 1
        db.execute("INSERT OR REPLACE INTO built VALUES (?, ?)", (region, json_path.stat().st_mtime))
    return count


def build_versions(db: sqlite3.Connection, json_path: Path) -> None:
    with db:
        db.execute("DELETE FROM versions")
        with open(json_path, encoding="utf-8") as handle:
            for key, versions in JsonStream(handle).items():
                if isinstance(versions, dict):
                    db.execute("INSERT OR REPLACE INTO versions VALUES (?, ?)", (str(key).upper(), json.dumps(versions)))
        db.execute("INSERT OR REPLACE INTO built VALUES (?, ?)", ("versions", json_path.stat().st_mtime))


def _ensure(db: sqlite3.Connection, name: str) -> bool:
    """Download (daily) and index one titledb file ("HU.en" or "versions"); False when unavailable."""

    path = fetch_file(RAW + f"{name}.json", _dir() / f"{name}.json", max_age_days=1.0, min_size=1000)
    if path is None:
        return False
    row = db.execute("SELECT mtime FROM built WHERE name = ?", (name,)).fetchone()
    if row is None or abs(row["mtime"] - path.stat().st_mtime) > 1:
        try:
            if name == "versions":
                build_versions(db, path)
            else:
                build_index(db, path, name)
        except (ValueError, OSError, sqlite3.Error) as exc:
            net.FAILURES.append(f"titledb: {name} nem dolgozható fel ({type(exc).__name__})")
            return False
    return True


def _db() -> sqlite3.Connection:
    return open_db(_dir() / "index.sqlite", SCHEMA)


def _row(row: sqlite3.Row) -> dict:
    out = dict(row)
    for key in ("screenshots", "languages", "category"):
        out[key] = json.loads(out.get(key) or "[]")
    out["title_id"] = out.pop("titleid")
    out["nsu_id"] = out.pop("nsuid")
    out.pop("name_norm", None)
    return out


def lookup(title_id: str) -> dict:
    """The titledb record of a Switch title (an update resolves to its game, a DLC to its own record or else its game)
    plus "versions": {version int: date}; {} when unknown or the dump is unavailable."""

    title_id = (title_id or "").upper()
    if not re.fullmatch(r"[0-9A-F]{16}", title_id):
        return {}
    try:
        return _lookup(title_id)
    except (sqlite3.Error, OSError, ValueError) as exc:  # locked/corrupt index, disk problem
        net.FAILURES.append(f"titledb: az index nem olvasható ({type(exc).__name__})")
        return {}


def _lookup(title_id: str) -> dict:
    db = _db()
    base = f"{int(title_id, 16) & ~0xFFF:016X}"
    if int(title_id, 16) & 0xFFF not in (0, 0x800):  # DLC: own record first, then its game
        base = f"{(int(title_id, 16) - 0x1000) & ~0xFFF:016X}"
    wanted = [title_id] if title_id == base else [title_id, base]
    with contextlib.closing(db):
        record: dict = {}
        for region in REGIONS:
            if not _ensure(db, region):
                continue
            for candidate in wanted:
                row = db.execute("SELECT * FROM titles WHERE titleid = ? AND region = ?", (candidate, region)).fetchone()
                if row:
                    record = _row(row)
                    break
            if record:
                break
        if not record:
            return {}
        if _ensure(db, "versions"):
            row = db.execute("SELECT data FROM versions WHERE titleid = ?", (record["title_id"],)).fetchone()
            data = json.loads(row["data"]) if row else {}
            record["versions"] = {int(k): str(v) for k, v in data.items() if str(k).isdigit()} if isinstance(data, dict) else {}
        return record


def search(name: str, limit: int = 5) -> list[dict]:
    """Titles whose normalized name equals (then contains) ``name``, from the already built index only."""

    wanted = norm(name)
    if not wanted:
        return []
    try:
        db = _db()
        with contextlib.closing(db):
            if not db.execute("SELECT 1 FROM built WHERE name = ?", (REGIONS[0],)).fetchone():
                _ensure(db, REGIONS[0])
            rows = db.execute("SELECT * FROM titles WHERE name_norm = ? AND titleid LIKE '%000' LIMIT ?", (wanted, limit)).fetchall()
            if not rows:
                rows = db.execute("SELECT * FROM titles WHERE name_norm LIKE ? AND titleid LIKE '%000' ORDER BY length(name_norm) LIMIT ?",
                                  (f"%{wanted}%", limit)).fetchall()
            return [_row(r) for r in rows]
    except (sqlite3.Error, OSError, ValueError):
        return []


# --- Nintendo of Europe search -------------------------------------------------------------------------------------

_NOE_LANGS = {"english": "en", "german": "de", "french": "fr", "spanish": "es", "italian": "it", "dutch": "nl", "russian": "ru",
              "portuguese": "pt", "japanese": "ja", "korean": "ko", "chinese": "zh", "polish": "pl", "hungarian": "hu",
              "czech": "cs", "swedish": "sv", "danish": "da", "norwegian": "no", "finnish": "fi", "greek": "el", "turkish": "tr"}


def parse_noe(data: dict) -> list[dict]:
    """NoE Solr response → [{"title", "url", "product_code", "nsu_ids", "publisher", "developer", "excerpt", "image_url",
    "release_date", "categories", "languages", "age_rating"}]."""

    out = []
    for doc in ((data or {}).get("response") or {}).get("docs") or []:
        languages = []
        for text in doc.get("language_availability") or []:
            for word in re.split(r"[,/]", str(text)):
                code = _NOE_LANGS.get(word.strip().lower())
                if code and code not in languages:
                    languages.append(code)
        url = str(doc.get("url") or "")
        codes = [str(p) for p in doc.get("product_code_txt") or []]
        out.append({
            "title": doc.get("title", ""),
            "url": (NOE_SITE + url) if url.startswith("/") else url,
            "product_code": next((p for p in codes if p.startswith(("HAC", "CTR", "KTR", "WUP", "RVL"))), codes[0] if codes else ""),
            "product_codes": codes,
            "systems": list(doc.get("system_names_txt") or []),
            "nsu_ids": [str(x) for x in doc.get("nsuid_txt") or []],
            "publisher": doc.get("publisher", ""),
            "developer": doc.get("developer", ""),
            "excerpt": (doc.get("excerpt") or "").strip(),
            "image_url": doc.get("image_url", ""),
            "release_date": ((doc.get("dates_released_dts") or [""])[0] or "")[:10],
            "categories": list(doc.get("pretty_game_categories_txt") or []),
            "languages": languages,
            "age_rating": f"PEGI {doc['age_rating_value']}" if doc.get("age_rating_value") else "",
        })
    return out


NOE_SYSTEMS = {"switch": "nintendoswitch*", "3ds": "3ds*", "wii": "wii_*", "wiiu": "wiiu*"}


def noe_search(*, nsu_id: str = "", title: str = "", product_code: str = "", system: str = "switch") -> dict:
    """Best NoE catalogue record for an nsuId or a product code ("CTR-P-AREP", exact) or a title on one system
    (switch, 3ds, wii, wiiu; the normalized title must match); {} when none."""

    code = re.sub(r"[^A-Z0-9]", "", (product_code or "").upper())
    if nsu_id and re.fullmatch(r"\d{14}", nsu_id):
        params = {"q": "*", "fq": f"type:GAME AND nsuid_txt:{nsu_id}", "rows": 1, "wt": "json"}
    elif re.fullmatch(r"[A-Z]{4}[A-Z0-9]{4,5}", code):
        params = {"q": "*", "fq": f"type:GAME AND product_code_txt:{code}", "rows": 1, "wt": "json"}
    elif title and system in NOE_SYSTEMS:
        params = {"q": re.sub(r"[^\w\s'&-]", " ", title).strip(), "fq": f"type:GAME AND system_type:{NOE_SYSTEMS[system]}",
                  "rows": 5, "wt": "json"}
    else:
        return {}
    hits = parse_noe(net.get_json(NOE_SEARCH, params=params, quiet=True) or {})
    if params["q"] != "*":
        hits = [h for h in hits if norm(h["title"]) == norm(title)]
    return hits[0] if hits else {}


def product_code_id(product_code: str) -> str:
    """"HACPAAACA" / "HAC-P-AAACA" → "AAACA" (the GameTDB Switch id)."""

    code = re.sub(r"[^A-Z0-9]", "", (product_code or "").upper())
    return code[4:] if code.startswith("HAC") and len(code) >= 9 else ""
