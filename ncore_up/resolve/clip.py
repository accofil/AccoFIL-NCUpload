"""Concert and music video resolver (nCore "clip"): artist, title, year, tracklist, style tags, proof link.

Artist/title/year come from the hints, the release name ("Artist - Title (2019)" or scene "Artist.Title.2019.1080p...",
split with a MusicBrainz artist lookup) and the container tags. Tracklist priority: real (non-placeholder)
chapter titles -> the MusicBrainz / Discogs video release (DVD-Video, Blu-ray) -> setlist.fm. Tags always
include "live".
"""

from __future__ import annotations

import re
from pathlib import Path

from .. import keys
from .. import parse as names
from .. import release as rel
from ..local import audiotags, chapters as chaps
from ..meta import Meta, Track
from ..sources import discogs, musicbrainz as mb, setlistfm
from . import music

DATE_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})[.\-_ ](0[1-9]|1[0-2])[.\-_ ](0[1-9]|[12]\d|3[01])(?!\d)")
DATE_DMY_RE = re.compile(r"(?<!\d)(0[1-9]|[12]\d|3[01])[.\-_](0[1-9]|1[0-2])[.\-_]((?:19|20)\d{2})(?!\d)")
VENUE_RE = re.compile(r"(?i)\blive\s+(at|in)\s+(?:the\s+)?(.+?)(?:\s+(?:19|20)\d{2}\b|$)")


def concert_date(text: str) -> str:
    match = DATE_RE.search(text or "")
    if match:
        return "-".join(match.groups())
    match = DATE_DMY_RE.search(text or "")
    return f"{match.group(3)}-{match.group(2)}-{match.group(1)}" if match else ""


def split_artist(words: str, lookup=None) -> tuple[str, str, dict]:
    """(artist, title, MB artist) of 'Pink Floyd Pulse': the longest leading word group that is exactly an artist name
    on MusicBrainz (at most 4 words, longest first)."""

    lookup = lookup or mb.search_artists
    parts = words.split()
    for size in range(min(4, len(parts) - 1), 0, -1):
        candidate = " ".join(parts[:size])
        for hit in lookup(candidate)[:5]:
            if music.fold(hit["name"]) == music.fold(candidate) or any(music.fold(a) == music.fold(candidate) for a in hit.get("aliases", [])):
                return hit["name"], " ".join(parts[size:]), hit
    return "", words, {}


def _first_artist_id(candidate: music.Candidate | None) -> str:
    ids = (candidate.data.get("artist_ids") or []) if candidate and candidate.source == "musicbrainz" else []
    return ids[0] if ids else ""


def _video_path(release: rel.Release) -> Path | None:
    return rel.main_video(release.path, release.files) if release.path.is_dir() else (release.path if release.path.is_file() else None)


def _format(release: rel.Release, video: Path | None, data: dict, resolution: str) -> str:
    if release.has_bdmv:
        return "Blu-ray (BDMV)"
    if release.has_video_ts:
        return "DVD-Video"
    stream = next((s for s in data.get("streams") or [] if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")), {})
    codec = {"h264": "H.264", "hevc": "H.265", "mpeg2video": "MPEG-2", "av1": "AV1", "vp9": "VP9"}.get(stream.get("codec_name", ""), stream.get("codec_name", "").upper())
    height = int(stream.get("height") or 0)
    res = resolution or (f"{height}p" if height else "")
    container = video.suffix.lstrip(".").upper() if video else ""
    return " ".join(x for x in [container, res, codec] if x)


def score(candidate: music.Candidate, artist: str, title: str, year: str) -> float:
    """Title and artist similarity (+ year): chapters are often a subset of the disc, so counts/durations are not compared."""

    info = music.candidate_info(candidate)
    value = 0.5 * music.similarity(info["title"], title) + 0.4 * music.similarity(info["artist"], artist)
    if year[:4].isdigit() and str(info["year"])[:4].isdigit():
        gap = abs(int(year[:4]) - int(str(info["year"])[:4]))
        value += 0.1 if gap == 0 else 0.05 if gap <= 2 else 0.0
    else:
        value += 0.05
    if info["unofficial"]:
        value -= 0.15
    return round(max(0.0, value), 3)


def _video_candidates(local: music.Local, artist: str, title: str) -> list[music.Candidate]:
    candidates: list[music.Candidate] = []
    hits = mb.search_releases(artist, title, formats=mb.VIDEO_FORMATS, limit=25)
    hits = [h for h in hits if any(f in mb.VIDEO_FORMATS for f in h["formats"])]
    candidates += music._mb_candidates(local, hits, "keresés (MusicBrainz, videó)", top=2)
    found = [h for h in discogs.search(artist=artist, release_title=title) if any(f in discogs.VIDEO_FORMATS for f in h["formats"])]
    candidates += music._discogs_candidates(local, found, "keresés (Discogs, videó)", top=2)
    return candidates


def resolve(release: rel.Release, code: str, *, original: bool, hints: dict) -> Meta:
    from ..local import nfo
    hints = dict(hints or {})
    meta = Meta(kind="concert")
    nfo.annotate(meta, release)
    video = _video_path(release)
    probe = audiotags.ffprobe_json(video) if video else {}
    tags = audiotags.parse_ffprobe(video, probe) if probe else audiotags.AudioTags(path=video or release.path)
    parsed = names.parse(release.name if release.path.is_dir() else (video.name if video else release.name))
    year = str(hints.get("year") or tags.year or nfo.year(release) or parsed.year)
    date = concert_date(release.name) or concert_date(tags.date)

    # artist and title
    artist, title, mb_artist = str(hints.get("artist") or ""), str(hints.get("title") or hints.get("album") or ""), {}
    artist = artist or tags.albumartist or tags.artist or nfo.value(release, "artist", "performer", "előadó")
    title = title or tags.album or nfo.value(release, "title", "album", "concert", "cím")
    base = release.name if " - " in release.name else parsed.title
    if " - " in base and not (artist and title):
        left, _, right = base.partition(" - ")
        right = re.sub(r"\s*[(\[]?(19|20)\d{2}[)\]]?.*$", "", right).strip() or right
        artist, title = artist or left.strip(), title or names.parse(right).title or right.strip()
    if not artist and (tags.artist or tags.albumartist):
        artist = tags.albumartist or tags.artist
        title = title or tags.album or (parsed.title[len(artist):].strip(" -") if music.fold(parsed.title).startswith(music.fold(artist)) else tags.title)
    if not artist and " - " in tags.title:
        left, _, right = tags.title.partition(" - ")
        artist, title = left.strip(), title or right.strip()
    if not artist:
        artist, rest, mb_artist = split_artist(re.sub(r"[-_]+", " ", parsed.title))
        title = title or rest
        if not artist:
            meta.warnings.append("klip: az előadót nem tudtam leválasztani a névből — add meg (--artist)")
    title = title or tags.album or tags.title or parsed.title
    if artist and not mb_artist:
        mb_artist = next((h for h in mb.search_artists(artist) if music.fold(h["name"]) == music.fold(artist)), {})
    meta.title, meta.creators, meta.year = title.strip(), [artist] if artist else [], year
    if mb_artist.get("id"):
        meta.identifiers["mb_artist"] = mb_artist["id"]
    if date:
        meta.date = date
        meta.year = meta.year or date[:4]

    # chapters
    items = chaps.chapters(video) if video and video.is_file() and not (release.has_bdmv or release.has_video_ts) else []
    named = chaps.named(items)
    if release.has_bdmv:
        starts = chaps.bluray_chapters(release.path)
        meta.extra["chapters"] = str(len(starts)) if starts else ""
    elif release.has_video_ts:
        starts = chaps.dvd_chapters(release.path if release.path.is_dir() else release.path.parent)
        meta.extra["chapters"] = str(len(starts)) if starts else ""
    elif items:
        meta.extra["chapters"] = str(len(items))
    if not meta.extra.get("chapters"):
        meta.extra.pop("chapters", None)

    local = music.Local(artist=artist, album=title, year=meta.year,
                        tracks=[music.LocalTrack(1, c["index"], c["title"], max(0.0, c["end"] - c["start"])) for c in named])
    local.hungarian_hint = (code or "").endswith("_hun")
    candidate = None
    if artist and title:
        pool = _video_candidates(local, artist, title)
        for item in pool:
            item.score = score(item, artist, title, meta.year)
        best = max(pool, key=lambda c: c.score) if pool else None
        if best and best.score >= 0.6:
            candidate = best
    confident = bool(candidate and candidate.score >= music.CONFIDENT)
    if confident and not (hints.get("title") or hints.get("album")):
        meta.title = music.candidate_info(candidate)["title"] or meta.title  # the database's spelling

    # tracklist
    if named:
        for chapter in named:
            meta.tracks.append(Track(position=f"{chapter['index']:02d}", title=chapter["title"] or "(cím nélkül)",
                                     length=chaps.length_text(chapter["end"] - chapter["start"])))
        meta.add_source("fejezetek")
    elif confident:
        remote = candidate.data
        tracks = ([t for m in remote.get("media", []) for t in m["tracks"]] if candidate.source == "musicbrainz"
                  else discogs.main_tracks(remote.get("tracks", [])))
        for index, track in enumerate(tracks, 1):
            length = track.get("length") if candidate.source == "musicbrainz" else track.get("duration")
            meta.tracks.append(Track(position=f"{index:02d}", title=track["title"], length=chaps.length_text(length or 0), artist=track.get("artist", "")))
    setlist: dict = {}
    if not meta.tracks and artist:
        mbid = mb_artist.get("id") or (_first_artist_id(candidate) if confident else "")
        venue = VENUE_RE.search(title)
        found = setlistfm.search(artist_mbid=mbid, artist_name=artist, date=date, year="" if date else meta.year,
                                 venue=venue.group(2) if venue and venue.group(1).lower() == "at" else "",
                                 city=venue.group(2) if venue and venue.group(1).lower() == "in" else "")
        if len(found) == 1 or (found and date):
            setlist = found[0]
            for index, song in enumerate(setlistfm.songs(setlist), 1):
                label = f" ({song['section']})" if song["section"] else ""
                meta.tracks.append(Track(position=f"{index:02d}", title=song["title"] + label))
            meta.add_source("setlist.fm")
            meta.add_proof(setlist.get("url", ""))
            meta.extra["venue"] = ", ".join(x for x in [setlist.get("venue", ""), setlist.get("city", "")] if x)
        elif len(found) > 1:
            meta.warnings.append(f"setlist.fm: {len(found)} koncert illik rá ({meta.year}) — add meg a dátumot a tracklistához")
    if not meta.tracks:
        meta.warnings.append("klip: nincs tracklista (nincsenek nevesített fejezetek, és adatbázisban sem találtam) — írd be a leírásba"
                             + ("" if keys.get("SETLISTFM_KEY") else "; setlist.fm-kulcs (SETLISTFM_KEY) nélkül a setlistet nem kerestem"))

    # tags, links, ids
    if hints.get("tags"):
        meta.tags = music._hint_tags(hints["tags"])
    else:
        terms = music.style_terms(candidate.data if candidate and candidate.source == "musicbrainz" else None,
                                  candidate.data if candidate and candidate.source == "discogs" else None, None, tags.genres)
        meta.tags = ["live"] + [t for t in music.style_tags(terms) if t != "live"][:4]
    if candidate:
        meta.add_source("MusicBrainz" if candidate.source == "musicbrainz" else "Discogs")
        meta.add_proof(candidate.data.get("url", ""))
        if candidate.source == "musicbrainz":
            meta.identifiers["mbid"] = candidate.data["id"]
            meta.publisher = candidate.data.get("label", "") if confident else ""
            if candidate.data.get("discogs_url"):
                meta.add_proof(candidate.data["discogs_url"])
        else:
            meta.identifiers["discogs"] = str(candidate.data["id"])
            meta.publisher = candidate.data.get("label", "") if confident else ""
        if not confident:
            meta.warnings.append(f"bizonytalan egyezés ({round(candidate.score * 100)}%): {candidate.data.get('url', '')} — ellenőrizd")
    if not meta.proof_links:
        meta.warnings.append("klip: nincs igazoló link (MusicBrainz/Discogs/setlist.fm) — add meg a leírásban")
    meta.extra["format"] = _format(release, video, probe, parsed.resolution)
    if not meta.extra["format"]:
        meta.extra.pop("format")
    meta.confidence = round(candidate.score if confident else 0.6 if artist and title and (named or setlist) else 0.4 if artist and title else 0.2, 2)

    if not original:
        artist_id = mb_artist.get("id") or _first_artist_id(candidate)
        artist_type = mb_artist.get("type", "") or ((candidate.data.get("artist_types") or {}).get(artist_id, "") if candidate and candidate.source == "musicbrainz" else "")
        group = candidate.data.get("release_group", {}) if confident and candidate.source == "musicbrainz" else {}
        music.describe_hu(meta, group=group, artist_id=artist_id, artist_type=artist_type, video=True)
    nfo.description(meta, release)
    return meta
