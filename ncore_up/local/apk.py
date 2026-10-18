"""Android packages read statically: APK, and the XAPK / APKS / APKM bundles (zip files holding a base APK, split APKs,
OBB data and a manifest.json / info.json).

* ``aapt dump badging`` gives package, versionCode/versionName, sdkVersion/targetSdkVersion, application-label (also
  the -hu variant), locales and native-code ABIs; a small binary-XML reader of AndroidManifest.xml is the fallback.
* ``apksigner verify --verbose --print-certs`` gives the signer DN and SHA-256: an APK that does not verify, or one
  signed with a debug/AOSP test key, has been modified or repackaged.

The APK is never installed or run; only its zip entries and manifest are read.
"""

from __future__ import annotations

import json
import re
import shutil
import struct
import subprocess
import tempfile
import zipfile
import zlib
from pathlib import Path

from .. import safeio

APK_EXT = {".apk", ".xapk", ".apks", ".apkm"}
_ZIP_ERRORS = (OSError, KeyError, RuntimeError, ValueError, NotImplementedError, EOFError, zlib.error, zipfile.BadZipFile)

ANDROID_VERSIONS = {
    14: "4.0", 15: "4.0.3", 16: "4.1", 17: "4.2", 18: "4.3", 19: "4.4", 20: "4.4W", 21: "5.0", 22: "5.1", 23: "6.0", 24: "7.0",
    25: "7.1", 26: "8.0", 27: "8.1", 28: "9", 29: "10", 30: "11", 31: "12", 32: "12L", 33: "13", 34: "14", 35: "15", 36: "16",
}
ABI_NAMES = {"arm64-v8a", "armeabi-v7a", "armeabi", "x86", "x86_64", "mips", "mips64"}


def android_version(sdk: int | str | None) -> str:
    """API level → "Android 8.0 (API 26)"; '' when unknown."""

    try:
        level = int(sdk)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ""
    version = ANDROID_VERSIONS.get(level)
    return f"Android {version} (API {level})" if version else f"API {level}"


# --- aapt ---------------------------------------------------------------------------------------------------------

_PAIR_RE = re.compile(r"([\w-]+)='((?:[^'\\]|\\.)*)'")
_QUOTED_RE = re.compile(r"'((?:[^'\\]|\\.)*)'")


def parse_badging(text: str) -> dict:
    """``aapt dump badging`` output → {"package", "version_code", "version_name", "min_sdk", "target_sdk", "label",
    "labels": {lang: label}, "locales", "abis", "permissions", "launchable", "compile_sdk"}."""

    out: dict = {"labels": {}, "locales": [], "abis": [], "permissions": [], "launchable": False}
    for line in (text or "").splitlines():
        key, _, rest = line.partition(":")
        key = key.strip()
        if key == "package":
            pairs = dict(_PAIR_RE.findall(rest))
            out["package"] = pairs.get("name", "")
            out["version_code"] = pairs.get("versionCode", "")
            out["version_name"] = pairs.get("versionName", "")
            out["compile_sdk"] = pairs.get("compileSdkVersion") or pairs.get("platformBuildVersionCode", "")
        elif key in {"sdkVersion", "minSdkVersion"}:
            out["min_sdk"] = _first_quoted(rest)
        elif key == "targetSdkVersion":
            out["target_sdk"] = _first_quoted(rest)
        elif key == "application-label":
            out["label"] = _first_quoted(rest)
        elif key.startswith("application-label-"):
            out["labels"][key[len("application-label-"):]] = _first_quoted(rest)
        elif key == "application" and "label" not in out:
            label = dict(_PAIR_RE.findall(rest)).get("label", "")
            if label:
                out["label"] = label
        elif key == "locales":
            out["locales"] = [x for x in _QUOTED_RE.findall(rest) if x and x != "--_--"]
        elif key in {"native-code", "alt-native-code"}:
            out["abis"] += [x for x in _QUOTED_RE.findall(rest) if x not in out["abis"]]
        elif key == "uses-permission":
            name = dict(_PAIR_RE.findall(rest)).get("name") or _first_quoted(rest)
            if name:
                out["permissions"].append(name)
        elif key == "launchable-activity":
            out["launchable"] = True
    return out


def _first_quoted(text: str) -> str:
    match = _QUOTED_RE.search(text or "")
    return match.group(1).replace("\\'", "'") if match else ""


def badging(apk: Path) -> dict:
    """parse_badging() of a real APK; {} without aapt or when aapt cannot read it."""

    tool = shutil.which("aapt")
    if not tool:
        return {}
    try:
        run = safeio.run([tool, "dump", "badging", str(apk)], timeout=120)
    except (OSError, subprocess.SubprocessError):
        return {}
    data = parse_badging(run.stdout.decode("utf-8", "replace"))
    return data if data.get("package") else {}


# --- apksigner ------------------------------------------------------------------------------------------------------

def parse_certs(text: str, returncode: int = 0) -> dict:
    """``apksigner verify --verbose --print-certs`` → {"verified", "schemes": [..], "signers": [{"dn", "sha256", "sha1"}],
    "errors": [..]}."""

    out: dict = {"verified": False, "schemes": [], "signers": [], "errors": []}
    signers: dict[int, dict] = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if line == "Verifies":
            out["verified"] = True
        elif line.startswith("DOES NOT VERIFY"):
            out["verified"] = False
        elif match := re.match(r"Verified using (v\d+(?:\.\d+)?) scheme .*?:\s*(true|false)", line):
            if match.group(2) == "true":
                out["schemes"].append(match.group(1))
        elif match := re.match(r"Signer #(\d+) certificate (DN|SHA-256 digest|SHA-1 digest):\s*(.+)", line):
            signer = signers.setdefault(int(match.group(1)), {"dn": "", "sha256": "", "sha1": ""})
            signer[{"DN": "dn", "SHA-256 digest": "sha256", "SHA-1 digest": "sha1"}[match.group(2)]] = match.group(3).strip()
        elif line.startswith("ERROR"):
            out["errors"].append(line)
    out["signers"] = [signers[k] for k in sorted(signers)]
    if returncode == 0 and out["signers"] and not out["errors"] and "DOES NOT VERIFY" not in (text or ""):
        out["verified"] = True
    return out


def certs(apk: Path) -> dict:
    tool = shutil.which("apksigner")
    if not tool:
        return {}
    try:
        run = safeio.run([tool, "verify", "--verbose", "--print-certs", str(apk)], timeout=180)
    except (OSError, subprocess.SubprocessError):
        return {}
    return parse_certs(run.stdout.decode("utf-8", "replace") + "\n" + run.stderr.decode("utf-8", "replace"), run.returncode)


DEBUG_DN_RE = re.compile(r"(?i)CN=Android Debug\b|EMAILADDRESS=android@android\.com|CN=Android, OU=Android, O=Android, L=Mountain View")


def signature_status(cert_info: dict) -> tuple[str, str]:
    """(status, Hungarian explanation) from the local check alone: "módosított", "érvényes" or "ismeretlen"."""

    if not cert_info or not cert_info.get("signers") and not cert_info.get("errors"):
        return "ismeretlen", "az aláírást nem tudtam ellenőrizni (nincs apksigner, vagy az APK nem olvasható)"
    if not cert_info.get("verified"):
        reason = (cert_info.get("errors") or ["az aláírás érvénytelen"])[0]
        return "módosított", f"az APK aláírása nem érvényes ({reason[:120]})"
    signer = cert_info["signers"][0]
    if DEBUG_DN_RE.search(signer.get("dn", "")):
        return "módosított", f"debug/AOSP tesztkulccsal aláírva ({signer['dn']}) — újracsomagolt APK"
    return "érvényes", f"érvényes aláírás: {signer.get('dn', '')} (SHA-256 {signer.get('sha256', '')[:16]}…)"


# --- binary AndroidManifest.xml (fallback without aapt) ----------------------------------------------------------

_ATTR_IDS = {0x0101021B: "versionCode", 0x0101021C: "versionName", 0x0101020C: "minSdkVersion", 0x01010270: "targetSdkVersion",
             0x01010001: "label"}


def _string_pool(data: bytes, start: int) -> list[str]:
    _type, header_size, _size, count, _styles, flags, strings_start, _styles_start = struct.unpack_from("<HHIIIIII", data, start)
    utf8 = bool(flags & 0x100)
    offsets = struct.unpack_from(f"<{count}I", data, start + header_size)
    base = start + strings_start
    out = []
    for offset in offsets:
        pos = base + offset
        if utf8:
            length = data[pos]
            pos += 2 if length & 0x80 else 1
            size = data[pos]
            if size & 0x80:
                size = ((size & 0x7F) << 8) | data[pos + 1]
                pos += 2
            else:
                pos += 1
            out.append(data[pos:pos + size].decode("utf-8", "replace"))
        else:
            length = struct.unpack_from("<H", data, pos)[0]
            pos += 2
            if length & 0x8000:
                length = ((length & 0x7FFF) << 16) | struct.unpack_from("<H", data, pos)[0]
                pos += 2
            out.append(data[pos:pos + length * 2].decode("utf-16-le", "replace"))
    return out


def parse_axml_manifest(data: bytes) -> dict:
    """{"package", "version_code", "version_name", "min_sdk", "target_sdk"} from a binary AndroidManifest.xml."""

    if len(data) < 8 or struct.unpack_from("<H", data, 0)[0] != 0x0003:
        return {}
    strings: list[str] = []
    resource_ids: list[int] = []
    out: dict = {}
    pos = struct.unpack_from("<H", data, 2)[0]
    try:
        while pos + 8 <= len(data):
            chunk_type, header_size, size = struct.unpack_from("<HHI", data, pos)
            if size < 8:
                break
            if chunk_type == 0x0001:
                strings = _string_pool(data, pos)
            elif chunk_type == 0x0180:
                resource_ids = list(struct.unpack_from(f"<{(size - header_size) // 4}I", data, pos + header_size))
            elif chunk_type == 0x0102:
                name_index = struct.unpack_from("<I", data, pos + 20)[0]
                attr_start, attr_size, attr_count = struct.unpack_from("<HHH", data, pos + 24)
                tag = strings[name_index] if name_index < len(strings) else ""
                for index in range(attr_count):
                    attr = pos + 16 + attr_start + index * attr_size
                    _ns, name_idx, raw, _vsize, _res, dtype, value = struct.unpack_from("<IIIHBBI", data, attr)
                    name = strings[name_idx] if name_idx < len(strings) else ""
                    if name_idx < len(resource_ids) and resource_ids[name_idx] in _ATTR_IDS:
                        name = _ATTR_IDS[resource_ids[name_idx]]
                    if dtype == 0x03 or raw != 0xFFFFFFFF:
                        text = strings[raw] if raw < len(strings) else (strings[value] if value < len(strings) else "")
                    else:
                        text = str(value) if dtype in (0x10, 0x11) else ""
                    if tag == "manifest" and name in {"package", "versionCode", "versionName"}:
                        out[{"package": "package", "versionCode": "version_code", "versionName": "version_name"}[name]] = text
                    elif tag == "uses-sdk" and name in {"minSdkVersion", "targetSdkVersion"}:
                        out["min_sdk" if name == "minSdkVersion" else "target_sdk"] = text
            pos += size
    except (struct.error, IndexError):
        pass
    return out


def manifest_from_zip(apk: Path) -> dict:
    try:
        with zipfile.ZipFile(apk) as archive:
            return parse_axml_manifest(safeio.read_zip(archive, "AndroidManifest.xml"))
    except _ZIP_ERRORS:
        return {}


# --- bundles --------------------------------------------------------------------------------------------------------

def parse_bundle_manifest(data: dict) -> dict:
    """XAPK manifest.json / APKM info.json / SAI meta.sai_v2.json → {"package", "version_name", "version_code", "name",
    "min_sdk", "target_sdk", "splits"}."""

    if not isinstance(data, dict):
        return {}
    split_apks = data.get("split_apks") if isinstance(data.get("split_apks"), list) else []
    splits = [str(s.get("file") or "") for s in split_apks if isinstance(s, dict)]
    return {
        "package": str(data.get("package_name") or data.get("pname") or data.get("package") or ""),
        "version_name": str(data.get("version_name") or data.get("release_version") or ""),
        "version_code": str(data.get("version_code") or data.get("versioncode") or ""),
        "name": str(data.get("name") or data.get("app_name") or data.get("label") or ""),
        "min_sdk": str(data.get("min_sdk_version") or data.get("min_api") or data.get("min_sdk") or ""),
        "target_sdk": str(data.get("target_sdk_version") or data.get("target_sdk") or ""),
        "splits": [s for s in splits if s],
    }


def split_configs(names: list[str]) -> dict:
    """ABIs, densities and languages of split APK names ("config.arm64_v8a.apk", "split_config.hu.apk",
    "splits/base-xxhdpi.apk")."""

    abis, langs, densities = [], [], []
    for name in names:
        match = re.search(r"(?:config\.|base-)([A-Za-z0-9_]+)\.apk$", Path(name).name)
        if not match:
            continue
        value = match.group(1)
        abi = value.replace("_", "-") if value.replace("_", "-") in ABI_NAMES else value if value in ABI_NAMES else ""
        if abi:
            abis.append(abi)
        elif re.fullmatch(r"(l|m|tv|h|xh|xxh|xxxh|no|any)dpi", value):
            densities.append(value)
        elif re.fullmatch(r"[a-z]{2,3}", value) and value != "master":
            langs.append(value)
    return {"abis": abis, "languages": langs, "densities": densities}


def _base_member(names: list[str], package: str) -> str:
    apks = [n for n in names if n.lower().endswith(".apk")]
    for wanted in ("base.apk", f"{package}.apk" if package else "", "splits/base-master.apk", "base-master.apk"):
        if wanted and wanted in apks:
            return wanted
    plain = [n for n in apks if "config." not in Path(n).name and not Path(n).name.startswith("split_")]
    return plain[0] if len(plain) == 1 else (apks[0] if len(apks) == 1 else "")


def inspect(path: Path) -> dict:
    """Everything about one APK or bundle: {"format", "package", "version_name", "version_code", "label", "labels",
    "min_sdk", "target_sdk", "abis", "locales", "hungarian", "hungarian_resources", "permissions", "splits", "obb",
    "certs", "signature", "warnings"}. Missing tools give partial results plus a Hungarian warning."""

    path = Path(path)
    ext = path.suffix.lower()
    out: dict = {"format": ext.lstrip(".").upper(), "warnings": [], "splits": [], "obb": [], "labels": {}, "abis": [], "locales": []}
    if ext == ".apk":
        _read_apk(path, out)
    else:
        _read_bundle(path, out)
    # Library resources (AndroidX, Play services) carry ~90 locales, "hu" included, and a downloader's language splits
    # follow its own device: only a translated app name counts as Hungarian, the rest is a hint.
    out["hungarian"] = "hu" in out.get("labels", {})
    out["hungarian_resources"] = "hu" in out.get("locales", []) or "hu" in out.get("split_languages", [])
    out["signature"] = signature_status(out.get("certs") or {})
    if not shutil.which("aapt"):
        out["warnings"].append("nincs aapt: a csomagnevet/verziót a manifestből olvastam, a címke és a nyelvek hiányozhatnak")
    return out


def _read_apk(apk: Path, out: dict, shown: str = "") -> None:
    data = badging(apk) or manifest_from_zip(apk)
    if not data:
        out["warnings"].append(f"az APK manifestje nem olvasható: {shown or apk.name}")
        return
    for key, value in data.items():
        if value and key != "labels":
            out[key] = value
    out["labels"].update(data.get("labels") or {})
    out["certs"] = certs(apk)


def _read_bundle(bundle: Path, out: dict) -> None:
    try:
        archive = zipfile.ZipFile(bundle)
    except (OSError, zipfile.BadZipFile):
        out["warnings"].append(f"a csomag nem zip fájl: {bundle.name}")
        return
    with archive:
        names = archive.namelist()
        meta: dict = {}
        for candidate in ("manifest.json", "info.json", "meta.sai_v2.json", "meta.sai_v1.json"):
            if candidate in names:
                try:
                    meta = parse_bundle_manifest(json.loads(safeio.read_zip(archive, candidate).decode("utf-8-sig", "replace")))
                except _ZIP_ERRORS:
                    meta = {}
                if meta.get("package"):
                    break
        for key in ("package", "version_name", "version_code", "min_sdk", "target_sdk"):
            if meta.get(key):
                out[key] = meta[key]
        if meta.get("name"):
            out["bundle_name"] = meta["name"]
        out["splits"] = [n for n in names if n.lower().endswith(".apk")]
        out["obb"] = [n for n in names if n.lower().endswith(".obb")]
        configs = split_configs(out["splits"])
        out["abis"] = configs["abis"]
        out["split_languages"] = configs["languages"]
        base = _base_member(names, out.get("package", ""))
        if not base:
            out["warnings"].append("a csomagban nem találtam az alap APK-t (base.apk)")
            return
        with tempfile.TemporaryDirectory(prefix="ncore-up-apk-") as work:
            target = Path(work) / "base.apk"
            try:
                safeio.copy_zip(archive, base, target)
            except _ZIP_ERRORS as exc:  # encrypted APKM, broken zip
                out["warnings"].append(f"az alap APK nem bontható ki ({type(exc).__name__}): {base}")
                return
            inner: dict = {"labels": {}, "abis": [], "locales": [], "warnings": []}
            _read_apk(target, inner, f"{bundle.name} → {base}")
    for key, value in inner.items():
        if key == "labels":
            out["labels"].update(value)
        elif key == "abis":
            out["abis"] = list(dict.fromkeys(out["abis"] + value))
        elif key == "warnings":
            out["warnings"] += value
        elif value and (key not in out or key in {"label", "locales", "certs", "permissions"}):
            out[key] = value
    if "hu" in out.get("split_languages", []) and "hu" not in out.get("locales", []):
        out.setdefault("locales", []).append("hu")
    if not out.get("label") and out.get("bundle_name"):
        out["label"] = out["bundle_name"]
