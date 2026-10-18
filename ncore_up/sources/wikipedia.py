"""Wikipedia page summaries (REST API) as Hungarian description text.

Wikipedia text is CC BY-SA: when used, the description must credit "Forrás: Wikipédia (CC BY-SA)". It is never a
proof link on nCore. Titles come from Wikidata sitelinks (``wikidata.parse_entity()["huwiki"]``), not from search.
"""

from __future__ import annotations

from urllib.parse import quote

from .. import net

SUMMARY = "https://{lang}.wikipedia.org/api/rest_v1/page/summary/{title}"
CREDIT = "Wikipédia (CC BY-SA)"


def parse_summary(data: dict) -> dict:
    """{"title", "extract", "url", "thumbnail"}; disambiguation pages give {}."""

    if not data or data.get("type") == "disambiguation":
        return {}
    return {
        "title": data.get("title", ""),
        "extract": (data.get("extract") or "").strip(),
        "url": ((data.get("content_urls") or {}).get("desktop") or {}).get("page", ""),
        "thumbnail": (data.get("thumbnail") or {}).get("source", ""),
    }


def summary(title: str, lang: str = "hu") -> dict:
    if not title:
        return {}
    data = net.get_json(SUMMARY.format(lang=lang, title=quote(title.replace(" ", "_"), safe="")), quiet=True) or {}
    return parse_summary(data)
