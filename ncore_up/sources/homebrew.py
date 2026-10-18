"""Homebrew (formulae.brew.sh JSON API): macOS applications (casks) and command-line programs (formulae) with name,
description, official homepage, version, minimum macOS and the .app name. No key; BSD-2 licensed data.

Single items come from /api/cask/<token>.json and /api/formula/<name>.json; name searches use the full cask list
(/api/cask.json, ~19 MB), downloaded at most once a day and kept as a compact index in ``net.cache_dir()``.
"""

from __future__ import annotations

import json
import re
import time

from .. import net

API = "https://formulae.brew.sh/api/"
_DAY = 86400
MACOS = {"tahoe": "26", "sequoia": "15", "sonoma": "14", "ventura": "13", "monterey": "12", "big_sur": "11", "catalina": "10.15",
         "mojave": "10.14", "high_sierra": "10.13", "sierra": "10.12", "el_capitan": "10.11", "yosemite": "10.10"}


def _min_macos(depends_on: dict) -> str:
    """'macOS 12 (Monterey)' from a cask's depends_on.macos ({">=": ["monterey"]} or ">= :monterey")."""

    value = (depends_on or {}).get("macos") or {}
    names = []
    if isinstance(value, dict):
        for key, items in value.items():
            if ">" in key:
                names += items if isinstance(items, list) else [items]
    elif isinstance(value, str):
        names = re.findall(r":?(\w+)", value.split(">=")[-1]) if ">=" in value else []
    for name in names:
        name = str(name).strip(":").lower()
        if name in MACOS:
            return f"macOS {MACOS[name]} ({name.replace('_', ' ').title()})"
        if re.fullmatch(r"\d+(?:\.\d+)?", name):  # the API now gives numbers: {">=": ["12"]}
            label = next((key for key, number in MACOS.items() if number == name), "")
            return f"macOS {name}" + (f" ({label.replace('_', ' ').title()})" if label else "")
    return ""


def parse_cask(data: dict) -> dict:
    """{"token", "name", "names", "desc", "homepage", "version", "min_macos", "apps", "url", "kind": "cask"}"""

    if not isinstance(data, dict) or not data.get("token"):
        return {}
    apps = []
    for artifact in data.get("artifacts") or []:
        if isinstance(artifact, dict) and artifact.get("app"):
            apps += [a for a in artifact["app"] if isinstance(a, str)]
    names = [n for n in data.get("name") or [] if n]
    return {"token": data["token"], "name": names[0] if names else data["token"], "names": names, "desc": data.get("desc") or "",
            "homepage": data.get("homepage") or "", "version": str(data.get("version") or ""), "min_macos": _min_macos(data.get("depends_on") or {}),
            "apps": apps, "url": data.get("url") or "", "kind": "cask"}


def parse_formula(data: dict) -> dict:
    if not isinstance(data, dict) or not data.get("name"):
        return {}
    return {"token": data["name"], "name": data.get("full_name") or data["name"], "names": [data["name"]], "desc": data.get("desc") or "",
            "homepage": data.get("homepage") or "", "version": str((data.get("versions") or {}).get("stable") or ""),
            "license": data.get("license") or "", "min_macos": "", "apps": [], "url": "", "kind": "formula"}


def token(name: str) -> str:
    """Homebrew's token style: 'VLC media player' -> 'vlc-media-player'."""

    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower().replace("+", "-plus").replace("&", "and")).strip("-")


def _slug(name: str) -> str:
    return name if re.fullmatch(r"[a-z0-9][a-z0-9@._+-]*", name or "") else token(name)


def cask(name: str) -> dict:
    """A cask by token or program name ({} when Homebrew has none)."""

    slug = _slug(name)
    if not slug:
        return {}
    return parse_cask(net.get_json(f"{API}cask/{slug}.json", quiet=True, expire=_DAY) or {})


def formula(name: str) -> dict:
    slug = _slug(name)
    if not slug:
        return {}
    return parse_formula(net.get_json(f"{API}formula/{slug}.json", quiet=True, expire=_DAY) or {})


def _no_cache():
    try:
        import requests_cache  # optional dependency

        return getattr(requests_cache, "DO_NOT_CACHE", 0)
    except ImportError:
        return 0


def index(max_age: float = _DAY) -> list[dict]:
    """Compact list of every cask ({"token", "names", "desc", "homepage", "version"}), refreshed daily."""

    path = net.cache_dir() / "homebrew-casks.json"
    try:
        if time.time() - path.stat().st_mtime < max_age:
            return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    response = net.request("GET", API + "cask.json", expire=_no_cache(), quiet=True)
    items: list[dict] = []
    if response is not None:
        try:
            items = [{"token": c.get("token", ""), "names": c.get("name") or [], "desc": c.get("desc") or "", "homepage": c.get("homepage") or "",
                      "version": str(c.get("version") or "")} for c in response.json() if isinstance(c, dict)]
            path.write_text(json.dumps(items), encoding="utf-8")
        except (ValueError, OSError, AttributeError):
            items = []
    if not items:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
    return items


def search(name: str, limit: int = 5, items: list[dict] | None = None) -> list[dict]:
    """Casks whose token or name resembles ``name``, best first, each with a "score" (0..100)."""

    from .winget import normalize, similarity

    wanted = normalize(name)
    if len(wanted) < 2:
        return []
    ranked = []
    for item in items if items is not None else index():
        candidates = [normalize(item.get("token", ""))] + [normalize(n) for n in item.get("names") or []]
        score = max((similarity(wanted, c) for c in candidates if c), default=0.0)
        if score >= 70:
            ranked.append(dict(item, score=round(score, 1)))
    ranked.sort(key=lambda i: -i["score"])
    return ranked[:limit]


def find(name: str) -> dict:
    """The cask (or formula) for a program name: direct token lookup, then the index search (score >= 92)."""

    found = cask(name)
    if found:
        found["score"] = 100.0
        return found
    for hit in search(name, 1):
        if hit["score"] >= 92:
            found = cask(hit["token"]) or {}
            if found:
                found["score"] = hit["score"]
                return found
    found = formula(name)
    if found:
        found["score"] = 100.0
    return found
