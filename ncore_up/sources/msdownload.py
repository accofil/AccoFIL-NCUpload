"""Microsoft's public Windows release information: build -> version name ("26100" -> "24H2"), the official ISO
SHA-256 values, and the minimum hardware requirements (Hungarian text).

* Build -> version: ``BUILDS`` is a maintained static table (checked 2026-10-16 against Microsoft's release health
  pages). With ``refresh=True`` it is completed at most once a day from
  learn.microsoft.com/en-us/windows/release-health/windows11-release-information and .../release-information
  (Windows 10): plain HTML pages that robots.txt allows. When a new version ships, add its row here as well.
* Official ISO hashes: microsoft.com/hu-hu/software-download/windows11 (and windows10ISO) show the SHA-256 of each
  language's 64-bit ISO in the "Kivonatkód" table, and the current version in the heading. Only that HTML is read;
  the download-link API behind the page has bot protection and is never called. Only the CURRENT release is listed,
  so older or modified ISOs simply do not match.
"""

from __future__ import annotations

import html as htmllib
import json
import re
import time

from .. import net

RELEASE_HEALTH = {
    "Windows 11": "https://learn.microsoft.com/en-us/windows/release-health/windows11-release-information",
    "Windows 10": "https://learn.microsoft.com/en-us/windows/release-health/release-information",
}
DOWNLOAD_PAGES = {
    "Windows 11": "https://www.microsoft.com/hu-hu/software-download/windows11",
    "Windows 11 arm64": "https://www.microsoft.com/hu-hu/software-download/windows11arm64",
    "Windows 10": "https://www.microsoft.com/hu-hu/software-download/windows10ISO",
}
SPEC_PAGES = {
    "Windows 11": "https://www.microsoft.com/hu-hu/windows/windows-11-specifications",
    "Windows 10": "https://support.microsoft.com/hu-hu/windows/a-windows-10-rendszerk%C3%B6vetelm%C3%A9nyei",
}

# build -> (product, version, general availability date); static, see the module docstring.
BUILDS: dict[int, tuple[str, str, str]] = {
    28000: ("Windows 11", "26H1", "2026-02-10"),
    26300: ("Windows 11", "26H2", "2026-09-29"),
    26200: ("Windows 11", "25H2", "2025-09-30"),
    26100: ("Windows 11", "24H2", "2024-10-01"),
    22631: ("Windows 11", "23H2", "2023-10-31"),
    22621: ("Windows 11", "22H2", "2022-09-20"),
    22000: ("Windows 11", "21H2", "2021-10-04"),
    19045: ("Windows 10", "22H2", "2022-10-18"),
    19044: ("Windows 10", "21H2", "2021-11-16"),
    19043: ("Windows 10", "21H1", "2021-05-18"),
    19042: ("Windows 10", "20H2", "2020-10-20"),
    19041: ("Windows 10", "2004", "2020-05-27"),
    18363: ("Windows 10", "1909", "2019-11-12"),
    18362: ("Windows 10", "1903", "2019-05-21"),
    17763: ("Windows 10", "1809", "2018-11-13"),
    17134: ("Windows 10", "1803", "2018-04-30"),
    16299: ("Windows 10", "1709", "2017-10-17"),
    15063: ("Windows 10", "1703", "2017-04-11"),
    14393: ("Windows 10", "1607", "2016-08-02"),
    10586: ("Windows 10", "1511", "2015-11-10"),
    10240: ("Windows 10", "1507", "2015-07-29"),
}
SERVER_BUILDS: dict[int, tuple[str, str, str]] = {
    26100: ("Windows Server 2025", "24H2", "2024-11-01"),
    20348: ("Windows Server 2022", "21H2", "2021-08-18"),
    17763: ("Windows Server 2019", "1809", "2018-11-13"),
    14393: ("Windows Server 2016", "1607", "2016-10-12"),
}
# Microsoft's published minimum requirements, in Hungarian.
REQUIREMENTS = {
    "Windows 11": ("Processzor: legalább 1 GHz-es, legalább 2 magos, 64 bites (x64 vagy ARM64) kompatibilis processzor\n"
                   "Memória: legalább 4 GB RAM\nTárhely: legalább 64 GB\n"
                   "Firmware: UEFI, biztonságos rendszerindításra (Secure Boot) alkalmas\nTPM: 2.0-s verziójú platformmegbízhatósági modul\n"
                   "Videokártya: DirectX 12 kompatibilis, WDDM 2.0 illesztőprogrammal\nKijelző: legalább 9\" átmérőjű, HD (720p) felbontású"),
    "Windows 10": ("Processzor: legalább 1 GHz-es\nMemória: 1 GB (32 bites) / 2 GB (64 bites)\nTárhely: 16 GB (32 bites) / 32 GB (64 bites)\n"
                   "Videokártya: DirectX 9 kompatibilis, WDDM 1.0 illesztőprogrammal\nKijelző: legalább 800x600"),
}
ARCH = {"64-bit": "x64", "32-bit": "x86", "arm64": "arm64"}
_CACHE = "msdownload-builds.json"
_DAY = 86400


def _cell(text: str) -> str:
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", text or ""))).strip()


def parse_release_health(page: str, product: str = "Windows 11") -> dict[int, dict]:
    """{build: {"product", "version", "date", "latest"}} from a release health page: the "Version 24H2 (OS build 26100)"
    headings give the mapping, the first table the availability date and the latest build of each version."""

    out: dict[int, dict] = {}
    for version, build in re.findall(r"Version (\w+)(?: \(RTM\))? \(OS build (\d{5})\)", page or ""):
        out.setdefault(int(build), {"product": product, "version": version, "date": "", "latest": ""})
    by_version = {v["version"]: v for v in out.values()}
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", page or "", re.S):
        cells = [_cell(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        if len(cells) < 4 or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", cells[2]):
            continue
        version = re.sub(r"^\d{4} \((\w+)\)$", r"\1", cells[0])  # LTSC rows: "2021 (21H2)"
        latest = cells[-1] if re.fullmatch(r"\d{5}\.\d+", cells[-1]) else ""
        entry = by_version.get(version)
        if entry is None and latest:
            entry = out.setdefault(int(latest.split(".")[0]), {"product": product, "version": version, "date": "", "latest": ""})
            by_version[version] = entry
        if entry is not None and not entry["date"] and "LTS" not in cells[1]:
            entry["date"], entry["latest"] = cells[2], latest
    return out


def _load_cache() -> dict:
    try:
        data = json.loads((net.cache_dir() / _CACHE).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def builds(refresh: bool = False) -> dict[int, dict]:
    """The static table, completed by the daily cached release health pages (fetched only when ``refresh``)."""

    table = {b: {"product": p, "version": v, "date": d, "latest": ""} for b, (p, v, d) in BUILDS.items()}
    cache = _load_cache()
    if refresh and time.time() - float(cache.get("time", 0)) > _DAY:
        fetched: dict[str, dict] = {}
        for product, url in RELEASE_HEALTH.items():
            page = net.get_text(url, quiet=True, expire=_DAY)
            for build, entry in parse_release_health(page or "", product).items():
                fetched[str(build)] = entry
        if fetched:
            cache = {"time": time.time(), "builds": fetched}
            try:
                (net.cache_dir() / _CACHE).write_text(json.dumps(cache), encoding="utf-8")
            except OSError:
                pass
    for build, entry in (cache.get("builds") or {}).items():
        if str(build).isdigit():
            current = table.setdefault(int(build), {"product": entry.get("product", ""), "version": "", "date": "", "latest": ""})
            for key in ("version", "date", "latest"):
                current[key] = entry.get(key) or current.get(key, "")
    return table


def version_for_build(build: str | int, *, server: bool = False, refresh: bool = False) -> dict:
    """{"product", "version", "date"} of a build ("26100.1742", 26100); {} when unknown."""

    match = re.match(r"(?:10\.0\.)?(\d{5})", str(build or ""))
    if not match:
        return {}
    major = int(match.group(1))
    if server and major in SERVER_BUILDS:
        product, version, date = SERVER_BUILDS[major]
        return {"product": product, "version": version, "date": date}
    entry = builds(refresh).get(major)
    return {"product": entry["product"], "version": entry["version"], "date": entry["date"]} if entry else {}


def parse_hash_table(page: str) -> dict[str, dict]:
    """{SHA256 (upper case): {"label": "magyar 64-bit", "language": "magyar", "arch": "x64"}} of a software-download page."""

    out: dict[str, dict] = {}
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", page or "", re.S):
        cells = [_cell(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        if len(cells) >= 2 and re.fullmatch(r"[0-9A-Fa-f]{64}", cells[1]):
            label = cells[0]
            match = re.match(r"(.*?)\s+(64-bit|32-bit|arm64)\s*$", label, re.I)
            language, arch = (match.group(1), ARCH.get(match.group(2).lower(), "")) if match else (label, "")
            out[cells[1].upper()] = {"label": label, "language": language.strip(), "arch": arch}
    return out


def parse_current_version(page: str) -> str:
    """'26H2' from the page heading ("Jelenlegi kiadás: Windows 11 2026 ... 26H2 verzió" / "Version 26H2")."""

    head = re.search(r"(?:Jelenlegi kiadás|Current release)([^<]{0,160})", page or "", re.I)
    found = re.search(r"\b(\d{2}H\d)\b", head.group(1)) if head else None
    found = found or re.search(r"\b(\d{2}H\d)(?:-es|-as)?\s+verzió|\bVersion\s+(\d{2}H\d)\b", page or "", re.I)  # Windows 10: "22H2 verzió"
    return next((g for g in found.groups() if g), "") if found else ""


def published(product: str = "Windows 11") -> dict:
    """{"url", "version", "hashes"} of Microsoft's download page for a product ({} when it cannot be read)."""

    url = DOWNLOAD_PAGES.get(product)
    if not url:
        return {}
    page = net.get_text(url, quiet=True, expire=_DAY)
    if not page:
        return {}
    return {"url": url, "version": parse_current_version(page), "hashes": parse_hash_table(page)}


def check_iso(sha256: str, product: str, arch: str = "x64") -> dict:
    """{"match", "url", "version", "language", "arch"} for an ISO hash against Microsoft's published table."""

    key = product if product in DOWNLOAD_PAGES else ("Windows 11" if "11" in product else "Windows 10" if "10" in product else "")
    if key == "Windows 11" and arch == "arm64":
        key = "Windows 11 arm64"
    page = published(key) if key else {}
    if not page:
        return {}
    hit = page["hashes"].get((sha256 or "").upper())
    return {"match": bool(hit), "url": page["url"], "version": page["version"], "language": (hit or {}).get("language", ""),
            "arch": (hit or {}).get("arch", ""), "known": len(page["hashes"])}
