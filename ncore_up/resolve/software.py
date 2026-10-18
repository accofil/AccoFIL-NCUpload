"""Resolver for PC programs: nCore "misc" (Program) and "iso" (Program ISO, Windows install media included).

Chain (local facts first, nothing from the release is ever executed):
    NFO / file_id.diz -> release name -> PE / MSI / installer metadata (+ Authenticode signer, payload architecture)
    -> winget (MSI code, official installer SHA-256, or name + publisher agreeing with PE company / signer)
    -> Chocolatey -> Homebrew (macOS) -> Wikidata (official site, Hungarian label/description, logo) -> huwiki summary.
Windows install ISOs: WIM metadata (editions, build -> 24H2/25H2 ..., languages, arch), the ISO SHA-256 against
Microsoft's published table, the Microsoft download page as proof link.

The Meta carries title, version, publisher, Hungarian description, requirements, proof links, cover, a Hungarian
install guide (``install_hu``, steps only: compose adds the "Telepítés:" header) and in ``extra``: "arch"
(x86 / x64 / x86-x64 / arm64), "license", "torrent_name" ("Programnév vX.Y x64"), "installer", "signer", "min_os".
"""

from __future__ import annotations

import hashlib
import re
import tempfile
from pathlib import Path

from .. import net, translate
from ..local import installer, msi, nfo, pe, winiso
from ..meta import Meta
from ..sources import chocolatey, homebrew, msdownload, wikidata, wikipedia, winget

# --- release name ----------------------------------------------------------------------------------------------------

ARCH_TOKENS = {"x64": "x64", "win64": "x64", "amd64": "x64", "64bit": "x64", "64-bit": "x64", "x86": "x86", "x32": "x86", "win32": "x86",
               "i386": "x86", "i686": "x86", "32bit": "x86", "32-bit": "x86", "arm64": "arm64", "aarch64": "arm64"}
BOTH_RE = re.compile(r"(?i)^(?:x86|x32)[-+&_]?(?:x64)$|^x64[-+&_]?(?:x86|x32)$|^(?:32|64)[-+&]?(?:64|32)[-_]?bits?$")
PLATFORM_TOKENS = {"winall": "Windows", "win7": "Windows", "win8": "Windows", "win10": "Windows", "win11": "Windows", "winxp": "Windows",
                   "macos": "macOS", "macosx": "macOS", "osx": "macOS", "mac": "macOS", "u2b": "macOS", "ub": "macOS", "linux": "Linux",
                   "android": "Android", "ios": "iOS"}
ACTIVATION = {"keygen": "Keygen", "keymaker": "Keymaker", "keyfilemaker": "Keyfilemaker", "patch": "Patch", "crack": "Crack",
              "cracked": "Crack", "serial": "Serial", "activator": "Activator", "loader": "Loader", "regged": "Regged",
              "preactivated": "Preactivated", "pre-activated": "Preactivated", "license": "License", "unlocker": "Unlocker"}
FLAGS = {"incl", "including", "retail", "repack", "repacked", "portable", "bootable", "proper", "dirfix", "nfofix", "readnfo",
         "multilingual", "multilanguage", "multilang", "hun", "hungarian", "magyar", "x64only"}
WEAK_FLAGS = {"and", "only", "update", "final", "lite", "silent", "unattended", "dvd", "iso", "activated", "eng", "english", "read", "nfo"}
VERSION_RE = re.compile(r"(?i)^v?(\d+(?:\.\d+)+[a-z]?|\d{2}H\d)$|^v(\d+[a-z]?)$")
GENERIC_PRODUCTS = {"setup", "installer", "install", "setup launcher", "nullsoft install system", "7-zip sfx", "winrar sfx", "setup program"}


def _merge_numeric(tokens: list[str]) -> list[str]:
    """Dotted scene names lose their version dots on splitting: ['v5', '7', '8'] -> ['v5.7.8']."""

    out: list[str] = []
    for token in tokens:
        if out and re.fullmatch(r"\d+[a-z]?", token) and re.fullmatch(r"(?i)v?\d+(?:\.\d+)*", out[-1]) and not re.fullmatch(r"(?i)x(?:86|64|32)", out[-1]):
            out[-1] += "." + token
        else:
            out.append(token)
    return out


def parse_name(name: str) -> dict:
    """Program facts from a scene / P2P release name:

    'APU.Software.TrueGain.v5.7.8.x64.WinALL.Incl.Keygen-BLiZZARD' -> {"title": "APU Software TrueGain", "version": "5.7.8",
    "arch": "x64", "platform": "Windows", "activation": ["Keygen"], "group": "BLiZZARD", ...}
    Keys: title, version, build, arch, archs, platform, activation, flags, languages, hungarian, portable, iso, group, extra."""

    text = re.sub(r"(?i)\.(iso|rar|zip|7z|exe|msi|dmg|pkg)$", "", (name or "").strip())
    text = re.sub(r"(?i)x86[_-]64(?!\d)", "x64", text)  # x86_64 / x86-64 is AMD64, unlike x86-x64
    group = ""
    match = re.search(r"-([A-Za-z0-9]{2,15})$", text)
    if match and not re.fullmatch(r"(?i)x(?:86|64|32)|arm64|\d+(?:\.\d+)*|bit|bits", match.group(1)) and not text[: match.start()].endswith(" "):
        group, text = match.group(1), text[: match.start()]
    spaced = " " in text
    tokens = [t for t in re.split(r"[ _]+" if spaced else r"[._ ]+", text) if t]
    if not spaced:
        tokens = _merge_numeric(tokens)
    result: dict = {"title": "", "version": "", "build": "", "arch": "", "archs": [], "platform": "", "activation": [], "flags": [],
                    "languages": [], "hungarian": False, "portable": False, "iso": False, "group": group, "extra": []}
    title: list[str] = []
    stopped = False
    i = 0
    while i < len(tokens):
        token = tokens[i].strip("()[]{}") or tokens[i]  # P2P names: "(x64)", "[Multilingual]"
        low = token.lower()
        nxt = tokens[i + 1].strip("()[]{}").lower() if i + 1 < len(tokens) else ""
        if BOTH_RE.match(token) or (low in {"x86", "x32"} and nxt == "x64") or (low == "x64" and nxt in {"x86", "x32"}):
            result["archs"] += ["x86", "x64"]
            i += 1 if BOTH_RE.match(token) else 2
            stopped = True
            continue
        if low in ARCH_TOKENS and (low not in {"win32", "win64"} or stopped or title):
            result["archs"].append(ARCH_TOKENS[low])
            if low in {"win32", "win64"}:
                result["platform"] = result["platform"] or "Windows"
            stopped = True
        elif (title or stopped) and (low in PLATFORM_TOKENS or re.fullmatch(r"win(?:all|xp|vista|7|8|8\.1|10|11)(?:[-_+.]?(?:10|11))?", low)):
            result["platform"] = result["platform"] or PLATFORM_TOKENS.get(low, "Windows")
            stopped = True
        elif low in ACTIVATION and (title or stopped):  # a leading word is the program's name ("Unlocker", "Patch My PC")
            result["activation"].append(ACTIVATION[low])
            stopped = True
        elif low == "build" and i + 1 < len(tokens) and re.fullmatch(r"\d+(?:\.\d+)*", tokens[i + 1]):
            result["build"] = tokens[i + 1]
            i += 2
            stopped = True
            continue
        elif not result["version"] and VERSION_RE.match(token) and (title or i > 0):
            result["version"] = (VERSION_RE.match(token).group(1) or VERSION_RE.match(token).group(2))
            stopped = True
        elif low in FLAGS or re.fullmatch(r"multi\d*", low) or (low in WEAK_FLAGS and stopped):
            result["flags"].append(token)
            if low in {"hun", "hungarian", "magyar"}:
                result["hungarian"] = True
                result["languages"].append("magyar")
            elif low.startswith("multi"):
                result["languages"].append("többnyelvű")
            elif low in {"eng", "english"}:
                result["languages"].append("angol")
            result["portable"] |= low == "portable"
            result["iso"] |= low == "iso"
            stopped = True
        elif not stopped:
            title.append(token)
        else:
            result["extra"].append(token)
        i += 1
    if not result["version"]:
        # a version without 'v' and without dots ("Photoshop 2025") stays in the title; a trailing dotted number is the version
        for index in range(len(title) - 1, 0, -1):
            if re.fullmatch(r"\d+(?:\.\d+)+", title[index]):
                result["version"] = title[index]
                result["extra"] = title[index + 1:] + result["extra"]
                title = title[:index]
                break
    if result["build"] and not re.fullmatch(r"(?i)\d{2}H\d", result["version"]):  # Windows: "25H2" stays, the build is kept apart
        result["version"] = f"{result['version']}.{result['build']}" if result["version"] else result["build"]
    result["title"] = " ".join(title).strip(" -")
    result["archs"] = list(dict.fromkeys(result["archs"]))
    result["arch"] = pe.combine_arch(result["archs"])
    result["languages"] = list(dict.fromkeys(result["languages"]))
    if not result["platform"] and result["archs"]:
        result["platform"] = "Windows"
    return result


def torrent_name(title: str, version: str = "", arch: str = "", *, hungarian: bool = False) -> str:
    """nCore program name: 'Programnév vX.Y x64' ('build' is never written; x86 / x64 / x86-x64 is mandatory)."""

    title = re.sub(r"\s*:\s*", " - ", title or "").strip()
    title = re.sub(r"(?i)\s+build\b", "", title)
    version = re.sub(r"(?i)[\s._-]*build[\s._-]*", ".", (version or "").strip()).strip(". ")
    version = re.sub(r"(?i)^v(?=\d)", "", version)
    parts = [title]
    if version:
        parts.append("v" + version)
    if hungarian:
        parts.append("HUN")
    if arch:
        parts.append(arch)
    return re.sub(r"\s{2,}", " ", " ".join(p for p in parts if p)).strip()


# --- Hungarian texts ---------------------------------------------------------------------------------------------------

ARCH_HU = {"x64": "64 bites (x64)", "x86": "32 bites (x86; 64 bites Windowson is fut)", "x86-x64": "32 és 64 bites (x86 és x64)",
           "arm64": "ARM64", "arm": "ARM"}
LICENSES = [
    (r"(?i)free for (?:personal|private|home|non-?commercial)|non-?commercial", "magáncélra ingyenes"),
    (r"(?i)^(freeware|free)$|ingyenes", "ingyenes (freeware)"), (r"(?i)shareware", "shareware"), (r"(?i)trial|demo|próba", "próbaverzió"),
    (r"(?i)\b(L?GPL|AGPL|MIT|BSD|Apache|MPL|Unlicense|ISC|CC0|EPL|Zlib|Artistic)\b", "nyílt forráskódú ({})"),
    (r"(?i)general public license|mozilla public license|\bopen[ -]?source\b", "nyílt forráskódú ({})"),  # "GNU General Public License Version 2"
    (r"(?i)commercial|kereskedelmi", "kereskedelmi"), (r"(?i)proprietary|copyright|eula|all rights", "zárt forráskódú"),
]
STEP_PHRASES = [
    (r"^(?:unpack|unrar|extract)(?: the)?(?: release| files| archives?| rars?)?(?:,| and)? install(?: the)?(?: program| software| application| app)?\W*$",
     "Csomagold ki a fájlokat, majd telepítsd a programot."),
    (r"^(?:unpack|unrar|extract)(?: the)?(?: release| files| archives?| rars?)?\W*$", "Csomagold ki a fájlokat."),
    (r"^install(?: the)?(?: program| software| application| app| game)?(?: normally)?\W*$", "Telepítsd a programot."),
    (r"^(?:run|start|launch)(?: the)? (?:setup|installer|setup\.exe|install\.exe)(?: and install)?.*$", "Futtasd a telepítőt, és telepítsd a programot."),
    (r"^(?:do not|don't|dont) (?:run|launch|start|open)(?: the)? (?:program|software|application|app)(?: yet)?.*$", "Még ne indítsd el a programot."),
    (r"^(?:use|run|start)(?: our| the)? (?:keygen|keymaker|key ?gen(?:erator)?)(?: to (?:register|activate|generate).*)?.*$",
     "Futtasd a mellékelt keygent, és regisztráld (aktiváld) vele a programot."),
    (r"^(?:copy|apply|replace)(?: the)? (?:crack(?:ed)?|patch(?:ed)?|content|files?|dll).*(?:to|into|in)(?: the)? (?:install(?:ation)?|program|app(?:lication)?|game) (?:dir|folder|directory).*$",
     "Másold a mellékelt (crack) fájlokat a program telepítési mappájába, a meglévőket felülírva."),
    (r"^(?:run|use|apply)(?: the)? patch.*$", "Futtasd a mellékelt patch-et a program telepítési mappájában (rendszergazdaként)."),
    (r"^block.*(?:firewall|internet|outbound).*$", "Tiltsd le a program internetelérését a tűzfalban."),
    (r"^(?:do not|don't|dont|never) update.*$", "Ne frissítsd a programot."),
    (r"^(?:mount|burn)(?: or (?:mount|burn))?(?: the)? (?:iso|image|disc).*$", "Csatold fel (vagy írd ki) a lemezképet."),
    (r"^register(?: it| the program)? with (?:any|the|your) (?:name|serial|key|data).*$", "Regisztráld a programot a megadott (vagy tetszőleges) adatokkal."),
    (r"^(?:restart|reboot).*$", "Indítsd újra a számítógépet."),
    (r"^(?:that'?s it|done|finished|ready)\W*$", "Kész."),
    (r"^(?:enjoy|have fun|play)\W*.*$", "Jó használatot!"),
]


def license_hu(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    for pattern, label in LICENSES:
        match = re.search(pattern, text)
        if match:
            return label.format(text) if "{}" in label else label
    return text


def translate_steps(steps: list[str]) -> list[str]:
    """NFO install steps in Hungarian via the phrase table; [] when any step is not covered (then templates are used)."""

    out = []
    for step in steps:
        clean = re.sub(r"\s+", " ", step.strip()).lower()
        hit = next((hu for pattern, hu in STEP_PHRASES if re.match(pattern, clean)), None)
        if hit is None:
            return []
        if not out or out[-1] != hit:
            out.append(hit)
    return out


def _numbered(steps: list[str]) -> str:
    return "\n".join(f"{index}. {step}" for index, step in enumerate([s for s in steps if s], 1))


def activation_step(facts: dict) -> list[str]:
    tools = facts.get("tools") or []
    dirs = facts.get("tool_dirs") or []
    kinds = {k.lower() for k in facts.get("activation") or []}
    names = " ".join(tools + dirs).lower()
    where = f" (mappa: {dirs[0]})" if dirs else ""
    keygen_re, patch_re = r"(?<![a-z])(?:keygen|keymaker|keyfilemaker|key[ ._-]?gen|kg)(?![a-z])", r"(?<![a-z])patch(?:er)?(?![a-z])"

    def tool(pattern: str) -> str:  # the matching tool's file name ('libpkg.dll' is no keygen, 'dispatcher.exe' no patch)
        name = next((Path(t).name for t in tools if re.search(pattern, Path(t).name.lower())), Path(tools[0]).name if tools else "")
        return f" ({name})" if name else ""

    if "keygen" in kinds or "keymaker" in kinds or "keyfilemaker" in kinds or re.search(keygen_re, names):
        return [f"Futtasd a mellékelt keygent{tool(keygen_re)}{where}, és a kapott adatokkal regisztráld (aktiváld) a programot."]
    if "patch" in kinds or re.search(patch_re, names):
        return [f"Másold a mellékelt patch-et{tool(patch_re)}{where} a program telepítési mappájába, és futtasd rendszergazdaként."]
    if kinds & {"crack", "loader", "unlocker"} or re.search(r"(?<![a-z])(?:crack(?:ed)?|loader)(?![a-z])", names) or dirs:
        source = dirs[0] if dirs else "Crack"
        return [f"Másold a(z) {source} mappa tartalmát a program telepítési mappájába, a meglévő fájlokat felülírva."]
    if "serial" in kinds or re.search(r"(?<![a-z])serial(?![a-z])", names):
        return ["A regisztrációhoz használd a mellékelt sorozatszámot."]
    return []


def install_guide(kind: str, facts: dict) -> str:
    """Hungarian install steps for: setup, msi, portable, iso, windows_iso, mac, linux (facts: file names, tools, ...)."""

    activation = activation_step(facts)
    archive = facts.get("archive", "")
    unpack = [f"Csomagold ki a(z) {archive} archívumot (pl. 7-Zip vagy WinRAR segítségével)."] if archive else []
    nfo_steps = translate_steps(facts.get("nfo_steps") or [])
    if kind == "windows_iso":
        iso = facts.get("iso") or ("" if facts.get("folder") else "ISO")
        editions = facts.get("editions") or ""
        if iso:
            first = [f"Készíts bootolható pendrive-ot (legalább 8 GB) a(z) {iso} fájlból a Rufus vagy a Ventoy segítségével "
                     "(Ventoynál elég az ISO-t a pendrive-ra másolni)."]
            update = "Meglévő Windows frissítéséhez elég felcsatolni az ISO-t (jobb klikk → Csatlakoztatás), és futtatni a setup.exe-t."
        else:  # an unpacked ISO (folder release)
            first = [f"Készíts a(z) {facts['folder']} mappából bootolható pendrive-ot (legalább 8 GB): a legegyszerűbb, ha előbb ISO-fájlt "
                     "készítesz belőle (pl. AnyBurn vagy ImgBurn segítségével), és azt írod ki a Rufus vagy a Ventoy segítségével. FAT32-re "
                     "formázott pendrive-ra a mappa tartalma közvetlenül is átmásolható, ha az install.wim / install.esd 4 GB-nál kisebb."]
            update = "Meglévő Windows frissítéséhez elég a mappában található setup.exe-t futtatni."
        steps = first + ["Indítsd a gépet a pendrive-ról (a boot menü általában F8, F11, F12 vagy Esc gombbal jön elő).",
                         "Válaszd ki a nyelvet és a kiadást" + (f" ({editions})" if editions else "") + ", majd kövesd a telepítő lépéseit.",
                         update, "A használathoz érvényes Windows-licenc (termékkulcs vagy digitális licenc) szükséges."]
        return _numbered(steps)
    if kind == "iso":
        iso = facts.get("iso") or "ISO"
        setup = facts.get("setup") or "setup.exe"
        steps = unpack + [f"Csatold fel a(z) {iso} képfájlt (Windows 10/11 alatt jobb klikk → Csatlakoztatás), vagy írd ki lemezre.",
                          f"Futtasd a lemezen található {setup} telepítőt, és kövesd a telepítő lépéseit."]
        if activation:
            steps += ["A telepítés végén még ne indítsd el a programot."] + activation
        steps += ["Indítsd el a programot; a képfájl ezután leválasztható."]
        return _numbered(steps)
    if kind == "mac":
        image = facts.get("mac") or "DMG"
        app = facts.get("app") or "az alkalmazást"
        steps = unpack + [f"Nyisd meg a(z) {image} fájlt.",
                          f"Húzd a(z) {app} alkalmazást az Alkalmazások (Applications) mappába (.pkg esetén futtasd a telepítőt).",
                          "Ha első indításkor a rendszer letiltja: Rendszerbeállítások → Adatvédelem és biztonság → „Megnyitás mindenképp”."]
        return _numbered(steps + activation)
    if kind == "linux":
        file = facts.get("linux") or "a csomagot"
        low = file.lower()
        if low.endswith(".appimage"):
            steps = [f"Tedd futtathatóvá: chmod +x {file}", f"Indítsd el: ./{file}"]
        elif low.endswith(".deb"):
            steps = [f"Telepítsd: sudo apt install ./{file}"]
        elif low.endswith(".rpm"):
            steps = [f"Telepítsd: sudo dnf install ./{file}"]
        else:
            steps = [f"Csomagold ki a(z) {file} fájlt, és kövesd a mellékelt README utasításait."]
        return _numbered(unpack + steps)
    if kind == "portable":
        exe = facts.get("exe") or "a program EXE"
        steps = unpack + ["A program hordozható (portable), telepítést nem igényel: másold a mappáját tetszőleges helyre (akár pendrive-ra).",
                          f"Indítsd el a(z) {exe} fájlt."]
        return _numbered(steps + activation)
    if nfo_steps and kind in {"setup", "msi", "archive", "other"}:
        if unpack and nfo_steps[0].startswith("Csomagold ki"):  # the archive step already says it
            nfo_steps = (["Telepítsd a programot."] if "telepítsd" in nfo_steps[0] else []) + nfo_steps[1:]
        if activation and not any(re.search(r"(?i)keygen|patch|crack|regisztr|aktiv", step) for step in nfo_steps):
            tail = [step for step in nfo_steps if step in {"Jó használatot!", "Kész."}]  # the NFO forgot the bundled tool
            nfo_steps = [step for step in nfo_steps if step not in tail] + activation + tail
        return _numbered(unpack + nfo_steps)
    if kind == "msi":
        package = facts.get("msi") or "MSI"
        steps = unpack + [f"Futtasd a(z) {package} telepítőcsomagot (dupla kattintás, vagy parancssorból: msiexec /i \"{package}\"), "
                          "és kövesd a telepítő lépéseit."]
        if activation:
            steps += ["A telepítés végén még ne indítsd el a programot."] + activation
        return _numbered(steps + ["Indítsd el a programot a Start menüből."])
    setup = facts.get("setup", "")
    steps = unpack + [f"Telepítsd a programot: futtasd a(z) {setup} fájlt" if setup else "Telepítsd a programot: futtasd a telepítőt"]
    steps[-1] += (" rendszergazdaként" if facts.get("admin") else "") + ", és kövesd a telepítő lépéseit."
    if activation:
        steps += ["A telepítés végén még ne indítsd el a programot."] + activation
    return _numbered(steps + ["Indítsd el a programot a Start menüből vagy az asztali parancsikonnal."])


def windows_name(version: str) -> str:
    """winget MinimumOSVersion / PE version -> Hungarian-friendly name ('10.0.17763.0' -> 'Windows 10 (1809)')."""

    numbers = [int(x) for x in re.findall(r"\d+", version or "")]
    if len(numbers) < 2:
        return ""
    if numbers[0] >= 10:
        build = numbers[2] if len(numbers) > 2 else 0
        if build >= 22000:
            known = msdownload.version_for_build(build, refresh=False)
            return "Windows 11" + (f" ({known['version']})" if known.get("version") and build > 22000 else "")
        known = msdownload.version_for_build(build, refresh=False) if build > 10240 else {}
        return "Windows 10" + (f" ({known['version']})" if known.get("version") else "")
    return pe.os_name(numbers[0], numbers[1])


def requirements_text(*, os_name: str = "", arch: str = "", platform: str = "Windows", nfo_text: str = "", extra: str = "") -> str:
    lines = []
    if os_name:
        lines.append(f"Operációs rendszer: {os_name} vagy újabb")
    elif platform and platform != "Windows":
        lines.append(f"Operációs rendszer: {platform}")
    if arch:
        lines.append(f"Architektúra: {ARCH_HU.get(arch, arch)}")
    if extra:
        lines.append(extra)
    if nfo_text:
        lines.append("Az NFO szerint:\n" + nfo_text.strip())
    return "\n".join(lines)


# --- helpers ---------------------------------------------------------------------------------------------------------

def _sha256(path: Path, limit: int = 2 * 1024**3) -> str:
    try:
        if path.stat().st_size > limit:
            return ""
        digest = hashlib.sha256()
        with open(path, "rb") as fh:
            for block in iter(lambda: fh.read(8 * 1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()
    except OSError:
        return ""


def _clean_company(text: str) -> str:
    text = re.sub(r"\S+@\S+", "", text or "")
    return re.sub(r"\s{2,}", " ", text).strip(" ,;-")


def _same_name(a: str, b: str) -> bool:
    x, y = winget.normalize(a), winget.normalize(b)
    return bool(x and y) and (x == y or (min(len(x), len(y)) >= 4 and (x in y or y in x)) or winget.similarity(x, y) >= 90)


def _lowest_os(binaries: list[dict]) -> str:
    """Minimum Windows of a set of setups / programs: the lowest PE OS version wins ('' when it predates XP)."""

    versions = [tuple(int(x) for x in re.findall(r"\d+", b["pe"].get("os_version", ""))[:2]) for b in binaries]
    versions = [v for v in versions if len(v) == 2]
    return pe.os_name(*min(versions)) if versions else ""


def _same_version(a: str, b: str) -> bool:
    na, nb = pe._numbers(a), pe._numbers(b)
    return bool(na) and bool(nb) and (na == nb or na[: len(nb)] == nb or nb[: len(na)] == na)


def _host(url: str) -> str:
    match = re.match(r"(?i)^(?:https?://)?(?:www\.)?([^/:?#]+)", (url or "").strip())
    return match.group(1).lower() if match else ""


def _public_url(url: str) -> bool:
    """An http(s) link to a public host name (NFO text is untrusted: no IP literals, localhost or LAN names)."""

    match = re.match(r"(?i)^https?://([^/:?#\s]+)", (url or "").strip())
    host = match.group(1).lower().rstrip(".") if match else ""
    return ("." in host and not re.fullmatch(r"[\d.]+|\[.*\]", host) and host != "localhost"
            and not host.endswith((".local", ".lan", ".localdomain", ".home.arpa", ".internal", ".localhost")))


def _reachable(url: str) -> bool:
    response = net.request("HEAD", url, quiet=True, retries=1)
    if response is None:
        response = net.request("GET", url, quiet=True, retries=0)
    return response is not None


def _wikidata_search(name: str) -> list[dict]:
    data = net.get_json("https://www.wikidata.org/w/api.php", params={"action": "wbsearchentities", "search": name, "language": "en",
                                                                       "uselang": "hu", "type": "item", "limit": 7, "format": "json"},
                        quiet=True) or {}
    return [{"id": i.get("id", ""), "label": i.get("label", ""), "description": i.get("description", "")}
            for i in data.get("search") or [] if i.get("id")]


SOFTWARE_WORDS = re.compile(r"(?i)software|program|application|\bapp\b|editor|player|browser|archiver|utility|\btool|manager|client|suite|"
                            r"framework|driver|antivirus|compiler|ide\b|emulator|szoftver|alkalmazás|szerkesztő|lejátszó|böngésző|"
                            r"operating system|operációs rendszer|file|fájl")


def wikidata_software(name: str, homepage: str = "") -> dict:
    """parse_entity() of the Wikidata item of a program (label match + software description or the same website)."""

    wanted = winget.normalize(name)
    for hit in _wikidata_search(name)[:4]:
        if winget.similarity(wanted, winget.normalize(hit["label"])) < 90:
            continue
        raw = wikidata.entity(hit["id"])
        info = wikidata.parse_entity(raw) if raw else {}
        if not info:
            continue
        site = wikidata.claim(info, "website")
        description = hit["description"] + " " + info.get("description_hu", "") + " " + \
            (((raw.get("descriptions") or {}).get("en") or {}).get("value", ""))
        if (homepage and site and _host(site) == _host(homepage)) or SOFTWARE_WORDS.search(description):
            info["description_en_short"] = (((raw.get("descriptions") or {}).get("en") or {}).get("value", ""))
            return info
    return {}


# --- local inspection -----------------------------------------------------------------------------------------------

def _paths(release) -> list[tuple[str, int]]:
    return [(f.relpath, f.size) for f in release.files]


def _abs(release, rel: str) -> Path:
    root = Path(release.path)
    return root if root.is_file() else root / rel


def _inspect_binary(path: Path, facts: dict, *, role: str) -> None:
    """PE / installer / payload facts of one EXE into ``facts`` (role: setup, exe)."""

    info = pe.read(path, verify=path.stat().st_size <= 1024**3 if path.exists() else False, icon="icon" not in facts)
    if not info:
        return
    if info.get("icon") and "icon" not in facts:
        facts["icon"] = info.pop("icon")
    info.pop("icon", None)
    detected = installer.detect(path, info) or {}
    engine = detected.get("type", "exe")
    entry = {"path": path.name, "role": role, "pe": info, "engine": engine, "engine_label": detected.get("label", ""),
             "engine_version": detected.get("engine_version", "")}
    facts.setdefault("binaries", []).append(entry)
    if info.get("manifest_level") == "requireAdministrator":
        facts["admin"] = True
    if engine in {"nsis", "7z-sfx", "zip-sfx", "rar-sfx"}:
        listing = installer.list_archive(path)
        names = [e["path"] for e in listing.get("entries") or []]
        entry["payload_archs"] = installer.payload_archs(path, listing)
        entry["ui_languages"] = installer.ui_languages(names)
    elif engine == "inno":
        inno = installer.inno_info(path)  # innoextract lists names only: the payload architecture stays unknown
        entry["inno"] = inno
        entry["engine_version"] = inno.get("data_version") or entry["engine_version"]
        entry["ui_languages"] = installer.ui_languages([f["path"] for f in inno.get("files") or []])
        entry["payload_archs"] = installer.inno_payload_archs(path, inno)


def _inspect_archive_member(archive: Path, listing: dict, facts: dict, prefix: str) -> dict:
    """Classify the inside of an archive / ISO and inspect its main installer statically (copied out, never run)."""

    entries = [e for e in listing.get("entries") or [] if not e["folder"]]
    inner = installer.classify([(e["path"], e["size"]) for e in entries], facts.get("group", ""))
    member = (inner["setups"] or [""])[0] or (inner["msis"] or [""])[0] or (inner["exes"] or [""])[0]
    size = next((e["size"] for e in entries if e["path"] == member), 0)
    if member and size <= 512 * 1024 * 1024:
        with tempfile.TemporaryDirectory(prefix="ncore-up-sw-") as temp:
            copied = installer.extract_member(archive, member, temp, size)
            if copied is not None:
                if copied.suffix.lower() == ".msi":
                    facts.setdefault("msis_info", []).append(msi.read(copied))
                else:
                    _inspect_binary(copied, facts, role="setup" if member in inner["setups"] else "exe")
                    facts.setdefault("sha256", {})[copied.name] = _sha256(copied)
                    last = (facts.get("binaries") or [{}])[-1]
                    if last.get("path") == copied.name and last.get("engine") not in {"exe", None} and member not in inner["setups"]:
                        last["role"] = "setup"  # an installer engine inside: not a portable program
                        inner["setups"].insert(0, member)
                        inner["kind"] = "setup"
    inner["member"] = member
    inner["prefix"] = prefix
    return inner


def inspect_local(release, parsed: dict) -> dict:
    """Everything the files tell (no network): classification, PE/MSI/installer facts, NFO."""

    facts: dict = {"group": parsed.get("group", ""), "activation": list(parsed.get("activation") or [])}
    paths = _paths(release)
    cls = installer.classify(paths, parsed.get("group", ""))
    facts["classify"] = cls
    facts["kind"] = cls["kind"]
    facts["tools"], facts["tool_dirs"] = cls["tools"], cls["tool_dirs"]
    facts["nfo"] = nfo.read_release(release)
    facts["nfo_steps"] = facts["nfo"].get("install_steps") or []
    facts["hungarian_files"] = list(cls["hungarian_files"])
    facts["ui_languages"] = installer.ui_languages([p for p, _s in paths])
    if cls["kind"] == "windows_iso":
        return facts
    if cls["isos"]:
        media = winiso.inspect(_abs(release, cls["isos"][0]))  # UDF walk: a few KB of reads even for a 6 GB ISO
        if media.get("images"):
            facts["kind"], facts["winiso"] = "windows_iso", media
            return facts
    for rel in cls["setups"][:2]:
        path = _abs(release, rel)
        _inspect_binary(path, facts, role="setup")
        facts.setdefault("sha256", {})[path.name] = _sha256(path)
    for rel in cls["exes"][:3]:
        path = _abs(release, rel)
        before = len(facts.get("binaries", []))
        _inspect_binary(path, facts, role="exe")
        if len(facts.get("binaries", [])) > before and facts["binaries"][-1]["engine"] not in {"exe", ""}:
            facts["binaries"][-1]["role"] = "setup"
            cls["setups"].append(rel)
            facts.setdefault("sha256", {})[path.name] = _sha256(path)
    if cls["setups"] and facts["kind"] == "portable":
        facts["kind"] = "setup"
    for rel in cls["msis"][:2]:
        info = msi.read(_abs(release, rel))
        if info:
            facts.setdefault("msis_info", []).append(info)
        facts.setdefault("sha256", {})[Path(rel).name] = _sha256(_abs(release, rel))
    if cls["isos"]:
        iso = _abs(release, cls["isos"][0])
        listing = installer.list_archive(iso)
        inner = _inspect_archive_member(iso, listing, facts, cls["isos"][0])
        facts["inner"] = inner
        facts["hungarian_files"] += inner["hungarian_files"]
        facts["ui_languages"] += installer.ui_languages([e["path"] for e in listing.get("entries") or []])
    elif cls["archives"] and facts["kind"] in {"archive", "other"}:
        archive = _abs(release, cls["archives"][0])
        listing = installer.list_archive(archive)
        inner = _inspect_archive_member(archive, listing, facts, cls["archives"][0])
        facts["inner"] = inner
        facts["archive"] = Path(cls["archives"][0]).name
        facts["kind"] = {"setup": "setup", "msi": "msi", "portable": "portable", "iso": "iso", "windows_iso": "windows_iso"}.get(inner["kind"], "setup")
        facts["tools"] = facts["tools"] + inner["tools"]
        facts["tool_dirs"] = facts["tool_dirs"] + inner["tool_dirs"]
        facts["hungarian_files"] += inner["hungarian_files"]
        facts["ui_languages"] += installer.ui_languages([e["path"] for e in listing.get("entries") or []])
    return facts


def decide_arch(parsed: dict, facts: dict, manifest: dict) -> tuple[str, list[str]]:
    """The compatibility tag from the files: payload programs of NSIS/7z/archive installers, portable EXEs, MSI
    templates, official installers whose SHA-256 winget lists, and x64/ARM64 setup stubs (an x86 stub tells nothing).
    The release name is only the fallback (a disagreement is warned about). Returns (arch, warnings)."""

    strong: list[str] = []
    for binary in facts.get("binaries") or []:
        arch = binary["pe"].get("arch", "")
        if binary.get("payload_archs"):
            strong += binary["payload_archs"]
        elif binary["role"] == "exe" and binary["engine"] == "exe" and binary["pe"].get("subsystem") in {"gui", "console"}:
            strong.append(arch)
        elif arch in {"x64", "arm64"} or (binary["engine"] == "7z-sfx" and arch):
            strong.append(arch)
    strong += [info.get("arch", "") for info in facts.get("msis_info") or []]
    hashes = set((facts.get("sha256") or {}).values()) - {""}
    official = [item for item in manifest.get("installers") or [] if item.get("sha256") and item["sha256"] in hashes]
    strong += [{"x64": "x64", "x86": "x86", "arm64": "arm64", "neutral": "anycpu"}.get(item["arch"], "") for item in official]
    arch = pe.combine_arch(strong)
    named = parsed.get("arch", "")
    warnings = []
    if arch and named and named != arch:
        warnings.append(f"architektúra: a név szerint {named}, a fájlok szerint {arch} — a fájlok alapján {arch} került a névbe")
    if not arch and named:
        arch = named
    if not arch and manifest.get("matched_version") and len(manifest.get("archs") or []) == 1:
        arch = {"neutral": "x86-x64"}.get(manifest["archs"][0], manifest["archs"][0])
        warnings.append(f"architektúra a winget szerint: {arch} — ellenőrizd")
    if not arch and facts.get("binaries"):
        stubs = sorted({b["pe"].get("arch", "") for b in facts["binaries"]} - {""})
        warnings.append("architektúra: a telepítőből nem állapítható meg (a telepítő maga " + ", ".join(stubs) +
                        ") — add meg a névben (x86 / x64 / x86-x64)")
    if arch == "arm64":
        warnings.append("ARM64 program: ellenőrizd az nCore névadási szabályát (x86 / x64 / x86-x64 kötelező)")
    return arch, warnings


# --- resolver -------------------------------------------------------------------------------------------------------

SCREENSHOT_WARNING = ("a szabályzat 3 SAJÁT képet kér (legalább egyet a telepítésről és egyet a működő programról) — ezeket nem tudom "
                      "automatikusan elkészíteni (a release programjait sosem futtatom), add meg: --images")


def resolve(release, code: str, *, original: bool, hints: dict) -> Meta:
    """Meta(kind="software") of a PC program or Windows ISO release (see the module docstring).

    hints: "title", "version", "platform" ("Windows" / "macOS" / "Linux"), "name" (parse this instead of the release
    name), "hash" (False: skip the SHA-256 of a Windows ISO against Microsoft's table, which reads the whole ISO)."""

    hints = hints or {}
    meta = Meta(kind="software")
    nfo.annotate(meta, release)
    parsed = parse_name(hints.get("name") or release.name)
    meta.add_source("név")
    try:
        facts = inspect_local(release, parsed)
    except Exception as exc:  # noqa: BLE001 - a broken file must not stop the upload
        facts = {"kind": "other", "nfo": {}, "tools": [], "tool_dirs": [], "activation": parsed.get("activation", [])}
        meta.warnings.append(f"helyi vizsgálat sikertelen ({type(exc).__name__}: {exc})")
    try:
        if facts.get("kind") == "windows_iso":
            _resolve_windows(release, meta, parsed, facts, hints, code=code, original=original)
        else:
            _resolve_program(release, meta, parsed, facts, hints, code=code, original=original)
    except Exception as exc:  # noqa: BLE001 - whatever is known so far is still returned
        meta.warnings.append(f"a program adatainak összegyűjtése megszakadt ({type(exc).__name__}: {exc}) — ellenőrizd a leírást")
        meta.title = meta.title or hints.get("title") or parsed["title"]
        meta.version = meta.version or hints.get("version") or parsed["version"]
        meta.extra.setdefault("torrent_name", release.name if original else torrent_name(meta.title, meta.version, parsed["arch"],
                                                                                           hungarian=parsed["hungarian"]))
    meta.warnings.append(SCREENSHOT_WARNING)
    nfo.description(meta, release)
    return meta


def _apply_nfo(meta: Meta, data: dict) -> None:
    if not data:
        return
    meta.add_source("NFO")
    meta.date = meta.date or (data.get("date") if re.fullmatch(r"\d{4}-\d{2}-\d{2}", data.get("date", "")) else "")
    meta.year = meta.year or (meta.date[:4] if meta.date else "")
    for language in data.get("languages") or []:
        if language not in meta.languages:
            meta.languages.append(language)


def _resolve_program(release, meta: Meta, parsed: dict, facts: dict, hints: dict, *, code: str, original: bool) -> None:
    data = facts.get("nfo") or {}
    _apply_nfo(meta, data)
    binaries = facts.get("binaries") or []
    setups = [b for b in binaries if b["role"] == "setup"] or binaries
    msis = facts.get("msis_info") or []
    main = setups[0]["pe"] if setups else {}
    products = [p for p in [msis[0].get("name") if msis else "", main.get("product_name", "")]
                if p and p.lower() not in GENERIC_PRODUCTS]
    inno = next((b.get("inno") for b in binaries if b.get("inno", {}).get("supported")), {})
    if inno.get("app_name"):
        products.append(inno["app_name"])
    products = list(dict.fromkeys(products))
    companies = [c for c in [msis[0].get("manufacturer") if msis else "", _clean_company(main.get("company", "")),
                             (main.get("signature") or {}).get("signer_org", ""), (main.get("signature") or {}).get("signer", ""),
                             data.get("publisher", "")] if c]
    platform = hints.get("platform") or parsed.get("platform") or ("macOS" if facts.get("kind") == "mac" else
                                                                    "Linux" if facts.get("kind") == "linux" else "Windows")
    meta.platform = platform
    name = hints.get("title") or parsed["title"] or data.get("title") or (products[0] if products else "")
    for product in products:
        if product and not _same_name(product, name) and parsed["title"]:
            meta.warnings.append(f"a telepítő terméknévként mást ír: „{product}” (a név szerint „{name}”)")
            break
    version = (hints.get("version") or parsed["version"] or data.get("version") or main.get("version") or
               (msis[0].get("version") if msis else "") or inno.get("app_version", ""))
    local_version = main.get("version") or (msis[0].get("version") if msis else "")
    if version and local_version and not _same_version(version, local_version):
        meta.warnings.append(f"verzió: a név szerint {version}, a telepítő szerint {local_version}")

    # --- online identity -------------------------------------------------------------------------------------------
    manifest: dict = {}
    via_product = ""  # the installer's product name found the package, not the release name
    hashes = set((facts.get("sha256") or {}).values()) - {""}
    if platform == "Windows":
        try:
            manifest = winget.lookup(name, companies, version=version, product_code=msis[0].get("product_code", "") if msis else "",
                                     upgrade_code=msis[0].get("upgrade_code", "") if msis else "")
            for product in products if not manifest else []:
                if _same_name(product, name):
                    continue
                candidate = winget.lookup(product, companies, version=local_version or version)
                official = bool(hashes & set(candidate.get("installer_sha256") or []))
                confirmed = candidate.get("matched_version") and (candidate.get("match") or {}).get("publisher_match")
                if candidate and (official or confirmed):  # the files themselves prove the identity
                    manifest, via_product = candidate, product
                    meta.warnings.append(f"azonosítás a telepítő terméknevéből: „{product}” ({candidate.get('id', '')}), nem a névből "
                                         f"(„{name}”) — ellenőrizd")
                    break
                if candidate:
                    meta.warnings.append(f"winget: a telepítő terméknevére („{product}”) talált {candidate.get('id', '')} csomagot nem "
                                         "használtam (sem a verzió és a kiadó, sem a telepítő SHA-256 értéke nem igazolja)")
        except Exception as exc:  # noqa: BLE001
            meta.warnings.append(f"winget: hiba ({type(exc).__name__})")
    if via_product and local_version and not hints.get("version"):
        version = local_version
    if manifest:
        meta.add_source("winget")
        meta.identifiers["winget"] = manifest.get("id", "")
        if (manifest.get("match") or {}).get("method") == "name" and not (manifest.get("match") or {}).get("publisher_match") \
                and not hashes & set(manifest.get("installer_sha256") or []):
            meta.warnings.append(f"winget: a(z) {manifest.get('id', '')} csomagot csak a név alapján azonosítottam (a kiadó nem "
                                 "ellenőrizhető) — ellenőrizd")
        if hashes & set(manifest.get("installer_sha256") or []):
            meta.extra["official_installer"] = "igen (SHA-256 egyezik a winget által nyilvántartott hivatalos telepítővel)"
            meta.confidence += 0.2
        if manifest.get("version") and version and not manifest.get("matched_version"):
            meta.warnings.append(f"a winget nem ismeri a(z) {version} verziót (legújabb: {manifest.get('latest_version') or manifest.get('version')}) "
                                 "— a leírás a legújabb verzió adataiból készült")
    # the NFO's link describes the named program: not used when the files proved another one
    homepage = data.get("url", "") if data.get("url") and not via_product and not re.search(r"(?i)srrdb|predb|imdb", data.get("url", "")) else ""
    homepage = homepage or manifest.get("package_url") or manifest.get("publisher_url") or (msis[0].get("url") if msis else "")
    choco: dict = {}
    if platform == "Windows":
        try:
            choco = chocolatey.best(manifest.get("name") or name, companies + [manifest.get("publisher", "")], homepage)
        except Exception as exc:  # noqa: BLE001
            meta.warnings.append(f"Chocolatey: hiba ({type(exc).__name__})")
        if choco:
            meta.add_source("Chocolatey")
            meta.identifiers["chocolatey"] = choco.get("id", "")
            homepage = homepage or choco.get("project_url", "")
    brew: dict = {}
    if platform == "macOS":
        brew = homebrew.find(name)
        if brew:
            meta.add_source("Homebrew")
            meta.identifiers["homebrew"] = brew.get("token", "")
            homepage = homepage or brew.get("homepage", "")
    wiki: dict = {}
    try:
        wiki = wikidata_software(manifest.get("name") or name, homepage)
    except Exception as exc:  # noqa: BLE001
        meta.warnings.append(f"Wikidata: hiba ({type(exc).__name__})")
    if wiki:
        meta.add_source("Wikidata")
        meta.identifiers["wikidata"] = wiki.get("qid", "")
        homepage = homepage or wikidata.claim(wiki, "website")

    # --- fields ----------------------------------------------------------------------------------------------------
    trusted = manifest.get("name", "") if manifest and (manifest.get("matched_version") or not version or via_product) else ""
    canonical = trusted or (products[0] if products else "") or brew.get("name", "")
    major = re.match(r"\d+", version or "")
    if canonical and major and re.search(rf"\s{major.group(0)}$", canonical) and not re.search(rf"\s{major.group(0)}$", name):
        canonical = canonical[: canonical.rfind(" ")]  # "Inno Setup 6" + v6.7.3 -> "Inno Setup v6.7.3"
    title = name
    if via_product and canonical and not hints.get("title"):
        title = canonical  # the files proved another name than the release name ("npp" -> "Notepad++")
        meta.warnings = [w for w in meta.warnings if not w.startswith("a telepítő terméknévként")]
    elif canonical and _same_name(canonical, name) and len(winget.normalize(canonical)) >= 3 and not hints.get("title"):
        title = canonical if len(canonical) <= len(name) + 3 else name
    meta.title = title
    meta.version = version
    if manifest.get("matched_version") and re.fullmatch(r"\d{4}-\d{2}-\d{2}", manifest.get("release_date", "")) and not meta.date:
        meta.date, meta.year = manifest["release_date"], manifest["release_date"][:4]
    meta.publisher = manifest.get("publisher") or (msis[0].get("manufacturer") if msis else "") or _clean_company(main.get("company", "")) \
        or (main.get("signature") or {}).get("signer_org", "") or data.get("publisher", "") or choco.get("authors", "")
    author = manifest.get("author", "")
    meta.creators = [author] if author and not winget.same_publisher(author, meta.publisher) else []
    license_text = license_hu(manifest.get("license", ""))
    if license_text:
        meta.extra["license"] = license_text
    arch, arch_warnings = decide_arch(parsed, facts, manifest)
    meta.warnings += arch_warnings
    if arch:
        meta.extra["arch"] = arch
    engines = [b["engine_label"] + (f" {b['engine_version']}" if b.get("engine_version") else "") for b in setups if b["engine"] != "exe"]
    if engines:
        meta.extra["installer"] = engines[0]
    signature = main.get("signature") or {}
    if signature.get("signed"):
        meta.extra["signer"] = signature.get("signer", "")
        if signature.get("digest_ok") is False:
            meta.warnings.append(f"a(z) {setups[0]['path']} digitális aláírása nem egyezik a fájl tartalmával (módosított fájl?)")
    elif setups:
        meta.extra["signer"] = "nincs digitális aláírás"
    # languages: NFO, name, Inno languages, language files (release, payload), Hungarian MSI / VersionInfo language
    languages = list(meta.languages) + list(parsed.get("languages") or []) + list(inno.get("languages") or [])
    languages += list(facts.get("ui_languages") or []) + [lang for b in binaries for lang in b.get("ui_languages") or []]
    if facts.get("hungarian_files"):
        languages.append("magyar")
    if not languages and "magyar" in ((msis[0].get("languages") if msis else []) or main.get("languages") or []):
        languages.append("magyar")  # an English installer language says nothing about the program
    languages = list(dict.fromkeys(languages))
    if len(languages) > 5 or ("többnyelvű" in languages and len(languages) > 2):
        languages = ["többnyelvű"] + (["magyar"] if "magyar" in languages else [])
    meta.languages = languages
    # requirements
    manifest_os = windows_name(manifest.get("min_os", "")) if manifest.get("matched_version") or not version else ""
    min_os = manifest_os or (msis[0].get("min_os") if msis else "") or _lowest_os(setups)
    if platform == "macOS":
        min_os = brew.get("min_macos", "")
    if min_os:
        meta.extra["min_os"] = min_os
    meta.requirements = requirements_text(os_name=min_os, arch=arch, platform=platform, nfo_text=data.get("requirements", ""))
    # description: huwiki > Wikidata hu > winget hu locale > machine translation > English
    _describe(meta, wiki, manifest, choco, brew, data)
    # proof links: official site first (never wiki); the installer's own URLs (MSI ARPURLINFOABOUT, signer's link) only when
    # the installer is the named program
    files_agree = via_product or not products or any(_same_name(p, name) for p in products)
    for url in [data.get("url", "") if homepage == data.get("url") else "", manifest.get("package_url", ""), manifest.get("publisher_url", ""),
                msis[0].get("url", "") if msis and files_agree else "", choco.get("project_url", ""),
                wikidata.claim(wiki, "website") if wiki else "", brew.get("homepage", ""), signature.get("url", "") if files_agree else ""]:
        if url and _public_url(url) and "github.com/microsoft/winget-pkgs" not in url and _host(url) not in {_host(u) for u in meta.proof_links}:
            meta.add_proof(url)
    if meta.proof_links and not _reachable(meta.proof_links[0]):
        meta.warnings.append(f"az igazoló link most nem érhető el: {meta.proof_links[0]} — ellenőrizd")
    if not meta.proof_links:
        meta.warnings.append("nincs igazoló link (hivatalos oldal): írd be a leírásba (--edit)")
    # cover: winget icon (official app icon) > Chocolatey icon (not SVG) > Wikidata logo > icon of the EXE
    icon = choco.get("icon_url", "")
    logo = wikidata.claim(wiki, "logo") if wiki else ""
    meta.cover_url = manifest.get("icon_url", "") or (icon if icon and not icon.lower().endswith(".svg") else "") or \
        (wikidata.commons_file_url(logo) if logo else "")
    if not meta.cover_url and facts.get("icon"):
        meta.cover = facts["icon"]
        meta.warnings.append("borító: a program / telepítő saját ikonja (nincs online logó) — ellenőrizd, hogy nem az általános telepítőikon-e")
    # install guide
    kind = facts.get("kind", "other")
    if code == "iso" and kind not in {"iso", "windows_iso"}:
        meta.warnings.append("Program (ISO) kategória, de a release-ben nincs lemezkép")
        meta.extra["category_hint"] = "misc"
        meta.extra.setdefault("category_errors", []).append("Program (ISO) kategóriához lemezkép szükséges")
    elif code == "misc" and kind == "iso":
        meta.warnings.append("a release lemezképet tartalmaz: a Program (ISO) kategória illik hozzá, nem a Program (RIP)")
        meta.extra["category_hint"] = "iso"
        meta.extra.setdefault("category_errors", []).append("a program lemezképe Program (ISO) kategóriába való")
    install_facts = dict(facts)
    cls = facts.get("classify") or {}
    inner = facts.get("inner") or {}
    install_facts.update({
        "setup": Path((cls.get("setups") or inner.get("setups") or [""])[0]).name if (cls.get("setups") or inner.get("setups")) else "",
        "msi": Path((cls.get("msis") or inner.get("msis") or [""])[0]).name if (cls.get("msis") or inner.get("msis")) else "",
        "exe": Path((cls.get("exes") or inner.get("exes") or [""])[0]).name if (cls.get("exes") or inner.get("exes")) else "",
        "iso": Path(cls["isos"][0]).name if cls.get("isos") else "",
        "mac": Path(cls["mac"][0]).name if cls.get("mac") else "",
        "linux": Path(cls["linux"][0]).name if cls.get("linux") else "",
        "app": next((a for a in brew.get("apps") or []), ""),
    })
    meta.install_hu = install_guide(kind if kind != "archive" else "setup", install_facts)
    if facts.get("nfo_steps") and not translate_steps(facts["nfo_steps"]):
        meta.warnings.append("az NFO telepítési lépései (angolul): " + " / ".join(facts["nfo_steps"][:6]) + " — vesd össze a magyar útmutatóval")
    # name
    suggested = torrent_name(meta.title, meta.version, arch, hungarian=bool(parsed.get("hungarian")))  # HUN only when the name says so
    meta.extra["torrent_name"] = release.name if original else suggested
    if original:
        meta.extra["torrent_name_own"] = suggested
    if not arch and platform == "Windows" and not any("architektúra" in w for w in arch_warnings):
        meta.warnings.append("program név: a kompatibilitást (x86 / x64 / x86-x64) kötelező feltüntetni a névben — nem tudtam megállapítani")
    meta.identifiers.update({k: v for k, v in {"msi_product_code": msis[0].get("product_code", "") if msis else ""}.items() if v})
    meta.confidence = min(1.0, meta.confidence + 0.3 + (0.2 if products and any(_same_name(p, name) for p in products) else 0)
                          + (0.3 if manifest and manifest.get("match", {}).get("method") != "name" else 0.2 if manifest else 0)
                          + (0.1 if wiki else 0) + (0.1 if choco else 0))
    if original and not data:
        meta.warnings.append("eredeti release, de nincs NFO a mappában")


def _describe(meta: Meta, wiki: dict, manifest: dict, choco: dict, brew: dict, data: dict) -> None:
    english = manifest.get("description") or chocolatey.clean_description(choco.get("description", "")) or \
        manifest.get("short_description") or choco.get("summary", "") or brew.get("desc", "") or data.get("description", "")
    meta.description_en = english
    summary = wikipedia.summary(wiki["huwiki"], "hu") if wiki.get("huwiki") else {}
    if summary.get("extract"):
        meta.description_hu, meta.description_source = summary["extract"], wikipedia.CREDIT
        meta.add_source("Wikipédia")
        return
    if manifest.get("description_hu"):
        meta.description_hu, meta.description_source = manifest["description_hu"], "winget"
        return
    if english:
        text, translated = translate.to_hungarian(english)
        if translated:
            meta.description_hu, meta.machine_translated = text, True
            meta.description_source = "winget" if english in (manifest.get("description"), manifest.get("short_description")) else \
                "Chocolatey" if choco and english in (chocolatey.clean_description(choco.get("description", "")), choco.get("summary")) else \
                "Homebrew" if brew and english == brew.get("desc") else "NFO"
            if wiki.get("description_hu"):
                meta.description_hu = wiki["description_hu"][:1].upper() + wiki["description_hu"][1:] + ".\n\n" + text
            return
    if wiki.get("description_hu"):
        meta.description_hu = wiki["description_hu"][:1].upper() + wiki["description_hu"][1:] + "."
        meta.description_source = "Wikidata"
        return
    if english:
        meta.description_source = "winget" if english in (manifest.get("description"), manifest.get("short_description")) else \
            "Chocolatey" if choco else "Homebrew" if brew else "NFO"
        meta.warnings.append("nincs magyar leírás (nincs DeepL-kulcs vagy forrás) — az angolt tettem be, fordítsd le, ha tudod")


def _windows_media(release, facts: dict) -> Path | None:
    root = Path(release.path)
    cls = facts.get("classify") or {}
    if root.is_file():
        return root
    if cls.get("isos"):
        return root / cls["isos"][0]
    if cls.get("windows_iso"):
        return root
    return None


def _resolve_windows(release, meta: Meta, parsed: dict, facts: dict, hints: dict, *, code: str, original: bool) -> None:
    meta.platform = "Windows"
    media = _windows_media(release, facts)
    info = facts.get("winiso") or (winiso.inspect(media) if media else {})
    if info.get("build") and not info.get("version"):  # a build newer than the static table: Microsoft's release health pages
        known = msdownload.version_for_build(info["build"], server=bool(info.get("server")), refresh=True)
        if known.get("version"):
            info = dict(info, version=known["version"], product=info.get("product") or known.get("product", ""))
    _apply_nfo(meta, facts.get("nfo") or {})
    if not info:
        meta.warnings.append("Windows telepítőkép: nem találtam olvasható sources/install.wim / install.esd fájlt")
    meta.add_source("WIM")
    product = info.get("product") or ("Windows 11" if "11" in parsed["title"] else "Windows 10" if "10" in parsed["title"] else "Windows")
    version = info.get("version") or parsed.get("version", "")
    if parsed.get("version") and info.get("version") and parsed["version"].upper() != info["version"].upper():
        meta.warnings.append(f"verzió: a név szerint {parsed['version']}, a telepítőkép (build {info.get('build')}) szerint {info['version']}")
    title = re.sub(r"(?i)^(microsoft\s+)?win(?=\d)", lambda m: (m.group(1) or "") + "Windows ", parsed["title"])  # "Win11" (MS file names)
    meta.title = hints.get("title") or title or f"Microsoft {product}"
    meta.version = hints.get("version") or version
    meta.publisher = "Microsoft"
    arch = info.get("arch") or parsed.get("arch", "")
    if arch:
        meta.extra["arch"] = arch
    if info.get("build"):
        meta.extra["build"] = info["build"]
    if info.get("editions"):
        meta.extra["editions"] = ", ".join(info["editions"])
    meta.languages = info.get("languages") or parsed.get("languages") or []
    meta.extra["license"] = "kereskedelmi (érvényes Windows-licenc szükséges)"
    meta.requirements = (msdownload.REQUIREMENTS.get(product.replace(" Server", ""), "") if not info.get("server") else "")
    if arch:
        meta.requirements = (meta.requirements + "\n" if meta.requirements else "") + f"Architektúra: {ARCH_HU.get(arch, arch)}"
    key = "Windows 11 arm64" if product == "Windows 11" and arch == "arm64" else product
    page = msdownload.DOWNLOAD_PAGES.get(key, "")
    if media and media.is_file() and media.suffix.lower() == ".iso" and page and hints.get("hash", True):
        published = msdownload.published(key)
        if published.get("hashes"):
            digest = winiso.sha256(media)
            hit = published["hashes"].get(digest.upper()) if digest else None
            meta.extra["sha256"] = digest
            if hit:
                meta.extra["official_iso"] = f"igen ({hit['label']}, a Microsoft által közölt SHA-256 egyezik)"
                meta.confidence += 0.3
            else:
                meta.warnings.append("az ISO SHA-256 értéke nem szerepel a Microsoft által közölt hivatalos ISO-k között "
                                     f"(jelenlegi kiadás: {published.get('version') or '?'}) — módosított vagy régebbi kiadás")
    if page:
        meta.add_proof(page)
    else:
        meta.warnings.append(f"nincs igazoló link: a(z) {product} kiadáshoz nincs ismert Microsoft letöltőoldal — írd be a leírásba (--edit)")
    if code != "iso":
        meta.warnings.append("Windows telepítőkép: a Program (ISO) kategória illik hozzá")
        meta.extra["category_hint"] = "iso"
        meta.extra.setdefault("category_errors", []).append("Windows telepítőkép Program (ISO) kategóriába való")
    meta.add_source("Microsoft")
    summary = wikipedia.summary(product, "hu") if product.startswith("Windows ") else {}
    if summary.get("extract"):
        meta.description_hu, meta.description_source = summary["extract"], wikipedia.CREDIT
        meta.add_source("Wikipédia")
    cls = facts.get("classify") or {}
    iso_name = Path(cls["isos"][0]).name if cls.get("isos") else (Path(release.path).name if Path(release.path).is_file() else "")
    folder = "" if iso_name else Path(release.path).name  # an unpacked ISO: the files are copied, not written as an image
    meta.install_hu = install_guide("windows_iso", {"iso": iso_name, "folder": folder, "editions": meta.extra.get("editions", "")})
    hungarian = parsed.get("hungarian") or meta.languages == ["magyar"]
    suggested = torrent_name(meta.title, meta.version, arch, hungarian=bool(hungarian))
    meta.extra["torrent_name"] = release.name if original else suggested
    if original:
        meta.extra["torrent_name_own"] = suggested
    meta.confidence = min(1.0, meta.confidence + (0.6 if info.get("images") else 0.2))
