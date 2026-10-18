"""Pull title, year, season/episode, resolution and group out of a scene/P2P release name."""

from __future__ import annotations

import re
from dataclasses import dataclass

YEAR_RE = re.compile(r"(?<![0-9])(19[0-9]{2}|20[0-9]{2})(?![0-9])")
SXE_RE = re.compile(r"(?i)(?<![a-z0-9])S(\d{1,3})(?:[ ._-]?E(\d{1,4})(?:[-E]+(\d{1,4}))?)?(?![a-z])")
RES_RE = re.compile(r"(?i)(?<![0-9])(2160p|1080p|1080i|720p|576p|480p)(?![0-9])")
STOP_RE = re.compile(
    r"(?i)(?<![a-z0-9])(2160p|1080p|1080i|720p|576p|480p|complete|bluray|blu-ray|bdrip|brrip|web-?dl|webrip|web|hdtv|dvdrip|"
    r"dvdr|remux|proper|repack|internal|multi|hun|hungarian|extended|unrated|remastered|uhd|hdr|x264|x265|h\.?264|h\.?265|hevc|avc|"
    r"imax|criterion|dc|amzn|nf|dsnp|atvp|hmax|pcok)(?![a-z0-9])"
)


@dataclass
class ParsedName:
    title: str
    year: str = ""
    season: int | None = None
    episode: int | None = None
    episode_last: int | None = None
    resolution: str = ""
    group: str = ""

    @property
    def episodes(self) -> str:
        if self.episode is None:
            return ""
        return f"{self.episode}-{self.episode_last}" if self.episode_last else str(self.episode)


def parse(name: str) -> ParsedName:
    stem = re.sub(r"\.(mkv|mp4|avi|m4v|ts)$", "", name, flags=re.I)
    group = ""
    match = re.search(r"-([A-Za-z0-9]+)$", stem)
    if match:
        group = match.group(1)
    cut = len(stem)
    sxe = SXE_RE.search(stem)
    if sxe:
        cut = min(cut, sxe.start())
    stop = STOP_RE.search(stem)
    if stop and stop.start() > 0:
        cut = min(cut, stop.start())
    # the release year is the last year before the series tag / quality tags ("Blade.Runner.2049.2017.1080p")
    years = [m for m in YEAR_RE.finditer(stem) if 0 < m.start() < cut]
    year = ""
    if years:
        year, cut = years[-1].group(1), years[-1].start()
    title = re.sub(r"[._]+", " ", stem[:cut]).strip(" -([")
    resolution = RES_RE.search(stem)
    return ParsedName(
        title=re.sub(r"\s{2,}", " ", title),
        year=year,
        season=int(sxe.group(1)) if sxe else None,
        episode=int(sxe.group(2)) if sxe and sxe.group(2) else None,
        episode_last=int(sxe.group(3)) if sxe and sxe.group(3) else None,
        resolution=resolution.group(1).lower() if resolution else "",
        group=group,
    )
