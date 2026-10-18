"""Shared HTTP layer for every outside source: honest User-Agent, per-host pacing, retries, an on-disk cache.

Every source module calls ``net.get_json`` / ``net.get_text`` (or ``net.session()`` for special cases). Failures are
never raised to the caller: a lookup that fails returns ``None`` and is recorded in ``net.FAILURES`` so that
``ncore-up --check`` and the run log can show which source is down.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests
from . import __version__

AGENT = f"AccoFIL-NCUpload/{__version__} (+https://github.com/accofil/AccoFIL-NCUpload)"
TIMEOUT = 25

# Minimum seconds between two requests to the same host (documented or polite limits).
PACE = {
    "musicbrainz.org": 1.1,
    "coverartarchive.org": 0.5,
    "api.discogs.com": 1.1,
    "api.acoustid.org": 0.34,
    "itunes.apple.com": 3.0,
    "graphql.anilist.co": 2.1,
    "comicvine.gamespot.com": 18.0,
    "www.pcgamingwiki.com": 1.0,
    "api.igdb.com": 0.26,
    "port.hu": 1.0,
    "www.mafab.hu": 1.0,
    "query.wikidata.org": 1.0,
    "moly.hu": 2.0,
    "play.google.com": 2.0,
    "store.steampowered.com": 1.5,
    "api.gog.com": 0.5,
    "www.googleapis.com": 0.2,
    "books.googleapis.com": 0.2,
    "api-free.deepl.com": 0.2,
    "api.deepl.com": 0.2,
    "api.tvmaze.com": 0.5,
    "v3.sg.media-imdb.com": 1.0,
    "api.setlist.fm": 0.55,
    "www.theaudiodb.com": 2.0,
    "ws.audioscrobbler.com": 0.25,
    "accounts.spotify.com": 0.2,
    "api.spotify.com": 0.2,
    "openlibrary.org": 1.0,
    "covers.openlibrary.org": 1.0,
    "api.mangaupdates.com": 1.0,
    "catalog.gog.com": 0.5,
    "content-system.gog.com": 0.5,
    "searching.nintendo-europe.com": 0.5,
    "displaycatalog.mp.microsoft.com": 0.5,
    "orbispatches.com": 2.0,
    "prosperopatches.com": 2.0,
    "www.gametdb.com": 30.0,
    "community.chocolatey.org": 1.0,
    "formulae.brew.sh": 0.5,
    "www.wikidata.org": 0.5,
    "learn.microsoft.com": 1.0,
    "www.microsoft.com": 1.0,
}
DEFAULT_PACE = 0.3
# Kept out of cache keys and redacted from stored responses (requests-cache), so no API key lands on disk.
SECRET_PARAMS = (
    "Authorization", "Proxy-Authorization", "X-API-Key", "x-api-key", "X-Auth-Token", "X-API-Token", "X-Access-Token",
    "access_token", "api_key", "apikey", "key", "token", "client", "client_id", "client_secret", "Client-ID", "auth_key",
)

FAILURES: list[str] = []
_LAST: dict[str, float] = {}
_LOCK = threading.Lock()
_SESSION: requests.Session | None = None
_CACHE_DIR: Path | None = None


def configure(cache_dir: Path | None, expire_days: float = 7.0) -> None:
    """Use an SQLite cache under ``cache_dir`` (requests-cache) when available; call once at start-up."""

    global _SESSION, _CACHE_DIR
    _CACHE_DIR = cache_dir
    session: requests.Session
    try:
        import requests_cache  # optional dependency

        if cache_dir is None:
            raise ImportError
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_dir.chmod(0o700)
        session = requests_cache.CachedSession(
            str(cache_dir / "http"), backend="sqlite", expire_after=expire_days * 86400,
            allowable_codes=(200,), allowable_methods=("GET", "HEAD", "POST"), stale_if_error=True,
            ignored_parameters=SECRET_PARAMS,
        )
        for path in cache_dir.glob('http.sqlite*'):
            path.chmod(0o600)
    except ImportError:
        session = requests.Session()
    session.headers["User-Agent"] = AGENT
    _SESSION = session


def session() -> requests.Session:
    if _SESSION is None:
        configure(None)
    assert _SESSION is not None
    return _SESSION


def cache_dir() -> Path:
    base = _CACHE_DIR or Path.home() / ".cache" / "ncore-up"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _pace(host: str) -> None:
    wait = PACE.get(host, DEFAULT_PACE)
    with _LOCK:
        now = time.monotonic()
        last = _LAST.get(host, 0.0)
        if now - last < wait:
            time.sleep(wait - (now - last))
        _LAST[host] = time.monotonic()


def _cached(method: str, url: str, params, headers, data, json_body) -> bool:
    """True when requests-cache already holds this exact request (then there is no reason to wait for the host)."""

    current = session()
    cache = getattr(current, "cache", None)
    if cache is None:
        return False
    try:
        prepared = current.prepare_request(requests.Request(method, url, params=params, headers=headers, data=data, json=json_body))
        return bool(cache.contains(request=prepared))
    except Exception:  # noqa: BLE001 - cache probing is best effort
        return False


def request(method: str, url: str, *, params: dict | None = None, headers: dict | None = None, data=None, json_body=None,
            retries: int = 3, expire: float | None = None, quiet: bool = False, timeout: float | None = None,
            stream: bool = False, cache: bool = True) -> requests.Response | None:
    """One HTTP call with pacing and retries on 429/5xx (honouring Retry-After). None when it finally fails."""

    host = urlsplit(url).netloc.lower()
    # OAuth response bodies contain credentials. Never send them through a cache,
    # even when a server adds ETag/Last-Modified to an expire=0 response.
    cache = cache and not stream and not urlsplit(url).path.rstrip('/').endswith('/token')
    extra = {}
    if cache and expire is not None and hasattr(session(), "cache"):
        extra["expire_after"] = expire
    for attempt in range(retries + 1):
        from_cache = False
        if not cache or not _cached(method, url, params, headers, data, json_body):
            _pace(host)
        try:
            options = dict(params=params, headers=headers, data=data, json=json_body, timeout=TIMEOUT if timeout is None else timeout)
            if cache:
                response = session().request(method, url, **options, **extra)
            else:
                with requests.Session() as plain:
                    plain.headers.update(session().headers)
                    response = plain.request(method, url, stream=stream, **options)
            from_cache = bool(getattr(response, "from_cache", False))
        except requests.RequestException as exc:
            response = None
            error = f"{host}: {type(exc).__name__}"
        else:
            if response.status_code < 400:
                return response
            error = f"{host}: HTTP {response.status_code}"
            if response.status_code not in {429, 500, 502, 503, 504}:
                response.close()
                break
        if from_cache:
            break
        delay = 2 ** attempt * 2
        if response is not None and response.headers.get("Retry-After", "").isdigit():
            delay = min(60, int(response.headers["Retry-After"]))
        if response is not None:
            response.close()
        if attempt < retries:
            time.sleep(delay)
    if not quiet:
        FAILURES.append(error)
    return None


def get(url: str, **kwargs) -> requests.Response | None:
    return request("GET", url, **kwargs)


def get_json(url: str, **kwargs):
    response = request("GET", url, **kwargs)
    if response is None:
        return None
    try:
        return response.json()
    except ValueError:
        FAILURES.append(f"{urlsplit(url).netloc}: nem JSON válasz")
        return None


def get_text(url: str, **kwargs) -> str | None:
    response = request("GET", url, **kwargs)
    if response is None:
        return None
    response.encoding = response.encoding or response.apparent_encoding
    return response.text


def get_bytes(url: str, *, max_bytes: int = 32 << 20, **kwargs) -> bytes:
    response = request("GET", url, stream=True, **kwargs)
    if response is None:
        return b""
    try:
        if response.headers.get('Content-Length', '').isdigit() and int(response.headers['Content-Length']) > max_bytes:
            raise ValueError('too large')
        data = bytearray()
        for chunk in response.iter_content(65536):
            if len(data) + len(chunk) > max_bytes:
                raise ValueError('too large')
            data.extend(chunk)
        return bytes(data)
    except (ValueError, requests.RequestException):
        if not kwargs.get('quiet'):
            FAILURES.append(f'{urlsplit(url).netloc}: a bináris válasz túl nagy vagy megszakadt')
        return b""
    finally:
        response.close()


def post_json(url: str, **kwargs):
    response = request("POST", url, **kwargs)
    if response is None:
        return None
    try:
        return response.json()
    except ValueError:
        return None
