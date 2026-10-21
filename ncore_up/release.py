"""Inspect a release on disk: its name, NFO, files, IMDb id and whether it may go up as an original release."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import categories as cats, safeio

IMDB_RE = re.compile(r"imdb\.com/(?:[a-z]{2}/)?title/(tt\d{5,10})", re.IGNORECASE)
COMPANION_DIRS = {"sample", "proof", "subs", "subtitles", "covers", "cover", "scans", "artwork"}
JUNK_FILES = {"thumbs.db", ".ds_store", "desktop.ini"}  # mkbrr leaves these (and .torrent files) out of the torrent
PARTIAL_EXT = {".!qb", ".part", ".!ut", ".crdownload"}  # unfinished downloads


@dataclass
class ReleaseFile:
    relpath: str
    size: int

    @property
    def ext(self) -> str:
        return os.path.splitext(self.relpath)[1].lower()

    @property
    def top_dir(self) -> str:
        parts = Path(self.relpath).parts
        return parts[0].lower() if len(parts) > 1 else ""


@dataclass
class Release:
    path: Path
    name: str
    files: list[ReleaseFile]
    nfo: Path | None
    has_bdmv: bool
    has_video_ts: bool
    imdb_id: str | None = None
    hungarian_audio: bool | None = None
    problems: list[str] = field(default_factory=list)
    nfo_data: dict | None = field(default=None, repr=False)

    @property
    def size(self) -> int:
        return sum(f.size for f in self.files)

    @property
    def bd_menu(self) -> bool | None:
        """True when every Blu-ray disc of the release carries a BD-J menu (BDMV/BDJO/*.bdjo plus a JAR file);
        None otherwise: an HDMV menu lives inside the streams and needs the uploader's answer (--menu)."""

        if not self.has_bdmv:
            return None
        discs: dict[tuple, set[str]] = {}
        for item in self.files:
            parts = Path(item.relpath).parts
            upper = [p.upper() for p in parts]
            if "BDMV" not in upper or len(parts) < upper.index("BDMV") + 3:
                continue
            at = upper.index("BDMV")
            found = discs.setdefault(tuple(parts[:at]), set())
            if upper[at + 1] == "BDJO" and upper[-1].endswith(".BDJO"):
                found.add("bdjo")
            elif upper[at + 1] == "JAR" and item.size > 0:
                found.add("jar")
        return True if discs and all(found == {"bdjo", "jar"} for found in discs.values()) else None


def read_nfo(path: Path) -> str:
    from .local import nfo
    return nfo.decode(safeio.read_file(path))


def inspect(path: str | os.PathLike) -> Release:
    root = Path(path).resolve()
    if not root.exists():
        raise FileNotFoundError(root)
    problems = []
    if root.is_file():
        files = [ReleaseFile(root.name, root.stat().st_size)]
        name = root.stem
        siblings = []
    else:
        files = []
        for current, dirs, names in os.walk(root):
            for name in list(dirs):
                if (Path(current) / name).is_symlink():
                    problems.append(f"szimbolikus link a release-ben: {(Path(current) / name).relative_to(root)}")
                    dirs.remove(name)
            for item in names:
                full = Path(current) / item
                if full.is_symlink() or not full.is_file():
                    problems.append(f"nem szabályos fájl vagy szimbolikus link: {full.relative_to(root)}")
                    continue
                files.append(ReleaseFile(str(full.relative_to(root)), full.stat().st_size))
        files.sort(key=lambda f: f.relpath)
        name = root.name
        siblings = files
    nfos = [f for f in siblings if f.ext == ".nfo" and len(Path(f.relpath).parts) == 1]
    nfo = root / nfos[0].relpath if len(nfos) == 1 else None
    release = Release(
        path=root,
        name=name,
        files=files,
        nfo=nfo,
        has_bdmv=any("BDMV" in (p.upper() for p in Path(f.relpath).parts) for f in files),
        has_video_ts=any("VIDEO_TS" in (p.upper() for p in Path(f.relpath).parts) for f in files),
        problems=problems,
    )
    from .local import nfo as nfo_reader
    data = nfo_reader.read_release(release)
    release.imdb_id = nfo_reader.unique_match(release, IMDB_RE).lower() or None
    if nfo is not None and nfo.name not in data.get("files", []):
        release.problems.append("az NFO nem olvasható vagy túllépi a méretkorlátot")
    release.hungarian_audio = hungarian_audio(root, files)
    return release


def assert_safe_tree(root: Path) -> None:
    """Recheck before staging/hashing; directory symlinks and special files are refused too."""
    if root.is_symlink():
        raise ValueError("szimbolikus link nem lehet a torrent tartalma")
    if root.is_file():
        return
    for current, dirs, names in os.walk(root, followlinks=False):
        for name in dirs + names:
            path = Path(current) / name
            if path.is_symlink() or (name in names and not path.is_file()):
                raise ValueError(f"szimbolikus link vagy nem szabályos fájl a torrent tartalmában: {path.relative_to(root)}")


def main_video(root: Path, files: list[ReleaseFile]) -> Path | None:
    videos = [f for f in files if f.ext in cats.VIDEO_EXT and f.top_dir not in COMPANION_DIRS and "sample" not in f.relpath.lower()]
    if not videos:
        return None
    biggest = max(videos, key=lambda f: f.size)
    return root / biggest.relpath if root.is_dir() else root


def hungarian_audio(root: Path, files: list[ReleaseFile]) -> bool | None:
    """True/False from MediaInfo when available; None when it cannot be told (no mediainfo, no video file)."""

    video = main_video(root, files)
    if video is None or not shutil.which("mediainfo"):
        return None
    try:
        run = safeio.run(["mediainfo", "--Output=JSON", "--", str(video)], timeout=120)
        tracks = (json.loads(run.stdout or b"{}").get("media") or {}).get("track") or []
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    languages = {str(t.get("Language") or "").lower()[:2] for t in tracks if t.get("@type") == "Audio"}
    return "hu" in languages


def validate_original(release: Release, category: cats.Category) -> list[str]:
    """Problems that stop an upload as an original release (nCore wiki: unpacked, unchanged, content + NFO + companions)."""

    problems = list(release.problems)
    nfos = [f.relpath for f in release.files if f.ext == ".nfo" and len(Path(f.relpath).parts) == 1]
    if len(nfos) > 1:
        problems.append("több NFO van a release gyökerében: " + ", ".join(nfos))
    if release.nfo is None and release.path.is_dir():
        problems.append("nincs NFO a release gyökerében (eredeti release-hez kötelező)")
    if release.path.is_file():
        problems.append("egyetlen fájl: eredeti release-hez a release mappáját add meg (benne az NFO-val)")
    content_ext = _content_extensions(category)
    for item in release.files:
        if item.ext in PARTIAL_EXT:
            problems.append(f"befejezetlen letöltés: {item.relpath}")
            continue
        if Path(item.relpath).name.lower() in JUNK_FILES or item.ext == ".torrent":
            continue
        if release.has_bdmv and any(p.casefold() in {"bdmv", "certificate", "aacs", "bdsvm", "any!", "fab!"}
                                   for p in Path(item.relpath).parts[:-1]):
            continue
        if release.has_video_ts and any(p.upper() in {"VIDEO_TS", "AUDIO_TS"} for p in Path(item.relpath).parts):
            continue
        if cats.ARCHIVE_RE.search(item.relpath) and not category.packed_ok:
            problems.append(f"tömörített fájl (ki kell bontani): {item.relpath}")
            continue
        if category.group in {"video", "xxx", "clip"} and item.ext in {".ts", ".m2ts"}:
            problems.append(f".ts/.m2ts kiterjesztés tiltott: {item.relpath}")
            continue
        if item.ext in content_ext or item.ext in category.allowed or category.packed_ok:
            continue
        if item.top_dir in COMPANION_DIRS and (item.ext in cats.VIDEO_EXT or item.ext in cats.IMAGE_EXT):
            continue
        problems.append(f"nem engedélyezett fájl az eredeti release-ben: {item.relpath}")
    return problems


def _content_extensions(category: cats.Category) -> set[str]:
    if category.group in {"video", "xxx", "clip"}:
        return cats.VIDEO_EXT | cats.DISC_IMAGE_EXT
    if category.group == "music":
        return cats.MP3_EXT | cats.LOSSLESS_EXT
    if category.group == "ebook":
        return cats.EBOOK_EXT
    return cats.DISC_IMAGE_EXT | cats.VIDEO_EXT | {".exe", ".msi", ".dmg", ".pkg", ".apk", ".nsp", ".xci", ".pkg"}
