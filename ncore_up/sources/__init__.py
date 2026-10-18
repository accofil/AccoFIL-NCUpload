"""Outside metadata sources, one module per service.

Conventions (every module follows them):

* Network only through ``ncore_up.net`` (honest User-Agent, pacing, cache, retries). Never imitate a browser,
  never bypass bot protection, respect robots.txt for scraped pages.
* Split fetching from parsing: ``parse_*`` functions take text/JSON and are tested offline with fixtures in
  ``tests/fixtures/<service>/``; fetch functions call ``net`` and the parser.
* Fetch functions never raise on network/HTTP/JSON problems: they return ``None`` / ``{}`` / ``[]``.
* Keys come from ``ncore_up.keys.get("NAME")``; without its key a module returns empty results.
* Return plain dicts / lists / ``ncore_up.meta`` objects with documented keys.
"""
