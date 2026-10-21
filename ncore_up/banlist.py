"""Versioned mirror of nCore Wiki 41 and mandatory upload checks.

The server refreshes this weekly through --refresh-banlist. Uploads re-read the
mirror; a stale, unavailable or unrecognised rule never permits an upload.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
from pathlib import Path

from bs4 import BeautifulSoup

from . import workflow
from .client import NcoreError
from .local import nfo
from .requirements import Requirement

URL = "https://ncore.pro/wiki.php?action=read&id=41"
MAX_AGE = 9 * 24 * 3600  # weekly refresh + slack for a missed or late run (the timer is Persistent)
SECTIONS = {
    "Tiltott játékok:": "game", "Tiltott programok:": "program",
    "Tiltott zenék/zenekarok/előadók/albumok:": "music", "Tiltott könyvek:": "ebook",
    "Tiltott filmek/sorozatok/klipek/videók:": "video", "Egyéb:": "all", "Tiltott release-ek:": "release",
}


def norm(value: str) -> str:
    value = unicodedata.normalize("NFKD", value.casefold())
    return " ".join(re.sub(r"[^a-z0-9]+", " ", "".join(c for c in value if not unicodedata.combining(c))).split())


# Only these exact semantic rules have custom interpretation. New prose rules
# need review; simple names and release-group additions are data-only updates.
SPECIAL = {
    "Hypervisor alapú crackelt játékok": "hypervisor",
    "Nem hivatalos portable anyagok": "portable",
    "Mozis hangú ( LiNE ) R5, DVDRip és BDRip feltöltése tilos": "line_audio",
    "Tilos feltölteni: CAM, TS, TC, Workprint, DVDSCR, PPVRip, WEBSCR, BRRip, HDTVRip, HDRip, továbbá minden kétszeresen tömörített (reencode), illetve DCP (Digital Cinema Package), és minden pre-retail anyagot.": "video_sources",
    "mHD anyagok": "mhd",
    "XviD kódolású anyagok feltöltése kizárólag előzetes engedély kérése után lehetséges.": "xvid_permission",
    "XXX kategóriában a CuckoldSessions , BiGuysFuck , BruceAndMorgan , HuCows , PerverseFamily sorozat.": "xxx",
}
SPECIAL = {norm(key): value for key, value in SPECIAL.items()}


def _rule(section: str, text: str) -> dict:
    key = norm(text)
    base = {"section": section, "text": text}
    if key in SPECIAL:
        return base | {"kind": SPECIAL[key]}
    if section == "release":
        names = re.findall(r'"-([A-Za-z0-9.]+)"', text)
        if not names or not re.match(r'^összes\s+"-', text, re.I):
            return base | {"kind": "unknown"}
        if key == norm('összes "-NoGrp", release - Ott a "Nem eredeti release" opció, tessék úgy feltölteni az adott anyagot.'):
            return base | {"kind": "original_group", "names": names}
        if key == norm('összes "-No1" release, aminek az NFO-jában BitHUmen reklám van (Nem eredeti release-ként feltölthető.)'):
            return base | {"kind": "no1", "names": names}
        if re.fullmatch(r'összes\s+"-[A-Za-z0-9.]+"\s+release(?:\s*\("-[A-Za-z0-9.]+"(?:,\s*"-[A-Za-z0-9.]+")*\))?', text, re.I):
            return base | {"kind": "group", "names": names}
        return base | {"kind": "unknown"}
    if section == "ebook" and key.endswith(" osszes muve"):
        return base | {"kind": "name", "names": [re.sub(r"\s+összes műve\s*$", "", text, flags=re.I)]}
    if re.search(r"\b(?:tilos|kiveve|kizarolag|engedely|feltolt\w*|amely\w*|eseten|csak|minden|osszes|alapu|hivatalos)\b", key):
        return base | {"kind": "unknown"}
    if len(key) < 3 or re.search(r"[;:!?]", text):
        return base | {"kind": "unknown"}
    names = [part.strip() for part in re.split(r"\s+[|–]\s+|\s+\|\s+", text)]
    return base | {"kind": "name", "names": names}


def parse_page(body: str) -> dict:
    soup = BeautifulSoup(body, "html.parser")
    main = soup.select_one("#main_tartalom") or soup.select_one("#main")
    if main is None:
        raise NcoreError("a tiltólista Wiki-oldalának szerkezete nem felismerhető")
    for node in main.select("script,style"):
        node.decompose()
    text = main.get_text("\n", strip=True)
    version = re.search(r"Verzió:\s*(v\d+)", text)
    if "Tiltott anyagok" not in text or not version:
        raise NcoreError("a tiltólista Wiki-oldala hiányos vagy megváltozott")
    lines = text.splitlines()
    entries, seen = [], set()
    section, pending = "", ""

    def finish():
        nonlocal pending
        if pending:
            entries.append(_rule(section, re.sub(r"\s+", " ", pending).strip()))
            pending = ""

    for line in lines:
        line = line.strip()
        if line in SECTIONS:
            finish()
            if line in seen:
                raise NcoreError("a tiltólista ismételt fejezetet tartalmaz")
            seen.add(line)
            section = SECTIONS[line]
        elif section and line == "Keresés":
            finish()
            break
        elif section:
            if line in {"(Új)", "(új)"} or not line:
                continue
            if line.endswith(":") and not line.startswith(("- ", "• ")):
                raise NcoreError("a tiltólista új, nem ismert fejezetet tartalmaz")
            if line.startswith(("- ", "• ")):
                finish()
                pending = line[2:]
            elif pending:
                pending += " " + line
            else:
                raise NcoreError("a tiltólista új, nem feldolgozható bejegyzést tartalmaz")
    finish()
    if seen != set(SECTIONS) or not all(any(r["section"] == sec for r in entries) for sec in SECTIONS.values()):
        raise NcoreError("a tiltólistából kötelező fejezet hiányzik")
    return {"schema": 1, "source": URL, "version": version[1], "checked_at": time.time(),
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "rules": entries,
            "unknown_rules": [r for r in entries if r["kind"] == "unknown"]}


def refresh(client, work_dir: Path) -> dict:
    folder = work_dir / "banlist"
    try:
        snapshot = parse_page(client.banlist_page())
        previous = {}
        try:
            previous = json.loads((folder / "current.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            pass
        old = {r["text"] for r in previous.get("rules", [])}
        new = {r["text"] for r in snapshot["rules"]}
        snapshot["changes"] = {"added": sorted(new - old), "removed": sorted(old - new)}
        history = folder / "history" / (snapshot["sha256"] + ".json")
        if not history.exists():
            workflow.write_json(history, snapshot)
        workflow.write_json(folder / "current.json", snapshot)
        workflow.write_json(folder / "status.json", {"ok": not snapshot["unknown_rules"], "checked_at": snapshot["checked_at"],
                                                   "version": snapshot["version"], "sha256": snapshot["sha256"],
                                                   "unknown_count": len(snapshot["unknown_rules"])})
        return snapshot
    except (NcoreError, OSError, ValueError) as exc:
        workflow.write_json(folder / "status.json", {"ok": False, "checked_at": time.time(), "error": type(exc).__name__})
        raise NcoreError("a tiltólista frissítése sikertelen; éles feltöltés leállítva") from None


def _contains(text: str, term: str) -> bool:
    return bool(term and (" " + term + " ") in (" " + norm(text) + " "))


def load(work_dir: Path) -> dict:
    """Re-read the script's latest snapshot and refresh status, with no implicit network refresh."""
    try:
        folder = work_dir / "banlist"
        snapshot = json.loads((folder / "current.json").read_text(encoding="utf-8"))
        status = json.loads((folder / "status.json").read_text(encoding="utf-8"))
        if not isinstance(snapshot, dict) or not isinstance(status, dict):
            raise ValueError("invalid mirror schema")
        age = time.time() - float(snapshot["checked_at"])
        valid = (snapshot.get("schema") == 1 and snapshot.get("source") == URL and 0 <= age <= MAX_AGE
                 and isinstance(snapshot.get("rules"), list) and bool(snapshot["rules"])
                 and re.fullmatch(r"[0-9a-f]{64}", snapshot.get("sha256", ""))
                 and all(isinstance(r, dict) and r.get("section") in SECTIONS.values() and r.get("kind") for r in snapshot["rules"])
                 and status.get("sha256") == snapshot.get("sha256") and "error" not in status)
        if not valid:
            raise ValueError("invalid mirror")
        return snapshot
    except (OSError, ValueError, KeyError, TypeError):
        raise NcoreError("a tiltólista hiányzik, hibás vagy hét napnál régebbi; futtasd a frissítőscriptet") from None


def evaluate(snapshot: dict, plan) -> list[Requirement]:
    checks = []
    age = time.time() - snapshot.get("checked_at", 0)
    if not 0 <= age <= MAX_AGE or snapshot.get("schema") != 1:
        return [Requirement("banlist", "UNKNOWN", "a tiltólista hiányzik vagy hét napnál régebbi", URL)]
    group = plan.category.group
    section = {"clip": "video", "console": "game", "mobil": "game"}.get(group, group)
    nfo_data = nfo.read_release(plan.release)
    meta, data = plan.meta, plan.data
    file_names = [plan.release.name] + [str(Path(f.relpath).with_suffix("")) for f in plan.release.files]
    source_release = getattr(plan, "source_release", None)
    if source_release is not None:
        file_names += [source_release.name] + [str(Path(f.relpath).with_suffix("")) for f in source_release.files]
    release_names = file_names + [part for value in file_names for part in Path(value).parts] + [nfo_data.get("release", "")]
    nfo_groups = [nfo_data.get("group", "")] + [r.get("data", {}).get("group", "") for r in nfo_data.get("records", [])]
    identities = [data.name, data.artist, data.album, data.book_title, *file_names]
    if meta:
        identities += [meta.title, meta.title_hu, meta.title_original, *meta.creators]
    if data.infobar:
        identities += [data.infobar.title_hu, data.infobar.title_en, data.infobar.title_other]
    identities += [nfo_data.get("fields", {}).get(k, "") for k in ("title", "artist", "album", "author")]
    source = plan.options.source if plan.options else ""
    supplied_sources = [source or ""] + re.findall(r"(?im)^\s*Forrás\s*:\s*(.+)", getattr(data, "description", ""))
    release_names += supplied_sources
    identities += supplied_sources
    provenance = "\n".join([*release_names, source or ""])
    nfo_text = "\n".join(r.get("text", "") for r in nfo_data.get("records", []))

    def group_hit(names):
        return any(any(re.search(r"-" + re.escape(name) + r"$", value, re.I) for value in release_names)
                   or any(norm(value) == norm(name) for value in nfo_groups) for name in names)

    for rule in snapshot.get("rules", []):
        if rule["section"] not in {section, "all", "release"}:
            continue
        kind, hit, unknown = rule["kind"], False, False
        if kind == "unknown":
            unknown = True
        elif kind == "name":
            for name in rule["names"]:
                # An album ban names both the artist and the album. Aliases separated by | are alternatives.
                terms = name.split(" - ") if section == "music" else [name]
                hit |= all(any(_contains(value, norm(term)) for value in identities) for term in terms)
        elif kind in {"group", "original_group", "no1"}:
            found = group_hit(rule["names"])
            if kind == "group":
                hit = found
            elif kind == "original_group":
                hit = found and data.original
            elif found and data.original:
                hit = "bithumen" in norm(nfo_text)
                unknown = bool(nfo_data.get("warnings"))
        elif kind == "hypervisor":
            text = norm(provenance + "\n" + nfo_text)
            hit = bool(re.search(r"hypervisor (?:alapu |based )?(?:crack|bypass)|(?:crack|bypass) (?:\w+ ){0,4}hypervisor", text))
            unknown = not hit and _contains(text, "hypervisor")
        elif kind == "portable":
            unknown = _contains(provenance, "portable") or _contains(nfo_text, "portable")
        elif kind == "xvid_permission":
            codec = getattr(plan.video_info, "codec", "")
            unknown = section == "video" and (codec in {"xvid", "mpeg4"} or _contains(provenance, "xvid"))
        elif kind in {"video_sources", "line_audio", "mhd"}:
            if section != "video":
                continue
            # Use release/source technical suffixes, not the film's synopsis or a CAM title alone.
            technical = []
            for value in release_names + [source or ""]:
                # Standard disc structure names (VIDEO_TS, VTS_01_1.VOB, BDMV ...) are not quality markers ("TS").
                value = re.sub(r"(?i)\b(?:VIDEO_TS|AUDIO_TS|BDMV|CERTIFICATE)\b|\bVTS_\d+_\d+\b|\bVIDEO_TS\.(?:IFO|BUP|VOB)\b", " ", value)
                match = re.search(r"(?i)(?:19|20)\d{2}|(?:480|576|720|1080|2160)[pi]", value)
                if match:
                    technical.append(value[match.end():])
                else:
                    # A quality marker without year/resolution must not evade the ban.
                    # Strip a known title prefix first (the film "Cam" is not CAM capture).
                    cleaned = norm(value)
                    if meta:
                        for title in (meta.title, meta.title_hu, meta.title_original):
                            prefix = norm(title)
                            if prefix and (cleaned == prefix or cleaned.startswith(prefix + " ")):
                                cleaned = cleaned[len(prefix):].strip()
                                break
                    technical.append(cleaned)
            text = " ".join(technical)
            if kind == "video_sources":
                hit = any(_contains(text, marker) for marker in ("cam", "hdcam", "ts", "hdts", "tc", "hdtc", "workprint", "dvdscr", "ppvrip", "webscr", "brrip", "hdtvrip", "hdrip", "reencode", "dcp", "pre retail"))
            elif kind == "mhd":
                hit = _contains(text, "mhd")
            else:
                hit = _contains(text, "line") and any(_contains(text, marker) for marker in ("r5", "dvdrip", "bdrip"))
        if hit or unknown:
            checks.append(Requirement("banlist", "FAIL" if hit else "UNKNOWN",
                                      ("Tiltott anyag: " if hit else "Tiltólista: ellenőrzés szükséges: ") + rule["text"], URL))
    if not checks:
        checks.append(Requirement("banlist", "PASS", "friss Wiki-tiltólista: nincs felismert találat", snapshot["version"] + " " + snapshot["sha256"]))
    return checks


def check(plan, client, work_dir: Path, run_dir: Path, token: str, *, phase: str) -> list[Requirement]:
    """Re-read current rules; check originals and generated identities in the live hint too."""
    if getattr(client, "offline", False):
        checks = [Requirement("banlist", "UNKNOWN", "offline módban a tiltólista nem ellenőrizhető", URL)]
        snapshot = None
    else:
        snapshot = load(work_dir)
        checks = evaluate(snapshot, plan)
        names = list(dict.fromkeys([plan.data.name, plan.release.name]))
        if plan.meta:
            names += [title for title in (plan.meta.title, plan.meta.title_hu, plan.meta.title_original) if title and title not in names]
        from .form import build
        fields = build(token, plan.data)
        for name in names:
            notice = client.banned_notice(token, plan.data.category, name, original=plan.data.original, fields=fields)
            if notice:
                checks.append(Requirement("banlist", "FAIL", "tiltott anyag találat az nCore szerint: " + notice, URL))
    workflow.write_json(run_dir / ("banlist-" + phase + ".json"), {"phase": phase, "checked_at": time.time(),
                        "snapshot_version": snapshot["version"] if snapshot else None,
                        "snapshot_sha256": snapshot["sha256"] if snapshot else None, "checks": [c.as_dict() for c in checks]})
    return checks
