"""AccoFIL NCUpload installer; standard library only, usable before pip install."""
from __future__ import annotations

import argparse
import getpass
import hashlib
import http.cookiejar
import io
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import warnings
from pathlib import Path
from urllib.parse import urlsplit

from . import __version__, keys
from .config import CONFIG_FILE, FALLBACK_FILES, read_env

APT_PACKAGES = ("python3-venv python3-pip ca-certificates git ffmpeg mediainfo mkvtoolnix p7zip-full unrar "
    "poppler-utils djvulibre-bin libimage-exiftool-perl mupdf-tools calibre libchromaprint-tools flac "
    "cuetools shntool lsdvd tesseract-ocr tesseract-ocr-hun msitools wimtools icoutils icnsutils "
    "osslsigncode cabextract unshield aapt apksigner wit bchunk mame-tools innoextract").split()
TOOLS = "ffmpeg ffprobe mediainfo 7z pdfinfo pdftoppm pdftotext pdfimages djvused ddjvu djvutxt exiftool mutool ebook-convert ebook-meta fpcalc lsdvd tesseract msiinfo wiminfo aapt apksigner wit innoextract mount umount unshare".split()
API_LABELS = {
    "TMDB_API": "TMDB – filmek/sorozatok, v3 API Key", "DEEPL_KEY": "DeepL – magyar fordítás",
    "SPOTIFY_CLIENT_ID": "Spotify Client ID", "SPOTIFY_CLIENT_SECRET": "Spotify Client Secret",
    "DISCOGS_TOKEN": "Discogs Personal Access Token (vagy az alábbi kulcspár)",
    "DISCOGS_CONSUMER_KEY": "Discogs Consumer Key", "DISCOGS_CONSUMER_SECRET": "Discogs Consumer Secret",
    "ACOUSTID_KEY": "AcoustID – application API key", "LASTFM_KEY": "Last.fm API Key",
    "SETLISTFM_KEY": "setlist.fm API Key", "IGDB_CLIENT_ID": "IGDB/Twitch Client ID",
    "IGDB_CLIENT_SECRET": "IGDB/Twitch Client Secret", "GOOGLE_BOOKS_KEY": "Google Books API key",
    "COMICVINE_KEY": "Comic Vine API Key", "TVDB_KEY": "TheTVDB API Key", "FANART_KEY": "fanart.tv API Key",
    "GITHUB_TOKEN": "GitHub opcionális olvasási token a szoftveradatokhoz",
}


class SetupError(ValueError):
    """Validation error whose message contains no supplied secret values."""


def ask(label: str, default: str = "", *, secret: bool = False, required: bool = False) -> str:
    while True:
        hint = (" [mentve; Enter: megtartás]" if default else " [Enter: kihagyás]") if secret else f" [{default}]" if default else ""
        if secret:
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                value = getpass.getpass(label + hint + ": ")
        else:
            value = input(label + hint + ": ").strip()
        value = "" if value == "-" else value or default
        if any(c in value for c in "\r\n\0") or required and not value:
            print("Érvényes, egysoros értéket adj meg.")
            continue
        return value


def yes(label: str, default: bool = False) -> bool:
    return ask(label + (" (I/n)" if default else " (i/N)"), "i" if default else "n").casefold() in {"i", "igen", "y", "yes"}


def full_path(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute() or path == Path("/") or any(c in str(path) for c in "\r\n\0"):
        raise SetupError("Teljes, nem gyökérkönyvtárra mutató útvonal szükséges.")
    return path


def ask_path(label: str, default: str) -> str:
    while True:
        try:
            return str(full_path(ask(label, default, required=True)))
        except ValueError as exc:
            print(exc)


def private_dir(path: Path) -> None:
    if path.is_symlink():
        raise SetupError("Védett konfigurációs/cache könyvtár nem lehet symlink.")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    if path.is_symlink():
        raise SetupError("A mentés célja nem lehet symlink.")
    fd, name = tempfile.mkstemp(prefix=".ncupload-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.chmod(name, mode)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def stored(path: Path) -> dict[str, str]:
    values = {}
    for fallback in FALLBACK_FILES:
        values.update({k: v for k, v in read_env(fallback).items() if k.startswith("QB_")})
    values.update(read_env(path))
    for name in keys.NAMES:
        values[name] = os.environ.get(name) or os.environ.get("NCORE_" + name) or values.get(name) or values.get("NCORE_" + name, "")
        values.pop("NCORE_" + name, None)
    return values


def collect(path: Path, overrides: dict[str, str] | None = None, *, existing: bool = False) -> tuple[dict[str, str], str]:
    values = stored(path)
    home, overrides = Path.home(), overrides or {}
    print("\nAccoFIL NCUpload " + __version__ + " – beállítás")
    print("Enter: alapérték/megtartás; -: opcionális érték törlése. A titkos mezők nem látszanak.")
    print("\n1/4 – Elérési utak (teljes, Linux-szerveres útvonalak)")
    defaults = (
        ("NCORE_VENV", "Python környezet helye", str(home / ".local/share/ncore-up/venv")),
        ("NCORE_BIN_DIR", "Indítóparancsok mappája", str(home / ".local/bin")),
        ("NCORE_NCUP_DIR", "NCUP célmappa – előkészített kiadások", str(home / "storage/NCUP")),
        ("NCORE_COOKIES", "Védett nCore sütifájl helye", str(path.parent / "cookies.txt")),
        ("NCORE_LOG_DIR", "Futási naplók mappája", str(home / "logs/ncore-up")),
        ("NCORE_WORK_DIR", "Gyorsítótár és állapot mappája", str(home / ".cache/ncore-up")),
    )
    for name, label, default in defaults:
        initial = overrides.get(name) or os.environ.get(name) or values.get(name) or default
        # Changing the venv requires a reinstall, not just rewriting the launcher.
        values[name] = initial if existing and name == "NCORE_VENV" else ask_path(label, initial)
    for name, tool in (("NCORE_MKBRR", "mkbrr"), ("NCORE_BDINFO", "bdinfo")):
        ua = home / "Upload-Assistant/bin" / tool / "linux/amd64" / tool
        default = os.environ.get(name) or values.get(name) or shutil.which(tool) or (str(ua) if ua.is_file() else str(home / ".local/share/ncore-up/bin" / tool))
        values[name] = ask_path(tool + " teljes útvonala", default)
    venv = Path(values["NCORE_VENV"])
    if Path(values["NCORE_BIN_DIR"]).resolve() == (venv / "bin").resolve():
        raise SetupError("Az indítómappa nem lehet a venv/bin mappa (rekurzív indítást okozna).")
    cookie_source = ask("Netscape cookies.txt importálása innen (Enter: a beállított fájlt használja)")
    if cookie_source:
        cookie_source = str(full_path(cookie_source))
    print("\n2/4 – qBittorrent WebUI")
    while True:
        url = ask("WebUI címe, porttal", values.get("QB_URL", "http://127.0.0.1:8080"), required=True)
        parsed = urlsplit(url)
        try:
            valid = parsed.scheme in {"http", "https"} and parsed.hostname and not (parsed.username or parsed.password or parsed.query or parsed.fragment)
            parsed.port
        except ValueError:
            valid = False
        if valid:
            values["QB_URL"] = url.rstrip("/")
            break
        print("Érvényes http:// vagy https:// címet adj meg; a jelszó külön mező.")
    values["QB_USER"] = ask("qBittorrent felhasználónév (helyi hitelesítésmentes elérésnél üres)", values.get("QB_USER", ""))
    values["QB_PASS"] = ask("qBittorrent jelszó", values.get("QB_PASS", ""), secret=True)
    values["NCORE_QB_CATEGORY"] = ask("qBittorrent kategória (nem az nCore kategóriája; opcionális)", values.get("NCORE_QB_CATEGORY", ""))
    print("\n3/4 – API-kulcsok (mind opcionális; Enterrel kihagyhatók)")
    print("Létrehozási útmutató: docs/api-keys.md. MusicBrainzhez nem kell kulcs.")
    for name in keys.NAMES:
        while True:
            value = ask(API_LABELS.get(name, name) + " – " + name, values.get(name, ""), secret=True)
            if not value or len(value) <= 8192 and not any(c.isspace() for c in value):
                values[name] = value
                break
            print("A kulcs nem tartalmazhat szóközt, és legfeljebb 8192 karakteres lehet.")
    for pair in (("SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET"), ("IGDB_CLIENT_ID", "IGDB_CLIENT_SECRET"), ("DISCOGS_CONSUMER_KEY", "DISCOGS_CONSUMER_SECRET")):
        if bool(values[pair[0]]) != bool(values[pair[1]]):
            print("Hiányos kulcspár: " + " + ".join(pair) + ". Később pótolható: ncupload --configure")
    print("\n4/4 – Heti tiltólista-frissítés")
    values["NCORE_REFRESH_WEEKLY"] = "yes" if yes("Heti frissítés systemd user timerrel?", values.get("NCORE_REFRESH_WEEKLY", "yes") == "yes") else "no"
    return values, cookie_source


def save(path: Path, values: dict[str, str]) -> None:
    private_dir(path.parent)
    for name in ("NCORE_WORK_DIR", "NCORE_LOG_DIR"):
        private_dir(Path(values[name]))
    ncup = Path(values["NCORE_NCUP_DIR"])
    if ncup.is_symlink():
        raise SetupError("Az NCUP célmappa nem lehet symlink.")
    ncup.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not os.access(ncup, os.W_OK):
        raise SetupError("Az NCUP mappa nem írható.")
    if path.exists():
        atomic_write(path.with_name(path.name + ".previous"), path.read_bytes())
    text = "# AccoFIL NCUpload – védett konfiguráció; ne tedd Gitbe, ne source-old.\n"
    text += "\n".join(name + "=" + json.dumps(value, ensure_ascii=False) for name, value in sorted(values.items())) + "\n"
    atomic_write(path, text.encode("utf-8"))


def copy_cookies(source: str, destination: Path) -> None:
    if not source:
        if not destination.exists():
            print("Sütifájl még nincs. Később: ncupload --import-cookies /teljes/út/cookies.txt")
        return
    jar = http.cookiejar.MozillaCookieJar(source)
    jar.load(ignore_discard=True, ignore_expires=True)
    selected = http.cookiejar.MozillaCookieJar()
    for cookie in jar:
        domain = cookie.domain.lstrip(".").lower()
        if domain == "ncore.pro" or domain.endswith(".ncore.pro"):
            selected.set_cookie(cookie)
    if not list(selected):
        raise SetupError("Az importált fájlban nincs nCore-süti.")
    private_dir(destination.parent)
    fd, name = tempfile.mkstemp(prefix=".cookies-", dir=destination.parent)
    os.close(fd)
    try:
        selected.save(name, ignore_discard=True, ignore_expires=True)
        atomic_write(destination, Path(name).read_bytes())
    finally:
        Path(name).unlink(missing_ok=True)
    print("nCore-sütik védetten mentve; belépésellenőrzés külön: ncupload --check")


def launchers(values: dict[str, str], config: Path) -> None:
    folder = Path(values["NCORE_BIN_DIR"])
    folder.mkdir(parents=True, exist_ok=True)
    for name in ("ncupload", "ncore-up"):
        target = folder / name
        if target.is_symlink():
            target.unlink()
        text = "#!/bin/sh\nexport NCORE_CONFIG=" + shlex.quote(str(config)) + "\nexec " + shlex.quote(str(Path(values["NCORE_VENV"]) / "bin/ncupload")) + ' "$@"\n'
        atomic_write(target, text.encode(), 0o755)


def system_packages() -> None:
    if not shutil.which("apt-get"):
        raise SetupError("Az automatikus csomagtelepítés apt-get-et igényel (Debian/Ubuntu).")
    prefix = [] if os.geteuid() == 0 else ["sudo"]
    subprocess.run(prefix + ["apt-get", "update"], check=True)
    available, missing = [], []
    for name in APT_PACKAGES:
        result = subprocess.run(["apt-cache", "show", name], capture_output=True)
        (available if result.returncode == 0 and b"Package:" in result.stdout else missing).append(name)
    subprocess.run(prefix + ["apt-get", "install", "-y", *available], check=True)
    if missing:
        print("A tárolókban nem található: " + ", ".join(missing) + ". Lásd docs/troubleshooting.md.")


def download(url: str, limit: int) -> bytes:
    if not url.startswith("https://"):
        raise SetupError("Csak HTTPS-letöltés engedett.")
    request = urllib.request.Request(url, headers={"User-Agent": "AccoFIL-NCUpload-installer/" + __version__})
    with urllib.request.urlopen(request, timeout=90) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise SetupError("Túl nagy letöltés.")
    return data


def install_mkbrr(target: Path) -> None:
    arch = {"x86_64": "x86_64", "aarch64": "arm64", "arm64": "arm64", "armv7l": "arm"}.get(platform.machine().lower())
    if not arch:
        raise SetupError("Ehhez az architektúrához kézzel add meg a mkbrr binárist.")
    release = json.loads(download("https://api.github.com/repos/autobrr/mkbrr/releases/latest", 2 << 20))
    assets = release.get("assets", [])
    archive = next((a for a in assets if re.fullmatch(r"mkbrr_[\d.]+_linux_" + arch + r"\.tar\.gz", a["name"])), None)
    checksum = next((a for a in assets if re.fullmatch(r"mkbrr_[\d.]+_checksums\.txt", a["name"])), None)
    prefix = "https://github.com/autobrr/mkbrr/releases/download/"
    if not archive or not checksum or not all(a["browser_download_url"].startswith(prefix) for a in (archive, checksum)):
        raise SetupError("Nem található ellenőrizhető mkbrr kiadás.")
    payload = download(archive["browser_download_url"], 128 << 20)
    sums = download(checksum["browser_download_url"], 2 << 20).decode("utf-8")
    entries = {m[2]: m[1].lower() for line in sums.splitlines() if (m := re.fullmatch(r"([0-9a-fA-F]{64})\s+\*?(\S+)", line.strip()))}
    if hashlib.sha256(payload).hexdigest() != entries.get(archive["name"]):
        raise SetupError("A mkbrr SHA-256 ellenőrzése sikertelen.")
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tar:
        members = [m for m in tar.getmembers() if Path(m.name).name == "mkbrr" and m.isfile() and m.size <= (128 << 20)]
        if len(members) != 1:
            raise SetupError("A mkbrr archívum tartalma váratlan.")
        with tar.extractfile(members[0]) as stream:
            binary = stream.read((128 << 20) + 1)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_write(target, binary, 0o755)
    print("mkbrr telepítve, kiadói SHA-256 alapján: " + release["tag_name"])


def weekly_timer(values: dict[str, str]) -> None:
    if values.get("NCORE_REFRESH_WEEKLY") != "yes":
        timer = Path.home() / ".config/systemd/user/ncore-up-banlist.timer"
        if timer.exists() and shutil.which("systemctl"):
            result = subprocess.run(["systemctl", "--user", "disable", "--now", "ncore-up-banlist.timer"], capture_output=True)
            print("Heti user timer kikapcsolva." if not result.returncode else "A user timer kikapcsolása nem sikerült; lásd docs/installation.md.")
        else:
            print("Heti user timer nem lett beállítva.")
        return
    if not shutil.which("systemctl"):
        print("Nincs systemd. Heti cron beállítás: docs/installation.md.")
        return
    def quote(value: str) -> str:
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'
    folder = Path.home() / ".config/systemd/user"
    folder.mkdir(parents=True, exist_ok=True)
    command = quote(str(Path(values["NCORE_BIN_DIR"]) / "ncupload"))
    service = "[Unit]\nDescription=AccoFIL NCUpload weekly ban list refresh\n\n[Service]\nType=oneshot\nExecStart=" + command + " --refresh-banlist --json\nUMask=0077\nTimeoutStartSec=180\n"
    timer = "[Unit]\nDescription=AccoFIL NCUpload weekly ban list refresh\n\n[Timer]\nOnCalendar=Mon *-*-* 04:15:00 UTC\nRandomizedDelaySec=300\nPersistent=true\nUnit=ncore-up-banlist.service\n\n[Install]\nWantedBy=timers.target\n"
    atomic_write(folder / "ncore-up-banlist.service", service.encode())
    atomic_write(folder / "ncore-up-banlist.timer", timer.encode())
    for args in (["daemon-reload"], ["enable", "--now", "ncore-up-banlist.timer"]):
        result = subprocess.run(["systemctl", "--user", *args], capture_output=True)
        if result.returncode:
            print("A user timer fájljai elmentve; indítása nem sikerült. Lásd docs/installation.md – linger.")
            return
    print("Heti user timer aktív: hétfő 04:15 UTC, legfeljebb 5 perc eltéréssel.")
    print("Kijelentkezés utáni futáshoz: sudo loginctl enable-linger " + shlex.quote(getpass.getuser()))


def configure_existing() -> int:
    try:
        if not sys.stdin.isatty():
            raise SetupError("Interaktív terminálból indítsd.")
        values, source = collect(CONFIG_FILE, existing=True)
        save(CONFIG_FILE, values)
        copy_cookies(source, Path(values["NCORE_COOKIES"]))
        launchers(values, CONFIG_FILE)
        weekly_timer(values)
        print("Beállítások mentve: " + str(CONFIG_FILE))
        return 0
    except SetupError as exc:
        print("HIBA: " + str(exc), file=sys.stderr)
        return 2
    except (ValueError, OSError, EOFError, KeyboardInterrupt, getpass.GetPassWarning):
        print("A beállítás megszakadt; titkos értéket nem írok ki. Lásd docs/troubleshooting.md.", file=sys.stderr)
        return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AccoFIL NCUpload interaktív Linux-telepítő")
    parser.add_argument("--system", action="store_true", help="csak kiírja a Debian/Ubuntu csomagtelepítési parancsot")
    parser.add_argument("--install-system", action="store_true", help="rendszercsomagok telepítése apt/sudo segítségével")
    parser.add_argument("--config", help="config.env teljes útvonala")
    parser.add_argument("--venv", help="Python környezet teljes útvonala")
    parser.add_argument("--bin-dir", help="indítóparancsok célmappája")
    parser.add_argument("--ncup-dir", help="NCUP alapérték a varázslóhoz")
    args = parser.parse_args(argv)
    if args.system:
        print("sudo apt-get update\nsudo apt-get install " + " ".join(APT_PACKAGES))
        print("mkbrr: a varázsló letöltheti; BDInfo: a saját bináris útvonala szükséges.")
        return 0
    try:
        if platform.system() != "Linux" or sys.version_info < (3, 10):
            raise SetupError("Linux és Python 3.10+ szükséges.")
        if os.geteuid() == 0:
            raise SetupError("Normál felhasználóként futtasd; a csomagtelepítéshez sudo használható.")
        if not sys.stdin.isatty():
            raise SetupError("Interaktív terminál szükséges: bash install.sh")
        config = full_path(args.config) if args.config else Path(ask_path("Konfigurációs fájl helye", str(CONFIG_FILE)))
        overrides = {k: v for k, v in {"NCORE_VENV": args.venv, "NCORE_BIN_DIR": args.bin_dir, "NCORE_NCUP_DIR": args.ncup_dir}.items() if v}
        values, cookie_source = collect(config, overrides)
        source = Path(__file__).resolve().parent.parent
        venv = Path(values["NCORE_VENV"])
        if venv.resolve() == source or source.is_relative_to(venv.resolve()):
            raise SetupError("A venv helye nem lehet a forrásmappa vagy annak szülője.")
        if args.install_system or yes("Telepítsem a rendszercsomagokat (sudo/apt)?", True):
            system_packages()
        if not Path(values["NCORE_MKBRR"]).is_file() and yes("A hiányzó mkbrr letöltése a hivatalos kiadásból, SHA-256 ellenőrzéssel?", True):
            install_mkbrr(Path(values["NCORE_MKBRR"]))
        save(config, values)
        copy_cookies(cookie_source, Path(values["NCORE_COOKIES"]))
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
        python = str(venv / "bin/python")
        subprocess.run([python, "-m", "pip", "install", "--upgrade", "pip"], check=True)
        subprocess.run([python, "-m", "pip", "install", str(source)], check=True)
        launchers(values, config)
        weekly_timer(values)
        absent = [tool for tool in TOOLS if not shutil.which(tool)]
        absent += [Path(values[k]).name for k in ("NCORE_MKBRR", "NCORE_BDINFO") if not os.access(values[k], os.X_OK)]
        print("\nKész: AccoFIL NCUpload " + __version__)
        print("Indító: " + str(Path(values["NCORE_BIN_DIR"]) / "ncupload"))
        print("Konfiguráció: " + str(config))
        print("Ha a parancs nem található: export PATH=" + shlex.quote(values["NCORE_BIN_DIR"]) + ':\"$PATH\"')
        print("Következő: ncupload --check; ncupload --refresh-banlist; ncupload --doctor")
        if absent:
            print("Hiányzó eszközök az érintett funkciókhoz: " + ", ".join(absent))
        return 0
    except SetupError as exc:
        print("HIBA: " + str(exc), file=sys.stderr)
        return 2
    except (ValueError, OSError, EOFError, KeyboardInterrupt, getpass.GetPassWarning, subprocess.SubprocessError):
        print("A telepítés megszakadt; titkos értéket nem írok ki. Lásd docs/troubleshooting.md.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
