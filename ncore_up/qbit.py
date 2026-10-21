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
from pathlib import Path

import requests

BAD_STATES = {"missingFiles", "error"}
SEED_STATES = {"uploading", "stalledUP", "forcedUP"}
# Still settling after the add: hash check, queue, move or metadata. Wait for these instead of failing.
PENDING_STATES = {"checkingUP", "checkingDL", "checkingResumeData", "queuedUP", "queuedDL", "moving", "metaDL", "allocating"}


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


def _login(session: requests.Session, base: str, user: str, password: str) -> None:
    if not user:
        return
    login = session.post(base + "/api/v2/auth/login", data={"username": user, "password": password}, timeout=30)
    if login.status_code == 403:
        raise QbitError("a qBittorrent kitiltotta ezt az IP-t a sok hibás bejelentkezés miatt")
    if login.text.strip() != "Ok.":
        raise QbitError(f"qBittorrent bejelentkezés sikertelen (QB_USER/QB_PASS): {login.status_code} {login.text[:80]}")


def _overlaps(a: Path, b: Path) -> bool:
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def incomplete_at(url: str, user: str, password: str, source: str, session: requests.Session | None = None) -> list[str]:
    """Names of qBittorrent torrents whose content overlaps ``source`` and that are not complete yet.

    With file preallocation on and no ".!qB" extension (the seedbox default), an unfinished download looks like a
    finished one on disk; only the client knows. Raises QbitError when qBittorrent cannot be asked.
    """

    base = url.rstrip("/")
    session = session or requests.Session()
    target = Path(source).resolve()
    try:
        _login(session, base, user, password)
        response = session.get(base + "/api/v2/torrents/info", timeout=60)
        items = response.json() if response.status_code == 200 else None
    except (requests.RequestException, ValueError) as exc:
        raise QbitError(f"a qBittorrent nem érhető el ({base}): {type(exc).__name__}") from exc
    if not isinstance(items, list):
        raise QbitError(f"a qBittorrent torrentlistája nem olvasható ({response.status_code})")
    busy = []
    for item in items:
        if not isinstance(item, dict) or not item.get("content_path"):
            continue
        try:
            content = Path(str(item["content_path"])).resolve()
        except (OSError, RuntimeError):
            continue
        if not _overlaps(content, target):
            continue
        state = str(item.get("state", ""))
        if float(item.get("progress", 0) or 0) < 1 or state in {"downloading", "stalledDL", "metaDL", "queuedDL", "checkingDL",
                                                                   "forcedDL", "allocating", "moving", "checkingResumeData"} or state.endswith("DL"):
            busy.append(f"{item.get('name', '?')} ({state}, {float(item.get('progress', 0) or 0) * 100:.1f}%)")
    return busy


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
        _login(session, base, user, password)
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
    forced = False
    while True:
        item = _find(session, base, info_hash, 30)
        state = str((item or {}).get("state", ""))
        if state in BAD_STATES:
            raise QbitError(f"a qBittorrent nem találja a fájlokat ({state}): ellenőrizd a mentési helyet: {save_path}")
        if state in SEED_STATES and float(item.get("progress", 1)) >= 1:
            return state
        if state.startswith(("paused", "stopped")):
            raise QbitError(f"a torrent leállítva van ({state}), a seedelés nem aktív")
        if state == "queuedUP" and not forced:
            # A full upload queue would keep it from seeding past nCore's 30-minute limit.
            try:
                session.post(base + "/api/v2/torrents/setForceStart", data={"hashes": info_hash.lower(), "value": "true"}, timeout=30)
            except requests.RequestException:
                pass
            forced = True
        if time.monotonic() >= deadline:
            break
        time.sleep(2 if state in PENDING_STATES else 0.5)
    if item is None:
        raise QbitError(f"a qBittorrent elfogadta, de nem jelent meg a listájában ({info_hash})")
    raise QbitError(f"a seedelés még nem igazolt (állapot: {item.get('state', '')}); folytasd --resume paranccsal")


def torrents(url: str, user: str, password: str, session: requests.Session | None = None) -> list[dict]:
    """Every torrent of the client (read-only), for the seeding audit."""

    base = url.rstrip("/")
    session = session or requests.Session()
    try:
        _login(session, base, user, password)
        response = session.get(base + "/api/v2/torrents/info", timeout=60)
        items = response.json() if response.status_code == 200 else None
    except (requests.RequestException, ValueError) as exc:
        raise QbitError(f"a qBittorrent nem érhető el ({base}): {type(exc).__name__}") from exc
    if not isinstance(items, list):
        raise QbitError("a qBittorrent torrentlistája nem olvasható")
    return [item for item in items if isinstance(item, dict)]
