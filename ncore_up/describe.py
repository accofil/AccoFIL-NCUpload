"""Torrent names and descriptions in the style of approved nCore uploads.

The templates come from a sample of approved uploads per category (see docs/elemzes.md). For non-release
films and series, approved uploads show the following details. HD/DVD films send
them as structured form fields; nCore prepends the block, so their free text must
not repeat it. SD films and series carry the details in their free text:

    Menü: nincs
    Felirat(ok): magyar kiegészítő, magyar, angol
    Hangsáv: magyar 5.1, angol 5.1, angol 2.0 kommentár
    Extra: nincs

    <Hungarian plot>

    Forrás: <the source release or service>

and names like "Az a bizonyos első év 1080p REMUX", "A fegyvertelen katona BD50", "Rezdülések 1080p".
"""

from __future__ import annotations

import re

LANG_HU = {
    "hu": "magyar", "hun": "magyar", "en": "angol", "eng": "angol", "de": "német", "ger": "német", "deu": "német",
    "fr": "francia", "fre": "francia", "fra": "francia", "it": "olasz", "ita": "olasz", "es": "spanyol", "spa": "spanyol",
    "pt": "portugál", "por": "portugál", "ru": "orosz", "rus": "orosz", "pl": "lengyel", "pol": "lengyel", "cs": "cseh",
    "cze": "cseh", "ces": "cseh", "sk": "szlovák", "slo": "szlovák", "slk": "szlovák", "ro": "román", "rum": "román",
    "ron": "román", "hr": "horvát", "hrv": "horvát", "sr": "szerb", "srp": "szerb", "sl": "szlovén", "slv": "szlovén",
    "bg": "bolgár", "bul": "bolgár", "uk": "ukrán", "ukr": "ukrán", "nl": "holland", "dut": "holland", "nld": "holland",
    "sv": "svéd", "swe": "svéd", "no": "norvég", "nor": "norvég", "nob": "norvég", "da": "dán", "dan": "dán",
    "fi": "finn", "fin": "finn", "el": "görög", "gre": "görög", "ell": "görög", "tr": "török", "tur": "török",
    "ja": "japán", "jpn": "japán", "ko": "koreai", "kor": "koreai", "zh": "kínai", "chi": "kínai", "zho": "kínai",
    "ar": "arab", "ara": "arab", "he": "héber", "heb": "héber", "hi": "hindi", "hin": "hindi", "th": "thai", "tha": "thai",
    "et": "észt", "est": "észt", "lv": "lett", "lav": "lett", "lt": "litván", "lit": "litván", "is": "izlandi", "ice": "izlandi",
    "fa": "perzsa", "per": "perzsa", "id": "indonéz", "ind": "indonéz", "vi": "vietnámi", "vie": "vietnámi",
    "ms": "maláj", "may": "maláj", "msa": "maláj", "ta": "tamil", "tam": "tamil", "te": "telugu", "tel": "telugu",
    "kn": "kannada", "kan": "kannada", "ml": "malajálam", "mal": "malajálam", "bn": "bengáli", "ben": "bengáli",
    "ur": "urdu", "urd": "urdu", "fil": "filippínó", "tl": "tagalog", "tgl": "tagalog", "ca": "katalán", "cat": "katalán",
    "eu": "baszk", "baq": "baszk", "eus": "baszk", "gl": "galíciai", "glg": "galíciai", "sq": "albán", "alb": "albán",
    "sqi": "albán", "mk": "macedón", "mac": "macedón", "mkd": "macedón", "bs": "bosnyák", "bos": "bosnyák",
    "nb": "norvég", "nn": "norvég", "yue": "kantoni", "cmn": "mandarin", "lat": "latin", "la": "latin",
}
CHANNELS = {1: "1.0", 2: "2.0", 3: "2.1", 4: "4.0", 5: "5.0", 6: "5.1", 7: "6.1", 8: "7.1"}


def language(code: str) -> str:
    code = (code or "").lower()
    return LANG_HU.get(code, LANG_HU.get(code[:2], code if code not in {"und", ""} else "ismeretlen"))


def channel_label(track: dict) -> str:
    """Prefer the measured speaker layout: four channels can be 4.0 or 3.1."""
    count = track.get("channels", 0)
    layout = str(track.get("channel_layout") or "").lower()
    named = {"mono": "1.0", "stereo": "2.0", "quad": "4.0", "quad(side)": "4.0", "hexagonal": "6.0", "octagonal": "8.0"}
    label = named.get(layout, "")
    match = re.match(r"^(\d+)\.(\d+)(?:\(|$)", layout)
    if match:
        label = f"{match[1]}.{match[2]}"
    if label and sum(int(n) for n in label.split(".")) == count:
        return label
    return CHANNELS.get(count, str(count) if count else "")


def audio_line(tracks: list[dict]) -> str:
    items = []
    for track in tracks:
        text = f"{language(track['lang'])} {channel_label(track)}".strip()
        title = (track.get("title") or "").lower()
        if "comment" in title or "kommentár" in title:
            text += " kommentár"
        elif "descri" in title or "narrat" in title or "ad" == title:
            text += " narráció (AD)"
        items.append(text)
    return ", ".join(items) or "nincs"


def subtitle_line(tracks: list[dict], external: list[str] = ()) -> str:
    items = []
    for track in tracks:
        title = (track.get("title") or "").lower()
        text = language(track["lang"])
        if track.get("forced") or "forced" in title or "kiegészítő" in title:
            text += " kiegészítő"
        items.append(text)
    items.extend(re.sub(r"(?i)\s*\bSDH\b", "", value).strip() for value in external)
    return ", ".join(dict.fromkeys(items)) or "nincs"


def source_text(value: str) -> str:
    """Keep complete release provenance, without our operational explanations (wiki 30/139)."""
    value = re.sub(r"(?i)^\s*(?:forrás\s*:\s*)?videó és hang\s*:\s*", "", value or "").strip()
    value = re.sub(r"(?is)\s*A szerveren meglévő fájl kép- és hangsávjai.*$", "", value)
    value = re.sub(r"(?is)\s*A kép- és hangsávok? újrakódolás nélkül.*$", "", value)
    return value.strip().rstrip(".")


def clean_music_description(text: str, *, artist: str, album: str, year: str) -> str:
    """Own music: nCore prepends artist/album/year from the form, including for clips.

    Apply at serialization too: supplied descriptions and editor output can contain
    the plain or BBCode headers. Keep fields that have no structured replacement.
    The author's separate credit in an audiobook is not the performer field.
    """
    fields = {"előadó": artist, "album címe": album, "kiadvány címe": album,
              "cím": album, "megjelenés éve": year}
    out = []
    for line in text.splitlines():
        plain = re.sub(r"\[/?(?:b|u|i)\]", "", line, flags=re.I).strip()
        match = re.match(r"^([^:]+)\s*:\s*(.*)$", plain)
        if match:
            label = match[1].strip().casefold()
            if fields.get(label):
                continue
        out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip() + "\n"


def clean_video_description(text: str, *, series: bool, structured_details: bool) -> str:
    """Normalize generated and supplied descriptions against the fields nCore renders itself."""
    out = []
    for line in text.splitlines():
        label = re.sub(r"\[/?b\]", "", line, flags=re.I).strip()
        if series and re.match(r"(?i)^(?:Évad|Epizód(?: címe)?)\s*:", label):
            continue
        if structured_details and re.match(r"(?i)^(?:Menü|Felirat\(ok\)|Felirat|Hangsáv|Extra)\s*:", label):
            continue
        if re.fullmatch(r"(?i)Extra\s*:\s*(?:nincs|ismeretlen)?", label):
            continue
        match = re.match(r"(?i)^Felirat(?:\(ok\))?\s*:\s*(.*)", label)
        if match:
            values = [re.sub(r"(?i)\s*\bSDH\b", "", item).strip() for item in match[1].split(",")]
            line = "Felirat(ok): " + ", ".join(dict.fromkeys(v for v in values if v))
        match = re.match(r"(?i)^Forrás\s*:\s*(.*)", label)
        if match:
            line = "Forrás: " + source_text(match[1])
        out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip() + "\n"


def video_description(*, menu: bool | None, audio: list[dict], subtitles: list[dict], external_subs: list[str], extras: str,
                      plot: str, source: str, include_details: bool = True) -> str:
    lines = []
    if include_details:
        if menu is not None:
            lines.append(f"Menü: {'van' if menu else 'nincs'}")
        lines += [f"Felirat(ok): {subtitle_line(subtitles, external_subs)}", f"Hangsáv: {audio_line(audio)}"]
        if extras and extras.casefold() not in {"nincs", "ismeretlen"}:
            lines.append(f"Extra: {extras}")
        lines.append("")
    if plot:
        lines += [plot.strip(), ""]
    if source:
        lines.append(f"Forrás: {source_text(source)}")
    return "\n".join(lines).strip() + "\n"


RESOLUTION_RE = re.compile(r"(?i)(?<![0-9])(2160p|1080p|1080i|720p|576p|480p)(?![0-9])")


def video_name(title_hu: str, title_en: str, release_name: str, *, hungarian: bool, disc: str = "", season: int | None = None,
               episodes: str = "", remux: bool = False, sd: bool = False, hdr: bool = False, dovi: bool = False,
               uhd_bluray: bool = False) -> str:
    """Torrent name of a non-release film/series, following the wiki's examples:

    "Tomb Raider 720p", "Tomb Raider 1080p REMUX", "Tomb Raider 1080p DV HDR", "Tomb Raider BD50",
    "Tomb Raider 2160p UHD BluRay REMUX DV HDR", "A szökés S01E10 1080p", "A függetlenség napja" (SD),
    "A függetlenség napja DVD5". Title: Hungarian when there is one, else English, else original.
    """

    title = title_hu if (title_hu and hungarian) or (title_hu and not title_en) else (title_en or title_hu)
    title = re.sub(r"\s*:\s*", " - ", title).strip()
    parts = [title]
    if season is not None:
        parts.append(f"S{season:02d}" + (f"E{episodes}" if episodes else ""))
    match = RESOLUTION_RE.search(release_name)
    resolution = match.group(1).lower() if match else ""
    if disc and not disc.startswith("UHD"):
        parts.append(disc)  # BD25 / BD50 / DVD5 (DVD9 has no tag)
        return " ".join(p for p in parts if p and p != "DVD9")
    if sd:
        return " ".join(parts)
    if disc.startswith("UHD") or uhd_bluray:
        resolution = "2160p"
    if resolution:
        parts.append(resolution)
    remux = remux or bool(re.search(r"(?i)\bremux\b", release_name))
    if resolution == "2160p" and (disc or uhd_bluray or (remux and re.search(r"(?i)blu-?ray|\bbd\b", release_name))):
        parts.append("UHD BluRay")
    if remux:
        parts.append("REMUX")
    if dovi:
        parts.append("DV")
    if hdr:
        parts.append("HDR")
    return " ".join(parts)


def disc_label(total_bytes: int, uhd: bool) -> str:
    """BD25/BD50/BD66/BD100 by size, as approved full-disc uploads are named."""

    gib = total_bytes / 1024**3
    for limit, label in ((23.3, "BD25"), (46.6, "BD50"), (61.5, "BD66"), (93.1, "BD100")):
        if gib <= limit:
            return "UHD " + label if uhd and label in {"BD66", "BD100"} else label
    return "BD100"


def tracklist(tracks: list[tuple[str, str, str]]) -> str:
    return "\n".join(f"{no}. {title}" + (f" ({length})" if length else "") for no, title, length in tracks)
