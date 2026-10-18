"""GOG Database (gogdb.org): the product page as version proof link, and a parser for its product JSON.

gogdb.org's robots.txt says ``User-agent: * / Disallow: /data/``, so this module does not fetch the JSON under
/data (GOG's own content-system build list, ``gog.builds``, gives the same build versions). ``parse_product``
stays for product.json files the user downloads by hand or takes from the operator's backups. The product page
URL needs no request.

    www.gogdb.org/data/products/<id>/product.json   -> builds[], dl_installer[], localizations[], ... (not fetched)
    www.gogdb.org/product/<id>                      -> proof link
"""

from __future__ import annotations

import re

PAGE = "https://www.gogdb.org/product/{id}"


def product_url(gog_id: str) -> str:
    return PAGE.format(id=gog_id) if re.fullmatch(r"\d{5,12}", str(gog_id or "")) else ""


def parse_product(data: dict) -> dict:
    """product.json -> {"id", "title", "slug", "type", "builds": [{"version", "date", "os", "branch"}] (newest
    first), "installers": [{"version", "os", "language", "size"}], "localizations": [{"code", "name", "text",
    "audio"}], "developers", "publishers", "date", "url"}."""

    if not isinstance(data, dict) or not data.get("id"):
        return {}
    builds = [{"version": (b.get("version") or "").strip(), "date": (b.get("date_published") or "")[:10], "os": b.get("os", ""),
               "branch": b.get("branch") or ""} for b in data.get("builds") or [] if isinstance(b, dict)]
    builds.sort(key=lambda b: b["date"], reverse=True)
    installers = [{"version": (i.get("version") or "").strip(), "os": i.get("os", ""),
                   "language": (i.get("language") or {}).get("code", "") if isinstance(i.get("language"), dict) else i.get("language", ""),
                   "size": int(i.get("total_size") or 0)} for i in data.get("dl_installer") or [] if isinstance(i, dict)]
    return {
        "id": str(data["id"]), "title": data.get("title", ""), "slug": data.get("slug", ""), "type": data.get("type", ""),
        "builds": builds, "installers": installers,
        "localizations": [{"code": loc.get("code", ""), "name": loc.get("name", ""), "text": bool(loc.get("text")), "audio": bool(loc.get("audio"))}
                          for loc in data.get("localizations") or [] if isinstance(loc, dict)],
        "developers": list(data.get("developers") or []), "publishers": list(data.get("publishers") or []),
        "date": (data.get("global_date") or data.get("store_date") or "")[:10], "url": product_url(str(data["id"])),
    }
