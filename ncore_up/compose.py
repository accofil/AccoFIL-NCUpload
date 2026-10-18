"""Descriptions ("szoveg") built from a resolver's Meta, one template per content group.

Templates follow approved uploads (docs/elemzes.md) and the wiki's mandatory items: proof link ("igazoló link",
never Wikipedia/YouTube/Videa), source ("Forrás"), Hungarian installation guide for games/programs/consoles,
requirements, console region, ebook format with SCAN/OCR, language for English ebooks, tracklist for music.
Machine-translated or Wikipedia text is marked; nothing is invented: empty fields are left out.
"""

from __future__ import annotations

import re

from .meta import Meta, valid_proof


def _lines(*pairs: tuple[str, str]) -> list[str]:
    return [f"{label}: {value}" for label, value in pairs if value]


BOILERPLATE = re.compile(
    r"(?i)terms of service|privacy policy|software usage terms|health warning|one-time licen[cs]e|download of this product|"
    r"library programs|playstation ?plus membership is required|in-game purchases|all rights reserved|"
    r"felhasználási feltételek|adatvédelmi (irányelv|nyilatkozat)|egészségügyi figyelmeztetés"
)
MAX_PLOT = 2000


def clean_text(text: str, limit: int = MAX_PLOT) -> str:
    """Drop store/legal boilerplate paragraphs and cut at a paragraph (or sentence) boundary under ``limit``."""

    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip() and not BOILERPLATE.search(p)]
    out = ""
    for paragraph in paragraphs:
        if len(out) + len(paragraph) + 2 > limit:
            if not out:
                cut = paragraph[:limit]
                out = cut[: max(cut.rfind(". ") + 1, limit // 2)]
            break
        out = f"{out}\n\n{paragraph}" if out else paragraph
    return out.strip()


def plot_block(meta: Meta) -> list[str]:
    """The descriptive text with its provenance notes ([] when there is none)."""

    text = clean_text((meta.description_hu or meta.description_en or "").strip())
    if not text:
        return []
    notes = []
    if meta.machine_translated:
        notes.append("gépi fordítás")
    if not meta.description_hu and meta.description_en:
        notes.append("angol nyelvű leírás")
    if meta.description_source:
        credit = "TVmaze (CC BY-SA)" if meta.description_source == "TVmaze" else meta.description_source
        notes.append("Forrás: " + credit)
    return [text + (f"\n({'; '.join(notes)})" if notes else "")]


NO_PROOF = ("wikipedia.org", "youtube.com", "youtu.be", "videa.hu")  # the wiki does not accept these as proof


def proof_block(meta: Meta, label: str = "Igazoló link") -> list[str]:
    links = [link for link in dict.fromkeys(meta.proof_links) if valid_proof(link)]
    if not links:
        return []
    if len(links) == 1:
        return [f"{label}: {links[0]}"]
    return [f"{label}:"] + links[:3]


def tracklist(meta: Meta) -> list[str]:
    lines = []
    for track in meta.tracks:
        artist = f"{track.artist} - " if track.artist else ""
        lines.append(f"{track.position}. {artist}{track.title}" + (f" ({track.length})" if track.length else ""))
    return lines


def join(blocks: list[list[str]]) -> str:
    return "\n\n".join("\n".join(block) for block in blocks if block).strip() + "\n"


def music(meta: Meta) -> str:
    catno = meta.extra.get("catno", "")
    head = _lines(("Előadó", ", ".join(meta.creators)), ("Album címe", meta.title), ("Alcím", meta.subtitle),
                  ("Megjelenés éve", meta.year), ("Kiadó", meta.publisher + (f" ({catno})" if meta.publisher and catno else "")),
                  ("Katalógusszám", "" if meta.publisher else catno))
    tracks = tracklist(meta)
    spotify = meta.extra.get("spotify_url", "")
    credit = ["Spotify-adatforrás: " + spotify] if spotify and spotify not in meta.proof_links else []
    plot = plot_block(meta)
    if not meta.description_source and meta.description_hu.startswith(meta.title + ":"):
        plot = []  # generated factual fallback only repeats the artist/title/year header
    return join([head, ["Tracklista:"] + tracks if tracks else [], plot, proof_block(meta), credit])


def audiobook(meta: Meta) -> str:
    head = _lines(("Szerző", ", ".join(meta.creators)), ("Cím", meta.title), ("Előadó", meta.extra.get("narrator", "")),
                  ("Kiadó", meta.publisher), ("Megjelenés éve", meta.year),
                  ("Teljes hossz", meta.extra.get("duration", "")))
    tracks = tracklist(meta)
    return join([head, plot_block(meta), ["Fejezetek:"] + tracks if tracks else [], proof_block(meta)])


def clip(meta: Meta, *, audio: str, subtitles: str, menu: bool | None, extras: str, chapters: bool, sample: bool, source: str) -> str:
    head = _lines(("Előadó", ", ".join(meta.creators)), ("Kiadvány címe", meta.title), ("Alcím", meta.subtitle),
                  ("Megjelenés éve", meta.year), ("Formátum", meta.extra.get("format", "")))
    tech = _lines(("Menü", "" if menu is None else ("van" if menu else "nincs")), ("Hangsáv", audio), ("Felirat", subtitles),
                  ("Extra", extras), ("Chapter", "van" if chapters else "nincs"), ("Sample", "van" if sample else "nincs"))
    tracks = tracklist(meta)
    return join([head, ["Tracklista:"] + tracks if tracks else [], plot_block(meta), tech, _lines(("Forrás", source)), proof_block(meta)])


def book(meta: Meta) -> str:
    fmt = meta.extra.get("format", "")
    head = _lines(("Könyv címe", meta.title), ("Alcím", meta.subtitle), ("Eredeti cím", meta.title_original),
                  ("Sorozat", meta.extra.get("series", "")), ("Szám", meta.extra.get("issue", "")),
                  ("Kiadás dátuma", meta.date or meta.year), ("Kiadó", meta.publisher), ("Formátum", fmt))
    who = _lines(("Nyelv", ", ".join(meta.languages)), ("Szerző(k)", ", ".join(meta.creators)), ("Fordító", meta.extra.get("translator", "")),
                 ("Oldalak száma", meta.extra.get("pages", "")),
                 ("ISBN", meta.identifiers.get("isbn", "")))
    return join([head, who, proof_block(meta), ["Leírás:"] + plot_block(meta) if plot_block(meta) else []])


def game(meta: Meta, name: str) -> str:
    head = [name] if name else []
    facts = _lines(("Megjelenés dátuma", meta.date or meta.year), ("Fejlesztő", ", ".join(meta.creators)), ("Kiadó", meta.publisher),
                   ("Műfaj", ", ".join(meta.genres)), ("Nyelv", ", ".join(meta.languages)), ("Verzió", meta.version))
    req = ["Gépigény:", meta.requirements] if meta.requirements else []
    install = ["Telepítés:", meta.install_hu] if meta.install_hu else []
    return join([head, facts, plot_block(meta), proof_block(meta, "Hivatalos oldal / igazoló link"), req, install])


def software(meta: Meta, name: str) -> str:
    head = [name] if name else []
    facts = _lines(("Program neve", meta.title), ("Verzió", meta.version), ("Fejlesztő", meta.publisher or ", ".join(meta.creators)),
                   ("Architektúra", meta.extra.get("arch", "")), ("Licenc", meta.extra.get("license", "")),
                   ("Nyelv", ", ".join(meta.languages)))
    req = ["Rendszerigény:", meta.requirements] if meta.requirements else []
    install = ["Telepítés:", meta.install_hu] if meta.install_hu else []
    return join([head, facts, plot_block(meta), proof_block(meta, "Hivatalos oldal / igazoló link"), req, install])


def console(meta: Meta, name: str) -> str:
    head = [name] if name else []
    facts = _lines(("Platform", meta.platform), ("Kiadó", meta.publisher), ("Fejlesztő", ", ".join(meta.creators)),
                   ("Kategória", ", ".join(meta.genres)), ("Megjelenés", meta.date or meta.year), ("Régió", meta.region),
                   ("Nyelv", ", ".join(meta.languages)), ("Verzió", meta.version),
                   ("Azonosító", meta.identifiers.get("content_id") or meta.identifiers.get("titleid") or meta.identifiers.get("serial") or meta.identifiers.get("id6", "")))
    need = _lines(("Szükséges gép", meta.extra.get("required_system", "")))
    install = ["Telepítés:", meta.install_hu] if meta.install_hu else []
    return join([head, facts, plot_block(meta), need, proof_block(meta), install])


def app(meta: Meta, name: str) -> str:
    head = [name] if name else []
    facts = _lines(("Verzió", meta.version), ("Fejlesztő", meta.publisher or ", ".join(meta.creators)), ("Kategória", ", ".join(meta.genres)),
                   ("Minimum rendszer", meta.extra.get("min_os", "")), ("Csomagnév", meta.identifiers.get("package", "")),
                   ("Aláírás", meta.extra.get("signature", "")))
    install = ["Telepítés:", meta.install_hu] if meta.install_hu else []
    return join([head, facts, plot_block(meta), proof_block(meta), install])
