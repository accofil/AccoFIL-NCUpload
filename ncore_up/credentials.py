"""Merge user-supplied API keys into config.env without logging secret values."""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

from . import keys
from .config import CONFIG_FILE, read_env
from .workflow import private_dir


def import_keys(source: Path, destination: Path = CONFIG_FILE) -> list[str]:
    incoming = {}
    for number, line in enumerate(source.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        name = name.strip().removeprefix("NCORE_")
        if not separator or name not in keys.NAMES:
            raise ValueError(f"érvénytelen kulcsmező a(z) {number}. sorban; csak a dokumentált API-kulcsok adhatók meg")
        value = value.strip().strip('"').strip("'")
        if not value or len(value) > 8192 or any(char.isspace() for char in value):
            raise ValueError(f"üres vagy hibás érték: {name}")
        if name in incoming:
            raise ValueError(f"ismétlődő kulcsmező: {name}")
        incoming[name] = value
    if not incoming:
        raise ValueError("a fájl nem tartalmaz API-kulcsot")
    current = read_env(destination)
    combined = {name: current.get(name) or current.get("NCORE_" + name) or "" for name in keys.NAMES}
    combined.update(incoming)
    for pair in (("SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET"), ("IGDB_CLIENT_ID", "IGDB_CLIENT_SECRET"),
                 ("DISCOGS_CONSUMER_KEY", "DISCOGS_CONSUMER_SECRET")):
        if any(name in incoming for name in pair) and not all(combined[name] for name in pair):
            raise ValueError("együtt szükséges: " + " + ".join(pair))
    lines = destination.read_text(encoding="utf-8").splitlines() if destination.exists() else []
    output = []
    for line in lines:
        match = re.match(r"\s*(?:NCORE_)?([A-Z_]+)\s*=", line)
        if not match or match[1] not in incoming:
            output.append(line)
    output.extend(name + "=" + value for name, value in sorted(incoming.items()))
    private_dir(destination.parent)
    descriptor, name = tempfile.mkstemp(prefix="config-", dir=destination.parent)
    temp = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write("\n".join(output) + "\n")
        temp.chmod(0o600)
        temp.replace(destination)
    finally:
        temp.unlink(missing_ok=True)
    return sorted(incoming)
