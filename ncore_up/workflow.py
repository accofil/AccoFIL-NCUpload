"""Persisted answers, private run state, upload reservations and batch preparation."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import tempfile
from pathlib import Path


class DailyLimitReached(ValueError):
    """A batch can stop before preparing further uploads once its quota is exhausted."""


def private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def write_json(path: Path, value) -> None:
    private_dir(path.parent)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        temp.chmod(0o600)
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def fingerprint(release) -> str:
    files = []
    for item in release.files:
        path = release.path / item.relpath if release.path.is_dir() else release.path
        stat = path.stat()
        files.append((item.relpath, stat.st_size, stat.st_mtime_ns, stat.st_ino))
    value = [str(release.path.resolve()), sorted(files)]
    return hashlib.sha256(json.dumps(value, ensure_ascii=False).encode()).hexdigest()


ANSWERS_DIR = Path.home() / ".config" / "ncore-up" / "answers"
# Files an answers sheet may never point at: they would end up in a public description or as an attachment.
SECRET_DIRS = (Path.home() / ".config" / "ncore-up", Path.home() / ".ssh", Path.home() / ".local" / "share" / "ncore-up",
               Path.home() / ".cache" / "ncore-up", Path.home() / "logs" / "ncore-up", Path.home() / ".config" / "qBittorrent")


def answers_path(source: Path) -> Path:
    """Where the answers sheet of a release lives: the user's own config folder, never next to downloaded content
    (a downloaded pack could otherwise bring its own sheet)."""

    return ANSWERS_DIR / (source.name + ".ncore.json")


def _safe_reference(path: Path) -> Path:
    resolved = path.resolve()
    name = resolved.name.casefold()
    if any(resolved.is_relative_to(folder.resolve()) and resolved != ANSWERS_DIR.resolve() and not resolved.is_relative_to(ANSWERS_DIR.resolve())
           for folder in SECRET_DIRS) or name.startswith("cookies") or name.endswith((".env", ".pem", ".key", ".sqlite3", ".torrent")):
        raise ValueError(f"az adatlap nem hivatkozhat erre a fájlra: {resolved}")
    return resolved


def load_answers(args) -> dict:
    source = Path(args.path).resolve()
    path = Path(args.answers).resolve() if args.answers else answers_path(source)
    if not path.exists() and not args.answers:
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("az adatlap JSON objektum legyen")
    allowed = {"category", "original", "imdb", "other_link", "name", "description", "source", "extras", "menu", "tags",
               "images", "cover", "proof_links", "install_guide", "hints", "dupe_decisions"}
    if set(data) - allowed:
        raise ValueError("ismeretlen adatlapmező: " + ", ".join(sorted(set(data) - allowed)))
    for key in ("original",):
        if key in data and not isinstance(data[key], bool):
            raise ValueError(f"{key}: true/false kell")
    for key in ("images", "proof_links", "tags"):
        if key in data and (not isinstance(data[key], list) or any(not isinstance(x, str) for x in data[key])):
            raise ValueError(f"{key}: szöveglista kell")
    for key in ("hints", "dupe_decisions"):
        if key in data and not isinstance(data[key], dict):
            raise ValueError(f"{key}: objektum kell")
    for key in allowed - {"original", "images", "proof_links", "tags", "hints", "dupe_decisions"}:
        if key in data and not isinstance(data[key], str):
            raise ValueError(f"{key}: szöveg kell")
    if data.get("menu") not in {None, "van", "nincs"}:
        raise ValueError("menu: van/nincs kell")
    for key in ("description", "install_guide", "cover"):
        if data.get(key):
            data[key] = str(_safe_reference(path.parent / data[key]))
    if data.get("images"):
        data["images"] = [str(_safe_reference(path.parent / x)) for x in data["images"]]
    for key, value in data.items():
        if key in {"hints", "dupe_decisions"}:
            continue
        attr = "proof_link" if key == "proof_links" else key
        if getattr(args, attr, None) in (None, [], ""):
            setattr(args, attr, ",".join(value) if key == "tags" else value)
    return data


class Ledger:
    """Single transactional local ledger; reservations survive interrupted POSTs.

    The limit counts this tool's successful, pending and uncertain uploads over
    a rolling day. Other clients/manual uploads are not visible to this ledger.
    """
    def __init__(self, folder: Path):
        private_dir(folder)
        path = folder / "uploads.sqlite3"
        self.db = sqlite3.connect(path, timeout=30)
        path.chmod(0o600)
        self.db.execute("CREATE TABLE IF NOT EXISTS jobs (key TEXT PRIMARY KEY, status TEXT, run_dir TEXT, updated REAL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS attempts (key TEXT PRIMARY KEY, started REAL, status TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS content (hash TEXT PRIMARY KEY, key TEXT)")
        self.db.commit()

    def close(self):
        self.db.close()

    def get(self, key: str):
        return self.db.execute("SELECT status,run_dir FROM jobs WHERE key=?", (key,)).fetchone()

    def reserve(self, key: str, run_dir: Path, limit: int, content_hash: str = "") -> None:
        if limit < 1:
            raise ValueError("a napi korlát legalább 1 legyen")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            old = self.get(key)
            if old:
                raise ValueError(f"már megkezdett feltöltés ({old[0]}): --resume {old[1]}")
            if content_hash:
                same = self.db.execute("SELECT jobs.status,jobs.run_dir FROM content JOIN jobs ON content.key=jobs.key WHERE content.hash=?", (content_hash,)).fetchone()
                if same:
                    raise ValueError(f"ezt a torrenttartalmat már elküldtem vagy lefoglaltam ({same[0]}): --resume {same[1]}")
            used = self.db.execute("SELECT COUNT(*) FROM attempts WHERE started>? AND status!='rejected'", (time.time() - 86400,)).fetchone()[0]
            if used >= limit:
                raise DailyLimitReached(f"az elmúlt 24 óra helyi feltöltési korlátja elérve ({limit}); később folytatható")
            self.db.execute("INSERT OR REPLACE INTO attempts VALUES (?,?,?)", (key, time.time(), "uploading"))
            self.db.execute("INSERT INTO jobs VALUES (?,?,?,?)", (key, "uploading", str(run_dir), time.time()))
            if content_hash:
                self.db.execute("INSERT OR REPLACE INTO content VALUES (?,?)", (content_hash, key))

    def mark(self, key: str, status: str, run_dir: Path):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO jobs VALUES (?,?,?,?)", (key, status, str(run_dir), time.time()))
            self.db.execute("UPDATE attempts SET status=? WHERE key=?", (status, key))
            if status == "rejected":
                self.db.execute("DELETE FROM jobs WHERE key=?", (key,))
                self.db.execute("DELETE FROM content WHERE key=?", (key,))


class OfflineClient:
    """Preparation only, never claims that the site dupe/ban/session checks passed."""
    offline = True

    def exact_matches(self, name):
        return []

    def search(self, text, **kwargs):
        return []

    def imdb_movie(self, imdb_id):
        return {}

    def upload_token(self):
        return "OFFLINE_PREPARATION_ONLY"

    def banned_notice(self, *args, **kwargs):
        return ""

    def upload(self, *args):
        raise ValueError("offline előkészítésből nem lehet feltölteni")
