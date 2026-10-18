"""Hungarian title/page fallback. A search hit is accepted only with the exact series/film IMDb id."""
from __future__ import annotations

import re
from urllib.parse import quote_plus, urljoin, urlsplit

from bs4 import BeautifulSoup

from .. import net

BASE = "https://www.mafab.hu"


def parse_search(page: str) -> list[dict]:
    soup = BeautifulSoup(page or "", "html.parser")
    hits = []
    for block in soup.select(".search-item-movie"):
        anchor = block.select_one("h4 a[href*='/movies/']")
        if anchor is None:
            continue
        url = urljoin(BASE, anchor["href"])
        if urlsplit(url).hostname not in {"mafab.hu", "www.mafab.hu"}:
            continue
        year = re.search(r"\b(?:19|20)\d{2}\b", block.get_text(" ", strip=True))
        hits.append({"url": url, "year": year[0] if year else ""})
    return hits


def parse_detail(page: str, url: str) -> dict:
    soup = BeautifulSoup(page or "", "html.parser")
    title = soup.select_one("h1")
    imdb_ids = set()
    for anchor in soup.select("a[href]"):
        parts = urlsplit(anchor["href"])
        if parts.hostname in {"imdb.com", "www.imdb.com"}:
            match = re.fullmatch(r"/title/(tt\d+)/?", parts.path)
            if match:
                imdb_ids.add(match[1])
    if title is None or len(imdb_ids) != 1:
        return {}
    year = re.search(r"\b(?:19|20)\d{2}\b", soup.title.get_text() if soup.title else "")
    return {"title": title.get_text(" ", strip=True), "imdb": next(iter(imdb_ids)),
            "year": year[0] if year else "", "url": url}


def find_by_imdb(imdb: str, titles: list[str], year: str = "") -> dict:
    if not re.fullmatch(r"tt\d+", imdb or ""):
        return {}
    seen = set()
    for title in list(dict.fromkeys(t for t in titles if t))[:2]:
        page = net.get_text(BASE + "/search/&search=" + quote_plus(title), retries=0)
        if page is None:
            continue
        for hit in parse_search(page)[:4]:
            if hit["url"] in seen or (year and hit["year"] and year != hit["year"]):
                continue
            seen.add(hit["url"])
            page = net.get_text(hit["url"], retries=0)
            detail = parse_detail(page or "", hit["url"])
            if detail.get("imdb") == imdb and not (year and detail.get("year") and year != detail["year"]):
                return detail
    return {}
