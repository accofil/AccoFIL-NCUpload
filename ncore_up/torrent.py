"""Create the .torrent with mkbrr and read info-hashes (nCore: the tracker URL does not matter, the file must be <= 1 MB).

mkbrr names the torrent after the content folder (or file), so the content path must carry the release name: the
release itself or its hard-linked staging copy ``.ncore-up/<name>``. The torrent gets a source tag, so its info-hash
differs from another tracker's torrent of the same files (qBittorrent refuses a second torrent with the same hash).
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

MAX_TORRENT_BYTES = 1024 * 1024
PLACEHOLDER_TRACKER = "https://ncore.pro/announce"
SOURCE_TAG = "nCore"
MAX_PIECE_EXPONENT = 27  # mkbrr: -l 16..27


def _decode(data: bytes, index: int = 0):
    char = data[index:index + 1]
    if char == b"i":
        end = data.index(b"e", index)
        return int(data[index + 1:end]), end + 1
    if char in (b"l", b"d"):
        index += 1
        items = []
        while data[index:index + 1] != b"e":
            if index >= len(data):
                raise ValueError("truncated bencode")
            value, index = _decode(data, index)
            items.append(value)
        if char == b"l":
            return items, index + 1
        return dict(zip(items[0::2], items[1::2])), index + 1
    if not char.isdigit():
        raise ValueError(f"invalid bencode at byte {index}")
    colon = data.index(b":", index)
    length = int(data[index:colon])
    if colon + 1 + length > len(data):
        raise ValueError("truncated bencode string")
    return data[colon + 1:colon + 1 + length], colon + 1 + length


def decode(torrent: bytes) -> dict:
    """The decoded top-level dictionary (ValueError for anything that is not a torrent)."""

    if not torrent.startswith(b"d"):
        raise ValueError("not a bencoded dictionary")
    try:
        value, _end = _decode(torrent, 0)
    except (IndexError, ValueError) as exc:
        raise ValueError(f"invalid torrent: {exc}") from exc
    if not isinstance(value, dict) or not isinstance(value.get(b"info"), dict):
        raise ValueError("no info dictionary in torrent")
    return value


def info_hash(torrent: bytes) -> str:
    """SHA-1 of the exact bytes of the top-level info dictionary."""

    if not torrent.startswith(b"d"):
        raise ValueError("not a bencoded dictionary")
    index = 1
    try:
        while torrent[index:index + 1] != b"e":
            if index >= len(torrent):
                raise ValueError("truncated torrent")
            key, index = _decode(torrent, index)
            start = index
            _value, index = _decode(torrent, index)
            if key == b"info":
                return hashlib.sha1(torrent[start:index]).hexdigest()
    except IndexError as exc:
        raise ValueError(f"invalid torrent: {exc}") from exc
    raise ValueError("no info dictionary in torrent")


def info(torrent: bytes) -> dict:
    return decode(torrent)[b"info"]


def name(torrent: bytes) -> str:
    return info(torrent).get(b"name", b"").decode("utf-8", errors="replace")


def piece_length(torrent: bytes) -> int:
    return int(info(torrent).get(b"piece length") or 0)


def _layout(meta: dict) -> tuple:
    files = meta.get(b"files")
    if files is None:
        return (meta.get(b"name"), meta.get(b"length"))
    return (meta.get(b"name"), tuple((tuple(f.get(b"path") or ()), f.get(b"length")) for f in files))


def same_content(a: bytes, b: bytes) -> bool:
    """Same name, file layout and piece hashes: the same data, even when the tracker changed other info fields
    (source, private) and so the info-hash."""

    try:
        x, y = info(a), info(b)
    except ValueError:
        return False
    return (_layout(x) == _layout(y) and x.get(b"piece length") == y.get(b"piece length")
            and bool(x.get(b"pieces")) and x.get(b"pieces") == y.get(b"pieces"))


def create(mkbrr: str, content: Path, output: Path, piece_exponent: int | None = None, source: str = SOURCE_TAG) -> bytes:
    """Make the torrent; when it is over 1 MB, retry with twice the piece size the previous attempt used."""

    content = Path(content)
    from .release import assert_safe_tree
    try:
        assert_safe_tree(content)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from None
    if not content.exists():
        raise RuntimeError(f"nincs meg a torrent tartalma: {content}")
    exponent = piece_exponent
    while True:
        output.unlink(missing_ok=True)
        command = [mkbrr, "create", str(content), "-t", PLACEHOLDER_TRACKER, "--private", "--skip-prefix", "--no-date", "-o", str(output), "-q"]
        if source:
            command += ["-s", source]
        if exponent:
            command += ["-l", str(exponent)]
        try:
            run = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
                                 timeout=6 * 3600, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            raise RuntimeError("mkbrr időtúllépés") from None
        except OSError as exc:
            raise RuntimeError(f"az mkbrr nem indítható ({mkbrr}): {exc}") from exc
        if run.returncode != 0 or not output.exists():
            raise RuntimeError(f"mkbrr hiba: {(run.stderr or '').strip() or (run.stdout or '').strip()}")
        data = output.read_bytes()
        try:
            torrent_name, used = name(data), piece_length(data)
        except ValueError as exc:
            raise RuntimeError(f"az mkbrr hibás torrentet írt: {exc}") from exc
        if torrent_name != content.name:
            raise RuntimeError(f"a torrent neve ({torrent_name}) nem egyezik a tartalom nevével ({content.name})")
        if len(data) <= MAX_TORRENT_BYTES:
            return data
        exponent = max(used.bit_length() - 1, exponent or 0) + 1
        if exponent > MAX_PIECE_EXPONENT:
            raise RuntimeError("a torrentfájl a legnagyobb darabmérettel is 1 MB fölött van")
