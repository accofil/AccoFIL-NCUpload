"""TheAudioDB: artist and album data by MusicBrainz id, mainly for the Hungarian biography (``strBiographyHU``).

The public test key "123" is used (v1 API, low limits); Hungarian texts are rare and sometimes not Hungarian at
all, so the caller checks them with ``translate.is_hungarian``. Images are not used.
"""

from __future__ import annotations

import re

from .. import net

API = "https://www.theaudiodb.com/api/v1/json/{key}/"
TEST_KEY = "123"
_MBID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _text(value) -> str:
    return re.sub(r"\r\n?", "\n", str(value or "")).strip()


def parse_artist(data: dict) -> dict:
    """{"id", "name", "mbid", "biography_hu", "biography_en", "genre", "style", "formed", "country", "website"}"""

    items = (data or {}).get("artists") or []
    if not items:
        return {}
    item = items[0]
    website = _text(item.get("strWebsite"))
    return {
        "id": str(item.get("idArtist") or ""), "name": _text(item.get("strArtist")), "mbid": _text(item.get("strMusicBrainzID")),
        "biography_hu": _text(item.get("strBiographyHU")), "biography_en": _text(item.get("strBiographyEN") or item.get("strBiography")),
        "genre": _text(item.get("strGenre")), "style": _text(item.get("strStyle")), "formed": _text(item.get("intFormedYear")),
        "country": _text(item.get("strCountry")), "website": ("https://" + website) if website and not website.startswith("http") else website,
    }


def parse_album(data: dict) -> dict:
    """{"id", "name", "artist", "year", "description_hu", "description_en", "genre", "style", "label", "mbid"}"""

    items = (data or {}).get("album") or []
    if not items:
        return {}
    item = items[0]
    return {
        "id": str(item.get("idAlbum") or ""), "name": _text(item.get("strAlbum")), "artist": _text(item.get("strArtist")),
        "year": _text(item.get("intYearReleased")), "description_hu": _text(item.get("strDescriptionHU")),
        "description_en": _text(item.get("strDescriptionEN") or item.get("strDescription")), "genre": _text(item.get("strGenre")),
        "style": _text(item.get("strStyle")), "label": _text(item.get("strLabel")), "mbid": _text(item.get("strMusicBrainzID")),
    }


def artist(mbid: str) -> dict:
    if not _MBID.match(mbid or ""):
        return {}
    return parse_artist(net.get_json(API.format(key=TEST_KEY) + "artist-mb.php", params={"i": mbid}, quiet=True) or {})


def album(release_group_mbid: str) -> dict:
    if not _MBID.match(release_group_mbid or ""):
        return {}
    return parse_album(net.get_json(API.format(key=TEST_KEY) + "album-mb.php", params={"i": release_group_mbid}, quiet=True) or {})
