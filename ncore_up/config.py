"""Settings from ~/.config/ncore-up/config.env (KEY=VALUE lines); qBittorrent falls back to ~/.aithercheck.env."""

from __future__ import annotations

import os
import json
import shutil
from dataclasses import dataclass
from pathlib import Path

CONFIG_FILE = Path(os.environ.get("NCORE_CONFIG", str(Path.home() / ".config" / "ncore-up" / "config.env"))).expanduser()
FALLBACK_FILES = [Path.home() / ".aithercheck.env"]


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            value = value.strip()
            if value.startswith('"') and value.endswith('"'):
                try:
                    decoded = json.loads(value)
                    value = decoded if isinstance(decoded, str) else value
                except ValueError:
                    value = value[1:-1]  # legacy quoted config
            elif value.startswith("'") and value.endswith("'"):
                value = value[1:-1]
            values[key.strip()] = os.path.expanduser(value) if key.strip().startswith("NCORE_") and not key.strip().endswith(("KEY", "SECRET", "TOKEN", "API")) else value
    return values


@dataclass
class Config:
    cookies: str
    qb_url: str
    qb_user: str
    qb_pass: str
    qb_category: str
    mkbrr: str
    log_dir: Path
    work_dir: Path
    tmdb_api: str = ""
    ncup_dir: Path | None = None
    bdinfo: str = ""

    @classmethod
    def load(cls, path: Path = CONFIG_FILE) -> "Config":
        values: dict[str, str] = {}
        for fallback in FALLBACK_FILES:
            values.update({k: v for k, v in read_env(fallback).items() if k.startswith("QB_")})
        values.update(read_env(path))
        values.update({k: v for k, v in os.environ.items() if k.startswith("NCORE_") or k.startswith("QB_")})
        home = Path.home()
        from . import keys, net

        keys.load(values)
        net.configure(Path(values.get("NCORE_WORK_DIR", str(home / ".cache" / "ncore-up"))))
        return cls(
            cookies=values.get("NCORE_COOKIES", str(home / ".config" / "ncore-up" / "cookies.txt")),
            qb_url=values.get("QB_URL", "http://127.0.0.1:8080"),
            qb_user=values.get("QB_USER", ""),
            qb_pass=values.get("QB_PASS", ""),
            qb_category=values.get("NCORE_QB_CATEGORY", ""),
            mkbrr=values.get("NCORE_MKBRR") or shutil.which("mkbrr") or str(home / "Upload-Assistant" / "bin" / "mkbrr" / "linux" / "amd64" / "mkbrr"),
            log_dir=Path(values.get("NCORE_LOG_DIR", str(home / "logs" / "ncore-up"))),
            work_dir=Path(values.get("NCORE_WORK_DIR", str(home / ".cache" / "ncore-up"))),
            tmdb_api=values.get("NCORE_TMDB_API", "") or keys.get("TMDB_API"),
            ncup_dir=Path(values["NCORE_NCUP_DIR"]).expanduser() if values.get("NCORE_NCUP_DIR") else None,
            bdinfo=values.get("NCORE_BDINFO") or shutil.which("bdinfo") or str(home / "Upload-Assistant" / "bin" / "bdinfo" / "linux" / "amd64" / "bdinfo"),
        )
