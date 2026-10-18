"""Chocolatey Community Repository (community.chocolatey.org OData v2 feed): longer program descriptions (Markdown,
often a feature list), project URL, icon, tags, license URL. No key; Atom XML only ($format=json is refused).

Requests stay light (cached for a week): the repository rate-limits package downloads per IP, and this module never
downloads packages, only reads feed metadata.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from .. import net

API = "https://community.chocolatey.org/api/v2/"
NS = {"a": "http://www.w3.org/2005/Atom", "m": "http://schemas.microsoft.com/ado/2007/08/dataservices/metadata",
      "d": "http://schemas.microsoft.com/ado/2007/08/dataservices"}
_WEEK = 7 * 86400
PROPERTIES = {"Version": "version", "Title": "title", "Summary": "summary", "Description": "description", "ProjectUrl": "project_url",
              "IconUrl": "icon_url", "Tags": "tags", "LicenseUrl": "license_url", "DocsUrl": "docs_url", "ProjectSourceUrl": "source_url",
              "PackageSourceUrl": "package_source_url", "GalleryDetailsUrl": "gallery_url", "DownloadCount": "downloads",
              "IsLatestVersion": "latest", "IsApproved": "approved", "Copyright": "copyright", "ReleaseNotes": "release_notes",
              "Published": "published"}


def parse_feed(text: str) -> list[dict]:
    """Entries of an OData Atom feed: {"id", "title", "version", "summary", "description", "project_url", "icon_url",
    "tags": [...], "license_url", "authors", "downloads", "latest", ...}."""

    try:
        root = ET.fromstring(text or "")
    except ET.ParseError:
        return []
    out = []
    for entry in root.findall("a:entry", NS):
        item: dict = {"id": (entry.findtext("a:title", "", NS) or "").strip(), "authors": (entry.findtext("a:author/a:name", "", NS) or "").strip()}
        properties = entry.find("m:properties", NS)
        for child in list(properties) if properties is not None else []:
            name = child.tag.rsplit("}", 1)[-1]
            if name == "Id":
                item["id"] = (child.text or "").strip() or item["id"]
            elif name in PROPERTIES:
                item[PROPERTIES[name]] = (child.text or "").strip()
        item["tags"] = [t for t in re.split(r"[\s,;]+", item.get("tags", "")) if t]
        item["downloads"] = int(item["downloads"]) if str(item.get("downloads", "")).isdigit() else 0
        item["latest"] = item.get("latest") == "true"
        item["package_url"] = f"https://community.chocolatey.org/packages/{item['id']}" if item["id"] else ""
        out.append(item)
    return out


def search(term: str, limit: int = 8) -> list[dict]:
    """Latest approved versions matching a search term ([] on any failure)."""

    term = re.sub(r"['\"]", " ", term or "").strip()
    if len(term) < 2:
        return []
    text = net.get_text(API + "Search()", params={"$filter": "IsLatestVersion", "searchTerm": f"'{term}'", "targetFramework": "''",
                                                  "includePrerelease": "false", "$top": str(limit)}, quiet=True, expire=_WEEK)
    return parse_feed(text or "")


def package(package_id: str, version: str = "") -> dict:
    """One package (a given version, else the latest); {} when missing."""

    if not re.fullmatch(r"[\w.\-]{1,100}", package_id or ""):
        return {}
    if version and re.fullmatch(r"[\w.\-]{1,40}", version):
        text = net.get_text(f"{API}Packages(Id='{package_id}',Version='{version}')", quiet=True, expire=_WEEK)
        items = parse_feed(f"<feed xmlns='{NS['a']}'>{_entry(text)}</feed>") if text else []
        if items:
            return items[0]
    # $filter expressions with spaces are refused by the feed when requests encodes them as '+': search by id instead
    return next((item for item in search(package_id, 10) if item["id"].lower() == package_id.lower()), {})


def _entry(text: str) -> str:
    """A single-entry OData response is an <entry> document: strip its XML declaration so it can sit in a <feed>."""

    return re.sub(r"^\s*<\?xml[^>]*\?>", "", text or "")


def clean_description(markdown: str, limit: int = 2500) -> str:
    """Plain text of a Chocolatey Markdown description: links and emphasis unwrapped, package-maintenance notes dropped."""

    text = markdown or ""
    text = re.split(r"(?im)^#+\s*(?:package |chocolatey |)(?:notes?|parameters|package parameters|installation notes)\b", text)[0]
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"(\*\*|__|`)", "", text)
    text = re.sub(r"(?m)^#+\s*", "", text)
    text = re.sub(r"(?m)^\s*[-*]\s+", "• ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > limit:
        cut = text[:limit]
        text = cut[: max(cut.rfind("\n"), cut.rfind(". ") + 1, limit // 2)].strip()
    return text


def best(name: str, publishers: list[str] | tuple[str, ...] = (), project_url: str = "") -> dict:
    """The search hit whose title matches ``name`` (and whose author or project URL agrees when known); {} otherwise.
    '.install' / '.portable' twins of the same program are merged into the plain package."""

    from .winget import normalize, same_publisher, similarity

    wanted = normalize(name)
    host = _host(project_url)
    ranked = []
    for item in search(name):
        title = normalize(re.sub(r"\((?:install|portable|commandline|portable, commandline)\)", "", item.get("title") or item["id"], flags=re.I))
        score = max(similarity(wanted, title), similarity(wanted, normalize(item["id"].split(".")[0])))
        author_ok = any(same_publisher(item.get("authors", ""), p) for p in publishers if p)
        host_ok = bool(host) and _host(item.get("project_url", "")) == host
        if "deprecated" in (item.get("title") or "").lower():
            score -= 30
        ranked.append((score + (5 if author_ok or host_ok else 0) - (2 if "." in item["id"] else 0), score, author_ok or host_ok, item))
    ranked.sort(key=lambda r: -r[0])
    for _rank, score, agrees, item in ranked:
        if score >= 95 or (score >= 85 and agrees):
            item["match"] = {"score": round(score, 1), "agrees": agrees}
            return item
    return {}


def _host(url: str) -> str:
    match = re.match(r"(?i)^(?:https?://)?(?:www\.)?([^/:?#]+)", (url or "").strip())
    return match.group(1).lower() if match else ""
