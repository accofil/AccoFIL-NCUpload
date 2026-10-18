"""F-Droid (free and open-source Android apps): the small per-package API tells whether an app is on F-Droid; only
then is the repository index (index-v2.json, ~66 MB, daily cache, streamed item by item into SQLite) used for the
localized name/summary/description (a few dozen apps have Hungarian text), categories, licence, website, source,
author, icon, screenshots and every published version with its signer SHA-256 (to tell an original F-Droid build).
Proof link: https://f-droid.org/packages/<package>/.
"""

from __future__ import annotations

import contextlib
import json
import re
import sqlite3
from pathlib import Path

from .. import net
from .titledb import JsonStream, fetch_file, open_db

INDEX = "https://f-droid.org/repo/index-v2.json"
API = "https://f-droid.org/api/v1/packages/{}"
REPO = "https://f-droid.org/repo"
PAGE = "https://f-droid.org/packages/{}/"

SCHEMA = """
CREATE TABLE IF NOT EXISTS packages (package TEXT PRIMARY KEY, data TEXT);
CREATE TABLE IF NOT EXISTS built (name TEXT PRIMARY KEY, mtime REAL);
"""


def _dir() -> Path:
    return net.cache_dir() / "fdroid"


def compact(package: dict) -> dict:
    """The parts of an index-v2 package record that are kept in the local index."""

    def obj(value) -> dict:  # one odd record must not break the whole index build
        return value if isinstance(value, dict) else {}

    def items(value) -> list:
        return list(value) if isinstance(value, list) else []

    metadata = obj(package.get("metadata"))
    keep = {k: metadata.get(k) for k in ("name", "summary", "description", "categories", "license", "webSite", "sourceCode",
                                         "authorName", "icon", "screenshots", "lastUpdated", "changelog") if metadata.get(k)}
    versions = []
    for entry in obj(package.get("versions")).values():
        entry = obj(entry)
        manifest = obj(entry.get("manifest"))
        code = manifest.get("versionCode")
        versions.append({
            "version_name": str(manifest.get("versionName") or ""),
            "version_code": code if isinstance(code, int) else None,
            "min_sdk": obj(manifest.get("usesSdk")).get("minSdkVersion"),
            "signers": [str(s) for s in items(obj(manifest.get("signer")).get("sha256"))],
            "sha256": str(obj(entry.get("file")).get("sha256") or ""),
            "abis": [str(a) for a in items(manifest.get("nativecode"))],
        })
    versions.sort(key=lambda v: v.get("version_code") or 0, reverse=True)
    return {"metadata": keep, "versions": versions}


def build_index(db: sqlite3.Connection, index_path: Path) -> int:
    count = 0
    with db, open(index_path, encoding="utf-8") as handle:
        db.execute("DELETE FROM packages")
        for name, package in JsonStream(handle).items(("packages",)):
            if isinstance(package, dict):
                db.execute("INSERT OR REPLACE INTO packages VALUES (?, ?)", (name, json.dumps(compact(package), ensure_ascii=False)))
                count += 1
        db.execute("INSERT OR REPLACE INTO built VALUES (?, ?)", ("index", index_path.stat().st_mtime))
    return count


def _localized(value, prefer: tuple[str, ...] = ("hu", "hu-HU")) -> tuple[str, str]:
    """(text, locale) of a localized index field, Hungarian first, then English, then anything."""

    if isinstance(value, str):
        return value, ""
    if not isinstance(value, dict) or not value:
        return "", ""
    for locale in prefer + ("en-US", "en", "en-GB"):
        if value.get(locale):
            return value[locale], locale
    locale = next(iter(value))
    return value[locale], locale


def _text(value: str) -> str:
    text = re.sub(r"(?i)<br\s*/?>|</p>|</li>", "\n", value if isinstance(value, str) else "")
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def parse_package(package: str, data: dict) -> dict:
    """Local index record → {"package", "name", "summary", "description", "description_locale", "description_en",
    "categories", "license", "website", "source", "author", "icon", "screenshots", "versions", "url"}."""

    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    description, locale = _localized(metadata.get("description"))
    english, _ = _localized(metadata.get("description"), prefer=("en-US",))
    summary, _ = _localized(metadata.get("summary"))
    name, _ = _localized(metadata.get("name"))
    icon, _ = _localized(metadata.get("icon"))
    screenshots = metadata.get("screenshots") if isinstance(metadata.get("screenshots"), dict) else {}
    shots, _ = _localized(screenshots.get("phone") or {})
    return {
        "package": package,
        "name": name if isinstance(name, str) else "",
        "summary": summary.strip() if isinstance(summary, str) else "",
        "description": _text(description),
        "description_locale": locale,
        "description_en": _text(english),
        "categories": list(metadata.get("categories") or []),
        "license": metadata.get("license", ""),
        "website": metadata.get("webSite", ""),
        "source": metadata.get("sourceCode", ""),
        "author": metadata.get("authorName", ""),
        "icon": REPO + icon["name"] if isinstance(icon, dict) and icon.get("name") else "",
        "screenshots": [REPO + s["name"] for s in shots if isinstance(s, dict) and s.get("name")] if isinstance(shots, list) else [],
        "versions": data.get("versions") or [],
        "url": PAGE.format(package),
    }


def known(package: str) -> dict:
    """{"suggested_version_code", "versions": [(name, code)]} from the per-package API; {} when not on F-Droid."""

    if not re.fullmatch(r"[A-Za-z][\w]*(\.[A-Za-z_][\w]*)+", package or ""):
        return {}
    data = net.get_json(API.format(package), quiet=True) or {}
    if not isinstance(data, dict) or data.get("packageName") != package:
        return {}
    return {"suggested_version_code": data.get("suggestedVersionCode"),
            "versions": [(p.get("versionName", ""), p.get("versionCode")) for p in data.get("packages") or [] if isinstance(p, dict)]}


def lookup(package: str) -> dict:
    """parse_package() of an F-Droid app; {} when the app is not on F-Droid or the index is unavailable."""

    if not known(package):
        return {}
    path = fetch_file(INDEX, _dir() / "index-v2.json", max_age_days=1.0, min_size=1 << 20, timeout=600)
    if path is None:
        return {}
    try:
        db = open_db(_dir() / "index.sqlite", SCHEMA)
        with contextlib.closing(db):
            row = db.execute("SELECT mtime FROM built WHERE name = 'index'").fetchone()
            if row is None or abs(row["mtime"] - path.stat().st_mtime) > 1:
                build_index(db, path)
            row = db.execute("SELECT data FROM packages WHERE package = ?", (package,)).fetchone()
        return parse_package(package, json.loads(row["data"])) if row else {}
    except (ValueError, OSError, sqlite3.Error) as exc:
        net.FAILURES.append(f"F-Droid: az index nem dolgozható fel ({type(exc).__name__})")
        return {}


def signer_matches(info: dict, sha256: str) -> bool:
    """True when ``sha256`` (apksigner's certificate digest) is the signer of any F-Droid build of the app."""

    wanted = (sha256 or "").lower().replace(":", "")
    return bool(wanted) and any(wanted in [s.lower() for s in v.get("signers") or []] for v in info.get("versions") or [])
