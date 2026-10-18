"""User-exported nCore cookies, validated before replacing the saved session."""
from __future__ import annotations

import http.cookiejar
import os
import tempfile
from pathlib import Path

from .client import NcoreClient, load_cookies
from .workflow import private_dir


def import_cookies(source: Path, destination: Path) -> None:
    jar = load_cookies(str(source))
    selected = http.cookiejar.MozillaCookieJar()
    for cookie in jar:
        domain = cookie.domain.lstrip(".").lower()
        if domain == "ncore.pro" or domain.endswith(".ncore.pro"):
            selected.set_cookie(cookie)
    if not list(selected):
        raise ValueError("a fájl nem tartalmaz ncore.pro sütit")
    NcoreClient(selected).upload_token()  # read-only; expired cookies do not replace the working file
    private_dir(destination.parent)
    descriptor, name = tempfile.mkstemp(prefix="cookies-", dir=destination.parent)
    os.close(descriptor)
    temp = Path(name)
    try:
        selected.save(str(temp), ignore_discard=True, ignore_expires=True)
        temp.chmod(0o600)
        temp.replace(destination)
    finally:
        temp.unlink(missing_ok=True)
