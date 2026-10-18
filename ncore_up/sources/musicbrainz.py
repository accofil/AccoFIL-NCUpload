"""MusicBrainz WS/2: release search and lookups (by release id, disc id/TOC, barcode, catalogue number, recording),
release groups, artists, genres/tags, labels, media/tracks and url relationships. Core data is CC0.

No key; a User-Agent with contact is required (net.AGENT). Limit ~1 request/s per IP (net paces musicbrainz.org at
1.1 s and retries 503 "server busy" with backoff). Search uses LOOSE word groups (``release:(w1 w2) AND
artist:(w1)``): phrase queries miss "Ágy, asztal, tv" vs "Ágy asztal tévé", and the index folds accents itself.
A release URL (https://musicbrainz.org/release/<mbid>) is a valid nCore proof link.
"""

from __future__ import annotations

import re

from .. import net

API = "https://musicbrainz.org/ws/2/"
SITE = "https://musicbrainz.org/"
RELEASE_INC = "recordings+labels+release-groups+genres+tags+url-rels+artist-credits+discids"
VIDEO_FORMATS = ("DVD-Video", "Blu-ray", "HD-DVD", "VHS", "VCD", "SVCD", "LaserDisc", "UMD")
_LUCENE = re.compile(r'([+\-&|!(){}\[\]^"~*?:\\/])')
_MBID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def escape(text: str) -> str:
    return _LUCENE.sub(r"\\\1", text or "")


def phrase(text: str) -> str:
    """A quoted Lucene phrase: only backslash and quote need escaping inside."""

    return '"' + (text or "").replace("\\", "\\\\").replace('"', '\\"') + '"'


def words(text: str) -> str:
    """Loose Lucene word group: punctuation dropped, '10 000' joined to '10000' (MusicBrainz writes it so).

    Upper-case AND/OR/NOT/TO are Lucene operators ('I AM NOT A ROBOT' finds the wrong releases), so they are
    lower-cased; the index itself is case-insensitive."""

    text = re.sub(r"(?<=\d)[ .,](?=\d{3}\b)", "", text or "")
    return " ".join(w.lower() if w in {"AND", "OR", "NOT", "TO"} else w for w in re.findall(r"\w+", text))


def build_query(artist: str = "", title: str = "", *, tracks: int | None = None, mediums: int | None = None, year: str = "",
                formats: tuple[str, ...] = (), barcode: str = "", catno: str = "", extra: str = "") -> str:
    """Lucene query of a release search. ``tracks`` only finds single-medium releases reliably (2-CD 'The Wall' does not
    match tracks:26), so multi-disc rips filter by ``mediums`` instead."""

    parts = []
    if title and words(title):
        parts.append(f"release:({words(title)})")
    if artist and words(artist):
        parts.append(f"artist:({words(artist)})")
    if tracks:
        parts.append(f"tracks:{int(tracks)}")
    if mediums:
        parts.append(f"mediums:{int(mediums)}")
    if year and re.fullmatch(r"\d{4}", year):
        parts.append(f"date:{year}")
    if formats:
        parts.append("(" + " OR ".join(f"format:{phrase(f)}" for f in formats) + ")")
    if barcode:
        parts.append(f"barcode:{re.sub(r'[^0-9]', '', barcode)}")
    if catno:
        parts.append(f"catno:{phrase(catno)}")
    if extra:
        parts.append(extra)
    return " AND ".join(parts)


def _get(path: str, quiet: bool = False, **params):
    params["fmt"] = "json"
    return net.get_json(API + path, params=params, headers={"Accept": "application/json"}, quiet=quiet)


def _credit(credits: list) -> tuple[str, list[str], list[str], dict[str, str]]:
    text = "".join((c.get("name") or (c.get("artist") or {}).get("name", "")) + (c.get("joinphrase") or "") for c in credits or [])
    names = [(c.get("artist") or {}).get("name") or c.get("name", "") for c in credits or []]
    ids = [(c.get("artist") or {}).get("id", "") for c in credits or []]
    types = {(c.get("artist") or {}).get("id", ""): (c.get("artist") or {}).get("type", "") for c in credits or []}
    return text.strip(), [n for n in names if n], [i for i in ids if i], types


def _votes(items: list) -> list[tuple[str, int]]:
    out = [(str(g.get("name", "")).strip(), int(g.get("count") or 0)) for g in items or [] if g.get("name")]
    return sorted(out, key=lambda x: -x[1])


def _urls(relations: list) -> list[dict]:
    return [{"type": r.get("type", ""), "url": (r.get("url") or {}).get("resource", "")} for r in relations or []
            if r.get("target-type") == "url" and (r.get("url") or {}).get("resource")]


def _wikidata(urls: list[dict]) -> str:
    for item in urls:
        match = re.search(r"wikidata\.org/wiki/(Q\d+)", item["url"])
        if item["type"] == "wikidata" and match:
            return match.group(1)
    return ""


def _labels(info: list) -> list[dict]:
    return [{"name": ((li.get("label") or {}).get("name") or "").strip(), "catno": (li.get("catalog-number") or "").strip()}
            for li in info or [] if li.get("label") or li.get("catalog-number")]


def parse_search(data: dict) -> list[dict]:
    """Release search hits: [{"id", "title", "artist", "artist_ids", "date", "year", "country", "score", "track_count",
    "formats", "barcode", "labels", "release_group_id", "primary_type", "secondary_types", "status", "language"}]"""

    hits = []
    for item in (data or {}).get("releases") or []:
        artist, _names, ids, _types = _credit(item.get("artist-credit"))
        group = item.get("release-group") or {}
        date = item.get("date") or ""
        hits.append({
            "id": item.get("id", ""), "title": item.get("title", ""), "artist": artist, "artist_ids": ids, "date": date, "year": date[:4],
            "country": item.get("country") or "", "score": int(item.get("score") or 0), "track_count": int(item.get("track-count") or 0),
            "formats": [m.get("format") or "" for m in item.get("media") or []], "barcode": item.get("barcode") or "",
            "labels": _labels(item.get("label-info")), "release_group_id": group.get("id", ""), "primary_type": group.get("primary-type") or "",
            "secondary_types": list(group.get("secondary-types") or []), "status": item.get("status") or "",
            "language": (item.get("text-representation") or {}).get("language") or "",
        })
    return hits


def parse_release(data: dict) -> dict:
    """A release lookup (inc=RELEASE_INC) as a flat dict; {} for an empty answer. Lengths are seconds (float)."""

    if not data or not data.get("id"):
        return {}
    artist, names, ids, types = _credit(data.get("artist-credit"))
    artist_genres: list = []
    for credit in data.get("artist-credit") or []:
        artist_genres += (credit.get("artist") or {}).get("genres") or []
    group = data.get("release-group") or {}
    media = []
    for medium in data.get("media") or []:
        tracks = []
        for track in medium.get("tracks") or []:
            track_artist, _n, _i, _t = _credit(track.get("artist-credit"))
            recording = track.get("recording") or {}
            length = track.get("length") or recording.get("length") or 0
            tracks.append({
                "number": str(track.get("number") or track.get("position") or ""), "position": int(track.get("position") or 0),
                "title": track.get("title") or recording.get("title", ""), "length": int(length) / 1000 if length else 0.0,
                "artist": track_artist if track_artist and track_artist != artist else "", "recording_id": recording.get("id", ""),
                "video": bool(recording.get("video")),
            })
        media.append({"position": int(medium.get("position") or len(media) + 1), "format": medium.get("format") or "",
                      "title": medium.get("title") or "", "discids": [d.get("id", "") for d in medium.get("discs") or []], "tracks": tracks})
    urls = _urls(data.get("relations"))
    labels = _labels(data.get("label-info"))
    date = data.get("date") or ""
    group_urls = _urls(group.get("relations"))
    return {
        "id": data["id"], "title": data.get("title", ""), "disambiguation": data.get("disambiguation") or "", "artist": artist,
        "artists": names, "artist_ids": ids, "artist_types": types, "date": date, "year": date[:4], "country": data.get("country") or "",
        "status": data.get("status") or "", "barcode": data.get("barcode") or "", "packaging": data.get("packaging") or "",
        "language": (data.get("text-representation") or {}).get("language") or "",
        "labels": labels, "label": next((li["name"] for li in labels if li["name"]), ""), "catno": next((li["catno"] for li in labels if li["catno"]), ""),
        "release_group": {
            "id": group.get("id", ""), "title": group.get("title", ""), "primary_type": group.get("primary-type") or "",
            "secondary_types": list(group.get("secondary-types") or []), "first_release_date": group.get("first-release-date") or "",
            "genres": _votes(group.get("genres")), "tags": _votes(group.get("tags")), "urls": group_urls, "wikidata": _wikidata(group_urls),
        },
        "genres": _votes(data.get("genres")), "tags": _votes(data.get("tags")), "artist_genres": _votes(artist_genres),
        "media": media, "track_count": sum(len(m["tracks"]) for m in media), "formats": [m["format"] for m in media],
        "urls": urls, "discogs_url": next((u["url"] for u in urls if u["type"] == "discogs"), ""),
        "cover_front": bool((data.get("cover-art-archive") or {}).get("front")), "url": f"{SITE}release/{data['id']}",
    }


def parse_release_group(data: dict) -> dict:
    if not data or not data.get("id"):
        return {}
    artist, _names, ids, _types = _credit(data.get("artist-credit"))
    urls = _urls(data.get("relations"))
    return {
        "id": data["id"], "title": data.get("title", ""), "artist": artist, "artist_ids": ids, "primary_type": data.get("primary-type") or "",
        "secondary_types": list(data.get("secondary-types") or []), "first_release_date": data.get("first-release-date") or "",
        "genres": _votes(data.get("genres")), "tags": _votes(data.get("tags")), "urls": urls, "wikidata": _wikidata(urls),
        "url": f"{SITE}release-group/{data['id']}",
    }


def parse_artist(data: dict) -> dict:
    if not data or not data.get("id"):
        return {}
    urls = _urls(data.get("relations"))
    return {
        "id": data["id"], "name": data.get("name", ""), "type": data.get("type") or "", "country": data.get("country") or "",
        "disambiguation": data.get("disambiguation") or "", "genres": _votes(data.get("genres")), "tags": _votes(data.get("tags")),
        "urls": urls, "wikidata": _wikidata(urls), "discogs_url": next((u["url"] for u in urls if u["type"] == "discogs"), ""),
        "official": next((u["url"] for u in urls if u["type"] == "official homepage"), ""), "url": f"{SITE}artist/{data['id']}",
    }


def parse_artist_search(data: dict) -> list[dict]:
    return [{"id": a.get("id", ""), "name": a.get("name", ""), "sort_name": a.get("sort-name", ""), "type": a.get("type") or "",
             "country": a.get("country") or "", "score": int(a.get("score") or 0), "aliases": [x.get("name", "") for x in a.get("aliases") or []]}
            for a in (data or {}).get("artists") or []]


def parse_discid(data: dict) -> list[dict]:
    """Releases of a disc id or fuzzy TOC answer (same release shape as parse_release)."""

    return [r for r in (parse_release(item) for item in (data or {}).get("releases") or []) if r]


# --- fetch -------------------------------------------------------------------------------------------------

def search(query: str, limit: int = 25) -> list[dict]:
    if not query:
        return []
    return parse_search(_get("release/", query=query, limit=min(100, max(1, limit))) or {})


def search_releases(artist: str = "", title: str = "", *, limit: int = 25, **filters) -> list[dict]:
    return search(build_query(artist, title, **filters), limit)


def release(mbid: str) -> dict:
    if not _MBID.match(mbid or ""):
        return {}
    return parse_release(_get(f"release/{mbid}", inc=RELEASE_INC) or {})


def release_group(rgid: str) -> dict:
    if not _MBID.match(rgid or ""):
        return {}
    return parse_release_group(_get(f"release-group/{rgid}", inc="genres+tags+url-rels+artist-credits") or {})


def artist(mbid: str) -> dict:
    if not _MBID.match(mbid or ""):
        return {}
    return parse_artist(_get(f"artist/{mbid}", inc="genres+tags+url-rels") or {})


def search_artists(name: str, limit: int = 5) -> list[dict]:
    if not words(name):
        return []
    return parse_artist_search(_get("artist/", query=f"artist:({words(name)})", limit=limit) or {})


def discid_lookup(discid: str = "", toc: str = "") -> list[dict]:
    """Releases with this disc id; with ``toc`` also the fuzzy matches when the id itself is unknown."""

    if not discid and not toc:
        return []
    params = {"inc": "artist-credits+labels+recordings+release-groups"}
    if toc:
        params["toc"] = toc
    if discid and not re.fullmatch(r"[A-Za-z0-9._-]{28}", discid):
        return []
    # an unknown disc is HTTP 404: a normal answer, not a source failure for net.FAILURES
    return parse_discid(_get(f"discid/{discid or '-'}", quiet=True, **params) or {})


def by_barcode(barcode: str) -> list[dict]:
    code = re.sub(r"[^0-9]", "", barcode or "")
    return search(f"barcode:{code}", 25) if len(code) >= 8 else []


def by_catno(catno: str, label: str = "") -> list[dict]:
    if not (catno or "").strip():
        return []
    query = f"catno:{phrase(catno.strip())}" + (f" AND label:({words(label)})" if words(label) else "")
    return search(query, 25)


def recording_releases(recording_id: str) -> list[str]:
    if not _MBID.match(recording_id or ""):
        return []
    data = _get(f"recording/{recording_id}", inc="releases") or {}
    return [r.get("id", "") for r in data.get("releases") or [] if r.get("id")]
