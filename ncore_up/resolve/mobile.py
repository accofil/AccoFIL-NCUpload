"""Mobile apps (nCore "Mobil"): Android APK / XAPK / APKS / APKM and iOS IPA.

Package name, version and signer always come from the file itself (``local.apk`` / ``local.ipa``), never from a store.
Store data adds the rest: Google Play (hl=hu&gl=HU: Hungarian listing when localized) and F-Droid for Android,
the App Store (iTunes lookup, country=hu&lang=hu_hu) for iOS. Hungarian description: the store's own Hungarian text,
else ``translate.to_hungarian`` of the English one. Torrent name: "[Android] App neve v1.2.3" / "[iPhone] …".
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .. import translate
from ..local import apk as apk_reader
from ..local import ipa as ipa_reader
from ..local import installer
from .. import safeio
from ..local.consoleid import seven_zip_list
from ..meta import Meta
from ..sources import fdroid, googleplay, itunes

ANDROID_EXT = apk_reader.APK_EXT
IOS_EXT = {".ipa"}

INSTALL_APK = (
    "1. A telefonon engedélyezd az ismeretlen forrásból származó alkalmazások telepítését annak az appnak, amivel a fájlt "
    "megnyitod (Beállítások → Alkalmazások → Speciális hozzáférés → Ismeretlen alkalmazások telepítése).\n"
    "2. Másold az APK-t a telefonra, és nyisd meg egy fájlkezelővel.\n"
    "3. Koppints a Telepítés gombra; a telepítés után a beállítást visszakapcsolhatod."
)
INSTALL_SPLIT = (
    "1. Telepítsd a SAI (Split APKs Installer) vagy az APKMirror Installer alkalmazást (XAPK-hoz az APKPure is jó).\n"
    "2. Engedélyezd neki az ismeretlen forrásból származó alkalmazások telepítését.\n"
    "3. Nyisd meg vele a csomagot ({fmt}), és telepítsd: a telepítő a készülékhez illő split APK-kat rakja fel.\n"
    "4. Ha a csomagban OBB-adat is van, az az Android/obb/<csomagnév> mappába kerül (a telepítő ezt elvégzi)."
)
INSTALL_RESIGNED = "Ha az eredeti (Play-es) változat fent van, előbb távolítsd el: az aláírás eltér, ezért frissítésként nem települ."
INSTALL_IOS = (
    "1. Sideload számítógépről: AltStore vagy Sideloadly (ingyenes Apple ID-val 7 naponta újra kell aláírni).\n"
    "2. TrollStore-ral (a támogatott iOS-verziókon) végleges telepítés: nyisd meg az IPA-t a TrollStore-ban.\n"
    "3. Jailbreakelt eszközön: AppSync Unified + Filza (vagy ideviceinstaller)."
)
SCREENSHOT_WARNING = ("a Google Play / App Store képernyőképei nem számítanak saját képnek: 3 saját képernyőkép kell "
                      "(legalább 1 a telepítésről és 1 a működő alkalmazásról)")


def _app_files(release) -> list:
    files = [f for f in release.files if f.ext in ANDROID_EXT | IOS_EXT]
    return sorted(files, key=lambda f: (len(Path(f.relpath).parts), -f.size))


def _path(release, relpath: str) -> Path:
    return release.path if release.path.is_file() else release.path / relpath


def _from_archive(release, workdir: Path) -> tuple[Path | None, str]:
    """(extracted app file, archive member) from a packed release; 7z extracts it flat into ``workdir``."""

    tool = shutil.which("7z") or shutil.which("7za")
    if not tool:
        return None, ""
    for item in release.files:
        if not re.search(r"(?i)\.(zip|7z|rar)$", item.relpath) or re.search(r"(?i)\.part(?!0*1\.rar)\d+\.rar$", item.relpath):
            continue
        archive = _path(release, item.relpath)
        members = [(m, s) for m, s in seven_zip_list(archive) if Path(m).suffix.lower() in ANDROID_EXT | IOS_EXT]
        if not members:
            continue
        member, size = max(members, key=lambda m: m[1])
        if size <= 0 or size > safeio.PACKAGE_LIMIT:
            return None, ""
        return installer.extract_member(archive, member, workdir, size), member
    return None, ""


def resolve(release, code: str = "mobil", *, original: bool = False, hints: dict | None = None) -> Meta:
    from ..local import nfo
    hints = dict(hints or {})
    # Package headers are read first below; these are fallbacks, never a reason
    # to execute a payload or trust an arbitrary URL as a package identity.
    for key, labels in (("package", ("package id", "package name", "bundle id", "application id")),
                        ("version", ("version", "app version")), ("fallback_title", ("title", "app name", "name"))):
        if not hints.get(key):
            hints[key] = nfo.value(release, *labels)
    meta = Meta(kind="app")
    nfo.annotate(meta, release)
    files = _app_files(release)
    with tempfile.TemporaryDirectory(prefix="ncore-up-mobil-") as work:
        member = ""
        if files:
            path = _path(release, files[0].relpath)
            others = [f.relpath for f in files[1:] if f.ext == files[0].ext]
            if others:
                meta.extra["other_files"] = ", ".join(others[:10])
                meta.warnings.append(f"több alkalmazásfájl van ({len(files)}); a legnagyobbat azonosítottam: {files[0].relpath}")
        else:
            path, member = _from_archive(release, Path(work))
            if member:
                meta.extra["packed"] = member
        if path is None:
            meta.warnings.append("nincs APK/XAPK/APKS/APKM/IPA fájl a release-ben — a névből próbálom azonosítani")
            _name_only(release, meta, hints)
        else:
            fallback = name_from_file(path.stem) if not re.fullmatch(r"(?i)base|app|game|\d+", path.stem) else name_from_file(release.name)
            hints["fallback_title"] = hints.get("fallback_title") or fallback
            if path.suffix.lower() in IOS_EXT:
                _ios(path, meta, hints)
            else:
                _android(path, meta, hints)
    if original and meta.extra.get("torrent_name"):  # an original release keeps its release name on nCore
        meta.extra["suggested_name"], meta.extra["torrent_name"] = meta.extra["torrent_name"], release.name
    nfo.description(meta, release)
    return meta


def app_name(store_name: str, label: str = "") -> str:
    """Store listing title without its marketing tagline: "Spotify — Zene és podcastok" (label "Spotify") → "Spotify".
    Cut where the app's own label ends when the listing starts with it, else only at a spaced em/en dash."""

    name = re.sub(r"\s+", " ", store_name or "").strip()
    label = (label or "").strip()
    if label and len(name) > len(label) and name.lower().startswith(label.lower()) \
            and re.match(r"\s*(?:[—–:|]|-\s)", name[len(label):]):
        return name[:len(label)].strip()
    return re.split(r"\s+[—–]\s+", name, maxsplit=1)[0].strip()


def name_from_file(stem: str) -> str:
    """"Player_5.0_MOD" → "Player", "Google.Maps.v11.152.0" → "Google Maps"."""

    text = re.sub(r"(?i)[ ._-]*\(?v?\d+(?:\.\d+)+.*$", "", stem or "")
    text = re.sub(r"(?i)\b(android|ios|iphone|ipad|apk|xapk|apks|apkm|ipa|mod|modded|premium|unlocked)\b", " ",
                  text.replace("_", " ").replace(".", " "))
    return re.sub(r"\s+", " ", text).strip(" -")


# --- Android ------------------------------------------------------------------------------------------------------

def _safe(meta: Meta, name: str, func, *args, **kwargs):
    """One local reader or outside source; a crash in it becomes a warning and an empty result, never a failed upload."""

    try:
        return func(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - an undocumented page/format changed under us
        meta.warnings.append(f"forrás hiba ({name}): {type(exc).__name__}")
        return {}


def _android(path: Path, meta: Meta, hints: dict) -> None:
    data = _safe(meta, "APK", apk_reader.inspect, path) or {}
    meta.warnings += data.get("warnings", [])
    meta.platform = "Android"
    meta.extra["platform_tag"] = "[Android]"
    meta.extra["format"] = data.get("format", "APK")
    package = data.get("package") or hints.get("package", "")
    meta.version = data.get("version_name") or str(hints.get("version") or "")
    if package:
        meta.identifiers["package"] = package
    if data.get("version_code"):
        meta.extra["version_code"] = str(data["version_code"])
    min_os = apk_reader.android_version(data.get("min_sdk"))
    play = _safe(meta, "Google Play", googleplay.app, package) if package else {}
    fd = _safe(meta, "F-Droid", fdroid.lookup, package) if package else {}
    if play:
        meta.add_source("Google Play")
        meta.add_proof(play["url"])
    if fd:
        meta.add_source("F-Droid")
        meta.add_proof(fd["url"])
    if not package:
        meta.warnings.append("a csomagnevet nem sikerült kiolvasni (nincs aapt / sérült APK)")
    elif not play and not fd:
        meta.warnings.append(f"a csomag ({package}) nincs fent a Google Play-en / F-Droidon — igazoló linket adj meg kézzel")
    labels = data.get("labels") or {}
    label = data.get("label") or data.get("bundle_name") or ""
    play_name = app_name(play.get("name", ""), labels.get("hu") or label)
    meta.title = label or fd.get("name", "") or play_name
    meta.title_hu = labels.get("hu", "") or (play_name if translate.is_hungarian(play.get("description", "")) else "")
    meta.publisher = play.get("developer") or fd.get("author", "")
    meta.creators = [meta.publisher] if meta.publisher else []
    meta.genres = [g for g in [play.get("category", "")] + list(fd.get("categories") or []) if g][:3]
    meta.date = play.get("updated", "")
    meta.year = meta.date[:4]
    meta.cover_url = play.get("icon") or fd.get("icon", "")
    hungarian = bool(data.get("hungarian")) or translate.is_hungarian(play.get("description", "")) \
        or str(fd.get("description_locale", "")).startswith("hu")
    meta.languages = ["magyar"] if hungarian else []
    meta.extra["hungarian"] = "igen" if hungarian else "nem"
    if not hungarian and data.get("hungarian_resources"):
        meta.warnings.append("az APK-ban vannak magyar erőforrások, de ezek a beépített könyvtárakból is jöhetnek — "
                             "ellenőrizd, magyar-e a felület")
    if data.get("locales"):
        meta.extra["ui_languages"] = str(len(data["locales"]))
    meta.extra["min_os"] = min_os or (f"Android {play['min_android']}" if play.get("min_android") else "")
    abis = data.get("abis") or []
    meta.extra["abis"] = ", ".join(abis) if abis else "univerzális (nincs natív kód)"
    meta.requirements = ", ".join(x for x in (f"{meta.extra['min_os']} vagy újabb" if meta.extra["min_os"] else "",
                                              meta.extra["abis"] if abis else "") if x)
    meta.extra["signature"] = _signature(data, fd, meta.publisher)
    for key in ("rating", "installs", "content_rating"):
        if play.get(key):
            meta.extra[f"play_{key}"] = str(play[key])
    if play.get("recent_changes"):
        meta.extra["recent_changes"] = play["recent_changes"]
    if fd.get("license"):
        meta.extra["license"] = fd["license"]
    screens = play.get("screenshots") or fd.get("screenshots") or []
    if screens:
        meta.extra["store_screenshots"] = "\n".join(screens[:8])
    if data.get("obb"):
        meta.extra["obb"] = ", ".join(data["obb"])
    _android_description(meta, play, fd)
    split = data.get("format") in {"XAPK", "APKS", "APKM"}
    meta.install_hu = INSTALL_SPLIT.format(fmt="." + data["format"].lower()) if split else INSTALL_APK
    if meta.extra["signature"].startswith(("módosított", "ismeretlen")):
        meta.install_hu += "\n" + INSTALL_RESIGNED
    meta.title = meta.title or hints.get("fallback_title", "")
    name = hints.get("title") or play_name or fd.get("name") or meta.title_hu or meta.title
    _finish(meta, "[Android]", name, bool(package), bool(play or fd))


def _android_description(meta: Meta, play: dict, fd: dict) -> None:
    play_text = play.get("description", "")
    if play_text and translate.is_hungarian(play_text):
        meta.description_hu, meta.description_source = play_text, "Google Play"
        return
    if fd.get("description") and fd.get("description_locale", "").startswith("hu"):
        meta.description_hu, meta.description_source = fd["description"], "F-Droid"
        meta.description_en = fd.get("description_en", "")
        return
    english = play_text or fd.get("description_en") or fd.get("description", "")
    source = "Google Play" if play_text else "F-Droid"
    _translated(meta, english, source)


def _signer_words(dn: str) -> set[str]:
    words = set()
    for part in re.findall(r"(?:^|,\s*)(?:CN|O|OU)=([^,]+)", dn or ""):
        words |= {w for w in re.findall(r"[a-z0-9]{3,}", part.lower()) if w not in {"inc", "llc", "ltd", "gmbh", "unknown", "android", "the"}}
    return words


def _signature(data: dict, fd: dict, developer: str) -> str:
    status, text = data.get("signature") or ("ismeretlen", "nincs adat")
    signers = (data.get("certs") or {}).get("signers") or []
    if status != "érvényes" or not signers:
        return f"{status}: {text}"
    signer = signers[0]
    if fd and fdroid.signer_matches(fd, signer.get("sha256", "")):
        return f"eredeti: az F-Droid build aláírása (SHA-256 {signer['sha256'][:16]}…)"
    dev_words = {w for w in re.findall(r"[a-z0-9]{3,}", (developer or "").lower()) if w not in {"inc", "llc", "ltd", "gmbh", "the"}}
    if dev_words and dev_words & _signer_words(signer.get("dn", "")):
        return f"eredeti (valószínű): az aláíró ({signer['dn']}) egyezik a fejlesztővel ({developer})"
    return (f"ismeretlen: érvényes aláírás, de az aláíró ({signer.get('dn', '')}) nem azonosítható a fejlesztővel"
            f"{f' ({developer})' if developer else ''} — újraaláírt/módosított is lehet, ellenőrizd")


# --- iOS ----------------------------------------------------------------------------------------------------------

def _ios(path: Path, meta: Meta, hints: dict) -> None:
    data = _safe(meta, "IPA", ipa_reader.inspect, path) or {}
    meta.warnings += data.get("warnings", [])
    meta.platform = "iOS"
    devices = data.get("devices") or []
    tag = "[iPad]" if devices == ["iPad"] else "[iPhone]"
    meta.extra["platform_tag"] = tag
    meta.extra["format"] = "IPA"
    bundle = data.get("bundle_id") or hints.get("package", "")
    meta.version = data.get("version") or str(hints.get("version") or "")
    if bundle:
        meta.identifiers["bundle_id"] = bundle
    if data.get("build") and data.get("build") != meta.version:
        meta.extra["build"] = data["build"]
    store = _safe(meta, "App Store", itunes.lookup, bundle) if bundle else {}
    if store:
        meta.add_source("App Store")
        meta.add_proof(store["url"])
        meta.identifiers["appstore"] = str(store.get("track_id") or "")
        if store.get("version") and meta.version and store["version"] != meta.version:
            meta.extra["store_version"] = store["version"]
    elif bundle:
        meta.warnings.append(f"az alkalmazás ({bundle}) nincs a magyar App Store-ban — igazoló linket adj meg kézzel")
    store_name = app_name(store.get("name", ""), data.get("name", ""))
    meta.title = data.get("name") or store_name
    meta.title_hu = store_name if translate.is_hungarian(store.get("description", "")) else ""
    meta.publisher = store.get("seller") or store.get("developer", "")
    meta.creators = [meta.publisher] if meta.publisher else []
    meta.genres = list(store.get("genres") or [])[:3]
    meta.date = store.get("updated", "")
    meta.year = meta.date[:4]
    meta.cover_url = store.get("icon", "")
    hungarian = bool(data.get("hungarian")) or bool(store.get("hungarian"))
    meta.languages = ["magyar"] if hungarian else []
    meta.extra["hungarian"] = "igen" if hungarian else "nem"
    meta.extra["min_os"] = f"iOS {data['min_os']}" if data.get("min_os") else (f"iOS {store['min_os']}" if store.get("min_os") else "")
    meta.extra["devices"] = ", ".join(devices)
    meta.requirements = f"{meta.extra['min_os']} vagy újabb" if meta.extra["min_os"] else ""
    if data.get("encrypted") is True:
        meta.extra["signature"] = "FairPlay-titkosított (nem dekódolt) IPA: csak a vásárló Apple ID-jával fut"
    elif data.get("encrypted") is False:
        meta.extra["signature"] = "dekódolt IPA (sideloadolható)"
    screens = (store.get("screenshots") or []) + (store.get("ipad_screenshots") or [])
    if screens:
        meta.extra["store_screenshots"] = "\n".join(screens[:8])
    if store.get("release_notes"):
        meta.extra["recent_changes"] = store["release_notes"]
    text = store.get("description", "")
    if text and translate.is_hungarian(text):
        meta.description_hu, meta.description_source = text, "App Store"
    else:
        _translated(meta, text, "App Store")
    meta.install_hu = INSTALL_IOS + ("\n4. Ez az IPA FairPlay-titkosított: csak a vásárló Apple ID-jával indul." if data.get("encrypted") else "")
    meta.title = meta.title or hints.get("fallback_title", "")
    name = hints.get("title") or store_name or meta.title
    _finish(meta, tag, name, bool(bundle), bool(store))


# --- shared ---------------------------------------------------------------------------------------------------------

def _translated(meta: Meta, english: str, source: str) -> None:
    if not english:
        meta.warnings.append("nem találtam leírást az alkalmazáshoz — írd meg a leírásban (--edit)")
        return
    meta.description_en, meta.description_source = english, source
    text, translated = translate.to_hungarian(english)
    if translated:
        meta.description_hu, meta.machine_translated = text, True
    else:
        meta.warnings.append("csak angol leírás van (a bolt nem ad magyart, gépi fordítás nélkül) — fordítsd le vagy add meg: --description")


def _finish(meta: Meta, tag: str, name: str, local: bool, store: bool) -> None:
    name = re.sub(r"\s+", " ", re.sub(r"[™®©]", "", name or "")).strip()
    version = meta.version.strip()
    label = (version if version.lower().startswith("v") else f"v{version}") if version else ""
    meta.extra["torrent_name"] = " ".join(p for p in (tag, name, label) if p)
    meta.warnings.append(SCREENSHOT_WARNING)
    meta.confidence = 0.9 if local and store else 0.6 if local else 0.4 if store else 0.1


def _name_only(release, meta: Meta, hints: dict) -> None:
    name = release.name
    ios = bool(re.search(r"(?i)\b(ios|iphone|ipad|ipa)\b", name))
    meta.platform = "iOS" if ios else "Android"
    tag = "[iPhone]" if ios else "[Android]"
    meta.extra["platform_tag"] = tag
    version = re.search(r"(?i)(?<![a-z0-9])v?(\d+(?:\.\d+){1,3})(?![0-9])", name)
    meta.version = str(hints.get("version") or (version.group(1) if version else ""))
    title = hints.get("title") or re.sub(r"(?i)[._]|\b(android|ios|iphone|ipad|apk|ipa|mod|premium|unlocked)\b|v?\d+(\.\d+)+.*$", " ", name)
    title = re.sub(r"\s+", " ", title).strip(" -")
    meta.title = title
    if ios and title:
        hits = _safe(meta, "App Store", itunes.search, title, limit=3) or []
        hit = next((h for h in hits if h["name"].lower() == title.lower()), None)
        if hit:
            meta.add_source("App Store")
            meta.add_proof(hit["url"])
            meta.identifiers["bundle_id"] = hit["bundle_id"]
            meta.publisher = hit["seller"]
            meta.cover_url = hit["icon"]
            if translate.is_hungarian(hit["description"]):
                meta.description_hu, meta.description_source = hit["description"], "App Store"
            else:
                _translated(meta, hit["description"], "App Store")
    meta.install_hu = INSTALL_IOS if ios else INSTALL_APK
    _finish(meta, tag, title, False, bool(meta.proof_links))
