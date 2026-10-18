"""GameTDB (www.gametdb.com) English databases, cached daily and indexed locally: Wii + WiiWare + GameCube (ID6),
3DS (4-character product id, "AREP" of CTR-P-AREP), Switch (5-character, "AAACA" of HAC-P-AAACA), PS3 (title id
"BLES01807"), Wii U. Per game: title, synopsis, region (PAL/NTSC-U/NTSC-J/ALL), languages, developer, publisher,
release date, genres, rating. Box art comes from art.gametdb.com; the game page is a proof link.

The server is overloaded (502/500, ~60 s HTML pages), so only the zipped dumps are used (``net`` retries 5xx) and a
stale copy is kept when a refresh fails. FAQ: free for anyone to use in game-related projects.

robots.txt (checked 2026-10-16) disallows ``/*tdb.zip`` on www.gametdb.com and every file on art.gametdb.com for all
agents. So by default nothing is downloaded from GameTDB: a dump the user saved by hand into
``~/.cache/ncore-up/gametdb/<name>.zip`` (e.g. wiitdb.zip from https://www.gametdb.com/wiitdb.zip?LANG=EN&WIIWARE=1&GAMECUBE=1)
is used as it is. Automatic downloads and box-art checks require the site's robots.txt to allow them;
no environment variable overrides that decision. ``WARNING`` explains when a manual dump is missing.

The 3DS title id → product code bridge uses hax0kartik/3dsdb (static eShop dumps on GitHub, weekly cache).
"""

from __future__ import annotations

import contextlib
import json
import re
import sqlite3
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from .. import net
from .googleplay import robots_allows
from .titledb import fetch_file, open_db

WARNING = ""


def download_allowed(url: str) -> bool:
    """True only when the site's robots.txt allows the URL."""

    return robots_allows(url)

BASE = "https://www.gametdb.com/"
DBS = {  # platform -> (dump name, query, page folder, art folder)
    "wii": ("wiitdb", "LANG=EN&WIIWARE=1&GAMECUBE=1", "Wii", "wii"),
    "3ds": ("3dstdb", "LANG=EN", "3DS", "3ds"),
    "switch": ("switchtdb", "LANG=EN", "Switch", "switch"),
    "ps3": ("ps3tdb", "LANG=EN", "PS3", "ps3"),
    "wiiu": ("wiiutdb", "LANG=EN", "WiiU", "wiiu"),
}
DB_OF = {"gc": "wii", "wii": "wii", "3ds": "3ds", "switch": "switch", "ps3": "ps3", "wiiu": "wiiu"}
N3DSDB = "https://raw.githubusercontent.com/hax0kartik/3dsdb/master/jsons/list_{}.json"

SCHEMA = """
CREATE TABLE IF NOT EXISTS games (db TEXT, id TEXT, type TEXT, region TEXT, languages TEXT, title TEXT, synopsis TEXT,
  developer TEXT, publisher TEXT, date TEXT, genres TEXT, rating TEXT, name TEXT, PRIMARY KEY (db, id));
CREATE TABLE IF NOT EXISTS n3ds (titleid TEXT PRIMARY KEY, product_code TEXT, name TEXT, publisher TEXT, list TEXT);
CREATE TABLE IF NOT EXISTS built (name TEXT PRIMARY KEY, mtime REAL);
"""


def _dir() -> Path:
    return net.cache_dir() / "gametdb"


def parse_game(element: ET.Element) -> dict:
    """One <game> element → {"id", "type", "region", "languages", "title", "synopsis", "developer", "publisher", "date",
    "genres", "rating", "name"}; the English locale is preferred."""

    locales = {loc.get("lang", ""): loc for loc in element.findall("locale")}
    locale = locales.get("EN")
    if locale is None:
        locale = next(iter(locales.values()), None)
    date = element.find("date")
    when = ""
    if date is not None and (date.get("year") or "").isdigit():
        when = date.get("year", "")
        if (date.get("month") or "").isdigit():
            when += f"-{int(date.get('month')):02d}"
            if (date.get("day") or "").isdigit():
                when += f"-{int(date.get('day')):02d}"
    rating = element.find("rating")
    return {
        "id": (element.findtext("id") or "").strip(),
        "type": (element.findtext("type") or "").strip(),
        "region": (element.findtext("region") or "").strip(),
        "languages": [x.lower() for x in (element.findtext("languages") or "").split(",") if x.strip()],
        "title": (locale.findtext("title") if locale is not None else "") or "",
        "synopsis": ((locale.findtext("synopsis") if locale is not None else "") or "").strip(),
        "developer": (element.findtext("developer") or "").strip(),
        "publisher": (element.findtext("publisher") or "").strip(),
        "date": when,
        "genres": [g.strip() for g in (element.findtext("genre") or "").split(",") if g.strip()],
        "rating": f"{rating.get('type')} {rating.get('value')}".strip() if rating is not None and rating.get("value") else "",
        "name": element.get("name", ""),
    }


def iter_games(xml_file):
    """Stream the <game> elements of a GameTDB XML file object (memory stays small)."""

    for _event, element in ET.iterparse(xml_file, events=("end",)):
        if element.tag == "game":
            yield parse_game(element)
            element.clear()


def build_index(db: sqlite3.Connection, name: str, zip_path: Path) -> int:
    count = 0
    with db, zipfile.ZipFile(zip_path) as archive:
        member = next((i for i in archive.infolist() if i.filename.lower().endswith(".xml")), None)
        if member is None:
            raise ValueError("no XML in the GameTDB zip")
        db.execute("DELETE FROM games WHERE db = ?", (name,))
        with archive.open(member) as handle:
            for game in iter_games(handle):
                if not game["id"]:
                    continue
                db.execute("INSERT OR REPLACE INTO games VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (name, game["id"].upper(), game["type"], game["region"], json.dumps(game["languages"]), game["title"],
                            game["synopsis"], game["developer"], game["publisher"], game["date"], json.dumps(game["genres"]),
                            game["rating"], game["name"]))
                count += 1
        db.execute("INSERT OR REPLACE INTO built VALUES (?, ?)", (name, zip_path.stat().st_mtime))
    return count


def _ensure(db: sqlite3.Connection, platform: str) -> str:
    global WARNING
    key = DB_OF.get(platform, "")
    if not key:
        return ""
    dump, query, _page, _art = DBS[key]
    url, dest = f"{BASE}{dump}.zip?{query}", _dir() / f"{dump}.zip"
    if download_allowed(url):
        path = fetch_file(url, dest, max_age_days=1.0, min_size=1000)
    else:
        path = dest if dest.exists() and dest.stat().st_size >= 100 else None
        if path is None:
            WARNING = (f"GameTDB: a robots.txt tiltja a(z) {dump}.zip automatikus letöltését — ha szeretnéd, töltsd le kézzel "
                       f"({url}) ide: {dest}")
    if path is None:
        return ""
    row = db.execute("SELECT mtime FROM built WHERE name = ?", (dump,)).fetchone()
    if row is None or abs(row["mtime"] - path.stat().st_mtime) > 1:
        try:
            build_index(db, dump, path)
        except (ValueError, OSError, ET.ParseError, zipfile.BadZipFile, sqlite3.Error) as exc:
            net.FAILURES.append(f"GameTDB: {dump} nem dolgozható fel ({type(exc).__name__})")
            return ""
    return dump


def _row(row: sqlite3.Row) -> dict:
    out = dict(row)
    out["languages"] = json.loads(out.get("languages") or "[]")
    out["genres"] = json.loads(out.get("genres") or "[]")
    out.pop("db", None)
    return out


def lookup(platform: str, game_id: str) -> dict:
    """GameTDB record of an ID6 (Wii/GC), 3DS/Switch product id or PS3 title id; for Wii/GC an unknown ID6 falls back
    to the same game code with another maker/region. {} when unknown."""

    global WARNING
    game_id = re.sub(r"[^A-Z0-9]", "", (game_id or "").upper())
    if not game_id:
        return {}
    WARNING = ""
    try:
        db = open_db(_dir() / "index.sqlite", SCHEMA)
        with contextlib.closing(db):
            dump = _ensure(db, platform)
            if not dump:
                return {}
            row = db.execute("SELECT * FROM games WHERE db = ? AND id = ?", (dump, game_id)).fetchone()
            if row is None and platform in {"wii", "gc"} and len(game_id) == 6:
                row = db.execute("SELECT * FROM games WHERE db = ? AND id LIKE ? ORDER BY id LIMIT 1", (dump, game_id[:4] + "%")).fetchone()
            if row is None and platform == "switch" and len(game_id) == 5:
                row = db.execute("SELECT * FROM games WHERE db = ? AND id LIKE ? ORDER BY id LIMIT 1", (dump, game_id[:4] + "%")).fetchone()
            return _row(row) if row else {}
    except (sqlite3.Error, OSError, ValueError) as exc:  # locked/corrupt index, disk problem
        net.FAILURES.append(f"GameTDB: az index nem olvasható ({type(exc).__name__})")
        return {}


def cover_folders(region: str) -> list[str]:
    """art.gametdb.com language folders to try for a GameTDB/ID region."""

    region = (region or "").upper()
    if region in {"NTSC-U", "USA"}:
        return ["US", "EN"]
    if region in {"NTSC-J", "JPN"}:
        return ["JA", "US", "EN"]
    if region in {"NTSC-K", "ASIA"}:
        return ["KO", "ZH", "US", "EN"]
    return ["EN", "US"]


def cover_url(platform: str, game_id: str, folder: str = "EN") -> str:
    key = DB_OF.get(platform, "")
    if not key or not game_id:
        return ""
    art = DBS[key][3]
    kind, ext = {"wii": ("cover", "png"), "3ds": ("box", "png"), "switch": ("coverM", "jpg"), "ps3": ("cover", "jpg"),
                 "wiiu": ("cover", "jpg")}[key]
    return f"https://art.gametdb.com/{art}/{kind}/{folder}/{game_id}.{ext}"


def find_cover(platform: str, game_id: str, region: str = "") -> str:
    """First existing box-art URL (HEAD requests, in region order); '' when none or when robots.txt forbids it."""

    for folder in cover_folders(region):
        url = cover_url(platform, game_id, folder)
        if not url or not download_allowed(url):
            return ""
        if net.request("HEAD", url, quiet=True, retries=1) is not None:
            return url
    return ""


def page_url(platform: str, game_id: str) -> str:
    key = DB_OF.get(platform, "")
    return f"{BASE}{DBS[key][2]}/{game_id}" if key and game_id else ""


# --- 3DS title id -> product code ----------------------------------------------------------------------------------

def parse_3dsdb(items: list) -> list[dict]:
    out = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        title_id = str(item.get("TitleID") or "").upper()
        if re.fullmatch(r"[0-9A-F]{16}", title_id):
            out.append({"title_id": title_id, "product_code": str(item.get("Product Code") or ""), "name": str(item.get("Name") or ""),
                        "publisher": str(item.get("Publisher") or "")})
    return out


def product_code_3ds(title_id: str) -> dict:
    """{"title_id", "product_code", "name", "publisher"} of a 3DS eShop title from 3dsdb (GB, US, JP lists)."""

    title_id = (title_id or "").upper()
    if not re.fullmatch(r"[0-9A-F]{16}", title_id):
        return {}
    try:
        return _product_code_3ds(title_id)
    except (sqlite3.Error, OSError) as exc:  # locked/corrupt index, disk problem
        net.FAILURES.append(f"3dsdb: az index nem olvasható ({type(exc).__name__})")
        return {}


def _product_code_3ds(title_id: str) -> dict:
    db = open_db(_dir() / "index.sqlite", SCHEMA)
    with contextlib.closing(db):
        for region in ("GB", "US", "JP"):
            row = db.execute("SELECT * FROM n3ds WHERE titleid = ?", (title_id,)).fetchone()
            if row:
                return {"title_id": row["titleid"], "product_code": row["product_code"], "name": row["name"], "publisher": row["publisher"]}
            name = f"3dsdb-{region}"
            path = fetch_file(N3DSDB.format(region), _dir() / f"{name}.json", max_age_days=7.0, min_size=100)
            if path is None:
                continue
            built = db.execute("SELECT mtime FROM built WHERE name = ?", (name,)).fetchone()
            if built is None or abs(built["mtime"] - path.stat().st_mtime) > 1:
                try:
                    items = parse_3dsdb(json.loads(path.read_text(encoding="utf-8")))
                except (OSError, ValueError):
                    continue
                with db:
                    for item in items:
                        db.execute("INSERT OR IGNORE INTO n3ds VALUES (?,?,?,?,?)",
                                   (item["title_id"], item["product_code"], item["name"], item["publisher"], region))
                    db.execute("INSERT OR REPLACE INTO built VALUES (?, ?)", (name, path.stat().st_mtime))
        row = db.execute("SELECT * FROM n3ds WHERE titleid = ?", (title_id,)).fetchone()
        return {"title_id": row["titleid"], "product_code": row["product_code"], "name": row["name"], "publisher": row["publisher"]} if row else {}


def id_from_product_code(code: str) -> str:
    """"CTR-P-AREP" → "AREP", "KTR-N-CF6P" → "CF6P", "HAC-P-AAACA" → "AAACA"."""

    match = re.match(r"(?i)\s*[A-Z]{3}-[A-Z]-([A-Z0-9]{4,5})", code or "")
    return match.group(1).upper() if match else ""
