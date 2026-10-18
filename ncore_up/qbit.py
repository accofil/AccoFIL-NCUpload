"""Add the torrent downloaded from nCore to qBittorrent so seeding starts within the required 30 minutes.

WebAPI v2: log in (skipped without a user name, for qBittorrent's localhost bypass), add with the content's parent
folder as save path, content layout "Original" (the torrent's root folder is the release folder itself), automatic
torrent management off (else the category's folder would win) and the hash check skipped only when the caller knows
the files are identical. With the info-hash given, the torrent is looked up afterwards: a torrent that is already
there counts as added, and one whose files qBittorrent cannot find is an error.
"""

from __future__ import annotations

import json
import time

import requests

BAD_STATES = {"missingFiles", "error"}
SEED_STATES = {"uploading", "stalledUP", "forcedUP"}


class QbitError(Exception):
    pass


def _accepted(response: requests.Response) -> bool:
    """qBittorrent answers "Ok." / "Fails."; newer versions may answer JSON with counts."""

    text = response.text.strip()
    if text in {"Ok.", ""}:
        return True
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except ValueError:
            return False
        return not data.get("failure_count") and bool(data.get("success_count") or data.get("pending_count") or data.get("added_torrent_ids"))
    return False


def _find(session: requests.Session, base: str, info_hash: str, timeout: float) -> dict | None:
    try:
        response = session.get(base + "/api/v2/torrents/info", params={"hashes": info_hash.lower()}, timeout=timeout)
        items = response.json() if response.status_code == 200 else []
    except (requests.RequestException, ValueError):
        return None
    return items[0] if isinstance(items, list) and items and isinstance(items[0], dict) else None


def add_torrent(url: str, user: str, password: str, torrent: bytes, save_path: str, skip_checking: bool, category: str = "",
                tags: str = "", info_hash: str = "", wait: float = 6.0, session: requests.Session | None = None) -> str:
    """Add the torrent and return its qBittorrent state ('' when it was not looked up)."""

    base = url.rstrip("/")
    session = session or requests.Session()
    data = {"savepath": save_path, "skip_checking": "true" if skip_checking else "false", "paused": "false", "stopped": "false",
            "contentLayout": "Original", "autoTMM": "false"}
    if category:
        data["category"] = category
    if tags:
        data["tags"] = tags
    try:
        if user:
            login = session.post(base + "/api/v2/auth/login", data={"username": user, "password": password}, timeout=30)
            if login.status_code == 403:
                raise QbitError("a qBittorrent kitiltotta ezt az IP-t a sok hibás bejelentkezés miatt")
            if login.text.strip() != "Ok.":
                raise QbitError(f"qBittorrent bejelentkezés sikertelen (QB_USER/QB_PASS): {login.status_code} {login.text[:80]}")
        response = session.post(base + "/api/v2/torrents/add", data=data, files={"torrents": ("ncore.torrent", torrent, "application/x-bittorrent")}, timeout=60)
    except requests.RequestException as exc:
        raise QbitError(f"a qBittorrent nem érhető el ({base}): {exc}") from exc
    if response.status_code == 403:
        raise QbitError("a qBittorrent bejelentkezést kér: add meg a QB_USER/QB_PASS beállítást")
    if response.status_code == 415:
        raise QbitError("a qBittorrent szerint a torrentfájl érvénytelen")
    if response.status_code != 200 or not _accepted(response):
        if not (info_hash and _find(session, base, info_hash, 30)):
            raise QbitError(f"qBittorrent nem vette fel a torrentet: {response.status_code} {response.text[:120]}")
    if not info_hash:
        return ""
    deadline = time.monotonic() + wait
    item = None
    while True:
        item = _find(session, base, info_hash, 30)
        state = str((item or {}).get("state", ""))
        if state in BAD_STATES:
            raise QbitError(f"a qBittorrent nem találja a fájlokat ({state}): ellenőrizd a mentési helyet: {save_path}")
        if state in SEED_STATES and float(item.get("progress", 1)) >= 1:
            return state
        if state.startswith(("paused", "stopped")):
            raise QbitError(f"a torrent leállítva van ({state}), a seedelés nem aktív")
        if time.monotonic() >= deadline:
            break
        time.sleep(0.5)
    if item is None:
        raise QbitError(f"a qBittorrent elfogadta, de nem jelent meg a listájában ({info_hash})")
    raise QbitError(f"a seedelés még nem igazolt (állapot: {item.get('state', '')}); folytasd --resume paranccsal")
