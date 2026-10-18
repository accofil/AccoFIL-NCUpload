"""Installers and packages of a program release, inspected statically (nothing from the release is ever run).

* ``detect(path)`` names the installer engine of an EXE/MSI from signatures and PE resources:
  inno, nsis, installshield, msi, 7z-sfx, rar-sfx, zip-sfx, wix-burn, advanced-installer, setup-factory,
  installaware, squirrel, or "exe" (a plain program) -> {"type", "label", "engine_version", ...}.
* ``inno_info(path)`` reads an Inno Setup installer with ``innoextract --info/--list`` (Inno Setup <= 6.0.5 with
  innoextract 1.9; newer ones come back with "supported": False).
* ``list_archive(path)`` lists archives, ISOs, NSIS and 7z SFX installers with ``7z l -slt``;
  ``read_member()`` copies one member out of them (to memory, size-limited) so that its PE header can be read.
* ``payload_archs(path)`` is the architecture of the programs inside an NSIS / 7z SFX / archive (the setup stub
  itself is often x86 even when it installs x64 binaries).
* ``classify(files)`` sorts a release's file list into setups, MSIs, ISOs, portable EXEs, activation tools
  (keygen/patch/crack), archives, macOS and Linux packages.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .. import safeio

from . import pe

LABELS = {
    "inno": "Inno Setup telepítő", "nsis": "NSIS telepítő", "installshield": "InstallShield telepítő", "msi": "Windows Installer (MSI) csomag",
    "msix": "MSIX/AppX csomag", "7z-sfx": "7-Zip önkicsomagoló telepítő", "rar-sfx": "WinRAR önkicsomagoló", "zip-sfx": "ZIP önkicsomagoló",
    "wix-burn": "WiX (Burn) telepítőcsomag", "advanced-installer": "Advanced Installer telepítő", "setup-factory": "Setup Factory telepítő",
    "installaware": "InstallAware telepítő", "squirrel": "Squirrel telepítő", "exe": "program (EXE)",
}
TOOL_RE = re.compile(r"(?i)(?<![a-z])(keygen|keymaker|keyfilemaker|key[ ._-]?gen|patch(?:er)?|crack(?:ed)?|activat(?:or|ion)|loader|"
                     r"serial|unlocker|license[ ._-]?gen|kg|regkey|reg[ ._-]?file|kms|block[ ._-]?firewall)(?![a-z])")
SETUP_RE = re.compile(r"(?i)(?:^|[\\/])(?:setup|install|installer|[^\\/]*[-_. ](?:setup|install|installer)|[^\\/]*setup)[^\\/]*\.exe$")
ARCHIVE_RE = re.compile(r"(?i)\.(rar|zip|7z|001|r00|tar|gz|tgz|xz|bz2|cab)$")
SECOND_VOLUME_RE = re.compile(r"(?i)(\.r\d\d|\.s\d\d|\.(?:00[2-9]|0[1-9]\d|[1-9]\d\d)|\.part(?:0*[2-9]|0*[1-9]\d+)\.rar)$")
HUN_FILE_RE = re.compile(r"(?i)(?:^|[\\/._-])(?:hu|hu[-_]hu|hun|hungarian|magyar)(?:\.(?:lng|lang|txt|xml|json|qm|mo|po|pak|ini|dll|isl|nlf|"
                         r"resources\.dll|language|loc|strings)|[\\/])")
# Bundled runtimes (Visual C++, .NET, DirectX, PhysX ...): neither the program nor its installer.
REDIST_RE = re.compile(r"(?i)(?:^|/)(?:_?redist|redistributables?|_commonredist|directx|dotnetfx|vcredist)/|(?:^|/)(?:vc_?redist[^/]*|vcredist[^/]*|"
                       r"dxsetup|dxwebsetup|dx\w*redist[^/]*|ndp\d[^/]*|dotnet(?:fx)?[^/]*|windowsdesktop-runtime[^/]*|aspnetcore-runtime[^/]*|"
                       r"physx[^/]*|oalinst|vulkanrt[^/]*)\.exe$")
MAC_EXT = {".dmg", ".pkg", ".mpkg", ".app", ".zip"}
LINUX_EXT = {".appimage", ".deb", ".rpm", ".run", ".flatpak", ".snap"}


def _tool(*args: str, timeout: int = 300, binary: bool = False):
    try:
        run = safeio.run(list(args), timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return (b"" if binary else ""), -1
    out = run.stdout if binary else run.stdout.decode("utf-8", errors="replace")
    return out, run.returncode


def _sevenzip() -> str:
    return shutil.which("7z") or shutil.which("7zz") or shutil.which("7za") or ""


# --- engine detection --------------------------------------------------------------------------------------------

def detect_bytes(stub: bytes, overlay: bytes, sections: list[str] | None = None, strings: dict | None = None) -> dict:
    """Installer engine from the PE stub bytes, the start of the overlay, section names and VersionInfo strings."""

    sections = [s.lower() for s in sections or []]
    strings = strings or {}
    text = " ".join(str(v) for v in strings.values()).lower()
    result = {"type": "exe", "engine_version": ""}
    inno = re.search(rb"Inno Setup Setup Data \((\d+\.\d+\.\d+)\)", stub) or re.search(rb"Inno Setup Setup Data \((\d+\.\d+\.\d+)\)", overlay)
    nsis_offset = overlay.find(b"\xef\xbe\xad\xdeNullsoftInst")
    if inno or b"rDlPtS" in stub or "built with inno setup" in text:
        result.update(type="inno", engine_version=inno.group(1).decode() if inno else "")
    elif nsis_offset >= 0 or b"Nullsoft Install System" in stub or b"NullsoftInst" in stub:
        version = re.search(rb"Nullsoft Install System v?(\d+\.\d+(?:\.\d+)?)", stub)
        result.update(type="nsis", engine_version=version.group(1).decode() if version else "")
    elif ".wixburn" in sections:
        result["type"] = "wix-burn"
    elif b"InstallShield" in stub and (b"ISSetupStream" in overlay or b"ISSetupStream" in stub or "installshield" in text or "flexera" in text
                                       or "setup launcher" in text):
        result["type"] = "installshield"
    elif "installshield" in text or "flexera" in text and "setup" in text:
        result["type"] = "installshield"
    elif "advanced installer" in text or "caphyon" in text or b"Advanced Installer" in stub:
        result["type"] = "advanced-installer"
    elif "setup factory" in text or b"Setup Factory" in stub:
        result["type"] = "setup-factory"
    elif "installaware" in text or b"InstallAware" in stub:
        result["type"] = "installaware"
    elif "squirrel" in text or b"SquirrelSetup" in stub:
        result["type"] = "squirrel"
    elif overlay[:6] == b"7z\xbc\xaf\x27\x1c" or overlay.find(b"7z\xbc\xaf\x27\x1c") in range(0, 8192):
        result["type"] = "7z-sfx"
    elif overlay.find(b"Rar!\x1a\x07") in range(0, 8192):
        result["type"] = "rar-sfx"
    elif overlay[:4] == b"PK\x03\x04":
        result["type"] = "zip-sfx"
    result["label"] = LABELS.get(result["type"], result["type"])
    return result


def detect(path: str | Path, info: dict | None = None) -> dict:
    """{"type", "label", "engine_version", "pe": pe.read() result} of an installer file ({} when unreadable)."""

    path = Path(path)
    try:
        with open(path, "rb") as fh:
            head = fh.read(8)
    except OSError:
        return {}
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        kind = "msi" if path.suffix.lower() in {".msi", ".msp", ""} else "ole"
        return {"type": kind, "label": LABELS.get(kind, "OLE compound file"), "engine_version": "", "pe": {}}
    if path.suffix.lower() in {".msix", ".appx", ".msixbundle", ".appxbundle"} and head.startswith(b"PK"):
        return {"type": "msix", "label": LABELS["msix"], "engine_version": "", "pe": {}}
    if not head.startswith(b"MZ"):
        return {}
    info = info if info is not None else pe.read(path, signature=False)
    if not info:
        return {}
    offset = info.get("overlay_offset") or 0
    try:
        with open(path, "rb") as fh:
            stub = fh.read(min(offset or 8 * 1024 * 1024, 8 * 1024 * 1024))
            overlay = b""
            if offset:
                fh.seek(offset)
                overlay = fh.read(1024 * 1024)
    except OSError:
        return {}
    strings = {k: info.get(k, "") for k in ("comments", "company", "description", "product_name", "internal_name", "original_filename")}
    result = detect_bytes(stub, overlay, info.get("sections"), strings)
    result["pe"] = info
    return result


# --- Inno Setup (innoextract) ------------------------------------------------------------------------------------

def parse_innoextract(info_text: str, list_text: str = "") -> dict:
    """``innoextract --info`` (+ ``--list``) output -> {"name", "data_version", "unicode", "languages", "files", "supported"}."""

    out: dict = {"name": "", "data_version": "", "unicode": False, "languages": [], "language_codes": [], "files": [], "supported": False}
    head = re.search(r'(?m)^(?:Inspecting|Listing|Extracting) "(.*)" - setup data version (\d+\.\d+\.\d+)(?: \((unicode|ansi)\))?', info_text or "")
    if head:
        out.update(name=head.group(1), data_version=head.group(2), unicode=head.group(3) == "unicode", supported=True)
    from .nfo import LANG_NAMES

    for code, label in re.findall(r"(?m)^ - (\w+): (.+)$", info_text or ""):
        out["language_codes"].append(code)
        name = LANG_NAMES.get(code.lower()) or LANG_NAMES.get(re.sub(r"(?i)(brazilian|serbian|chinese).*", r"\1", code.lower()), "") or label.strip()
        if name not in out["languages"]:
            out["languages"].append(name)
    for path, flags, size in re.findall(r'(?m)^ - "(.+?)"((?: \[[^\]]+\])*)(?: \(([^)]+)\))?$', list_text or ""):
        out["files"].append({"path": path, "temp": "[temp]" in flags, "size": size})
    return out


def split_app_name(name: str) -> tuple[str, str]:
    """'Foo Bar 2.1.3 (x64)' -> ('Foo Bar', '2.1.3'); a name without a version comes back unchanged."""

    clean = re.sub(r"\s*\((?:x64|x86|64-bit|32-bit)\)\s*$", "", name or "", flags=re.I).strip()
    match = re.match(r"^(.*?)[ ]+v?(\d+(?:\.\d+)+[a-z]?(?:[ -](?:beta|rc|alpha)\s*\d*)?)$", clean, re.I)
    return (match.group(1).strip(), match.group(2)) if match else (clean, "")


def _size(text: str) -> int:
    match = re.match(r"([\d.]+)\s*(B|KiB|MiB|GiB|bytes)?", text or "")
    if not match:
        return 0
    factor = {"KiB": 1024, "MiB": 1024**2, "GiB": 1024**3}.get(match.group(2) or "B", 1)
    return int(float(match.group(1)) * factor)


def inno_payload_archs(path: str | Path, info: dict, limit: int = 64 * 1024 * 1024) -> list[str]:
    """PE architectures of the main programs of an innoextract-readable Inno Setup installer: the files are
    unpacked by innoextract into a temporary folder (static extraction, nothing is run) and their headers read."""

    if not info.get("supported") or not shutil.which("innoextract"):
        return []
    exes = [f for f in info.get("files") or [] if f["path"].lower().endswith(".exe") and not f["temp"]
            and not TOOL_RE.search(f["path"]) and not Path(f["path"]).name.lower().startswith("unins") and _size(f["size"]) <= limit]
    archs = []
    with tempfile.TemporaryDirectory(prefix="ncore-up-inno-") as temp:
        for item in sorted(exes, key=lambda f: -_size(f["size"]))[:2]:
            _tool("innoextract", "--extract", "--silent", "--color", "0", "--progress", "0", "--include", item["path"],
                  "--output-dir", temp, str(path), timeout=300)
            found = next((p for p in Path(temp).rglob(Path(item["path"]).name) if p.is_file()), None)
            info_pe = pe.read(found, signature=False) if found else {}
            if info_pe.get("arch"):
                archs.append(info_pe["arch"])
    return archs


def inno_info(path: str | Path) -> dict:
    if not shutil.which("innoextract"):
        return {}
    info, _rc = _tool("innoextract", "--info", "--color", "0", "--progress", "0", str(path), timeout=120)
    result = parse_innoextract(info)
    if result["supported"]:
        listing, _rc = _tool("innoextract", "--list", "--color", "0", "--progress", "0", str(path), timeout=300)
        result["files"] = parse_innoextract("", listing)["files"]
        result["app_name"], result["app_version"] = split_app_name(result["name"])
    return result


# --- 7-Zip listings ----------------------------------------------------------------------------------------------

def parse_7z_slt(text: str) -> dict:
    """``7z l -slt`` output -> {"archives": [{Type, Path, ...}], "type": innermost Type, "entries": [{"path", "size", "packed",
    "folder", "modified", "attributes", "method", "encrypted"}]}."""

    archives: list[dict] = []
    entries: list[dict] = []
    mode, current = "", None
    for line in (text or "").split("\n"):
        line = line.rstrip("\r")
        if line == "--":
            mode, current = "archive", {}
            archives.append(current)
            continue
        if line == "----":
            mode, current = "parent", None
            continue
        if re.fullmatch(r"-{10,}", line):
            mode, current = "entries", None
            continue
        match = re.match(r"^([A-Za-z][\w ]*?) = (.*)$", line)
        if not match:
            if mode == "entries" and not line.strip():
                current = None
            continue
        key, value = match.group(1), match.group(2)
        if mode == "archive" and current is not None:
            current.setdefault(key, value)
        elif mode == "entries":
            if current is None or key == "Path" and "Path" in current:
                current = {}
                entries.append(current)
            current[key] = value
    items = []
    for entry in entries:
        if "Path" not in entry:
            continue
        size = entry.get("Size", "")
        items.append({
            "path": entry["Path"].replace("\\", "/"), "size": int(size) if size.isdigit() else 0,
            "packed": int(entry["Packed Size"]) if entry.get("Packed Size", "").isdigit() else 0,
            "folder": entry.get("Folder") == "+" or "D" in entry.get("Attributes", "")[:1],
            "modified": entry.get("Modified", ""), "attributes": entry.get("Attributes", ""), "method": entry.get("Method", ""),
            "encrypted": entry.get("Encrypted") == "+",
        })
    types = [a.get("Type", "") for a in archives if a.get("Type")]
    return {"archives": archives, "type": types[-1] if types else "", "entries": items}


def list_archive(path: str | Path, timeout: int = 300) -> dict:
    """parse_7z_slt() of an archive / ISO / NSIS / SFX ({} without 7z or when 7z cannot open it)."""

    tool = _sevenzip()
    if not tool:
        return {}
    text, code = _tool(tool, "l", "-slt", "-pnone", "--", str(path), timeout=timeout)
    if code not in (0, 1) and "----------" not in text:
        return {}
    result = parse_7z_slt(text)
    return result if result["archives"] or result["entries"] else {}


def read_member(archive: str | Path, member: str, limit: int = 64 * 1024 * 1024) -> bytes:
    """The bytes of one archive member (up to ``limit``), streamed by ``7z e -so``; b'' when missing or too large."""

    tool = _sevenzip()
    if not tool:
        return b""
    try:
        result = safeio.run([tool, "e", "-so", "-pnone", "-spd", "--", str(archive), member], timeout=120, stdout_limit=limit)
        return result.stdout if result.returncode == 0 else b""
    except (OSError, subprocess.SubprocessError):
        return b""


def extract_member(archive: str | Path, member: str, target_dir: str | Path, size: int = 0) -> Path | None:
    """Copy one member to ``target_dir`` (checks free space first); for readers that need a real file."""

    tool = _sevenzip()
    target = Path(target_dir)
    target.mkdir(parents=True, exist_ok=True)
    path = target / Path(member).name
    if path.exists() or path.is_symlink():
        return None
    if not tool or size <= 0 or size > safeio.PACKAGE_LIMIT or shutil.disk_usage(target).free < size + (512 << 20):
        return None
    _out, code = _tool(tool, "e", "-y", "-pnone", "-spd", f"-o{target}", "--", str(archive), member, timeout=600)
    if code == 0 and path.is_file() and not path.is_symlink() and path.stat().st_size <= safeio.PACKAGE_LIMIT:
        return path
    if path.is_file() or path.is_symlink():
        path.unlink(missing_ok=True)
    return None


def main_programs(entries: list[dict], limit: int = 3) -> list[dict]:
    """The biggest EXE/DLL members that are not uninstallers, plugins of the installer or activation tools."""

    def wanted(entry: dict) -> bool:
        path = entry["path"].lower()
        name = path.rsplit("/", 1)[-1]
        return (not entry["folder"] and name.endswith((".exe", ".dll")) and "$pluginsdir" not in path and not name.startswith(("uninst", "unins"))
                and not TOOL_RE.search(path) and "redist" not in path and "vcredist" not in name and not name.startswith("vc_redist"))

    exes = [e for e in entries if wanted(e) and e["path"].lower().endswith(".exe")]
    dlls = [e for e in entries if wanted(e) and e["path"].lower().endswith(".dll")]
    return sorted(exes, key=lambda e: -e["size"])[:limit] or sorted(dlls, key=lambda e: -e["size"])[:1]


def payload_archs(path: str | Path, listing: dict | None = None, limit: int = 64 * 1024 * 1024) -> list[str]:
    """PE architectures of the main programs inside an archive-like installer (NSIS, 7z SFX, ZIP, ISO ...)."""

    listing = listing if listing is not None else list_archive(path)
    archs = []
    for entry in main_programs(listing.get("entries") or []):
        if entry["size"] > limit:
            continue
        info = pe.parse(read_member(path, entry["path"], limit), signature=False)
        if info.get("arch"):
            archs.append(info["arch"])
    return archs


# --- release file list ---------------------------------------------------------------------------------------------

def classify(paths: list[tuple[str, int]], group: str = "") -> dict:
    """Sort (relative path, size) pairs of a release:

    {"setups", "msis", "isos", "exes", "tools", "tool_dirs", "archives", "mac", "linux", "redist", "windows_iso", "hungarian_files", "kind"}
    with "kind" one of setup / msi / portable / iso / windows_iso / archive / mac / linux / other ("redist": bundled runtimes,
    which count as the setups only when the release holds nothing else)."""

    out: dict = {"setups": [], "msis": [], "isos": [], "exes": [], "tools": [], "tool_dirs": [], "archives": [], "mac": [], "linux": [],
                 "redist": [], "windows_iso": False, "hungarian_files": [], "kind": "other"}
    group = (group or "").lower()
    # .bin / .mdf are disc images only next to their .cue / .mds (Inno Setup's setup-1.bin, SQL .mdf files are not)
    sheets = {rel.replace("\\", "/").lower() for rel, _s in paths if rel.lower().endswith((".cue", ".mds"))}
    for rel, size in paths:
        norm = rel.replace("\\", "/")
        low = norm.lower()
        parts = low.split("/")
        ext = os.path.splitext(low)[1]
        in_tool_dir = any(TOOL_RE.search(p) or (group and p == group) for p in parts[:-1])
        if in_tool_dir:
            tool_dir = "/".join(norm.split("/")[: next(i for i, p in enumerate(parts[:-1]) if TOOL_RE.search(p) or (group and p == group)) + 1])
            if tool_dir not in out["tool_dirs"]:
                out["tool_dirs"].append(tool_dir)
        if HUN_FILE_RE.search("/" + low):
            out["hungarian_files"].append(norm)
        if low.endswith(("sources/install.wim", "sources/install.esd", "sources/install.swm")):
            out["windows_iso"] = True
        if ext == ".reg" or ext in {".exe", ".msi", ".com", ".bat", ".cmd", ".dll"} and (in_tool_dir or TOOL_RE.search(parts[-1])):
            out["tools"].append(norm)
        elif ext in {".exe", ".msi"} and REDIST_RE.search("/" + low):
            out["redist"].append((norm, size))
        elif ext == ".msi":
            out["msis"].append(norm)
        elif ext == ".exe":
            (out["setups"] if SETUP_RE.search("/" + norm) else out["exes"]).append((norm, size))
        elif (ext in {".iso", ".img", ".nrg"} and size > 10 * 1024 * 1024) or \
                (ext in {".bin", ".mdf"} and os.path.splitext(low)[0] + (".cue" if ext == ".bin" else ".mds") in sheets):
            out["isos"].append(norm)
        elif ARCHIVE_RE.search(low) and not SECOND_VOLUME_RE.search(low) and not re.search(r"\.part0*[2-9]\d*\.rar$", low):
            out["archives"].append(norm)
        elif ext in MAC_EXT - {".zip"} or ".app/" in low:
            if ".app/" in low:
                app = norm[: low.index(".app/") + 4]
                if app not in out["mac"]:
                    out["mac"].append(app)
            else:
                out["mac"].append(norm)
        elif ext in LINUX_EXT or low.endswith((".tar.gz", ".tar.xz")):
            out["linux"].append(norm)
    if out["redist"] and not (out["setups"] or out["exes"] or out["msis"]):  # the release IS a runtime (VC++ AIO, .NET ...)
        out["setups"] = [item for item in out["redist"] if item[0].lower().endswith(".exe")]
        out["msis"] = [p for p, _s in out["redist"] if p.lower().endswith(".msi")]
    out["redist"] = [p for p, _s in out["redist"]]
    out["setups"] = [p for p, _s in sorted(out["setups"], key=lambda x: (-x[1], x[0]))]
    out["exes"] = [p for p, _s in sorted(out["exes"], key=lambda x: (-x[1], x[0]))]
    if out["windows_iso"]:
        out["kind"] = "windows_iso"
    elif out["isos"]:
        out["kind"] = "iso"
    elif out["msis"] and not out["setups"]:
        out["kind"] = "msi"
    elif out["setups"]:
        out["kind"] = "setup"
    elif out["exes"]:
        out["kind"] = "portable"
    elif out["mac"]:
        out["kind"] = "mac"
    elif out["linux"]:
        out["kind"] = "linux"
    elif out["archives"]:
        out["kind"] = "archive"
    return out


def hungarian_ui(names: list[str]) -> bool:
    """True when a file list contains Hungarian language files (hu.lng, Hungarian.isl, locales/hu/ ...)."""

    return any(HUN_FILE_RE.search("/" + n.lower().replace("\\", "/")) for n in names)


# "Lang/", "locales/", also prefixed folder names such as Notepad++'s "nppLocalization/"
LANG_DIR_RE = re.compile(r"(?i)(?:^|/)(?:lang|langs|i18n|nls|[\w$.-]*?(?:language|languages|locale|locales|localization|localisation|translations))/")


def ui_languages(names: list[str]) -> list[str]:
    """['többnyelvű', 'magyar'] / ['többnyelvű'] / ['magyar'] / [] from the language files of a file list
    (five or more files in a language folder count as a multilingual program)."""

    paths = [n.replace("\\", "/") for n in names]
    in_dirs = {p.rsplit("/", 1)[0] + "/" + p.rsplit("/", 1)[-1].split(".")[0].lower() for p in paths if LANG_DIR_RE.search(p)}
    out = ["többnyelvű"] if len(in_dirs) >= 5 else []
    if hungarian_ui(paths):
        out.append("magyar")
    return out
