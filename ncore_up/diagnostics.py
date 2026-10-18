"""Source readiness and bounded live metadata probes; never upload or print keys."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from importlib.util import find_spec

from . import __version__, keys, net


# module, display name, groups, credentials; availability is not an identification score.
CATALOGUE = [
    ("porthu", "PORT.hu", ["video"], []),
    ("tmdb", "TMDB", ["video"], ["TMDB_API"]),
    ("tvmaze", "TVmaze", ["video"], []),
    ("wikidata", "Wikidata", ["video", "music", "ebook", "game", "console", "program"], []),
    ("wikipedia", "Wikipédia", ["video", "music", "game", "console", "program"], []),
    ("musicbrainz", "MusicBrainz", ["music", "clip"], []),
    ("coverart", "Cover Art Archive", ["music", "clip"], []),
    ("discogs", "Discogs", ["music", "clip"], ["DISCOGS_TOKEN"]),
    ("acoustid", "AcoustID", ["music"], ["ACOUSTID_KEY"]),
    ("lastfm", "Last.fm", ["music"], ["LASTFM_KEY"]),
    ("theaudiodb", "TheAudioDB", ["music"], []),
    ("spotify", "Spotify", ["music"], ["SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET"]),
    ("setlistfm", "setlist.fm", ["clip"], ["SETLISTFM_KEY"]),
    ("moly", "moly.hu", ["ebook", "music"], []),
    ("googlebooks", "Google Books", ["ebook", "music"], ["GOOGLE_BOOKS_KEY"]),
    ("openlibrary", "Open Library", ["ebook", "music"], []),
    ("comicvine", "ComicVine", ["ebook"], ["COMICVINE_KEY"]),
    ("anilist", "AniList", ["ebook", "video"], []),
    ("mangaupdates", "MangaUpdates", ["ebook"], []),
    ("steam", "Steam", ["game"], []),
    ("gog", "GOG", ["game"], []),
    ("gogdb", "GOGDB", ["game"], []),
    ("igdb", "IGDB", ["game", "console"], ["IGDB_CLIENT_ID", "IGDB_CLIENT_SECRET"]),
    ("pcgw", "PCGamingWiki", ["game"], []),
    ("titledb", "Switch titledb", ["console"], []),
    ("gametdb", "GameTDB", ["console"], []),
    ("libretro_dat", "libretro / Redump", ["console"], []),
    ("psstore", "PS Store", ["console"], []),
    ("msstore", "Microsoft Store", ["console", "program"], []),
    ("googleplay", "Google Play", ["mobil"], []),
    ("itunes", "App Store", ["mobil"], []),
    ("fdroid", "F-Droid", ["mobil"], []),
    ("winget", "winget", ["program"], []),
    ("chocolatey", "Chocolatey", ["program"], []),
    ("homebrew", "Homebrew", ["program"], []),
    ("msdownload", "Microsoft ISO", ["program"], []),
]


def _probe(source: dict) -> dict:
    identifier = source["id"]
    common = {"retries": 0, "expire": 0, "timeout": 10, "quiet": True}
    try:
        if identifier == "spotify":
            from .sources import spotify
            found = spotify.search_albums("Omega", "10000 lépés", limit=1)
            return {**source, "status": "OK" if found else "LIVE_FAILED", "probe": "album_search"}
        urls = {
            "porthu": ("https://port.hu/search/suggest-list", {"q": "Breaking Bad"}),
            "tvmaze": ("https://api.tvmaze.com/lookup/shows", {"imdb": "tt0903747"}),
            "wikidata": ("https://www.wikidata.org/wiki/Special:EntityData/Q1079.json", {}),
            "musicbrainz": ("https://musicbrainz.org/ws/2/artist/", {"query": "artist:Omega", "fmt": "json", "limit": 1}),
            "openlibrary": ("https://openlibrary.org/api/books", {"bibkeys": "ISBN:9780441172719", "format": "json", "jscmd": "data"}),
            "steam": ("https://store.steampowered.com/api/appdetails", {"appids": "400", "l": "hungarian", "cc": "HU"}),
            "gog": ("https://api.gog.com/products/1207658924", {"expand": "description", "locale": "en-US"}),
            "itunes": ("https://itunes.apple.com/lookup", {"bundleId": "com.spotify.client", "country": "hu"}),
            "fdroid": ("https://f-droid.org/api/v1/packages/org.videolan.vlc", {}),
            "chocolatey": ("https://community.chocolatey.org/api/v2/Search()", {"$filter": "IsLatestVersion", "searchTerm": "'vlc'", "targetFramework": "''", "includePrerelease": "false", "$top": "1"}),
            "moly": ("https://moly.hu/konyvek/szabo-magda-az-ajto", {}),
        }
        headers = None
        if identifier == "tmdb":
            params = {"external_source": "imdb_id", "language": "hu-HU"}
            key = keys.get("TMDB_API")
            headers = {"Authorization": "Bearer " + key} if key.startswith("eyJ") else None
            if headers is None:
                params["api_key"] = key
            url = "https://api.themoviedb.org/3/find/tt0903747"
        elif identifier == "googlebooks":
            from .sources import googlebooks
            url = googlebooks.API
            params = {"q": "harry potter", "maxResults": 10, "printType": "BOOKS", "key": keys.get("GOOGLE_BOOKS_KEY")}
            common = {**common, "retries": 1}  # the service intermittently returns 503 even for valid searches
        elif identifier == "discogs":
            from .sources import discogs
            url = discogs.API + "database/search"
            params = {"artist": "Omega", "release_title": "10000 lépés", "type": "release", "per_page": 1}
            headers = discogs._headers()
        elif identifier in urls:
            url, params = urls[identifier]
        else:
            return source  # no huge database downloads or generated API calls in a health check
        response = net.get(url, params=params, headers=headers, **common)
        if response is None:
            return {**source, "status": "LIVE_FAILED", "probe": "metadata"}
        if identifier == "moly":
            from .sources.moly import parse_book
            valid = bool(parse_book(response.text, response.url).get("title"))
        elif identifier == "chocolatey":
            from .sources.chocolatey import parse_feed
            valid = bool(parse_feed(response.text))
        else:
            data = response.json()
            valid = {
                "tvmaze": lambda: isinstance(data, dict) and bool(data.get("id")),
                "wikidata": lambda: isinstance(data, dict) and bool(data.get("entities", {}).get("Q1079")),
                "musicbrainz": lambda: isinstance(data, dict) and bool(data.get("artists")),
                "openlibrary": lambda: isinstance(data, dict) and bool(data.get("ISBN:9780441172719", {}).get("title")),
                "googlebooks": lambda: isinstance(data, dict) and bool(googlebooks.parse_search(data)),
                "steam": lambda: isinstance(data, dict) and bool(data.get("400", {}).get("success")),
                "gog": lambda: isinstance(data, dict) and bool(data.get("title")),
                "itunes": lambda: isinstance(data, dict) and bool(data.get("results")),
                "fdroid": lambda: isinstance(data, dict) and bool(data.get("packages")),
                "tmdb": lambda: isinstance(data, dict) and bool(data.get("tv_results")),
                "discogs": lambda: isinstance(data, dict) and bool(data.get("results")),
                "porthu": lambda: _porthu_valid(data),
            }[identifier]()
        return {**source, "status": "OK" if valid else "INVALID_RESPONSE", "probe": "metadata", "http_status": response.status_code}
    except Exception as exc:  # a diagnostic must not interrupt batch processing or expose response bodies
        return {**source, "status": "LIVE_FAILED", "probe": "metadata", "error_type": type(exc).__name__}


def _porthu_valid(data) -> bool:
    from .sources.porthu import parse_suggest
    return bool(parse_suggest(data))


def report(live: bool = False) -> dict:
    sources = []
    for module, name, groups, credentials in CATALOGUE:
        missing = [key for key in credentials if not keys.get(key)]
        if module == "discogs" and keys.get("DISCOGS_CONSUMER_KEY") and keys.get("DISCOGS_CONSUMER_SECRET"):
            missing = []
        implemented = find_spec("ncore_up.sources." + module) is not None
        sources.append({"id": module, "name": name, "groups": groups, "missing_keys": missing,
                        "status": "MISSING_MODULE" if not implemented else "MISSING_CREDENTIALS" if missing else "NOT_CHECKED"})
        if module == "discogs":
            sources[-1]["credential_options"] = [["DISCOGS_TOKEN"], ["DISCOGS_CONSUMER_KEY", "DISCOGS_CONSUMER_SECRET"]]
    sources.append({"id": "deepl", "name": "DeepL fordítás", "groups": ["all"], "missing_keys": [] if keys.get("DEEPL_KEY") else ["DEEPL_KEY"],
                    "status": "NOT_CHECKED" if keys.get("DEEPL_KEY") else "MISSING_CREDENTIALS"})
    if live:
        with ThreadPoolExecutor(max_workers=4) as executor:
            sources = list(executor.map(lambda item: _probe(item) if item["status"] == "NOT_CHECKED" else item, sources))
    return {"version": __version__, "checked_at": datetime.now(timezone.utc).isoformat(), "live": live, "sources": sources,
            "note": "NOT_CHECKED nem igazol élő elérhetőséget; a hiányzó kulcs csak az adott kiegészítő forrást kapcsolja ki."}
