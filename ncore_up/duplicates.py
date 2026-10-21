"""Conservative semantic dupe decisions. No candidate may silently become an approval.

Book Text and Reader families may coexist (wiki 374). Two Reader formats are NOT
automatically distinct. Video/source/language upgrades and editions require review
unless a persisted candidate-specific user decision resolves the uncertainty.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from . import parse, categories


@dataclass
class Decision:
    torrent_id: str
    status: str  # ALLOW, BLOCK, REVIEW
    reason: str
    name: str

    def as_dict(self):
        return asdict(self)


def book_family(value: str) -> str:
    tokens = set(re.findall(r"[a-z0-9]+", str(value).lower()))
    if tokens & {"scan", "scanned", "szkennelt"}:
        return "scan"
    if tokens & {"epub", "mobi", "prc", "azw", "azw3", "fb2", "lit"}:
        return "reader"
    if tokens & {"ocr", "doc", "docx", "rtf", "txt"}:
        return "text"
    return ""  # a bare PDF/DJVU says nothing about its text layer


def _series_span(name: str):
    matches = list(parse.SXE_RE.finditer(name))
    if len(matches) != 1:
        return None
    match = matches[0]
    # Multi-season ranges and additional episode suffixes are ambiguous here.
    if re.match(r'(?i)^(?:[a-z0-9]|[ ._-]*(?:E\d|S\d|[-]\d{1,3}(?!\d)))', name[match.end():]):
        return None
    parsed = parse.parse(name)
    if parsed.season is None:
        return None
    episodes = None if parsed.episode is None else (parsed.episode, parsed.episode_last or parsed.episode)
    if episodes and episodes[1] < episodes[0]:
        return None
    return parsed.season, episodes


def _disjoint_episodes(plan, name: str, code: str) -> bool:
    if plan.category.group != 'video' or 'ser' not in plan.category.code:
        return False
    if code not in categories.CATEGORIES or 'ser' not in code:
        return False
    own, other = _series_span(plan.release.name), _series_span(name)
    if not own or not other:
        return False
    if own[0] != other[0]:
        return True
    if own[1] is None or other[1] is None:
        return False  # a season pack overlaps any episode in its season
    return own[1][1] < other[1][0] or other[1][1] < own[1][0]


DISC_LABEL_RE = re.compile(r"(?i)(?<![a-z0-9])(BD(?:25|50|66|100)|UHD[ ._-]?BD)(?![a-z0-9])")
DISC_NAME_RE = re.compile(r"(?i)(?<![a-z0-9])(?:COMPLETE[ ._-]+(?:UHD[ ._-]+)?BLU-?RAY|BDMV|AVC[ ._-]+REMUX[ ._-]+DISC|DISC\d)(?![a-z0-9])")


def _disc_label(name: str) -> str:
    found = DISC_LABEL_RE.search(name)
    return re.sub(r"[ ._-]", "", found[1]).upper() if found else ""


def _disc_class(plan, name: str, code: str):
    """Wiki 374 keeps full Blu-ray discs in their own slot ("BD25 / BD50 maximum 1 darab"): an encode of any
    resolution never takes it, and a disc of another size neither. None when this rule does not decide."""

    if plan.category.group != "video" or not plan.release.has_bdmv or plan.data.original:
        return None
    own = _disc_label(plan.data.name)
    if not own or code.startswith("dvd"):
        return None
    other = _disc_label(name)
    if not other and not DISC_NAME_RE.search(name):
        return "ALLOW", f"eltérő dupe-osztály: teljes BluRay lemez ({own}) – a fent lévő encode nem lemez (wiki 374)"
    if other and other != own:
        return "ALLOW", f"eltérő lemezosztály: {own} ↔ {other} (wiki 374: lemezméretenként max. 1)"
    return "REVIEW", f"ugyanilyen lemez már fent van ({other or 'teljes BluRay'}; wiki 374: max. 1, HUN előnyben)"


def decide(plan, candidates: list[dict], answers: dict | None = None) -> list[Decision]:
    answers = answers or {}
    result = []
    seen = set()
    local_name = plan.data.name.strip().casefold()
    for item in candidates:
        tid = str(item.get("torrent_id", ""))
        if tid in seen:
            continue
        seen.add(tid)
        name = str(item.get("release_name", ""))
        code = str(item.get("category", ""))
        status, reason = "REVIEW", "műazonosság/nyelv/minőség/kiadás/verzió/platform összevetése szükséges"
        if plan.data.original and name.casefold() == plan.release.name.casefold() and code == plan.category.code:
            status, reason = "BLOCK", "ugyanaz az eredeti release ugyanabban a kategóriában"
        elif _disjoint_episodes(plan, name, code):
            status, reason = "ALLOW", "eltérő évad/epizód: a nevek alapján nincs közös epizód"
        elif _disc_class(plan, name, code):
            status, reason = _disc_class(plan, name, code)
        elif plan.category.group == "ebook" and code == plan.category.code:
            own = book_family(plan.data.book_format)
            other = book_family(item.get("format", "") or item.get("type", ""))
            if {own, other} == {"text", "reader"}:
                status, reason = "ALLOW", "eltérő Text / Reader könyvváltozat (wiki 374)"
            elif own and other and own == other and name.casefold() == local_name:
                status, reason = "REVIEW", "azonos cím és könyvformátum-család; kiadáskülönbség ellenőrzése szükséges"
        decision = answers.get(tid)
        if status != "BLOCK" and isinstance(decision, dict) and len(str(decision.get("reason", "")).strip()) >= 12:
            if decision.get("action") == 'block' or (status == 'REVIEW' and decision.get("action") == 'allow'):
                status, reason = decision["action"].upper(), "felhasználói döntés: " + str(decision["reason"])
        result.append(Decision(tid, status, reason, name))
    return result
