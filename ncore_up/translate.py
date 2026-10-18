"""Hungarian text when a source has none: DeepL API (Free or Pro), with a local cache and a quota guard.

The output is always marked by the caller as machine translation ("(gépi fordítás)"). Without a key, or when the
monthly quota is nearly used up, ``to_hungarian`` returns the text unchanged with ``translated=False``.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from . import keys, net

MAX_CHARS = 1500  # keep the 500k chars/month Free quota for ~300 uploads


def language(text: str) -> str:
    """Two-letter language of a text ('' when unknown); langdetect when installed, a cheap Hungarian check otherwise."""

    text = (text or "").strip()
    if len(text) < 20:
        return ""
    try:
        from langdetect import DetectorFactory, detect  # optional dependency

        DetectorFactory.seed = 0
        return detect(text)
    except Exception:  # noqa: BLE001 - missing package or undetectable text
        # Without langdetect: Hungarian-only letters plus Hungarian function words (not "a"/"az", which English has).
        words = re.findall(r"\b(és|hogy|egy|nem|is|ez|azt|meg|már|csak|mint|amely|akik?|volt|lesz)\b", text, flags=re.I)
        special = len(re.findall(r"[őűŐŰ]", text))
        accented = len(re.findall(r"[áéíóöúüÁÉÍÓÖÚÜ]", text)) / max(1, len(re.findall(r"[A-Za-zÀ-ž]", text)))
        return "hu" if (special >= 1 and len(words) >= 2) or (accented > 0.06 and len(words) >= 3) else ""


def is_hungarian(text: str) -> bool:
    return language(text) == "hu"


def _cache_file() -> Path:
    return net.cache_dir() / "deepl-cache.json"


def _load() -> dict:
    try:
        return json.loads(_cache_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _endpoint(key: str) -> str:
    return "https://api-free.deepl.com" if key.endswith(":fx") else "https://api.deepl.com"


def usage() -> tuple[int, int] | None:
    key = keys.get("DEEPL_KEY")
    if not key:
        return None
    data = net.post_json(_endpoint(key) + "/v2/usage", headers={"Authorization": f"DeepL-Auth-Key {key}"}, expire=0) or {}
    if "character_count" not in data:
        return None
    return int(data["character_count"]), int(data.get("character_limit") or 0)


def to_hungarian(text: str, max_chars: int = MAX_CHARS) -> tuple[str, bool]:
    """(Hungarian text, translated?). Text that is already Hungarian comes back unchanged with False."""

    text = (text or "").strip()
    if not text or is_hungarian(text):
        return text, False
    key = keys.get("DEEPL_KEY")
    if not key:
        return text, False
    if len(text) > max_chars:
        cut = text[:max_chars]
        text = cut[: max(cut.rfind(". "), max_chars // 2) + 1].strip()
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()
    cache = _load()
    if digest in cache:
        return cache[digest], True
    used = usage()
    if used and used[1] and used[0] + len(text) > used[1] * 0.98:
        net.FAILURES.append("DeepL: a havi keret elfogyott")
        return text, False
    data = net.post_json(_endpoint(key) + "/v2/translate", headers={"Authorization": f"DeepL-Auth-Key {key}"},
                         data={"text": text, "target_lang": "HU"}, expire=0) or {}
    translations = data.get("translations") or []
    if not translations:
        return text, False
    result = translations[0].get("text", "").strip()
    cache[digest] = result
    try:
        _cache_file().write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return result, True
