"""libretro-database DATs (GitHub, CC BY-SA 4.0): Redump/No-Intro game names with region, serial and ROM hashes,
cached weekly and indexed locally. A disc serial read from SYSTEM.CNF / PARAM.SFO ("SLES-50330", "BLES-01807"),
a Wii/GC game code ("RJ2P"), a 3DS product id ("AREP") or a PSN title id ("NPUB31857") gives the exact Redump/No-Intro
name, e.g. "Grand Theft Auto III (Europe) (En,Fr,De,Es,It) (v1.40)": title, region and the disc's languages.

Also: libretro-thumbnails URLs (box art, in-game snap, title screen) keyed by that exact name, and the Redump
quick-search page as a proof link for disc games.
"""

from __future__ import annotations

import contextlib
import re
import sqlite3
from pathlib import Path
from urllib.parse import quote

from .. import net
from .titledb import fetch_file, open_db

RAW = "https://raw.githubusercontent.com/libretro/libretro-database/master/metadat/"
SYSTEMS = {
    "ps1": "redump/Sony - PlayStation",
    "ps2": "redump/Sony - PlayStation 2",
    "ps3": "redump/Sony - PlayStation 3",
    "psp": "redump/Sony - PlayStation Portable",
    "gc": "redump/Nintendo - GameCube",
    "wii": "redump/Nintendo - Wii",
    "xbox": "redump/Microsoft - Xbox",
    "xbox360": "redump/Microsoft - Xbox 360",
    "3ds": "no-intro/Nintendo - Nintendo 3DS",
    "ps3psn": "no-intro/Sony - PlayStation 3 (PSN)",
    "vita": "no-intro/Sony - PlayStation Vita (PSN)",
}
CREDIT = "libretro-database (CC BY-SA 4.0)"
THUMBNAILS = "https://thumbnails.libretro.com/"

SCHEMA = """
CREATE TABLE IF NOT EXISTS games (system TEXT, key TEXT, name TEXT, region TEXT, serial TEXT, size INTEGER, crc TEXT, sha1 TEXT);
CREATE INDEX IF NOT EXISTS games_key ON games (system, key);
CREATE INDEX IF NOT EXISTS games_sha1 ON games (system, sha1);
CREATE TABLE IF NOT EXISTS built (name TEXT PRIMARY KEY, mtime REAL);
"""
_TOKEN_RE = re.compile(r'"((?:[^"\\]|\\.)*)"|(\()|(\))|([^\s()"]+)')


def _dir() -> Path:
    return net.cache_dir() / "libretro"


def _tokens(text: str):
    for match in _TOKEN_RE.finditer(text):
        if match.group(1) is not None:
            yield ("s", match.group(1))
        elif match.group(2):
            yield ("(", "")
        elif match.group(3):
            yield (")", "")
        else:
            yield ("s", match.group(4))


def _block(tokens) -> list[tuple[str, object]]:
    pairs: list[tuple[str, object]] = []
    key = None
    for kind, value in tokens:
        if kind == ")":
            return pairs
        if kind == "(":
            pairs.append((key or "", _block(tokens)))
            key = None
        elif key is None:
            key = value
        else:
            pairs.append((key, value))
            key = None
    return pairs


def parse_dat(text: str) -> list[dict]:
    """clrmamepro DAT → [{"name", "region", "serials", "roms": [{"name", "size", "crc", "md5", "sha1", "serial"}]}]."""

    games = []
    for key, value in _block(_tokens(text or "")):
        if key != "game" or not isinstance(value, list):
            continue
        game: dict = {"name": "", "region": "", "serials": [], "roms": []}
        for field, content in value:
            if field == "rom" and isinstance(content, list):
                rom = {k: v for k, v in content if isinstance(v, str)}
                game["roms"].append({"name": rom.get("name", ""), "size": int(rom["size"]) if rom.get("size", "").isdigit() else 0,
                                     "crc": rom.get("crc", "").upper(), "md5": rom.get("md5", "").upper(),
                                     "sha1": rom.get("sha1", "").upper(), "serial": rom.get("serial", "")})
            elif field == "serial" and isinstance(content, str):
                game["serials"] += [s.strip() for s in content.split(",") if s.strip()]
            elif field in {"name", "region"} and isinstance(content, str):
                game[field] = content
        games.append(game)
    return games


def serial_key(system: str, serial: str = "", rom_name: str = "") -> str:
    """Index key of a serial: PlayStation "SLES-50330", Wii/GC/3DS game code "RJ2P"/"AREP", PSN "NPUB-31857"."""

    serial = (serial or "").upper()
    if system in {"ps1", "ps2", "ps3", "psp"}:
        match = re.match(r"\s*([A-Z]{4})[-_ ]?(\d{3})[._]?(\d{2})", serial)
        return f"{match.group(1)}-{match.group(2)}{match.group(3)}" if match else ""
    if system in {"wii", "gc"}:
        match = re.search(r"(?:RVL|DOL)-([A-Z0-9]{4})", serial)
        return match.group(1) if match else (serial[:4] if re.fullmatch(r"[A-Z0-9]{4,6}", serial) else "")
    if system == "3ds":
        match = re.search(r"[A-Z]{3}-[A-Z]-([A-Z0-9]{4})", serial)
        return match.group(1) if match else (serial if re.fullmatch(r"[A-Z0-9]{4}", serial) else "")
    if system in {"ps3psn", "vita"}:
        match = re.search(r"[A-Z]{2}\d{4}-([A-Z]{4})(\d{5})_", (rom_name or serial).upper())
        if match:
            return f"{match.group(1)}-{match.group(2)}"
        match = re.match(r"\s*([A-Z]{4})-?(\d{5})", serial)
        return f"{match.group(1)}-{match.group(2)}" if match else ""
    return serial.strip()


def build_index(db: sqlite3.Connection, system: str, dat_path: Path) -> int:
    count = 0
    games = parse_dat(dat_path.read_text(encoding="utf-8", errors="replace"))
    with db:
        db.execute("DELETE FROM games WHERE system = ?", (system,))
        for game in games:
            serials = game["serials"] or [r["serial"] for r in game["roms"] if r["serial"]] or [""]
            rom = max(game["roms"], key=lambda r: r["size"]) if game["roms"] else {"name": "", "size": 0, "crc": "", "sha1": ""}
            keys = {serial_key(system, s, rom["name"]) for s in serials} - {""}
            for key in keys or {""}:
                db.execute("INSERT INTO games VALUES (?,?,?,?,?,?,?,?)",
                           (system, key, game["name"], game["region"], ", ".join(serials).strip(", "), rom["size"], rom["crc"], rom["sha1"]))
                count += 1
        db.execute("INSERT OR REPLACE INTO built VALUES (?, ?)", (system, dat_path.stat().st_mtime))
    return count


def _ensure(db: sqlite3.Connection, system: str) -> bool:
    if system not in SYSTEMS:
        return False
    path = fetch_file(RAW + quote(SYSTEMS[system]) + ".dat", _dir() / (SYSTEMS[system].split("/")[1] + ".dat"), max_age_days=7.0,
                      min_size=1000)
    if path is None:
        return False
    row = db.execute("SELECT mtime FROM built WHERE name = ?", (system,)).fetchone()
    if row is None or abs(row["mtime"] - path.stat().st_mtime) > 1:
        try:
            build_index(db, system, path)
        except (OSError, ValueError, sqlite3.Error) as exc:
            net.FAILURES.append(f"libretro-database: {system} nem dolgozható fel ({type(exc).__name__})")
            return False
    return True


def _query(system: str, sql: str, args: tuple) -> list[dict]:
    try:
        db = open_db(_dir() / "index.sqlite", SCHEMA)
        with contextlib.closing(db):
            if not _ensure(db, system):
                return []
            return [_entry(dict(r)) for r in db.execute(sql, args).fetchall()]
    except (sqlite3.Error, OSError) as exc:  # locked/corrupt index, disk problem
        net.FAILURES.append(f"libretro-database: az index nem olvasható ({type(exc).__name__})")
        return []


def _entry(row: dict) -> dict:
    row.pop("system", None)
    row["title"] = title_from_name(row["name"])
    row["languages"] = languages_from_name(row["name"])
    return row


def lookup(system: str, serial: str) -> list[dict]:
    """All DAT entries of a serial / game code, best first (more languages, newer revision last in name order)."""

    key = serial_key(system, serial)
    if not key:
        return []
    rows = _query(system, "SELECT * FROM games WHERE system = ? AND key = ? ORDER BY name", (system, key))
    unique = list({r["name"]: r for r in rows}.values())
    return sorted(unique, key=lambda r: (-len(r["languages"]), r["name"]))


def by_hash(system: str, *, sha1: str = "", crc: str = "", size: int = 0) -> list[dict]:
    if sha1:
        return _query(system, "SELECT * FROM games WHERE system = ? AND sha1 = ?", (system, sha1.upper()))
    if crc and size:
        return _query(system, "SELECT * FROM games WHERE system = ? AND crc = ? AND size = ?", (system, crc.upper(), size))
    return []


def languages_from_name(name: str) -> list[str]:
    """"... (Europe) (En,Fr,De,Es,It)" → ["en", "fr", "de", "es", "it"]."""

    for group in re.findall(r"\(([^)]*)\)", name or ""):
        if re.fullmatch(r"[A-Z][a-z](?:-[A-Z][a-z]+)?(?:,[A-Z][a-z](?:-[A-Z][a-z]+)?)*", group):
            return [g.split("-")[0].lower() for g in group.split(",")]
    return []


def region_from_name(name: str) -> str:
    groups = re.findall(r"\(([^)]*)\)", name or "")
    return groups[0] if groups else ""


_ARTICLES = ("The", "A", "An", "Der", "Die", "Das", "Le", "La", "Les", "L'", "El", "Il", "Lo", "Los", "Las")


def title_from_name(name: str) -> str:
    """"Pyramid, The (USA)" → "The Pyramid"; "Grand Theft Auto III (Europe) (En,Fr)" → "Grand Theft Auto III"."""

    title = re.sub(r"\s*[([][^)\]]*[)\]]", "", name or "").strip()
    match = re.match(r"^(.*?), (" + "|".join(re.escape(a) for a in _ARTICLES) + r")( - .*)?$", title)
    if match:
        title = f"{match.group(2)} {match.group(1)}{match.group(3) or ''}"
    return title


def thumbnail_url(system: str, name: str, kind: str = "Named_Boxarts") -> str:
    """libretro-thumbnails URL (Named_Boxarts / Named_Snaps / Named_Titles) of an exact DAT name."""

    if system not in SYSTEMS or not name:
        return ""
    folder = SYSTEMS[system].split("/")[1]
    safe = re.sub(r"[&*/:`<>?\\|\"]", "_", name)
    return f"{THUMBNAILS}{quote(folder)}/{kind}/{quote(safe)}.png"


def find_thumbnail(system: str, name: str, kind: str = "Named_Boxarts") -> str:
    """thumbnail_url() when the image exists (one HEAD request); ''."""

    url = thumbnail_url(system, name, kind)
    return url if url and net.request("HEAD", url, quiet=True, retries=1) is not None else ""


def redump_url(serial: str) -> str:
    """Redump quick-search page of a disc serial (proof link for verified disc dumps)."""

    serial = (serial or "").strip().upper()
    return f"http://redump.org/discs/quicksearch/{quote(serial)}/" if re.fullmatch(r"[A-Z0-9-]{5,20}", serial) else ""
