"""MediaInfo TEXT for the final torrent payload, including every companion.

The audio/video extension filter used to omit JPEG, playlists and checksum
files. Read the assembled payload after staging/sample creation instead.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import re

from . import media, release


def inventory(root: Path) -> dict[str, int]:
    release.assert_safe_tree(root)
    if root.is_file():
        return {root.name: root.stat().st_size}
    return {path.relative_to(root).as_posix(): path.stat().st_size for path in sorted(root.rglob("*"))
            if path.is_file() and path.name.casefold() not in release.JUNK_FILES and path.suffix.casefold() != ".torrent"}


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def generate(root: Path, base: str = "") -> tuple[str, dict[str, int]]:
    """Preserve BDInfo/DVD analysis and append a real MediaInfo report per file.

    Relative Complete name values distinguish CD1/01.flac from CD2/01.flac;
    absolute server paths never go into the uploaded text. No synthetic codec,
    container or media information is substituted on a reader failure.
    """
    expected = inventory(root)
    reports = [base.rstrip()] if base.strip() else []
    covered = {}
    for name, size in expected.items():
        path = root / name if root.is_dir() else root
        report = media.mediainfo_text(path)
        report = re.sub(r"(?m)^(Complete name\s*:\s*).*$", lambda m: m[1] + name, report)
        reports.append(report.rstrip())
        covered[name] = size
    if not covered:
        raise media.MediaError("üres torrent-fájllista a techinfó készítésekor")
    return "\n\n\n".join(reports) + "\n", covered
