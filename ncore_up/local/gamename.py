"""PC game release names and GOG installer file names -> title, edition, version, group, source, kind.

    parse("Total.Chaos.v1.0.342-TENOKE")
    -> {"title": "Total Chaos", "version": "1.0.342", "group": "TENOKE", "source": "scene", "kind": "full", ...}

``source`` is one of scene, gog, fitgirl, dodi, elamigos, repack (other repackers), p2p or '' (unknown);
``kind`` is full, update, dlc or crackfix. Pure string work, no network, no file access.
"""

from __future__ import annotations

import re

SCENE_GROUPS = {
    "RUNE", "TENOKE", "FLT", "SKIDROW", "PLAZA", "CODEX", "DOGE", "TINYISO", "DARKSIDERS", "RAZOR1911", "RAZOR",
    "HOODLUM", "SIMPLEX", "PROPHET", "CHRONOS", "SUXXORS", "HI2U", "VACE", "RELOADED", "ALI213", "POSTMORTEM", "ACTIVATED",
    "DEFA", "GOLDBERG", "UNLEASHED", "ANOMALY", "BAT", "RAZORDOX", "SKIDROWRELOADED", "CPY", "STEAMPUNKS", "DINOBYTES",
}
P2P_GROUPS = {"P2P", "I_KNOW", "FCKDRM", "EMPRESS", "0XDEADC0DE", "KAOS", "CHOVKA", "XATAB", "TINYREPACKS", "DARCK", "ONLINEFIX"}
# repacker tag (upper case) -> (source, display name used in torrent names)
REPACKERS = {
    "FITGIRL": ("fitgirl", "FitGirl Repack"), "DODI": ("dodi", "DODI Repack"), "ELAMIGOS": ("elamigos", "ElAmigos"),
    "KAOS": ("repack", "KaOs Repack"), "CHOVKA": ("repack", "Chovka Repack"), "XATAB": ("repack", "xatab Repack"),
    "DARCK": ("repack", "Darck Repack"), "TINYREPACKS": ("repack", "TinyRepacks"),
}
EDITION_WORDS = (
    r"Deluxe|Ultimate|Gold|Complete|Definitive|Collector'?s|Premium|Enhanced|Special|Anniversary|Supporter'?s?|Legendary|"
    r"Digital|Royal|Platinum|Standard|Classic|Legacy|Extended|Expanded|Founder'?s|Season|Limited|Steelbook|Signature|Super|"
    r"Director'?s|Champion'?s|Commander|Imperial"
)
EDITION_RE = re.compile(
    rf"(?:\s+-\s+|\s+)(?P<ed>(?:The\s+)?(?:(?:(?:{EDITION_WORDS})\s+){{0,2}}(?:{EDITION_WORDS})\s+Edition|GOTY(?:\s+Edition)?|"
    rf"Game\s+of\s+the\s+Year(?:\s+Edition)?|Director'?s\s+Cut|Final\s+Cut|Complete))\s*$", re.I)
# any last " - ... Edition" segment: "MechWarrior 5 - Mercenaries - JumpShip Edition"
SEGMENT_EDITION_RE = re.compile(r"\s+-\s+(?P<ed>[\w'’&. ]{1,40}?\s(?:Edition|Cut|Collection))\s*$", re.I)
VERSION_TAIL =r"(?:[ ](?:v\d[\w.\-]*|hotfix\s*\d*|\d{4,}|[a-z]\b))*"
VERSION_RE = re.compile(rf"(?:^|\s)(?:v|ver\.?\s?|version\s)(?P<ver>\d[\w.\-]*?{VERSION_TAIL})\s*$", re.I)
WORD_VERSION_RE = re.compile(r"(?:^|\s)v(?P<ver>[A-Za-z]{2,12}[ ._]?\d[\w.]*)\s*$")  # "vDay 90" (GOG build names)
UPDATE_RANGE_RE = re.compile(r"(?:^|\s)v?(?P<from>\d[\w.\-]*)\s+to\s+v?(?P<to>\d[\w.\-]*)\s*$", re.I)
BUILD_RE = re.compile(r"(?:^|\s)Build\s?(?P<build>\d{3,})\b", re.I)
MULTI_RE = re.compile(r"(?:^|[\s\-\[(])MULTi\s?(?P<n>\d{1,2})?(?=$|[\s\-\])])", re.I)
# "HUN" only in capitals: "Attila the Hun" is a title, not a language tag
HUN_RE = re.compile(r"(?:^|[\s\-\[(])(HUN|(?i:hungarian|magyar|magyar[ií]t[aá]s|magyar[ií]tott))(?=$|[\s\-\])])")
CRACKFIX_RE = re.compile(r"\b(?:Crack\s?Fix|Fix\s?Crack|Hotfix\s?Crack|CrackFix)\b", re.I)
UPDATE_RE = re.compile(r"\b(?:Update|Patch)\b", re.I)
DLC_INCLUDED_RE = re.compile(r"\b(?:incl(?:uding)?|plus|with|all|and|\+)\s*(?:\d+\s+)?(?:DLCs?|Updates?)\b|\bDLCs\b|\d+\s+DLCs?\b", re.I)
DLC_RE = re.compile(r"\bDLC\b", re.I)
JUNK_RE = re.compile(
    r"(?:^|\s)(?:REPACK|PROPER|READ\s?NFO|x64|x86|64bit|32bit|DRM[\s\-]?Free|Win(?:dows)?(?:64)?|PC|ISO|"
    r"Linux|MacOS|Mac|Portable|Early\s+Access|Steam\s?Rip|GOG\s?Rip|Retail|Unlocked|Preinstalled)(?=$|\s)", re.I)
INCL_TAIL_RE = re.compile(r"\s+(?:incl(?:uding)?|plus|with)\s+.*$", re.I)


def _tidy(text: str) -> str:
    """Collapse spaces and drop dangling ' - ' separators left behind by removed tags."""

    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"(?:\s+-)+\s*$|^\s*(?:-\s+)+", "", text)
    return re.sub(r"\s+-\s+(?:-\s+)+", " - ", text).strip()


def _after_title(regex: re.Pattern, text: str) -> re.Match | None:
    """First match of ``regex`` that is not at the start of ``text``."""

    return next((m for m in regex.finditer(text) if m.start() > 0), None)


def _known_group(tag: str) -> bool:
    upper = tag.upper()
    return upper in SCENE_GROUPS or upper in P2P_GROUPS or upper in REPACKERS or upper == "GOG"


def _source_of_group(tag: str) -> str:
    upper = tag.upper()
    if upper in REPACKERS:
        return REPACKERS[upper][0]
    if upper == "GOG":
        return "gog"
    if upper in P2P_GROUPS:
        return "p2p"
    return "scene"


def _set_repacker(out: dict, tag: str) -> None:
    source, display = REPACKERS[tag.upper()]
    out["source"], out["repack"], out["group"] = source, display, out["group"] or tag


def _strip_trailing_blocks(text: str, out: dict) -> str:
    """Remove "[FitGirl Repack]", "(GOG)", "[MULTi8]", "(v1.2 + 3 DLCs)", "(2024)" blocks from the end."""

    while True:
        match = re.search(r"\s*[\[(](?P<inner>[^\[\]()]*)[\])]\s*$", text)
        if not match:
            return text
        inner = match["inner"].strip()
        word = re.sub(r"(?i)\s*repacks?$", "", inner).replace(" ", "").upper()
        if word in REPACKERS:
            _set_repacker(out, re.sub(r"(?i)\s*repacks?$", "", inner))
        elif word == "GOG":
            out["source"] = out["source"] or "gog"
        elif MULTI_RE.fullmatch(" " + inner) or re.fullmatch(r"(?i)MULTi\s?\d*", inner):
            number = re.search(r"\d+", inner)
            out["multi"] = int(number.group()) if number else out["multi"]
        elif re.fullmatch(r"(?:19|20)\d\d", inner):
            out["year"] = inner
        elif re.search(r"(?i)\bv\d|\bbuild\b|\bDLC|\bupdate|\bversion\b", inner):
            if DLC_RE.search(inner) or re.search(r"(?i)DLCs", inner):
                out["dlc_included"] = True
            version = re.search(r"(?i)(?:^|\s|\+)v(\d[\w.\-]*(?:\s+v\d+|\s+hotfix\s*\d*)?)", inner)
            build = re.search(r"(?i)build\s?(\d{3,})", inner)
            if version:
                out["version"] = version.group(1)
            if build:
                out["build"] = build.group(1)
            if MULTI_RE.search(inner):
                number = MULTI_RE.search(inner)["n"]
                out["multi"] = int(number) if number else out["multi"]
        elif HUN_RE.search(" " + inner):
            out["languages"].append("hu")
        else:
            return text  # a real part of the title, e.g. "Alone in the Dark (2024 remake)"
        text = text[: match.start()]


def parse(name: str) -> dict:
    """{"raw", "title", "subtitle", "edition", "version", "build", "group", "source", "kind", "dlc_included",
    "languages" (["hu"] when the name says HUN), "multi" (MULTi8 -> 8), "repack" (display name), "update_from",
    "year", "early_access", "platform" ("" = Windows)}"""

    out: dict = {"raw": name, "title": "", "subtitle": "", "edition": "", "version": "", "build": "", "group": "",
                 "source": "", "kind": "full", "dlc_included": False, "languages": [], "multi": 0, "repack": "",
                 "update_from": "", "year": "", "early_access": False, "platform": ""}
    text = (name or "").strip()
    text = re.sub(r"(?i)\.(?:exe|iso|rar|zip|7z|bin|nfo|torrent)$", "", text)
    if re.search(r"(?i)\bEarly[ ._]Access\b", text):
        out["early_access"] = True
    if re.search(r"(?i)(?:^|[ ._\-(\[])(Linux)(?:$|[ ._\-)\]])", text):
        out["platform"] = "linux"
    elif re.search(r"(?i)(?:^|[ ._\-(\[])(MacOS|OSX)(?:$|[ ._\-)\]])", text):
        out["platform"] = "mac"
    text = _strip_trailing_blocks(text, out)
    # " - FitGirl Repack", "-ElAmigos", " - DODI Repack"
    match = re.search(r"(?:\s*-\s*|\s+)(?P<tag>FitGirl|DODI|ElAmigos|KaOs|Chovka|xatab|Darck|TinyRepacks)(?:\s+Repacks?)?\s*$", text, re.I)
    if match:
        _set_repacker(out, match["tag"])
        text = text[: match.start()]
    # scene / P2P group suffix: "-RUNE", "-I_KnoW"
    match = re.search(r"-(?P<grp>[A-Za-z0-9_]{2,15})$", text)
    if match:
        head = text[: match.start()]
        dotted = ("." in head or "_" in head) and " " not in head.strip()
        shouting = match["grp"].isupper() and len(match["grp"]) >= 3 and " " not in head
        if dotted or _known_group(match["grp"]) or shouting:
            out["group"] = out["group"] or match["grp"]
            if match["grp"].upper() in REPACKERS:
                _set_repacker(out, match["grp"])
            elif not out["source"]:
                out["source"] = _source_of_group(match["grp"])
            text = head
    # trailing GOG marker: "Tank Squad v1.3.10 v4 GoG", "Game_GOG", "Game.GOG"
    match = re.search(r"[\s._\-]+GOG$", text, re.I)
    if match:
        out["source"] = "gog"
        text = text[: match.start()]
    text = _strip_trailing_blocks(text, out)
    # dotted / underscored scene names -> spaces (dots inside numbers stay)
    if " " not in text.strip() and ("." in text or "_" in text):
        text = re.sub(r"(?<!\d)\.|\.(?!\d)", " ", text).replace("_", " ")
    text = re.sub(r"\s+", " ", text).strip(" -")
    # language flags
    multi = MULTI_RE.search(text)
    if multi:
        out["multi"] = int(multi["n"]) if multi["n"] else out["multi"] or 0
        text = MULTI_RE.sub(" ", text)
    if HUN_RE.search(text):
        out["languages"].append("hu")
        text = HUN_RE.sub(" ", text)
    out["languages"] = sorted(set(out["languages"]))
    text = _tidy(text)
    # kind
    if CRACKFIX_RE.search(text):
        out["kind"] = "crackfix"
        text = CRACKFIX_RE.sub(" ", text)
    if DLC_INCLUDED_RE.search(text):
        out["dlc_included"] = True
    # a tag word that starts the name is part of the title: "PC Building Simulator", "Win That War!"
    text = _tidy(JUNK_RE.sub(lambda m: m.group() if m.start() == 0 else " ", INCL_TAIL_RE.sub("", text)))
    # build / version
    match = BUILD_RE.search(text)
    if match:
        out["build"] = out["build"] or match["build"]
        text = _tidy(text[: match.start()] + " " + text[match.end():])
    match = UPDATE_RANGE_RE.search(text)
    if match and UPDATE_RE.search(text[: match.start()]):
        out["update_from"], out["version"] = match["from"], match["to"]
        text = text[: match.start()]
    if not out["version"]:
        match = VERSION_RE.search(text) or WORD_VERSION_RE.search(text)
        if match:
            out["version"] = match["ver"].replace("_", " ").strip()
            text = text[: match.start()]
    text = _tidy(text)
    # "Update"/"Patch"/"DLC" after a title marks the kind; as the first word it is the title ("Patch Quest", "DLC Quest")
    match = _after_title(UPDATE_RE, text)
    if out["kind"] == "full" and match:
        out["kind"] = "update"
        text = _tidy(text[: match.start()] + " " + text[match.end():])
    match = _after_title(DLC_RE, text)
    if out["kind"] == "full" and not out["dlc_included"] and match:
        out["kind"] = "dlc"
        text = _tidy(text[: match.start()] + " " + text[match.end():])
    text = text.strip(" -:")
    # edition, then "Title - Subtitle"
    match = EDITION_RE.search(text) or SEGMENT_EDITION_RE.search(text)
    if match and match.start() > 0:
        out["edition"] = re.sub(r"\s+", " ", match["ed"]).strip()
        text = text[: match.start()].strip(" -:")
    parts = [p.strip() for p in re.split(r"\s+-\s+|:\s+", text) if p.strip()]
    if len(parts) > 1:
        out["title"], out["subtitle"] = parts[0], " - ".join(parts[1:])
    else:
        out["title"] = text
    return out


def full_title(parsed: dict, *, edition: bool = True) -> str:
    """'Title - Subtitle - Edition' as written in torrent names."""

    parts = [parsed.get("title", ""), parsed.get("subtitle", "")]
    if edition:
        parts.append(parsed.get("edition", ""))
    return " - ".join(p for p in parts if p)


def search_titles(parsed: dict) -> list[str]:
    """Store search candidates, most specific first: with edition, with subtitle, the bare title."""

    names = [full_title(parsed), full_title(parsed, edition=False), parsed.get("title", "")]
    if parsed.get("kind") == "dlc" and names[0]:
        names.insert(0, names[0] + " DLC")  # store DLC names usually end in "DLC"
    return [n for i, n in enumerate(names) if n and n not in names[:i]]


# --- GOG offline installers ---------------------------------------------------------------------------------

GOG_SETUP_RE = re.compile(
    r"^setup_(?P<rest>.+?)(?:_\((?P<lang>[a-z]{3,})\))?(?:_\((?P<arch>64|32)bit\))?(?:_\((?P<build>\d+)\))?(?:-(?P<part>\d+))?"
    r"\.(?P<ext>exe|bin)$", re.I)
GOG_PATCH_RE = re.compile(r"^patch_(?P<rest>.+?)_\((?P<from_id>\d+)\)_to_(?P<to>.+?)_\((?P<build>\d+)\)\.(?P<ext>exe|bin)$", re.I)
_VERSION_TOKEN = re.compile(r"^(?:\d+[.]\d|gog[-_]?\d+|v\d)", re.I)


def is_gog_installer(filename: str) -> bool:
    return bool(GOG_SETUP_RE.match(filename.rsplit("/", 1)[-1]))


def parse_gog_installer(filename: str, *, versions: list[str] | None = None, slug: str = "") -> dict:
    """setup_<slug>_<version>[_(<lang>)][_(64bit)]_(<build>)[-<part>].exe|bin -> {"slug", "version", "title",
    "language", "arch", "build", "part", "ext"}; {} when the name is not a GOG installer.

    ``build`` is GOG's installer build number, not the game version. The slug/version border is ambiguous
    ("cyberpunk_2077_2.13"), so known installer versions from the GOG API (``versions``) or the product
    ``slug`` are tried first, then a heuristic: the version starts at the first token after the first that looks
    like a version number.
    """

    match = GOG_SETUP_RE.match(filename.rsplit("/", 1)[-1])
    if not match:
        return {}
    rest = match["rest"]
    found_slug, version = "", ""
    flat = re.sub(r"[\s()_]+", "_", rest).strip("_").lower()
    for candidate in versions or []:
        tail = "_" + re.sub(r"[\s()_]+", "_", candidate).strip("_").lower()
        if len(tail) > 1 and flat.endswith(tail):
            found_slug, version = flat[: -len(tail)], candidate
            break
    if not version and slug and rest.lower().startswith(slug.lower() + "_"):
        found_slug, version = slug, rest[len(slug) + 1:].replace("_", " ")
    if not version:
        tokens = rest.split("_")
        for index, token in enumerate(tokens):
            if index > 0 and _VERSION_TOKEN.match(token):
                found_slug, version = "_".join(tokens[:index]), " ".join(tokens[index:])
                break
    found_slug = found_slug or rest
    return {
        "slug": found_slug, "version": version, "title": slug_title(found_slug), "language": (match["lang"] or "").lower(),
        "arch": match["arch"] or "", "build": match["build"] or "", "part": match["part"] or "", "ext": match["ext"].lower(),
    }


def slug_title(slug: str) -> str:
    """'tank_squad' -> 'Tank Squad' (search text only; the store title is authoritative)."""

    small = {"of", "the", "and", "a", "an", "in", "on", "to", "for"}
    words = [w for w in re.split(r"[_\-\s]+", slug or "") if w]
    roman = re.compile(r"^(?:i{1,3}|iv|v|vi{1,3}|ix|x)$", re.I)
    out = []
    for index, word in enumerate(words):
        if roman.match(word) and index > 0:
            out.append(word.upper())
        elif index > 0 and word.lower() in small:
            out.append(word.lower())
        else:
            out.append(word[:1].upper() + word[1:])
    return " ".join(out)
