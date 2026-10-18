"""winget (Windows Package Manager) community repository: Windows programs' publisher, official site, license,
description, tags, minimum Windows, installer architectures and the SHA-256 of the official installers.

No key needed. The index is the one the winget client itself downloads:

* https://cdn.winget.microsoft.com/cache/source2.msix (~4 MB zip, refreshed once a day into ``net.cache_dir()/winget``)
  holds Public/index.db (SQLite): packages(id, name, moniker, latest_version, hash), norm_names2, norm_publishers2,
  tags2, productcodes2, upgradecodes2 (MSI codes -> exact matches).
* .../cache/packages/<id>/<hash[:8] lowercase>/versionData.mszyml: MSZIP-compressed YAML listing every version and the
  CDN path of its merged manifest (.../cache/<rP>), a plain YAML with all fields (ManifestType: merged).
* Fallback when the CDN fails: raw.githubusercontent.com/microsoft/winget-pkgs (60 requests/hour without a token;
  GITHUB_TOKEN raises it). Manifests are MIT licensed.
"""

from __future__ import annotations

import io
import os
import re
import sqlite3
import time
import zipfile
import zlib
from pathlib import Path

from .. import keys, net

CDN = "https://cdn.winget.microsoft.com/cache/"
INDEX_URL = CDN + "source2.msix"
RAW = "https://raw.githubusercontent.com/microsoft/winget-pkgs/master/manifests/"
MSZIP_MAGIC = b"\x0a\x51\xe5\xc0"
LEGAL = {"inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation", "gmbh", "co", "company", "sa", "ag", "bv", "srl", "sro",
         "oy", "ab", "as", "plc", "pty", "kg", "kft", "zrt", "nv", "llp", "spa", "sas", "sarl", "the"}
_DAY = 86400
_INDEX_FAILED = [0.0]  # time of the last failed index download (this process)


def _no_cache():
    """requests-cache's DO_NOT_CACHE (big downloads are stored by this module itself), 0 without requests-cache."""

    try:
        import requests_cache  # optional dependency

        return getattr(requests_cache, "DO_NOT_CACHE", 0)
    except ImportError:
        return 0


def normalize(text: str) -> str:
    """winget-style normalized name: lower case, letters and digits only ('Notepad++' -> 'notepad', '7-Zip' -> '7zip')."""

    text = re.sub(r"\((?:x64|x86|arm64|64-bit|32-bit)\)", "", (text or "").lower())
    return re.sub(r"[^0-9a-zÀ-￿]+", "", text)


def normalize_publisher(text: str) -> str:
    words = [w for w in re.split(r"[^0-9a-zÀ-￿]+", (text or "").lower()) if w]
    while len(words) > 1 and words[-1] in LEGAL:
        words.pop()
    return "".join(w for w in words if w not in {"the"})


def same_publisher(a: str, b: str) -> bool:
    """Loose publisher equality: 'Igor Pavlov' ~ 'igorpavlov', 'Notepad++ Team' ~ 'NOTEPAD++', 'Ghisler Software GmbH' ~ 'ghisler'."""

    x, y = normalize_publisher(a), normalize_publisher(b)
    if not x or not y:
        return False
    if x == y or (min(len(x), len(y)) >= 4 and (x in y or y in x)):
        return True
    tokens = lambda s: {t for t in re.split(r"[^0-9a-z]+", s.lower()) if len(t) >= 4 and t not in LEGAL | {"software", "team", "group"}}  # noqa: E731
    return bool(tokens(a) & tokens(b))


def similarity(a: str, b: str) -> float:
    """0..100 similarity of two normalized names (rapidfuzz when installed, difflib otherwise)."""

    if not a or not b:
        return 0.0
    try:
        from rapidfuzz import fuzz  # optional dependency

        return float(fuzz.ratio(a, b))
    except ImportError:
        from difflib import SequenceMatcher

        return 100.0 * SequenceMatcher(None, a, b).ratio()


# --- index -------------------------------------------------------------------------------------------------------

def index_db(max_age: float = _DAY) -> Path | None:
    """Path of the local copy of the winget index (downloaded at most once per ``max_age`` seconds); None if unavailable."""

    target = net.cache_dir() / "winget" / "index.db"
    if target.exists() and time.time() - target.stat().st_mtime < max_age:
        return target
    if time.time() - _INDEX_FAILED[0] < 600:  # one resolve looks up several names: do not retry a failed download each time
        return target if target.exists() else None
    response = net.request("GET", INDEX_URL, expire=_no_cache(), quiet=True, retries=1)
    if response is not None:
        try:
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                data = archive.read("Public/index.db")
            target.parent.mkdir(parents=True, exist_ok=True)
            temp = target.with_suffix(".tmp")
            temp.write_bytes(data)
            os.replace(temp, target)
        except (zipfile.BadZipFile, KeyError, OSError):
            _INDEX_FAILED[0] = time.time()
            net.FAILURES.append("cdn.winget.microsoft.com: hibás index (source2.msix)")
    else:
        _INDEX_FAILED[0] = time.time()
        if not target.exists():
            net.FAILURES.append("cdn.winget.microsoft.com: az index nem tölthető le")
    return target if target.exists() else None


def _connect(db: Path | str | None):
    path = db or index_db()
    if not path:
        return None
    try:
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None


def _rows(con, sql: str, args: tuple) -> list[tuple]:
    try:
        return con.execute(sql, args).fetchall()
    except sqlite3.Error:
        return []


def _package(con, rowid: int) -> dict:
    row = _rows(con, "SELECT rowid, id, name, moniker, latest_version, hex(hash) FROM packages WHERE rowid = ?", (rowid,))
    if not row:
        return {}
    rowid, pid, name, moniker, version, digest = row[0]
    publishers = [r[0] for r in _rows(con, "SELECT norm_publisher FROM norm_publishers2 WHERE package = ?", (rowid,)) if r[0]]
    names = [r[0] for r in _rows(con, "SELECT norm_name FROM norm_names2 WHERE package = ?", (rowid,)) if r[0]]
    tags = [r[0] for r in _rows(con, "SELECT t.tag FROM tags2 t JOIN tags2_map m ON m.tag = t.rowid WHERE m.package = ?", (rowid,))]
    return {"rowid": rowid, "id": pid, "name": name, "moniker": moniker or "", "version": version, "hash": (digest or "").lower(),
            "publishers": publishers, "norm_names": names, "tags": tags}


def by_code(code: str, db: Path | str | None = None) -> dict:
    """Package of an MSI ProductCode / UpgradeCode ('{23170F69-...}'), {} when unknown."""

    con = _connect(db) if code else None
    if con is None:
        return {}
    try:
        for table, column in (("productcodes2", "productcode"), ("upgradecodes2", "upgradecode")):
            rows = _rows(con, f"SELECT package FROM {table} WHERE {column} = ?", (code.strip().lower(),))
            if rows:
                return _package(con, rows[0][0])
    finally:
        con.close()
    return {}


def search(name: str, publishers: tuple[str, ...] | list[str] = (), limit: int = 5, db: Path | str | None = None) -> list[dict]:
    """Packages whose normalized name (or moniker) resembles ``name``, best first:
    [{"id", "name", "version", "hash", "score", "publisher_match", "publishers", "tags"}]."""

    query = normalize(name)
    con = _connect(db) if len(query) >= 2 else None
    if con is None:
        return []
    words = [normalize(w) for w in re.split(r"[\s._-]+", name or "") if normalize(w)]
    variants = list(dict.fromkeys([query] + ["".join(words[i:]) for i in range(1, len(words)) if len("".join(words[i:])) >= 3]))
    found: dict[int, float] = {}
    try:
        for variant in variants:
            rows = _rows(con, "SELECT package, norm_name FROM norm_names2 WHERE norm_name = ? OR norm_name LIKE ? LIMIT 300",
                         (variant, variant + "%"))
            rows += _rows(con, "SELECT rowid, ? FROM packages WHERE lower(moniker) IN (?, ?) OR lower(id) LIKE ?",
                          (variant, variant, (name or "").lower(), "%." + variant))
            for rowid, norm in rows:
                plain = re.sub(r"\(x(?:64|86)\)|\(arm64\)$", "", (norm or "").lower())
                score = similarity(variant, plain) - (0 if variant == query else 3)
                found[rowid] = max(found.get(rowid, 0.0), score)
        best = sorted(found.items(), key=lambda kv: -kv[1])[: max(limit * 4, 20)]
        out = []
        raw = (name or "").strip().lower()
        for rowid, score in best:
            package = _package(con, rowid)
            if not package:
                continue
            package["score"] = round(score, 1)
            package["publisher_match"] = any(same_publisher(p, hint) for p in package["publishers"] for hint in publishers if hint)
            # normalization folds 'Notepad++' and 'Notepad--' together: the unnormalized name decides between them
            package["exact"] = bool(raw) and raw in {(package["name"] or "").strip().lower(), package["moniker"].lower(), package["id"].lower()}
            out.append(package)
    finally:
        con.close()
    out.sort(key=lambda p: (-(p["score"] + (8 if p["publisher_match"] else 0) + (3 if p["exact"] else 0)), len(p["id"])))
    return out[:limit]


def best(name: str, publishers: tuple[str, ...] | list[str] = (), *, product_code: str = "", upgrade_code: str = "",
         db: Path | str | None = None) -> dict:
    """The one package that fits, with "match": {"method", "score", "publisher_match"}; {} when nothing is sure enough.
    MSI codes are exact; a name match needs >= 95 similarity (publisher agreeing, or no publisher known) or >= 85 with an
    agreeing publisher."""

    for code, method in ((product_code, "productcode"), (upgrade_code, "upgradecode")):
        if code:
            package = by_code(code, db)
            if package:
                package["match"] = {"method": method, "score": 100.0, "publisher_match": True}
                return package
    hints = [p for p in publishers if p]
    hits = search(name, hints, db=db)
    for package in hits:
        if package["publisher_match"] and package["score"] >= 85:
            ok = True
        elif not hints and package["score"] >= 95:
            # name only: the exact name, or the one package that close (else 'Notepad++' could pick 'Notepad--')
            rivals = [p for p in hits if p is not package and p["score"] >= package["score"] - 3]
            ok = package["exact"] or not rivals
        else:
            ok = False
        if ok:
            package["match"] = {"method": "name", "score": package["score"], "publisher_match": package["publisher_match"]}
            return package
    return {}


# --- manifests -----------------------------------------------------------------------------------------------------

def mszip_decompress(raw: bytes) -> bytes:
    """Decode winget's .mszyml (MSZIP: a header, then 'CK' + raw deflate blocks chained by a 32 KB dictionary)."""

    if not raw.startswith(MSZIP_MAGIC):
        return raw
    out = b""
    data = raw
    pos = data.find(b"CK", 0, 64)
    for _block in range(100000):
        if pos < 0:
            break
        decoder = zlib.decompressobj(-15, zdict=out[-32768:]) if out else zlib.decompressobj(-15)
        try:
            out += decoder.decompress(data[pos + 2:])
        except zlib.error:
            break
        data = decoder.unused_data
        pos = data.find(b"CK", 0, 32) if data else -1
    return out


def _yaml(text: str):
    """Every scalar as a string (BaseLoader): unquoted versions like 26.00 or 11.50 must not become floats."""

    try:
        import yaml  # optional dependency

        return yaml.load(text, Loader=yaml.BaseLoader)  # noqa: S506 - BaseLoader builds only str/list/dict
    except ImportError:
        return _mini_yaml(text)
    except Exception:  # noqa: BLE001 - yaml.YAMLError and friends
        return None


def _scalar(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        inner = value[1:-1]
        if value[0] == '"':
            inner = re.sub(r"\\u([0-9A-Fa-f]{4})", lambda m: chr(int(m.group(1), 16)), inner)
            inner = re.sub(r"\\x([0-9A-Fa-f]{2})", lambda m: chr(int(m.group(1), 16)), inner).replace('\\"', '"').replace("\\n", "\n")
        else:
            inner = inner.replace("''", "'")
        return inner
    return value


def _mini_yaml(text: str) -> dict:
    """Top-level scalars, block scalars and lists of a manifest, for when PyYAML is missing (Installers: arch/type only)."""

    out: dict = {}
    lines = (text or "").split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].rstrip("\r")
        match = re.match(r"^([A-Za-z][\w]*):(?:\s+(.*))?$", line)
        i += 1
        if not match:
            continue
        key, value = match.group(1), (match.group(2) or "").strip()
        if value in {"|", "|-", ">", ">-", "|+"}:
            block = []
            while i < len(lines) and (lines[i].startswith("  ") or not lines[i].strip()):
                block.append(lines[i][2:].rstrip("\r"))
                i += 1
            out[key] = ("\n" if value.startswith("|") else " ").join(block).strip()
        elif value:
            while i < len(lines) and lines[i].startswith("  ") and not lines[i].lstrip().startswith("- "):
                value += " " + lines[i].strip()  # folded plain/quoted scalar
                i += 1
            out[key] = _scalar(value)
        else:
            items: list = []
            while i < len(lines) and (lines[i].startswith(("- ", "  ")) or not lines[i].strip()):
                item = lines[i].rstrip("\r")
                if item.startswith("- ") and ":" not in item[2:]:
                    items.append(_scalar(item[2:]))
                elif item.startswith("- "):
                    items.append({})
                    item = "  " + item[2:]
                if items and isinstance(items[-1], dict):
                    pair = re.match(r"^  ([A-Za-z]\w*):\s+(.+)$", item)
                    if pair:
                        items[-1][pair.group(1)] = _scalar(pair.group(2))
                i += 1
            out[key] = items
    return out


def parse_version_data(raw: bytes) -> list[dict]:
    """[{"version", "path", "sha256"}] of a versionData.mszyml, newest first."""

    data = _yaml(mszip_decompress(raw).decode("utf-8", errors="replace")) or {}
    items = data.get("vD") if isinstance(data, dict) else None
    return [{"version": str(v.get("v", "")), "path": v.get("rP", ""), "sha256": v.get("s256H", "")} for v in items or [] if isinstance(v, dict)]


def _text(value) -> str:
    return re.sub(r"[ \t]+\n", "\n", str(value)).strip() if value not in (None, "") else ""


def parse_manifest(text: str) -> dict:
    """Fields of a merged (or locale/installer) winget manifest YAML (see the module docstring)."""

    data = _yaml(text) or {}
    if not isinstance(data, dict):
        return {}
    root_type = data.get("InstallerType", "")
    installers = []
    for item in data.get("Installers") or []:
        if not isinstance(item, dict):
            continue
        installers.append({"arch": str(item.get("Architecture", "")).lower(), "type": item.get("InstallerType") or root_type,
                           "nested": item.get("NestedInstallerType", ""), "url": item.get("InstallerUrl", ""),
                           "sha256": str(item.get("InstallerSha256", "")).lower(), "product_code": str(item.get("ProductCode", "")),
                           "scope": item.get("Scope", "")})
    locales = {}
    for item in data.get("Localization") or []:
        if isinstance(item, dict) and item.get("PackageLocale"):
            locales[item["PackageLocale"]] = {"short_description": _text(item.get("ShortDescription")), "description": _text(item.get("Description")),
                                              "publisher_url": item.get("PublisherUrl", ""), "package_url": item.get("PackageUrl", "")}
    icons = [i.get("IconUrl", "") for i in data.get("Icons") or [] if isinstance(i, dict) and i.get("IconUrl")]
    hu = next((v for k, v in locales.items() if k.lower().startswith("hu")), {})
    tags = [str(t) for t in data.get("Tags") or [] if t]
    return {
        "id": data.get("PackageIdentifier", ""), "name": data.get("PackageName", ""), "version": str(data.get("PackageVersion", "")),
        "publisher": data.get("Publisher", ""), "publisher_url": data.get("PublisherUrl", ""), "package_url": data.get("PackageUrl", ""),
        "support_url": data.get("PublisherSupportUrl", ""), "license": _text(data.get("License")), "license_url": data.get("LicenseUrl", ""),
        "copyright": _text(data.get("Copyright")), "author": _text(data.get("Author")), "moniker": data.get("Moniker", ""),
        "short_description": _text(data.get("ShortDescription")), "description": _text(data.get("Description")),
        "description_hu": hu.get("description") or hu.get("short_description", ""), "tags": tags,
        "release_date": str(data.get("ReleaseDate", "") or ""), "release_notes_url": data.get("ReleaseNotesUrl", ""),
        "min_os": str(data.get("MinimumOSVersion", "") or ""), "icon_url": icons[0] if icons else "",
        "installers": installers, "archs": list(dict.fromkeys(i["arch"] for i in installers if i["arch"])),
        "installer_types": list(dict.fromkeys(i["type"] for i in installers if i["type"])),
        "installer_sha256": [i["sha256"] for i in installers if i["sha256"]], "locales": locales,
    }


def versions(package: dict) -> list[dict]:
    """parse_version_data() of a package from search()/best() (needs its "id" and "hash")."""

    if not package.get("id") or not package.get("hash"):
        return []
    raw = net.get_bytes(f"{CDN}packages/{package['id']}/{package['hash'][:8].lower()}/versionData.mszyml", quiet=True, expire=_DAY)
    return parse_version_data(raw) if raw else []


def _same_version(a: str, b: str) -> bool:
    na = [int(x) for x in re.findall(r"\d+", a or "")]
    nb = [int(x) for x in re.findall(r"\d+", b or "")]
    while na and na[-1] == 0:
        na.pop()
    while nb and nb[-1] == 0:
        nb.pop()
    return bool(na) and na == nb


def _github_manifest(package_id: str, version: str) -> dict:
    """Locale + installer manifest from GitHub raw (fallback; 60 requests/hour without GITHUB_TOKEN)."""

    if not package_id or not version:
        return {}
    base = RAW + package_id[0].lower() + "/" + "/".join(package_id.split(".")) + f"/{version}/{package_id}"
    headers = {"Authorization": f"Bearer {keys.get('GITHUB_TOKEN')}"} if keys.get("GITHUB_TOKEN") else None
    locale = net.get_text(base + ".locale.en-US.yaml", headers=headers, quiet=True, expire=7 * _DAY) or ""
    installer = net.get_text(base + ".installer.yaml", headers=headers, quiet=True, expire=7 * _DAY) or ""
    if not locale:
        return {}
    result = parse_manifest(locale)
    if installer:
        extra = parse_manifest(installer)
        for key in ("installers", "archs", "installer_types", "installer_sha256", "min_os"):
            result[key] = result.get(key) or extra.get(key)
    return result


def manifest(package: dict, version: str = "") -> dict:
    """The merged manifest of a package (the given version when the repository has it, else the latest) with
    "matched_version": True when ``version`` was found."""

    listed = versions(package)
    chosen = next((v for v in listed if version and (v["version"] == version or _same_version(v["version"], version))), None)
    entry = chosen or (listed[0] if listed else None)
    result: dict = {}
    if entry and entry.get("path"):
        text = net.get_text(CDN + entry["path"], quiet=True, expire=7 * _DAY)
        result = parse_manifest(text or "")
    if not result:
        result = _github_manifest(package.get("id", ""), (chosen or {}).get("version") or version or package.get("version", ""))
    if result:
        result["matched_version"] = chosen is not None
        result["latest_version"] = package.get("version", "")
    return result


def lookup(name: str, publishers: tuple[str, ...] | list[str] = (), *, version: str = "", product_code: str = "",
           upgrade_code: str = "") -> dict:
    """best() + manifest(): the manifest fields plus "match" and "index" (the index row); {} when not found."""

    package = best(name, publishers, product_code=product_code, upgrade_code=upgrade_code)
    if not package:
        return {}
    result = manifest(package, version)
    if not result:
        result = {"id": package["id"], "name": package["name"], "version": package["version"], "tags": package["tags"]}
    result["match"] = package["match"]
    result["index"] = {k: package[k] for k in ("id", "name", "version", "publishers", "tags")}
    return result
