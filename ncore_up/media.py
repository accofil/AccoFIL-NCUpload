"""Technical data and screenshots of the content: MediaInfo, BDInfo, ffmpeg.

nCore wants a techinfo file for every non-release upload ("A technikai adatokat a MediaInfo vagy a BDInfo nevű
programmal nyerheted ki") and three screenshots for films, series, games and programs.

Screenshots are taken at the video's own resolution. In MPEG-TS/PS streams (Blu-ray .m2ts, DVD .vob) a seek lands
between keyframes and the first decoded frames are grey garbage, so there only keyframes are decoded. HDR is
tone-mapped to SDR (zscale + tonemap), and a PNG over 5 MB (4K PNGs are 15-40 MB) is sent as a high-quality JPEG.
"""

from __future__ import annotations

import functools
import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import safeio

from . import categories as cats

HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}
KEYFRAME_SEEK_EXT = {".m2ts", ".mts", ".ts", ".vob", ".mpg", ".mpeg", ".m2v", ".evo"}
MAX_SCREENSHOT_BYTES = 5 * 1024**2
TONEMAP = "zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=pc,format=gbrp"
PLAYLIST_RE = re.compile(r"(?i)\b(\d{5}\.MPLS)\s+(\d+):(\d{2}):(\d{2})")


class MediaError(Exception):
    pass


def _tag_delay(tags: dict) -> float:
    """Explicit container delay metadata only; stream start_time is not an audio delay."""
    for key in ("delay", "delay_relative_to_video", "video_delay"):
        value = str(tags.get(key, "")).strip()
        match = re.fullmatch(r"([+-]?[\d.]+)\s*(ms|s)?", value)
        if match:
            return float(match[1]) * (1000 if match[2] == "s" else 1)
    return 0.0


@dataclass
class VideoInfo:
    path: Path
    duration: float = 0.0
    width: int = 0
    height: int = 0
    hdr: bool = False
    dovi: bool = False
    codec: str = ""
    bitrate: int = 0
    audio: list[dict] = field(default_factory=list)  # {"lang", "format", "channels", "title", "default"}
    subtitles: list[dict] = field(default_factory=list)  # {"lang", "format", "title", "forced"}
    chapters: bool = False


def _run(args: list[str], timeout: int = 600, memory_limit: int | None = safeio.MEMORY_LIMIT,
         file_limit: int | None = safeio.PACKAGE_LIMIT) -> subprocess.CompletedProcess:
    # stdin closed: BDInfo without a playlist would wait for a key press
    try:
        return safeio.run(args, timeout=timeout, memory_limit=memory_limit, file_limit=file_limit)
    except (OSError, subprocess.SubprocessError) as exc:
        raise MediaError(f"{args[0]}: {exc}") from exc


def _tail(run: subprocess.CompletedProcess, size: int = 400) -> str:
    return ((run.stderr or b"") + (run.stdout or b"")).decode("utf-8", errors="replace").strip()[-size:]


def strip_paths(text: str, path: Path) -> str:
    """MediaInfo prints the full path ("Complete name : /home/.../Movie.mkv"): keep only the file name."""

    lines = []
    for line in text.split("\n"):
        match = re.match(r"^(Complete name\s*:\s*)(.*?)(\r?)$", line)
        if match:
            line = match.group(1) + match.group(2).rsplit("/", 1)[-1] + match.group(3)
        lines.append(line)
    return "\n".join(lines).replace(str(path.parent) + "/", "")


def mediainfo_text(path: Path) -> str:
    """The plain MediaInfo report, as nCore's techinfo file expects it (file name only, not the full path)."""

    if not shutil.which("mediainfo"):
        raise MediaError("a mediainfo nincs telepítve")
    full = Path(path).absolute()
    run = _run(["mediainfo", "--", str(full)], timeout=300)
    text = run.stdout.decode("utf-8", errors="replace")
    if run.returncode or not text.strip() or not re.search(r"(?m)^Complete name\s*:\s*\S", text):
        raise MediaError(f"a mediainfo nem adott kimenetet: {path}")
    return strip_paths(text, full)


def bdinfo_text(bdinfo: str, disc_root: Path, work_dir: Path) -> str:
    """BDInfo report of the main playlist (the longest one in ``bdinfo -l``), full scan."""

    if not Path(bdinfo).exists() and not shutil.which(bdinfo):
        raise MediaError(f"nem találom a BDInfót: {bdinfo}")
    work_dir.mkdir(parents=True, exist_ok=True)
    out = Path(tempfile.mkdtemp(prefix="bdinfo-", dir=work_dir))
    # The .NET runtime dies with SIGXFSZ under an RLIMIT_FSIZE and reserves huge address space: no limits for BDInfo.
    listing = _run([bdinfo, "-l", str(disc_root), str(out)], timeout=600, memory_limit=None, file_limit=None)
    playlist = _main_playlist(listing.stdout.decode("utf-8", errors="replace"))
    if not playlist:
        raise MediaError(f"a BDInfo nem listázott playlistet (kilépési kód {listing.returncode}): " + _tail(listing, 300))
    run = _run([bdinfo, "-m", playlist, str(disc_root), str(out)], timeout=7200, memory_limit=None, file_limit=None)
    reports = sorted(out.glob("*.txt"), key=lambda p: p.stat().st_size, reverse=True)
    if not reports:
        raise MediaError("a BDInfo nem készített jelentést: " + _tail(run))
    return bdinfo_report(reports[0].read_text(encoding="utf-8", errors="replace"), Path(disc_root))


def bdinfo_report(text: str, disc_root: Path | None = None) -> str:
    """The QUICK SUMMARY of the scanned main playlist: the form of the nCore sample ("a scan csak arról a konkrét
    playlistről, amiben a film vagy sorozat van"). Without one, the full report from "DISC INFO:" to the end of the
    playlist report, without the forum paste's [code] markers."""

    quick = text.find("QUICK SUMMARY:")
    if quick >= 0 and text[quick + len("QUICK SUMMARY:"):].strip():
        body = text[quick + len("QUICK SUMMARY:"):]
    else:
        start = text.find("DISC INFO:")
        body = text[start:] if start >= 0 else text
        end = body.find("<---- END FORUMS PASTE")
        if end >= 0:
            body = body[:end]
    lines = [line.rstrip() for line in body.splitlines() if line.strip() not in {"[code]", "[/code]"}]
    result = "\n".join(lines).strip() + "\n"
    if disc_root is not None:
        result = result.replace(str(disc_root.parent) + "/", "")
    return result


def _main_playlist(listing: str) -> str | None:
    """Pick the longest playlist from `bdinfo -l` output (lines like '  1: 00800.MPLS   01:52:10 /path/00800.mpls')."""

    best, best_len = None, -1
    for line in listing.splitlines():
        match = PLAYLIST_RE.search(line)
        if not match:
            continue
        seconds = int(match.group(2)) * 3600 + int(match.group(3)) * 60 + int(match.group(4))
        if seconds > best_len:
            best, best_len = match.group(1).upper(), seconds
    return best


def probe(path: Path) -> VideoInfo:
    if not shutil.which("ffprobe"):
        raise MediaError("az ffprobe nincs telepítve")
    run = _run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", "-show_chapters", "--", str(path)], timeout=300)
    try:
        data = json.loads(run.stdout or b"{}")
    except ValueError as exc:
        raise MediaError(f"ffprobe: {exc}") from exc
    if run.returncode != 0 and not data.get("streams"):  # unreadable or unfinished file: not an empty VideoInfo
        raise MediaError(f"ffprobe nem tudja olvasni ({path.name}): " + _tail(run, 200))
    fmt = data.get("format") or {}
    info = VideoInfo(path=path, duration=float(fmt.get("duration") or 0), chapters=bool(data.get("chapters")), bitrate=int(fmt.get("bit_rate") or 0))
    for stream in data.get("streams") or []:
        tags = {k.lower(): v for k, v in (stream.get("tags") or {}).items()}
        disposition = stream.get("disposition") or {}
        kind = stream.get("codec_type")
        if kind == "video" and not info.width and not disposition.get("attached_pic"):
            info.width, info.height = int(stream.get("width") or 0), int(stream.get("height") or 0)
            info.hdr = stream.get("color_transfer") in HDR_TRANSFERS
            info.codec = stream.get("codec_name", "")
            info.dovi = any("DOVI" in str(sd.get("side_data_type", "")).upper() for sd in stream.get("side_data_list") or [])
        elif kind == "audio":
            info.audio.append({"lang": tags.get("language", "und"), "format": stream.get("codec_name", ""), "profile": stream.get("profile", ""),
                               "channels": int(stream.get("channels") or 0), "channel_layout": stream.get("channel_layout", ""),
                               "title": tags.get("title", ""), "default": bool(disposition.get("default")),
                               "delay_ms": _tag_delay(tags)})
        elif kind == "subtitle":
            info.subtitles.append({"lang": tags.get("language", "und"), "format": stream.get("codec_name", ""), "title": tags.get("title", ""), "forced": bool(disposition.get("forced"))})
    return info


def screenshot_args(video: Path, at: float, target: Path, *, hdr: bool, image_format: str = "png") -> list[str]:
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    if video.suffix.lower() in KEYFRAME_SEEK_EXT:
        args += ["-skip_frame", "nokey"]
    args += ["-ss", f"{at:.2f}", "-i", str(video), "-map", "0:V:0", "-frames:v", "1"]
    if hdr:
        args += ["-vf", TONEMAP]
    args += ["-pix_fmt", "rgb24"] if image_format == "png" else ["-q:v", "2"]  # PNG: 8-bit, not 48-bit from 10-bit video
    return args + [str(target)]


def screenshots(video: Path, out_dir: Path, count: int = 3, image_format: str = "png", max_bytes: int = MAX_SCREENSHOT_BYTES) -> list[Path]:
    """`count` frames from evenly spread points (skipping intro and credits), at the video's own resolution."""

    if not shutil.which("ffmpeg"):
        raise MediaError("az ffmpeg nincs telepítve")
    info = probe(video)
    if info.duration <= 0:
        raise MediaError(f"nem tudom a videó hosszát: {video}")
    out_dir.mkdir(parents=True, exist_ok=True)
    tonemap = info.hdr and has_tonemap()
    result = []
    for index in range(count):
        at = info.duration * (0.15 + 0.6 * index / max(1, count - 1))
        target = out_dir / f"screen{index + 1}.{image_format}"
        target.unlink(missing_ok=True)
        run = _run(screenshot_args(video, at, target, hdr=tonemap, image_format=image_format), timeout=600)
        if not target.exists() or target.stat().st_size == 0:
            raise MediaError("ffmpeg: " + _tail(run))
        if target.stat().st_size > max_bytes and image_format == "png":
            target = _to_jpeg(target)
        result.append(target)
    return result


def _to_jpeg(png: Path) -> Path:
    """High-quality JPEG of an oversized PNG screenshot (the PNG stays when the conversion fails)."""

    jpeg = png.with_suffix(".jpg")
    _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(png), "-frames:v", "1", "-q:v", "2", str(jpeg)], timeout=120)
    if jpeg.exists() and jpeg.stat().st_size:
        png.unlink(missing_ok=True)
        return jpeg
    return png


def sample_args(video: Path, target: Path, start: float, seconds: int) -> list[str]:
    # video (no cover art), every audio and subtitle track, timestamps from zero
    return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{start:.1f}", "-i", str(video), "-t", str(seconds),
            "-map", "0:V", "-map", "0:a?", "-map", "0:s?", "-c", "copy", "-avoid_negative_ts", "make_zero", str(target)]


def make_sample(video: Path, target: Path, min_bytes: int = 0, min_seconds: int = 60) -> Path:
    """Cut a sample from the middle of the encode with stream copy (nCore: ≥1 min, UHD ≥200 MB)."""

    if not shutil.which("ffmpeg"):
        raise MediaError("az ffmpeg nincs telepítve")
    info = probe(video)
    seconds = min_seconds + 5
    if min_bytes and info.bitrate:
        seconds = max(seconds, int(min_bytes * 8 / info.bitrate) + 10)
    target.parent.mkdir(parents=True, exist_ok=True)
    for _attempt in range(3):
        start = max(0.0, info.duration / 2 - seconds / 2)
        target.unlink(missing_ok=True)
        run = _run(sample_args(video, target, start, seconds), timeout=1800)
        if not target.exists() or target.stat().st_size == 0:
            raise MediaError("sample készítése nem sikerült: " + _tail(run, 300))
        size = target.stat().st_size
        if not min_bytes or size >= min_bytes or (info.duration and seconds >= info.duration):
            actual = probe(target)
            if actual.duration < min_seconds:
                raise MediaError(f"a sample rövidebb a szükségesnél ({actual.duration:.1f} < {min_seconds} mp)")
            if min_bytes and size < min_bytes:
                raise MediaError("a sample kisebb a kötelező UHD minimumnál")
            return target
        seconds = int(seconds * min_bytes / size * 1.1) + 5  # bitrate in the middle was lower than the average
    raise MediaError(f"a sample kisebb a szükségesnél ({target.stat().st_size // 1024**2} MB < {min_bytes // 1024**2} MB)")


def document_pages(book: Path, out_dir: Path, count: int = 3) -> list[Path]:
    """`count` page images from the body of a PDF or DjVu (the wiki wants text pages, not cover/TOC/imprint, so the
    pages are taken from 30-70% of the book)."""

    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = book.suffix.lower()
    if suffix == ".pdf" and shutil.which("pdfinfo") and shutil.which("pdftoppm"):
        info = _run(["pdfinfo", str(book)], timeout=120).stdout.decode("utf-8", errors="replace")
        pages = next((int(line.split()[-1]) for line in info.splitlines() if line.startswith("Pages:")), 0)
    elif suffix == ".djvu" and shutil.which("djvused") and shutil.which("ddjvu"):
        pages = int((_run(["djvused", "-e", "n", str(book)], timeout=120).stdout or b"0").strip() or 0)
    else:
        return []
    if pages < 5:
        return []
    picks = sorted({max(1, int(pages * (0.3 + 0.4 * k / max(1, count - 1)))) for k in range(count)})
    result = []
    for index, page in enumerate(picks, 1):
        target = out_dir / f"oldal{index}.png"
        if suffix == ".pdf":
            stem = out_dir / f"oldal{index}"
            _run(["pdftoppm", "-f", str(page), "-l", str(page), "-r", "110", "-png", "-singlefile", str(book), str(stem)], timeout=300)
        else:
            pnm = out_dir / f"oldal{index}.pnm"
            _run(["ddjvu", "-format=pnm", f"-page={page}", "-scale=110", str(book), str(pnm)], timeout=300)
            if pnm.exists():
                _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(pnm), str(target)], timeout=120)
                pnm.unlink(missing_ok=True)
        if target.exists() and target.stat().st_size:
            result.append(target)
    return result if len(result) == count else []


def cover_args(source: Path, out: Path, limit: int = 300) -> list[str]:
    scale = f"scale='min({limit},iw)':'min({limit},ih)':force_original_aspect_ratio=decrease:flags=lanczos"
    return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source), "-frames:v", "1", "-vf", scale, "-q:v", "3", str(out)]


def infobar_cover(image: bytes, out: Path, limit: int = 300) -> Path:
    """Shrink a cover to nCore's infobar limit ("A kép mérete maximum 300x300 pixel lehet!") as JPEG (first frame of
    an animated image)."""

    if not shutil.which("ffmpeg"):
        raise MediaError("az ffmpeg nincs telepítve")
    out.parent.mkdir(parents=True, exist_ok=True)
    source = out.with_suffix(".src")
    source.write_bytes(image)
    out.unlink(missing_ok=True)
    run = _run(cover_args(source, out, limit), timeout=120)
    source.unlink(missing_ok=True)
    if not out.exists() or out.stat().st_size == 0:
        raise MediaError("borító átméretezése nem sikerült: " + _tail(run, 300))
    return out


@functools.lru_cache(maxsize=None)
def _filters() -> frozenset[str]:
    try:
        run = _run(["ffmpeg", "-hide_banner", "-filters"], timeout=60)
    except MediaError:
        return frozenset()
    return frozenset(line.split()[1] for line in run.stdout.decode("utf-8", errors="replace").splitlines() if len(line.split()) > 2)


def _has_filter(name: str) -> bool:
    return name in _filters()


def has_tonemap() -> bool:
    return _has_filter("zscale") and _has_filter("tonemap")


def disc_main_stream(disc_root: Path) -> Path | None:
    """Biggest .m2ts of a Blu-ray (screenshots) or biggest VOB of a DVD."""

    streams = [p for p in disc_root.rglob("*") if p.suffix.lower() in {".m2ts", ".vob"} and p.is_file()]
    return max(streams, key=lambda p: p.stat().st_size) if streams else None


def dvd_image_entries(image: Path) -> list[tuple[str, int]]:
    """Read a DVD's unencrypted directory using 7z; never mount/run its content."""
    if not shutil.which("7z"):
        return []
    run = _run(["7z", "l", "-slt", "--", str(image)], timeout=180)
    if run.returncode:
        return []
    entries = []
    for block in re.split(r"\r?\n\s*\r?\n", run.stdout.decode("utf-8", errors="replace")):
        path = re.search(r"(?m)^Path = (.+)$", block)
        size = re.search(r"(?m)^Size = (\d+)\s*$", block)
        if path and size and re.search(r"(?i)(?:^|[/\\])VIDEO_TS[/\\]VTS_\d{2}_[1-9]\.VOB$", path[1].strip()):
            entries.append((path[1].strip(), int(size[1])))
    return entries


def dvd_image_stream(image: Path, work_dir: Path) -> Path:
    """Extract one main-title segment for codec/audio/screenshots (the ISO stays intact).

    Total film duration/playlist identity cannot be proved from this segment: the
    disc menu, extras and source remain explicit review requirements.
    """
    entries = dvd_image_entries(image)
    if not entries:
        raise MediaError("a DVD lemezkép fő videója nem olvasható (7z / titkosítás / formátum)")
    groups = {}
    for member, size in entries:
        key = re.search(r"(?i)VTS_(\d{2})_", member)[1]
        groups.setdefault(key, []).append((member, size))
    title = max(groups.values(), key=lambda items: sum(size for _, size in items))
    member, expected = max(title, key=lambda item: item[1])
    if expected > safeio.PACKAGE_LIMIT:
        raise MediaError("a DVD szegmens meghaladja az ideiglenes kibontás méretkorlátját")
    work_dir.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(work_dir).free < expected + 1024**2:
        raise MediaError("nincs elég hely a DVD fő videójának ideiglenes vizsgálatához")
    target = work_dir / "dvd-main.vob"
    try:
        with target.open("wb") as stream:
            result = subprocess.run(safeio.command(["7z", "e", "-so", "-pnone", "-spd", "--", str(image), member], file_limit=expected), stdout=stream, stderr=subprocess.PIPE,
                                    stdin=subprocess.DEVNULL, timeout=900, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise MediaError(f"DVD kicsomagolás: {exc}") from exc
    if result.returncode or target.stat().st_size != expected:
        raise MediaError("a DVD fő videójának ideiglenes kicsomagolása sikertelen")
    return target


def techinfo_target(release_path: Path, files: list, has_bdmv: bool) -> Path | None:
    """What the techinfo describes: the disc root for Blu-ray, otherwise the main video file."""

    if has_bdmv:
        bdmv = next(release_path.rglob("BDMV"), None)
        return bdmv.parent if bdmv else release_path
    videos = [f for f in files if f.ext in cats.VIDEO_EXT and "sample" not in f.relpath.lower()]
    if not videos:
        return None
    biggest = max(videos, key=lambda f: f.size)
    return release_path / biggest.relpath if release_path.is_dir() else release_path
