"""What a PC game release consists of: disc image, GOG offline installer, repack, crack folder, update, NFO.

Everything here is static inspection. Files of a release are never executed: disc images and archives are only
listed (``7z l``), GOG installers are read by ``innoextract --gog-game-id/--info/--list-languages`` and their PE
version resource (``pefile`` or ``exiftool``), NFOs are decoded as CP437. Missing tools or packages just leave
the corresponding fields empty.

``classify()`` works on a plain file list and is what the tests exercise; ``inspect()`` adds the tool-based
reads for a ``release.Release`` on disk.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .. import safeio
from typing import Iterable

from . import gamename, nfo

DISC_EXT = {".iso", ".mdf", ".mds", ".img", ".nrg", ".ccd", ".cue", ".isz", ".chd"}
ARCHIVE_RE = re.compile(r"(?i)\.(rar|zip|7z|r\d{2}|s\d{2}|\d{3})$")
FIRST_VOLUME_RE = re.compile(r"(?i)(?:\.part0*1\.rar|(?<!\.part\d)(?<!\.part\d\d)(?<!\.part\d\d\d)\.rar|\.zip|\.7z(?:\.001)?|\.001)$")
CRACK_DIRS = {
    "crack", "cracked", "crackfix", "codex", "rune", "tenoke", "plaza", "empress", "flt", "skidrow", "cpy", "hoodlum",
    "darksiders", "tinyiso", "doge", "razor1911", "prophet", "simplex", "reloaded", "nodvd", "no-dvd", "nocd", "no-cd",
    "steampunks", "goldberg", "dinobytes", "i_know", "fckdrm", "chronos", "suxxors", "ali213", "vace", "anomaly",
    "activated", "p2p", "kaos", "hi2u", "razor", "postmortem", "unleashed", "defa",
}
REDIST_DIRS = {"_commonredist", "commonredist", "redist", "_redist", "redists", "redistributables", "directx", "vcredist", "_redistributables"}
SETUP_NAMES = {"setup.exe", "install.exe", "installer.exe", "autorun.exe", "setup64.exe"}
FG_BIN_RE = re.compile(r"(?i)^(?:fg-\d+|setup-fitgirl(?:-\d+)?|fg-optional-.+|fg-selective-.+)\.bin$")
VERIFY_RE = re.compile(r"(?i)^(?:verify bin files before installation\.bat|quicksfv\.(?:exe|ini)|fitgirl-bins\.md5|.*\.md5)$")
TOOLS_TIMEOUT = 180


@dataclass
class GameFiles:
    layout: str = "unknown"  # gog, fitgirl, dodi, elamigos, disc_image, packed, installer, portable, unknown
    disc_images: list[str] = field(default_factory=list)
    setup: str = ""  # the main installer (setup.exe, setup_<slug>_....exe)
    gog_installers: list[str] = field(default_factory=list)  # main GOG setup_*.exe first, then DLC/extra installers
    gog_parts: list[str] = field(default_factory=list)  # setup_*-N.bin
    gog_patches: list[str] = field(default_factory=list)
    crack_dirs: list[str] = field(default_factory=list)
    repack_markers: list[str] = field(default_factory=list)
    verify: list[str] = field(default_factory=list)  # "Verify BIN files before installation.bat", MD5/QuickSFV
    optional: list[str] = field(default_factory=list)  # fg-optional-*.bin
    selective: list[str] = field(default_factory=list)  # fg-selective-*.bin (language packs)
    redist_dirs: list[str] = field(default_factory=list)
    dlc: list[str] = field(default_factory=list)
    updates: list[str] = field(default_factory=list)
    archives: list[str] = field(default_factory=list)  # first volumes of packed sets
    nfo_files: list[str] = field(default_factory=list)
    executables: list[str] = field(default_factory=list)
    inner: list[str] = field(default_factory=list)  # file list inside the disc image / archive (when read)
    nfo: dict = field(default_factory=dict)  # parse_nfo()
    gog: dict = field(default_factory=dict)  # gog_installer_info()
    warnings: list[str] = field(default_factory=list)

    @property
    def gog_parsed(self) -> dict:
        return gamename.parse_gog_installer(self.setup) if self.layout == "gog" and self.setup else {}

    @property
    def facts(self) -> list[str]:
        """Hungarian one-line facts for the description/log ("Lemezkép: x.iso", "Crack mappa: TENOKE", ...)."""

        out = []
        if self.layout == "gog":
            parts = f" + {len(self.gog_parts)} .bin rész" if self.gog_parts else ""
            out.append(f"GOG offline telepítő: {_base(self.setup)}{parts}")
            if len(self.gog_installers) > 1:
                out.append("további GOG telepítők (DLC/extra): " + ", ".join(_base(x) for x in self.gog_installers[1:6]))
        elif self.layout in {"fitgirl", "dodi", "elamigos"}:
            label = {"fitgirl": "FitGirl", "dodi": "DODI", "elamigos": "ElAmigos"}[self.layout]
            out.append(f"{label} repack: {_base(self.setup) or 'setup.exe'}" + (f" + {len(self.repack_markers)} .bin" if self.repack_markers else ""))
        if self.disc_images:
            out.append("Lemezkép: " + ", ".join(_base(x) for x in self.disc_images[:4]))
        if self.archives:
            out.append("Tömörített (RAR/ZIP) release: " + ", ".join(_base(x) for x in self.archives[:3]))
        if self.setup and self.layout not in {"gog", "fitgirl", "dodi", "elamigos"}:
            out.append(f"Telepítő: {self.setup}")
        if self.crack_dirs:
            out.append("Crack mappa: " + ", ".join(self.crack_dirs))
        if self.optional:
            out.append("Opcionális részek: " + ", ".join(_base(x) for x in self.optional))
        if self.selective:
            out.append("Választható nyelvi csomagok: " + ", ".join(_base(x) for x in self.selective))
        if self.verify:
            out.append("Ellenőrzés: " + ", ".join(_base(x) for x in self.verify[:3]))
        if self.redist_dirs:
            out.append("Redist mappa: " + ", ".join(self.redist_dirs))
        if self.dlc:
            out.append("DLC: " + ", ".join(_base(x) for x in self.dlc[:6]))
        if self.updates:
            out.append("Frissítés/patch: " + ", ".join(_base(x) for x in self.updates[:4]))
        if self.gog.get("gog_id"):
            out.append(f"GOG azonosító (innoextract): {self.gog['gog_id']}")
        if self.nfo.get("install"):
            out.append(f"NFO telepítési lépések: {len(self.nfo['install'])}")
        return out


def _base(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _parts(path: str) -> list[str]:
    return [p for p in path.replace("\\", "/").split("/") if p]


def classify(files: Iterable[str | tuple[str, int]]) -> GameFiles:
    """Classify a file list (relative paths, optionally with sizes) of a release or of a disc image's content."""

    entries: list[tuple[str, int]] = []
    for item in files:
        path, size = (item, 0) if isinstance(item, str) else (item[0], int(item[1] or 0))
        entries.append(("/".join(_parts(path)), size))
    info = GameFiles()
    sizes = dict(entries)
    cue_stems = {p.rsplit(".", 1)[0].lower() for p, _ in entries if p.lower().endswith(".cue")}
    seen_dirs: dict[str, str] = {}
    redist: dict[str, str] = {}
    for path, _size in entries:
        parts = path.split("/")
        name, lower = parts[-1], parts[-1].lower()
        ext = ("." + lower.rsplit(".", 1)[-1]) if "." in lower else ""
        for directory in parts[:-1]:
            if directory.lower() in CRACK_DIRS:
                seen_dirs.setdefault(directory.lower(), directory)
        top_redist = next((d for d in parts[:-1] if d.lower() in REDIST_DIRS), "")
        if top_redist:
            redist.setdefault(top_redist.lower(), top_redist)
        if ("dodi" in lower or "elamigos" in lower) and ext != ".nfo":
            info.repack_markers.append(path)
        if ext == ".nfo":
            info.nfo_files.append(path)
        elif gamename.GOG_PATCH_RE.match(name):
            info.gog_patches.append(path)
        elif gamename.is_gog_installer(name):
            (info.gog_installers if ext == ".exe" else info.gog_parts).append(path)
        elif FG_BIN_RE.match(name):
            info.repack_markers.append(path)
            if lower.startswith("fg-optional-"):
                info.optional.append(path)
            elif lower.startswith("fg-selective-"):
                info.selective.append(path)
        elif VERIFY_RE.match(name) or (len(parts) > 1 and parts[-2].lower() == "md5"):
            info.verify.append(path)
        elif ext in DISC_EXT:
            info.disc_images.append(path)
        elif ext == ".bin" and (path.rsplit(".", 1)[0].lower() in cue_stems or len(cue_stems) == 1):
            info.disc_images.append(path)
        elif ARCHIVE_RE.search(name):
            if FIRST_VOLUME_RE.search(name):
                info.archives.append(path)
        elif ext == ".exe":
            if lower in SETUP_NAMES:
                info.executables.insert(0, path)
            else:
                info.executables.append(path)
        lowered_dirs = [d.lower() for d in parts[:-1]]
        gog_file = gamename.is_gog_installer(name) or bool(gamename.GOG_PATCH_RE.match(name))
        if ext in {".exe", ".bin", ".zip", ".rar", ".7z"} and not (gog_file and ext == ".bin") and (
                {"dlc", "dlcs"} & set(lowered_dirs) or re.search(r"(?i)(?:^|[_ .\-])dlc(?:$|[_ .\-])", name.rsplit(".", 1)[0])):
            info.dlc.append(path)
        elif ext == ".exe" and not gog_file and ({"update", "updates", "patch", "patches"} & set(lowered_dirs)
                                                 or re.search(r"(?i)(?:^|[_ .\-])(?:update|patch)(?:$|[_ .\-\d])", name)):
            info.updates.append(path)
    info.crack_dirs = sorted(seen_dirs.values(), key=str.lower)
    info.redist_dirs = sorted(redist.values(), key=str.lower)
    info.updates.extend(p for p in info.gog_patches if p not in info.updates)
    # the main GOG installer: most .bin parts, then the shortest name; the rest are DLC/extra installers
    if info.gog_installers:
        def parts_of(exe: str) -> int:
            stem = exe.rsplit(".", 1)[0]
            return sum(1 for part in info.gog_parts if part.startswith(stem + "-"))

        info.gog_installers.sort(key=lambda exe: (-parts_of(exe), len(exe)))
        info.setup = info.gog_installers[0]
        for extra in info.gog_installers[1:]:
            if extra not in info.dlc and re.search(r"(?i)dlc|soundtrack|bonus|artbook|expansion", extra):
                info.dlc.append(extra)
        info.layout = "gog"
    else:
        setups = sorted((p for p in info.executables if _base(p).lower() in SETUP_NAMES), key=lambda p: (p.count("/"), len(p)))
        info.setup = setups[0] if setups else ""
        markers = " ".join(_base(p).lower() for p in info.repack_markers)
        if any(FG_BIN_RE.match(_base(p)) for p in info.repack_markers):
            info.layout = "fitgirl"
        elif "dodi" in markers:
            info.layout = "dodi"
        elif "elamigos" in markers:
            info.layout = "elamigos"
        elif info.disc_images:
            info.layout = "disc_image"
            info.disc_images.sort(key=lambda p: -sizes.get(p, 0))
        elif info.archives:
            info.layout = "packed"
        elif info.setup:
            info.layout = "installer"
        elif info.executables:
            info.layout = "portable"
    if info.layout in {"fitgirl", "dodi", "elamigos"} and not info.setup and not info.archives:
        info.warnings.append("repack: nem találom a setup.exe-t")
    return info


# --- external tools (static listing / reading only) ---------------------------------------------------------

def _run(args: list[str], timeout: int = TOOLS_TIMEOUT) -> tuple[int, str, str]:
    """Run a local inspection tool (never a file of the release); (-1, '', error) when it cannot run."""

    if not shutil.which(args[0]):
        return -1, "", f"{args[0]}: nincs telepítve"
    try:
        run = safeio.run(args, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return -1, "", f"{args[0]}: {type(exc).__name__}"
    return run.returncode, run.stdout.decode("utf-8", "replace"), run.stderr.decode("utf-8", "replace")


def parse_7z_listing(text: str) -> list[tuple[str, int]]:
    """``7z l -slt`` output -> [(path, size)] of the files (folders skipped)."""

    out: list[tuple[str, int]] = []
    body = text.split("\n----------\n", 1)
    if len(body) < 2:
        return out
    for block in re.split(r"\n\s*\n", body[1]):
        fields = dict(re.findall(r"(?m)^(\w[\w ]*?) = (.*)$", block))
        path = fields.get("Path", "").strip()
        if not path or fields.get("Folder") == "+" or fields.get("Attributes", "").startswith("D"):
            continue
        out.append((path.replace("\\", "/"), int(fields.get("Size") or 0) if (fields.get("Size") or "0").isdigit() else 0))
    return out


def list_archive(path: Path) -> list[tuple[str, int]]:
    """Files inside a disc image or archive (first volume), by ``7z l``; [] when unreadable or encrypted."""

    code, out, _err = _run(["7z", "l", "-slt", "-bd", "-p-", "--", str(path)])
    if code != 0 or not out:
        return []
    return parse_7z_listing(out.replace("\r\n", "\n"))


def parse_innoextract_gog_id(text: str) -> str:
    """'GOG.com game ID is 1207658787' (or a bare id with --silent) -> '1207658787'."""

    match = re.search(r"GOG\.com game ID is\s+(\d{5,})", text) or re.fullmatch(r"\s*(\d{5,})\s*", text or "")
    return match.group(1) if match else ""


def parse_innoextract_info(text: str) -> dict:
    """innoextract --info/--list header -> {"app_name", "inno_version", "gog_id" (from goggame-<id>.info), ...}."""

    out: dict[str, str] = {}
    match = re.search(r'(?m)^(?:Inspecting|Listing|Extracting|Testing)\s+"(?P<name>.+?)"\s+-\s+setup data version\s+(?P<ver>[\d.]+)', text or "")
    if match:
        out["app_name"], out["inno_version"] = match["name"], match["ver"]
    for key, value in re.findall(r"(?m)^\s*([A-Z][\w ]{2,30}):\s+(.+?)\s*$", text or ""):
        low = key.lower().strip()
        if "version" in low and "setup data" not in low and "app_version" not in out:
            out["app_version"] = value
        elif low in {"publisher", "app publisher", "company"}:
            out["publisher"] = value
    game_id = re.search(r"goggame-(\d{5,})\.(?:info|hashdb|id)", text or "")
    if game_id:
        out["gog_id"] = game_id.group(1)
    return out


def innoextract_error(stderr: str) -> str:
    """The telling line of innoextract's error output ("Could not determine setup data version!"), not the
    trailing "Done with 1 error" / bug-report boilerplate; notes when the installer is newer than innoextract."""

    lines = [line.strip() for line in (stderr or "").splitlines() if line.strip()]
    useful = [line for line in lines if not re.match(r"(?i)warning\b|done\b|if you are sure|filing a bug|consider\b", line)]
    text = (useful or lines or ["innoextract hiba"])[0]
    if re.search(r"(?i)setup data version|loader revision", stderr or ""):
        text += "; valószínűleg újabb Inno Setup, mint amit ez az innoextract ismer"
    return text


def parse_innoextract_languages(text: str) -> list[str]:
    """--list-languages lines ' - english: English' -> ['english', ...]."""

    return re.findall(r"(?m)^\s*-\s*([\w\-]+)\s*(?::|$)", text or "")


def parse_exiftool_json(text: str) -> dict:
    try:
        data = json.loads(text or "[]")
    except ValueError:
        return {}
    item = data[0] if isinstance(data, list) and data else {}
    return {k: str(item[v]).strip() for k, v in (("product_name", "ProductName"), ("product_version", "ProductVersion"),
                                                  ("company", "CompanyName"), ("description", "FileDescription")) if item.get(v)}


def pe_version(path: Path) -> dict:
    """{"product_name", "product_version", "company", "description"} from the PE version resource (static)."""

    try:
        import pefile  # optional dependency

        pe = pefile.PE(str(path), fast_load=True)
        pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_RESOURCE"]])
        strings: dict[str, str] = {}
        for info in getattr(pe, "FileInfo", None) or []:
            for entry in info if isinstance(info, list) else [info]:
                for table in getattr(entry, "StringTable", []) or []:
                    for key, value in table.entries.items():
                        strings[key.decode("utf-8", "replace")] = value.decode("utf-8", "replace")
        pe.close()
        out = {k: strings[v].strip() for k, v in (("product_name", "ProductName"), ("product_version", "ProductVersion"),
                                                   ("company", "CompanyName"), ("description", "FileDescription")) if strings.get(v, "").strip()}
        if out:
            return out
    except Exception:  # noqa: BLE001 - missing package or a PE file pefile cannot read
        pass
    code, out_text, _err = _run(["exiftool", "-j", "-ProductName", "-ProductVersion", "-CompanyName", "-FileDescription", "--", str(path)], 60)
    return parse_exiftool_json(out_text) if code == 0 else {}


def gog_installer_info(path: Path) -> dict:
    """Static data of a GOG offline installer: {"gog_id", "app_name", "app_version", "inno_version",
    "installer_languages", "product_name", "product_version", "company", "error"}. Never executes it."""

    out: dict = {}
    code, text, err = _run(["innoextract", "--gog-game-id", "--color=0", "--progress=0", "--", str(path)])
    if code == -1:
        out["error"] = err
    else:
        out["gog_id"] = parse_innoextract_gog_id(text)
        if code != 0 and not out["gog_id"]:
            out["error"] = innoextract_error(err)
        code, text, _err = _run(["innoextract", "--info", "--color=0", "--progress=0", "--", str(path)])
        out.update({k: v for k, v in parse_innoextract_info(text).items() if v and not out.get(k)})
        code, text, _err = _run(["innoextract", "--list-languages", "--color=0", "--progress=0", "--", str(path)])
        out["installer_languages"] = parse_innoextract_languages(text) if code == 0 else []
        if not out.get("gog_id") and "error" not in out:
            code, text, _err = _run(["innoextract", "--list", "--color=0", "--progress=0", "--", str(path)], 300)
            out["gog_id"] = parse_innoextract_info(text).get("gog_id", "")
    out.update(pe_version(path))
    return {k: v for k, v in out.items() if v}


# --- NFO ------------------------------------------------------------------------------------------------------

_DECOR = re.compile("[─-◿|¦]")  # box drawing, blocks, geometric shapes, pipes
FIELDS = {
    "title": r"title|game(?:\s+name)?|name",
    "release_date": r"(?:release|rls|street|store)\s*date|date",
    "version": r"(?:game\s*)?version|ver",
    "build": r"build(?:\s*id)?",
    "protection": r"protection|drm|copy\s*protection",
    "genre": r"genre|type",
    "size": r"size",
    "disks": r"disks?|discs?|archives",
    "languages": r"languages?|lang",
    "developer": r"developer|dev",
    "publisher": r"publisher",
    "requires": r"requires?|requirements?",
}
HEADING_RE = re.compile(
    r"(?i)^(?P<head>install(?:ation)?(?:\s*(?:notes?|info|instructions?|guide|steps|procedure))?|how\s+to\s+install|"
    r"installing)\s*(?P<sep>[:.\-=>\]\[]+)?\s*(?P<rest>.*)$")
NOTES_RE = re.compile(r"(?i)^(?:general\s+|release\s+|game\s+|important\s+)?notes?\s*(?P<sep>[:.\-=>\]\[]+)?\s*(?P<rest>.*)$")
OTHER_HEADING_RE = re.compile(r"(?i)^(?:greet(?:ing)?s|group\s+(?:news|info)|about|description|features|contact|news|"
                              r"release\s+info|game\s+info|requirements?|system\s+requirements|thanks|we\s+are|recruit\w*|"
                              r"credits|links?|support|update\s+info|notes?)\b")
STEP_PREFIX = re.compile(r"^\s*(?:\d{1,2}\s*[.):\-]|[-*•o>»·]|\[\d{1,2}\]|step\s*\d+\s*[:.)-]?)\s*", re.I)
ALREADY_CRACKED_RE = re.compile(
    r"(?i)already\s+cracked|pre-?cracked|crack(?:ed)?\s+(?:is\s+)?(?:already\s+)?(?:included|applied|preapplied)|"
    r"no\s+crack\s+(?:is\s+)?(?:needed|required)|does\s*n[o']t\s+need\s+(?:a\s+)?crack|drm[\s\-]?free")


def nfo_lines(text: str) -> list[str]:
    """NFO lines with box/ASCII-art characters blanked and trailing spaces removed."""

    return [_DECOR.sub(" ", line).rstrip() for line in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")]


def _field(lines: list[str], pattern: str) -> str:
    regex = re.compile(rf"(?i)(?:^|\s{{2,}}|^\W+)(?:{pattern})\s*[.:\-_ ]*?[:.]\s*(?P<val>\S.*?)(?=\s{{3,}}|\s*$)")
    for line in lines:
        match = regex.search(line)
        if match:
            value = match["val"].strip(" .:-")
            if value and not re.fullmatch(r"[\W_]+", value):
                return value
    return ""


def _steps(lines: list[str], start: int, inline: str) -> list[str]:
    steps: list[str] = []
    marked = False
    if inline.strip():
        pieces = re.split(r"\s*(?:,|;|\s\d{1,2}[.)]\s)\s*", " " + inline.strip())
        steps.extend(p.strip(" .") for p in pieces if p.strip(" ."))
    for line in lines[start: start + 25]:
        stripped = line.strip()
        if not stripped:
            if steps:
                break
            continue
        if OTHER_HEADING_RE.match(stripped) and not STEP_PREFIX.match(stripped):
            break
        if re.fullmatch(r"[\W_]+", stripped):
            if steps:
                break
            continue
        is_step = bool(STEP_PREFIX.match(stripped))
        marked = marked or is_step
        text = re.sub(r"\s+", " ", STEP_PREFIX.sub("", stripped)).strip()
        if not text:
            continue
        # a wrapped line: unmarked inside a marked list, or starting in lower case after an unfinished sentence
        wrapped = steps and ((marked and not is_step) or (not is_step and text[:1].islower() and not steps[-1].endswith((".", "!", "?", ":"))))
        if wrapped:
            steps[-1] = f"{steps[-1]} {text}"
        else:
            steps.append(text)
    return steps[:15]


def parse_nfo(text: str) -> dict:
    """{"title", "release_date", "version", "build", "protection", "genre", "size", "disks", "languages",
    "developer", "publisher", "requires", "install": [steps], "notes": [lines], "already_cracked": bool,
    "update_from", "update_to"} — keys with nothing found are left out."""

    lines = nfo_lines(text)
    out: dict = {}
    for key, pattern in FIELDS.items():
        value = _field(lines, pattern)
        if value:
            out[key] = value
    if out.get("version"):
        match = re.match(r"(?i)v?(\d[\w.\-]*(?:\s+v\d+)?)", out["version"])
        build = re.search(r"(?i)build\s*(\d{3,})", out["version"])
        if build and not out.get("build"):
            out["build"] = build.group(1)
        out["version"] = match.group(1) if match else out["version"]
    if out.get("build"):
        match = re.search(r"\d{3,}", out["build"])
        if match:
            out["build"] = match.group()
        else:
            out.pop("build")
    for index, line in enumerate(lines):
        stripped = re.sub(r"^[\W_]+(?=[A-Za-z])", "", line.strip())
        match = HEADING_RE.match(stripped)
        if match and "install" not in out and (not match["rest"] or match["sep"]):
            rest = re.sub(r"^[\W_]+$", "", match["rest"])
            steps = _steps(lines, index + 1, rest)
            if steps:
                out["install"] = steps
            continue
        match = NOTES_RE.match(stripped)
        if match and "notes" not in out and (not match["rest"] or match["sep"]) and len(stripped) < 80:
            notes = _steps(lines, index + 1, re.sub(r"^[\W_]+$", "", match["rest"]))
            if notes:
                out["notes"] = notes[:10]
    flat = " ".join(lines)
    out["already_cracked"] = bool(ALREADY_CRACKED_RE.search(flat))
    match = re.search(r"(?i)\bv?(\d+(?:\.\d+)+[\w\-]*)\s+(?:->|to|=>)\s+v?(\d+(?:\.\d+)+[\w\-]*)", flat)
    if match:
        out["update_from"], out["update_to"] = match.group(1), match.group(2)
        if out.get("version") in {None, "", out["update_from"]}:
            out["version"] = out["update_to"]
    match = re.search(r"(?i)requires?\s*[:.]?\s*(?:the\s+)?(?P<rls>[\w.()\-]+-[A-Za-z0-9_]{2,15})\b", flat)
    if match and "." in match["rls"]:
        out["requires"] = match["rls"]
    return out


MAX_NFO = 512 * 1024  # real NFOs are a few KB; a huge file named .nfo is not read whole


def read_nfo(path: Path) -> str:
    """NFO text: UTF-8 when it decodes cleanly, else CP437 (the scene default)."""

    with Path(path).open("rb") as handle:
        data = handle.read(MAX_NFO)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        if len(data) == MAX_NFO and exc.start >= len(data) - 3:  # a character cut at the read limit
            return data[: exc.start].decode("utf-8")
        return data.decode("cp437", errors="replace")


# --- whole release --------------------------------------------------------------------------------------------

def inspect(release, *, deep: bool = True) -> GameFiles:
    """classify() the files of a ``release.Release``, read its NFO, and (``deep``) list the main disc image or
    archive and read the GOG installer statically."""

    root = release.path if release.path.is_dir() else release.path.parent
    info = classify([(f.relpath, f.size) for f in release.files])
    snapshot = nfo.read_release(release)
    info.warnings.extend(snapshot.get("warnings", []))
    blocked = set()
    for record in snapshot.get("records", []):
        for key, value in parse_nfo(record["text"]).items():
            if key in blocked or not value:
                continue
            if info.nfo.get(key) and info.nfo[key] != value:
                info.nfo.pop(key)
                blocked.add(key)
                info.warnings.append(f"eltérő játék-NFO mező, kihagytam: {key}")
            else:
                info.nfo[key] = value
    if not deep:
        return info
    target = None
    if info.layout == "disc_image" and info.disc_images and info.disc_images[0].lower().endswith(".iso"):
        target = info.disc_images[0]
    elif info.layout == "packed" and info.archives:
        target = info.archives[0]
    if target:
        inner = list_archive(root / target if release.path.is_dir() else release.path)
        if inner:
            info.inner = [p for p, _ in inner]
            nested = classify(inner)
            info.setup = info.setup or nested.setup
            for name in ("crack_dirs", "redist_dirs", "dlc", "updates", "disc_images"):
                merged = getattr(info, name) + [x for x in getattr(nested, name) if x not in getattr(info, name)]
                setattr(info, name, merged)
            if not info.nfo and nested.nfo_files:
                info.warnings.append("az NFO csak a lemezképen/archívumon belül van")
        elif shutil.which("7z"):
            info.warnings.append(f"a(z) {_base(target)} tartalma nem listázható (7z)")
    if info.layout == "gog" and info.setup:
        exe = root / info.setup if release.path.is_dir() else release.path
        info.gog = gog_installer_info(exe)
        if info.gog.get("error") and not info.gog.get("gog_id"):
            info.warnings.append("GOG telepítő: az innoextract nem tudta olvasni (" + info.gog["error"] + ") — a GOG-azonosítót a név alapján keresem")
    return info
