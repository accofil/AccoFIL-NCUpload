"""CUE sheets and EAC/XLD rip logs: tracklist, TOC and the MusicBrainz disc ID.

Disc ID (https://musicbrainz.org/doc/Disc_ID_Calculation): SHA-1 over the upper-case hex of the first and last
track number (%02X) and of 100 frame offsets (%08X: lead-out first, then tracks 1..99, 0 when absent), Base64
with ``+/=`` replaced by ``._-``. Offsets include the 150-frame (2 s) lead-in. The same numbers joined with ``+``
("first+last+leadout+off1+...") form the ``toc`` parameter of ``/ws/2/discid/-?toc=`` (fuzzy lookup).
"""

from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path

from .. import safeio

LEAD_IN = 150
FRAMES_PER_SECOND = 75
SAMPLES_PER_FRAME = 588  # 44100 / 75
DATA_GAP = 11400  # frames between the last audio track and the data track of an Enhanced CD
TOC_LINE = re.compile(r"^\s*(\d{1,2})\s*\|\s*[\d:.]+\s*\|\s*[\d:.]+\s*\|\s*(\d+)\s*\|\s*(\d+)\s*$", re.M)


def decode_text(data: bytes) -> str:
    """UTF-16 (BOM or NUL-heavy), UTF-8, else cp1250 when the bytes hold ő/ű (Hungarian rips), else cp1252."""

    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", "replace")
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", "replace")
    if len(data) > 3 and data[1:2] == b"\x00" and data.count(b"\x00") > len(data) // 3:
        return data.decode("utf-16-le", "replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    hungarian = any(b in data for b in (b"\xf5", b"\xfb", b"\xd5", b"\xdb"))
    return data.decode("cp1250" if hungarian else "cp1252", "replace")


def msf_to_frames(value: str) -> int:
    match = re.match(r"\s*(\d+):(\d{1,2}):(\d{1,2})", value or "")
    if not match:
        return 0
    minutes, seconds, frames = (int(x) for x in match.groups())
    return (minutes * 60 + seconds) * FRAMES_PER_SECOND + frames


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == '"':
        end = value.rfind('"')
        return value[1:end] if end > 0 else value[1:]
    return value


# --- CUE ---------------------------------------------------------------------------------------------------

def parse_cue(text: str) -> dict:
    """{"title", "performer", "songwriter", "catalog", "date", "genre", "discid", "comment", "files": [...],
    "tracks": [{"number", "type", "title", "performer", "songwriter", "isrc", "file", "index0", "index1"}]}

    index0/index1 are frame positions inside the track's file (index1 is in the file named by ``file``; -1 when absent).
    """

    disc = {"title": "", "performer": "", "songwriter": "", "catalog": "", "date": "", "genre": "", "discid": "", "comment": "",
            "files": [], "tracks": []}
    current_file = ""
    track: dict | None = None
    for line in (text or "").splitlines():
        match = re.match(r"^\s*([A-Za-z]+)\s*(.*?)\s*$", line)
        if not match:
            continue
        command, rest = match.group(1).upper(), match.group(2)
        if command == "REM":
            sub = re.match(r"^(\w+)\s+(.*)$", rest)
            if sub and track is None:
                key = sub.group(1).lower()
                if key in {"date", "genre", "discid", "comment"}:
                    disc[key] = _unquote(sub.group(2))
            continue
        if command == "FILE":
            current_file = _unquote(re.sub(r"\s+(WAVE|MP3|AIFF|BINARY|MOTOROLA|FLAC)$", "", rest, flags=re.I))
            disc["files"].append(current_file)
        elif command == "TRACK":
            parts = rest.split()
            number = int(parts[0]) if parts and parts[0].isdigit() else len(disc["tracks"]) + 1
            track = {"number": number, "type": (parts[1].upper() if len(parts) > 1 else "AUDIO"), "title": "", "performer": "",
                     "songwriter": "", "isrc": "", "file": current_file, "index0": -1, "index1": -1}
            disc["tracks"].append(track)
        elif command == "INDEX" and track is not None:
            parts = rest.split()
            if len(parts) >= 2 and parts[0].isdigit():
                if int(parts[0]) == 1:
                    track["index1"] = msf_to_frames(parts[1])
                    track["file"] = current_file  # gaps-appended CUEs: INDEX 01 is in the next FILE
                elif int(parts[0]) == 0:
                    track["index0"] = msf_to_frames(parts[1])
        elif command in {"TITLE", "PERFORMER", "SONGWRITER"}:
            target = track if track is not None else disc
            target[command.lower()] = _unquote(rest)
        elif command == "ISRC" and track is not None:
            track["isrc"] = _unquote(rest)
        elif command == "CATALOG":
            disc["catalog"] = re.sub(r"\D", "", rest)
    return disc


def read_cue(path: Path) -> dict:
    try:
        return parse_cue(decode_text(safeio.read_file(path)))
    except (OSError, safeio.LimitError):
        return {}


def match_file(name: str, candidates: list[Path]) -> Path | None:
    """The audio file a CUE FILE line means: exact name, else same stem (CUE says .wav, the rip is .flac)."""

    base = Path(name.replace("\\", "/")).name.lower()
    for path in candidates:
        if path.name.lower() == base:
            return path
    stem = Path(base).stem
    for path in candidates:
        if path.stem.lower() == stem:
            return path
    return candidates[0] if len(candidates) == 1 else None


def cue_positions(cue: dict, file_frames: dict[str, int]) -> tuple[list[tuple[int, int]], int] | None:
    """([(track number, absolute start frame)], end frame) of the audio tracks, or None when a file length is missing.

    ``file_frames`` maps each FILE name of the CUE to its length in CD frames (samples / 588).
    """

    starts: dict[str, int] = {}
    position = 0
    for name in cue.get("files") or []:
        if name not in file_frames:
            return None
        starts[name] = position
        position += int(file_frames[name])
    result = []
    for track in cue.get("tracks") or []:
        if track["type"] != "AUDIO" or track["index1"] < 0 or track["file"] not in starts:
            continue
        result.append((track["number"], starts[track["file"]] + track["index1"]))
    return (result, position) if result else None


def cue_lengths(cue: dict, file_frames: dict[str, int]) -> dict[int, float]:
    """Track number -> length in seconds (INDEX 01 to the next INDEX 01, the last one to the end of its file)."""

    found = cue_positions(cue, file_frames)
    if not found:
        return {}
    positions, end = found
    lengths = {}
    for index, (number, start) in enumerate(positions):
        stop = positions[index + 1][1] if index + 1 < len(positions) else end
        lengths[number] = max(0, stop - start) / FRAMES_PER_SECOND
    return lengths


def toc_from_cue(cue: dict, file_frames: dict[str, int]) -> dict:
    found = cue_positions(cue, file_frames)
    if not found:
        return {}
    positions, end = found
    return make_toc(positions[0][0], positions[-1][0], end + LEAD_IN, [start + LEAD_IN for _, start in positions])


# --- EAC / XLD logs ------------------------------------------------------------------------------------------

def parse_log(text: str) -> dict:
    """{"tool": "EAC"|"XLD"|"", "artist", "album", "tracks": [(number, start sector, end sector)], "toc": make_toc()}"""

    text = text or ""
    tool = "EAC" if re.search(r"Exact Audio Copy|EAC extraction logfile", text) else "XLD" if "X Lossless Decoder" in text else ""
    artist = album = ""
    match = re.search(r"(?:EAC|XLD) extraction logfile from[^\n]*\n\s*([^\n]+?)\s+/\s+([^\n]+?)[ \t]*$", text, re.M)
    if match:
        artist, album = match.group(1).strip(), match.group(2).strip()
    rows: list[tuple[int, int, int]] = []
    for number, start, end in TOC_LINE.findall(text):
        if rows and int(number) <= rows[-1][0]:
            break  # the logs of several discs appended into one file: only the first disc's TOC
        rows.append((int(number), int(start), int(end)))
    return {"tool": tool, "artist": artist, "album": album, "tracks": rows, "toc": toc_from_log_rows(rows)}


def toc_from_log_rows(rows: list[tuple[int, int, int]]) -> dict:
    if not rows:
        return {}
    audio = list(rows)
    leadout = audio[-1][2] + 1
    if len(audio) > 1 and audio[-1][1] - audio[-2][2] - 1 == DATA_GAP:  # Enhanced CD: drop the data track
        leadout = audio[-1][1] - DATA_GAP
        audio = audio[:-1]
    return make_toc(audio[0][0], audio[-1][0], leadout + LEAD_IN, [start + LEAD_IN for _, start, _ in audio])


def read_log(path: Path) -> dict:
    try:
        return parse_log(decode_text(safeio.read_file(path)))
    except (OSError, safeio.LimitError):
        return {}


# --- TOC / disc id --------------------------------------------------------------------------------------------

def make_toc(first: int, last: int, leadout: int, offsets: list[int]) -> dict:
    """{"first", "last", "leadout", "offsets", "toc", "discid"} (offsets in frames, lead-in included)."""

    toc = {"first": first, "last": last, "leadout": leadout, "offsets": list(offsets)}
    toc["toc"] = "+".join(str(x) for x in [first, last, leadout] + list(offsets))
    toc["discid"] = disc_id(first, last, leadout, offsets)
    return toc


def disc_id(first: int, last: int, leadout: int, offsets: list[int]) -> str:
    table = [0] * 100
    table[0] = leadout
    for number, offset in zip(range(first, last + 1), offsets):
        if 0 < number < 100:
            table[number] = offset
    sha = hashlib.sha1()
    sha.update(f"{first:02X}{last:02X}".encode("ascii"))
    for value in table:
        sha.update(f"{value:08X}".encode("ascii"))
    return base64.b64encode(sha.digest()).decode("ascii").replace("+", ".").replace("/", "_").replace("=", "-")


def toc_from_flac_cuesheet(starts: list[int]) -> dict:
    """TOC from a FLAC CUESHEET block (track start samples, the last one is the lead-out)."""

    if len(starts) < 2:
        return {}
    offsets = [s // SAMPLES_PER_FRAME + LEAD_IN for s in starts[:-1]]
    return make_toc(1, len(offsets), starts[-1] // SAMPLES_PER_FRAME + LEAD_IN, offsets)


def toc_from_lengths(lengths_samples: list[int]) -> dict:
    """TOC of a disc ripped as one file per track with no hidden pregaps (track lengths in samples).

    Only exact when the rip kept the gaps in the files (EAC 'appended to previous'); MusicBrainz' fuzzy TOC lookup
    still finds the disc when it is a few frames off.
    """

    if not lengths_samples or any(n <= 0 for n in lengths_samples):
        return {}
    offsets, position = [], LEAD_IN
    for samples in lengths_samples:
        offsets.append(position)
        position += round(samples / SAMPLES_PER_FRAME)
    return make_toc(1, len(offsets), position, offsets)
