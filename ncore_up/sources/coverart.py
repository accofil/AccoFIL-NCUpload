"""Cover Art Archive: front covers of MusicBrainz releases and release groups (thumbnails 250/500/1200 px).

No key, no published limit (net paces it at 0.5 s); the image URLs redirect to archive.org. nCore's infobar takes
at most 300x300, so the caller shrinks the 500 px thumbnail (``media.infobar_cover``).
"""

from __future__ import annotations

import re

from .. import net

API = "https://coverartarchive.org/"
_MBID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def is_image(data: bytes) -> bool:
    return data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n" or data[:4] == b"GIF8" or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")


def front_url(mbid: str, size: int = 500, group: bool = False) -> str:
    if not _MBID.match(mbid or ""):
        return ""
    suffix = f"front-{size}" if size in (250, 500, 1200) else "front"
    return f"{API}{'release-group' if group else 'release'}/{mbid}/{suffix}"


def front(mbid: str, size: int = 500, group: bool = False) -> bytes:
    """Front cover bytes, b'' when there is none (404) or the archive fails."""

    url = front_url(mbid, size, group)
    if not url:
        return b""
    data = net.get_bytes(url, quiet=True, retries=2)
    return data if is_image(data) else b""


def parse_index(data: dict) -> dict:
    """{"release", "front": {"image", "250", "500", "1200"}, "count"} from the JSON index of a release/release group."""

    images = (data or {}).get("images") or []
    chosen = next((i for i in images if i.get("front")), None) or next((i for i in images if "Front" in (i.get("types") or [])), None)
    result = {"release": (data or {}).get("release", ""), "front": {}, "count": len(images)}
    if chosen:
        thumbs = chosen.get("thumbnails") or {}
        result["front"] = {"image": chosen.get("image", ""), "250": thumbs.get("250") or thumbs.get("small", ""),
                           "500": thumbs.get("500") or thumbs.get("large", ""), "1200": thumbs.get("1200", "")}
    return result


def index(mbid: str, group: bool = False) -> dict:
    if not _MBID.match(mbid or ""):
        return {}
    return parse_index(net.get_json(f"{API}{'release-group' if group else 'release'}/{mbid}", quiet=True) or {})
