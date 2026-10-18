"""Google Play app pages: https://play.google.com/store/apps/details?id=<package>&hl=hu&gl=HU, one request per app
(net caches it). There is no public listing API, so the page itself is read: the JSON-LD block (name, short
description, category, developer, rating, icon) and the ``AF_initDataCallback`` ds:5 data (full Hungarian description
when the developer localized the listing, screenshots, updated date, installs, minimum Android, recent changes).

robots.txt allows /store/apps/details (it is checked before every page; /store/xhr, /apps, /store/search are not used).
The page structure is undocumented: every field is optional. The version is never taken from here (the APK's own
versionName counts). Proof link: the store page itself.
"""

from __future__ import annotations

import html
import json
import math
import re
import time
from urllib.parse import urlsplit

from .. import net

DETAILS = "https://play.google.com/store/apps/details"
_ROBOTS: dict[str, list[tuple[bool, str]]] = {}


# --- robots.txt (RFC 9309: longest match wins, Allow wins ties, * and $ wildcards) -------------------------------

def parse_robots(text: str, agent: str = "ncore-up") -> list[tuple[bool, str]]:
    """Rules (allow?, pattern) of the group for ``agent`` (else the * group)."""

    groups: list[tuple[list[str], list[tuple[bool, str]]]] = []
    agents: list[str] = []
    rules: list[tuple[bool, str]] = []
    last_was_agent = False
    for raw in (text or "").splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        field, value = (x.strip() for x in line.split(":", 1))
        field = field.lower()
        if field == "user-agent":
            if not last_was_agent and (agents or rules):
                groups.append((agents, rules))
                agents, rules = [], []
            agents.append(value.lower())
            last_was_agent = True
        elif field in {"allow", "disallow"}:
            last_was_agent = False
            if value:
                rules.append((field == "allow", value))
        else:
            last_was_agent = False
    if agents or rules:
        groups.append((agents, rules))
    agent = agent.lower()
    mine = [r for a, r in groups if any(x != "*" and x in agent for x in a)]
    if mine:
        return [rule for group in mine for rule in group]
    return [rule for a, r in groups if "*" in a for rule in r]


def robots_match(rules: list[tuple[bool, str]], path: str) -> bool:
    best: tuple[int, bool] = (-1, True)
    for allow, pattern in rules:
        regex = re.escape(pattern).replace(r"\*", ".*")
        if regex.endswith(r"\$"):
            regex = regex[:-2] + "$"
        if re.match(regex, path):
            length = len(pattern)
            if length > best[0] or (length == best[0] and allow):
                best = (length, allow)
    return best[1]


def robots_allows(url: str) -> bool:
    """True when the site's robots.txt lets ncore-up fetch ``url``. A robots.txt that cannot be fetched counts as
    "disallow everything" (RFC 9309 for unreachable files; ``net`` cannot tell a 404 apart, and every site used here
    has one) and is asked again on the next call."""

    parts = urlsplit(url)
    origin = f"{parts.scheme}://{parts.netloc}"
    if _ROBOTS.get(origin) is None:
        text = net.get_text(origin + "/robots.txt", quiet=True, retries=1)
        if text is None:
            net.FAILURES.append(f"{parts.netloc}: a robots.txt nem érhető el — az oldalt kihagytam")
            return False
        _ROBOTS[origin] = parse_robots(text)
    rules = _ROBOTS[origin] or []
    return robots_match(rules, (parts.path or "/") + (f"?{parts.query}" if parts.query else ""))


# --- the app page ---------------------------------------------------------------------------------------------------

_DS5_RE = re.compile(r"AF_initDataCallback\(\{key: 'ds:5', hash: '\d+', data:(.*?), sideChannel: \{\}\}\);</script>", re.S)
_LD_RE = re.compile(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', re.S)


def _get(data, *path):
    for key in path:
        try:
            data = data[key]
        except (IndexError, KeyError, TypeError):
            return None
    return data


def html_text(value: str) -> str:
    text = re.sub(r"(?i)<br\s*/?>|</p>|</li>", "\n", value or "")
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", text)).strip()


def parse_app_page(page: str) -> dict:
    """{"name", "description", "summary", "developer", "developer_website", "category", "category_id", "rating",
    "ratings", "installs", "content_rating", "updated", "version", "min_android", "min_sdk", "icon", "header_image",
    "screenshots", "recent_changes", "price"}; {} when the page holds no app."""

    out: dict = {}
    for block in _LD_RE.findall(page or ""):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("@type") == "SoftwareApplication":
            rating = _dict(data.get("aggregateRating"))
            author = _dict(data.get("author"))
            offers = data.get("offers")
            offer = _dict(offers[0] if isinstance(offers, list) and offers else offers)
            out.update({
                "name": _str(data.get("name")),
                "summary": html_text(_str(data.get("description"))),
                "category_id": _str(data.get("applicationCategory")),
                "developer": _str(author.get("name")),
                "developer_website": _str(author.get("url")),
                "rating": _float(rating.get("ratingValue")),
                "ratings": int(min(max(_float(str(rating.get("ratingCount") or 0).replace(",", "")) or 0, 0), 1e12)),
                "content_rating": _str(data.get("contentRating")),
                "icon": _str(data.get("image")),
                "price": _str(offer.get("price")),
            })
            break
    match = _DS5_RE.search(page or "")
    base = None
    if match:
        try:
            base = _get(json.loads(match.group(1)), 1, 2)
        except ValueError:
            base = None
    if isinstance(base, list):  # undocumented positions: every value is type-checked, a moved field is just missing
        updated = _get(base, 145, 0, 1, 0)
        shot_list = _get(base, 78, 0)
        shots = [_get(s, 3, 2) for s in shot_list] if isinstance(shot_list, list) else []
        min_android = _str(_get(base, 140, 1, 1, 0, 0, 1)) or _str(_get(base, 140, 1, 0, 0, 1))
        min_sdk = _get(base, 140, 1, 0, 0, 0)
        out.update({k: v for k, v in {
            "name": _str(_get(base, 0, 0)),
            "description": html_text(_str(_get(base, 72, 0, 1))),
            "summary": html_text(_str(_get(base, 73, 0, 1))) or out.get("summary", ""),
            "developer": _str(_get(base, 68, 0)),
            "developer_website": _str(_get(base, 69, 0, 5, 2)),
            "category": _str(_get(base, 79, 0, 0, 0)),
            "category_id": _str(_get(base, 79, 0, 0, 2)),
            "installs": _str(_get(base, 13, 0)),
            "content_rating": _str(_get(base, 9, 0)),
            "updated": time.strftime("%Y-%m-%d", time.gmtime(updated)) if isinstance(updated, int) and 0 < updated < 4102444800 else "",
            "version": _str(_get(base, 140, 0, 0, 0)),
            "min_android": min_android,
            "min_sdk": min_sdk if isinstance(min_sdk, int) and not isinstance(min_sdk, bool) else None,
            "icon": _str(_get(base, 95, 0, 3, 2)),
            "header_image": _str(_get(base, 96, 0, 3, 2)),
            "screenshots": [s for s in shots if isinstance(s, str) and s],
            "recent_changes": html_text(_str(_get(base, 144, 1, 1))),
        }.items() if v not in (None, "", [])})
    if not out.get("name"):
        return {}
    out.setdefault("description", out.get("summary", ""))
    return out


def _float(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return round(number, 2) if math.isfinite(number) else None


def _str(value) -> str:
    """A text field of the page data: strings as they are, numbers as text, anything else ''."""

    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return ""


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def store_url(package: str, lang: str = "hu") -> str:
    return f"{DETAILS}?id={package}&hl={lang}"


def app(package: str, lang: str = "hu", country: str = "HU") -> dict:
    """parse_app_page() of one package plus "url" (proof link); {} when the app is not on Google Play (404),
    the page is unusable or robots.txt forbids it."""

    if not re.fullmatch(r"[A-Za-z][\w]*(\.[A-Za-z_][\w]*)+", package or ""):
        return {}
    url = f"{DETAILS}?id={package}&hl={lang}&gl={country}"
    if not robots_allows(url):
        net.FAILURES.append("play.google.com: a robots.txt tiltja")
        return {}
    page = net.get_text(DETAILS, params={"id": package, "hl": lang, "gl": country}, quiet=True)
    data = parse_app_page(page or "")
    if data:
        data["url"] = store_url(package, lang)
        data["package"] = package
    return data
