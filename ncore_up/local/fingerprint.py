"""Chromaprint audio fingerprints (``fpcalc -json``) for AcoustID lookups of untagged music."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from .. import safeio


def parse_fpcalc(text: str | bytes) -> dict:
    """{"duration": int seconds, "fingerprint": str} from fpcalc's JSON output; {} when unusable."""

    try:
        data = json.loads(text or b"{}")
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    fingerprint = str(data.get("fingerprint") or "")
    try:
        duration = int(round(float(data.get("duration") or 0)))
    except (TypeError, ValueError):
        duration = 0
    if not fingerprint or duration <= 0:
        return {}
    return {"duration": duration, "fingerprint": fingerprint}


def fingerprint(path: Path, length: int = 120) -> dict:
    """Fingerprint of the first ``length`` seconds plus the whole file's duration; {} without fpcalc or on error."""

    if not shutil.which("fpcalc"):
        return {}
    try:
        run = safeio.run(["fpcalc", "-json", "-length", str(length), str(path)], timeout=180)
    except (OSError, subprocess.SubprocessError):
        return {}
    return parse_fpcalc(run.stdout)
