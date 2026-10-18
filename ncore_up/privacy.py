"""Remove credentials from human-readable diagnostics without changing requests."""
from __future__ import annotations
import re

REDACTED = '[REDACTED]'
FIELDS = r'(?:getUnique|passkey|access_token|refresh_token|api_key|apikey|auth_key|client_secret|password|passwd|PHPSESSID|key|token)'


def redact(text: str) -> str:
    value = str(text or '')
    # HTML input attribute order may vary; strip the entire credential input.
    value = re.sub(r'(?is)<input\b[^>]*>', lambda m: REDACTED if
                   re.search(r'(?i)\b(?:name|id)\s*=\s*[\"\x27]?' + FIELDS + r'(?:[\"\x27\s>]|$)', m[0]) else m[0], value)
    # Raw, HTML-escaped and percent-encoded query separators all occur in logs.
    value = re.sub(r'(?i)((?:[?&;]|&amp;|%26|%3f)' + FIELDS + r'(?:=|%3d))[^\s\"\x27<>?&;]+', r'\1' + REDACTED, value)
    value = re.sub(r'(?i)([\"\x27]?' + FIELDS + r'[\"\x27]?\s*[:=]\s*[\"\x27]?)[^\s\"\x27<>;&,}]+', r'\1' + REDACTED, value)
    value = re.sub(r'(?i)((?:Authorization|Proxy-Authorization|Cookie|Set-Cookie)\s*:\s*)[^\r\n<]+', r'\1' + REDACTED, value)
    # Tracker announce URLs also put passkeys in a path instead of a query.
    value = re.sub(r'(?i)(https?://[^/\s<>\"\x27]+/)([0-9a-f]{32,64})(?=/announce(?:\.php)?(?:[/?\s\"\x27<]|$))', r'\1' + REDACTED, value)
    return value
