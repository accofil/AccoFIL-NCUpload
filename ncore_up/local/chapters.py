"""Chapters of mkv/mp4/m4b files (ffprobe), DVD titles (lsdvd) and Blu-ray playlists (MPLS, parsed here).

Generic names written by muxers and rippers ("Chapter 01", "Fejezet 1", "00:05:00.000") carry no information:
they are flagged as placeholders so that a tracklist is only built from real chapter titles.
"""

from __future__ import annotations

import json
import re
import shutil
import struct
import subprocess
from pathlib import Path

from .. import safeio

PLACEHOLDER_RE = re.compile(
    r"(?i)^\s*(?:(?:chapter|chap|ch|fejezet|kapitel|chapitre|capitolo|cap[ií]tulo|hoofdstuk|rozdzia[lł]|kapitola|глава|track|"
    r"sz[aá]m|part|r[eé]sz|title|scene|jelenet|szakasz|esemény|entry|marker|mark)\s*[#.:\-_]?\s*\d+[.)]?|"
    r"\d+\s*\.?\s*(?:fejezet|r[eé]sz|sz[aá]m|chapter)|\d{1,3}[.)]?|"
    r"\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d+)?)\s*$"
)
TICKS = 45000  # Blu-ray time stamps


def is_placeholder(title: str) -> bool:
    return not (title or "").strip() or bool(PLACEHOLDER_RE.match(title))


def parse_chapters(data: dict) -> list[dict]:
    """[{"index", "start", "end", "title", "placeholder"}] from ``ffprobe -show_chapters`` JSON (seconds)."""

    result = []
    for index, chapter in enumerate(data.get("chapters") or [], 1):
        title = str((chapter.get("tags") or {}).get("title", "") or "").strip()
        try:
            start, end = float(chapter.get("start_time") or 0), float(chapter.get("end_time") or 0)
        except (TypeError, ValueError):
            start = end = 0.0
        result.append({"index": index, "start": start, "end": end, "title": title, "placeholder": is_placeholder(title)})
    return result


def chapters(path: Path) -> list[dict]:
    if not shutil.which("ffprobe"):
        return []
    try:
        run = safeio.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_chapters", "--", str(path)], timeout=120)
        return parse_chapters(json.loads(run.stdout or b"{}"))
    except (OSError, ValueError, subprocess.SubprocessError):
        return []


def named(items: list[dict]) -> list[dict]:
    """The chapters when most of them have real titles, else [] (a few named markers among 'Chapter NN' do not make a tracklist)."""

    real = [c for c in items if not c["placeholder"]]
    return items if items and len(real) * 2 > len(items) else []


def length_text(seconds: float) -> str:
    seconds = int(round(seconds or 0))
    if seconds <= 0:
        return ""
    hours, rest = divmod(seconds, 3600)
    return f"{hours}:{rest // 60:02d}:{rest % 60:02d}" if hours else f"{rest // 60}:{rest % 60:02d}"


# --- DVD (lsdvd) -------------------------------------------------------------------------------------------

def parse_lsdvd_xml(text: str) -> list[dict]:
    """[{"title", "length", "chapters": [lengths]}] from ``lsdvd -c -Ox`` (regex based: lsdvd's XML is not always well-formed)."""

    titles = []
    for block in re.findall(r"(?s)<track>(.*?)</track>", text or ""):
        number = re.search(r"<ix>(\d+)</ix>", block)
        length = re.search(r"<length>([\d.]+)</length>", block)
        chapter_lengths = [float(x) for x in re.findall(r"(?s)<chapter>.*?<length>([\d.]+)</length>.*?</chapter>", block)]
        titles.append({"title": int(number.group(1)) if number else len(titles) + 1, "length": float(length.group(1)) if length else 0.0,
                       "chapters": chapter_lengths})
    return titles


def dvd_chapters(disc: Path) -> list[float]:
    """Chapter start times (s) of the longest title of a DVD folder (with VIDEO_TS) or ISO; [] without lsdvd."""

    if not shutil.which("lsdvd"):
        return []
    try:
        run = safeio.run(["lsdvd", "-c", "-Ox", str(disc)], timeout=120)
    except (OSError, subprocess.SubprocessError):
        return []
    titles = parse_lsdvd_xml(run.stdout.decode("utf-8", "replace"))
    if not titles:
        return []
    longest = max(titles, key=lambda t: t["length"])
    starts, position = [], 0.0
    for length in longest["chapters"]:
        starts.append(round(position, 3))
        position += length
    return starts


# --- Blu-ray (MPLS) ---------------------------------------------------------------------------------------

def parse_mpls(data: bytes) -> dict:
    """{"duration", "clips", "chapters": [start s]} of a Blu-ray playlist; {} when it is not an MPLS file.

    Layout (BD-ROM part 3): header "MPLS" + version, PlayList and PlayListMark start addresses at 8 and 12; PlayItem:
    length(2) clip(5) codec(4) flags(2) stc(1) IN(4) OUT(4); mark: reserved(1) type(1) item(2) time(4) pid(2) duration(4).
    """

    if len(data) < 20 or data[:4] != b"MPLS":
        return {}
    try:
        playlist_at, marks_at = struct.unpack_from(">II", data, 8)
        count_items = struct.unpack_from(">H", data, playlist_at + 6)[0]
        position = playlist_at + 10
        items = []
        for _ in range(count_items):
            length = struct.unpack_from(">H", data, position)[0]
            clip = data[position + 2:position + 7].decode("ascii", "replace")
            in_time, out_time = struct.unpack_from(">II", data, position + 14)
            items.append((clip, in_time, out_time))
            position += 2 + length
        count_marks = struct.unpack_from(">H", data, marks_at + 4)[0]
        offsets, total = [], 0
        for _clip, in_time, out_time in items:
            offsets.append(total)
            total += max(0, out_time - in_time)
        starts = []
        for index in range(count_marks):
            _, mark_type, item, stamp = struct.unpack_from(">BBHI", data, marks_at + 6 + 14 * index)
            if mark_type != 1 or item >= len(items):
                continue
            start = (offsets[item] + stamp - items[item][1]) / TICKS
            if not starts or start - starts[-1] > 0.5:
                starts.append(round(start, 3))
    except struct.error:
        return {}
    duration = total / TICKS
    starts = [s for s in starts if duration - s > 5 or s == 0]  # drop the end-of-disc marker
    return {"duration": duration, "clips": [c for c, _, _ in items], "chapters": starts}


def bluray_chapters(disc_root: Path) -> list[float]:
    """Chapter start times of the longest playlist under BDMV/PLAYLIST."""

    best: dict = {}
    for path in sorted(Path(disc_root).rglob("*.mpls")) + sorted(Path(disc_root).rglob("*.MPLS")):
        try:
            info = parse_mpls(safeio.read_file(path))
        except (OSError, safeio.LimitError):
            continue
        if info and info["duration"] > best.get("duration", 0):
            best = info
    return best.get("chapters", [])
