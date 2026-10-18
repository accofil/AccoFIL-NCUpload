"""Mandatory rule evidence, evaluated against the FINAL form and attachments.

UNKNOWN requires a user decision; FAIL means a demonstrable missing/invalid item.
Neither may be bypassed by --force. Identification confidence cannot replace evidence.
Rule mapping: nCore wiki 30, 31, 142, 143, 144, 258 (reviewed 2026-10-18).
"""
from __future__ import annotations

import re
import hashlib
from dataclasses import asdict, dataclass
from urllib.parse import urlsplit

from PIL import Image

from . import form
from .meta import valid_proof


@dataclass
class Requirement:
    id: str
    status: str
    message: str
    evidence: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def proof_urls(text: str) -> list[str]:
    out = []
    # Only labelled proof sections count; a random image/description link does not.
    sections = re.findall(r"(?is)(?:igazol[oó]\s+link|hivatalos\s+(?:oldal|link))[^\n]*\n?(.*?)(?=\n\s*\n|$)", text)
    sections += re.findall(r"(?im)^.*?(?:igazol[oó]\s+link|hivatalos\s+(?:oldal|link))[^\n]*", text)
    for section in sections:
        for url in re.findall(r"https?://[^\s\[\]<>]+", section):
            parts = urlsplit(url.rstrip(".,;)"))
            if valid_proof(parts.geturl()):
                out.append(parts.geturl())
    return list(dict.fromkeys(out))


def evaluate(plan) -> list[Requirement]:
    data, meta, options = plan.data, plan.meta, plan.options
    group, own = plan.category.group, not data.original
    checks = []

    def add(key, status, message, evidence=""):
        checks.append(Requirement(key, status, message, evidence))

    if meta and meta.extra.get("category_errors"):
        add("content_category", "FAIL", "; ".join(meta.extra["category_errors"]))

    if data.original:
        add("original_name", "PASS" if data.name == plan.release.name else "FAIL", "eredeti release változatlan neve", data.name)
    if own:
        if form.needs_techinfo(data.category, data.original):
            from . import techinfo
            try:
                expected = techinfo.inventory(plan.content_path)
                covered = getattr(plan, "techinfo_files", {})
                text = plan.texts.get("techinfo_fajl", "")
                valid = bool(expected) and covered == expected and bool(text.strip()) and techinfo.digest(text) == getattr(plan, "techinfo_digest", "")
                add("techinfo_coverage", "PASS" if valid else "FAIL", "a végleges torrent minden fájlja a techinfóban",
                    f"{len(covered)}/{len(expected)} fájl")
            except (OSError, ValueError) as exc:
                add("techinfo_coverage", "FAIL", f"a teljes techinfo nem ellenőrizhető: {exc}")
        add("description", "PASS" if len(data.description.strip()) >= 30 else "FAIL", "érdemi leírás")
        if group in {"music", "ebook", "game", "console", "mobil", "program", "clip"}:
            links = proof_urls(data.description)
            add("proof", "PASS" if links else "FAIL", "kötelező igazoló link", ", ".join(links))
        if group in {"game", "console", "mobil", "program"}:
            match = re.search(r"(?is)telep[ií]t[eé]s[^\n]*\n(.*?)(?:\n\s*\n|$)", data.description)
            guide = match[1].strip() if match else ""
            valid = len(guide) >= 15 and bool(re.search(r"(?i)(telep[ií]t|ind[ií]t|m[aá]sol|v[aá]las|futtas|csatlakoz|nyisd|helyez|t[oö]lts)", guide))
            add("installation", "PASS" if valid else "FAIL", "magyar telepítési útmutató a végleges leírásban")
            system = re.search(r"(?im)^(?:Gépigény|Rendszerigény|Minimum rendszer|Szükséges gép)\s*:\s*(\S[^\n]*)", data.description)
            add("system_requirements", "PASS" if system else "UNKNOWN", "hardver-/szoftverkövetelmények a végleges leírásban")
            if group == "console":
                region = re.search(r"(?im)^Régió\s*:\s*(\S[^\n]*)", data.description)
                add("console_region", "PASS" if region else "UNKNOWN", "konzolkiadás régiója a végleges leírásban")
        if group in {"video", "clip"}:
            explicit = options.source if options else None
            supplied = bool(options and options.description_file)
            source = re.search(r"(?im)^\s*forr[aá]s\s*:\s*(.+)", data.description)
            actual = source[1].strip() if source else ""
            valid = bool(actual and actual.casefold() != "ismeretlen" and (explicit or supplied))
            add("source", "PASS" if valid else "UNKNOWN", "a videó és hang eredetének igazolása (--source vagy adatlap)", explicit or "")
            if plan.release.has_bdmv or plan.release.has_video_ts or plan.category.code.startswith("dvd"):
                add("disc_menu", "PASS" if options and options.menu is not None else "UNKNOWN", "lemez menüjének ellenőrzése (--menu)")
                add("disc_extras", "PASS" if options and options.extras is not None else "UNKNOWN", "lemez extráinak ellenőrzése (--extras)")
        if group == "ebook" and meta:
            langs = {str(x).lower() for x in meta.languages}
            allowed = {"hu", "hun", "magyar", "en", "eng", "angol", "de", "deu", "ger", "német"}
            exception = bool(set(data.styles) & {"szótár.nyelvkönyv", "magazin"})
            add("book_language", "PASS" if langs and (langs <= allowed or exception) else "UNKNOWN" if not langs else "FAIL",
                "könyv megengedett nyelve", ", ".join(sorted(langs)))
            if "titkosított" in " ".join(meta.warnings).lower() or meta.extra.get("encrypted") in {True, "igen"}:
                add("book_encryption", "FAIL", "titkosított könyv nem tölthető fel")
        if group == "game" and meta:
            if meta.extra.get("early_access") == "igen":
                add("early_access", "UNKNOWN" if meta.identifiers.get("gog") else "FAIL", "Early Access kivétel ellenőrzése")
            warnings = " ".join(meta.warnings).lower()
            if any(word in warnings for word in ("demo", "demó", "még nem jelent meg", "nem tudtam lefordítani", "crack lépést")):
                add("game_release", "UNKNOWN", "játék kiadásának/telepítésének tisztázása")
    if group in {"video", "clip"}:
        info = plan.video_info
        add("video_probe", "PASS" if info and info.codec and info.width and info.height and info.duration > 0 else "UNKNOWN", "olvasható videó technikai adatok")
    needs = form.needs_screenshots(data.category, data.original, plan.has_sample)
    book_images = []
    for i in range(1, 4):
        path = plan.attachments.get(f"kep{i}")
        if path is None:
            if needs:
                add(f"image_{i}", "FAIL", f"{i}. kötelező saját kép hiányzik")
            continue
        try:
            with Image.open(path) as image:
                size = image.size
                image.verify()
            native = plan.video_info
            valid_size = not native or size == (native.width, native.height)
            valid = min(size) > 0 and path.stat().st_size <= 5 * 1024**2 and valid_size
            add(f"image_{i}", "PASS" if valid else "FAIL", f"{i}. valódi, legfeljebb 5 MB-os kép, videónál eredeti felbontás", str(size))
            if valid and group == 'ebook' and own:
                from .safeio import read_file, IMAGE_LIMIT
                book_images.append(hashlib.sha256(read_file(path, IMAGE_LIMIT)).hexdigest())
        except (OSError, ValueError, Image.DecompressionBombError):
            add(f"image_{i}", "FAIL", f"{i}. kép nem olvasható képfájlként")
    if len(book_images) == 3:
        add('book_distinct_pages', 'PASS' if len(set(book_images)) == 3 else 'FAIL', 'három különböző könyvoldalkép szükséges')
    plan.requirements = checks
    return checks
