"""PC games (nCore game_iso / game_rip).

Chain: release name + NFO + GOG installer (innoextract, static) -> GOG (GOG releases, or when Steam has no match)
-> Steam -> IGDB (needs keys) -> PCGamingWiki (requirements / Hungarian language row) -> Wikidata/huwiki for a
Hungarian description. ``gather()`` does the lookups, ``compose()`` turns them into a ``Meta`` (pure, tested
with fixtures).

Output besides the common Meta fields:

    install_hu              Hungarian installation guide (numbered steps, no heading)
    requirements            "Minimális gépigény:\\nOp. rendszer: ...\\n\\nAjánlott gépigény: ..."
    extra["torrent_name"]   name per the wiki: "Játék neve - Alcím v1.2", "... v1.2 GoG", "... v1.2 - FitGirl Repack"
    extra["category_hint"]  game_iso (disc image) / game_rip
    extra["hungarian"]      "felület: igen, felirat: igen, szinkron: nem" (store/PCGW data)
    extra["hun_name_hint"]  "HUN" when the game itself supports Hungarian (the name may carry it)
    extra["source"], extra["version_proof"], extra["release_date"], extra["steam_tags"], extra["files"]
"""

from __future__ import annotations

import re

from .. import keys, net, translate
from ..local import gamefiles, gamename, nfo
from ..meta import Meta
from ..sources import gog, gogdb, igdb, pcgw, steam, wikidata, wikipedia

SCREENSHOT_WARNING = ("a 3 saját képet (legalább 1 a telepítésről és 1 játék közben) még nem tudom automatikusan "
                      "elkészíteni — készítsd el őket, és add meg: --images")
SOURCE_LABEL = {"gog": "GOG offline telepítő", "fitgirl": "FitGirl Repack", "dodi": "DODI Repack", "elamigos": "ElAmigos",
                "scene": "scene release", "p2p": "P2P release", "repack": "repack"}
PRECRACKED_GROUPS = {"ELAMIGOS", "FCKDRM", "GOG", "DINOBYTES", "KAOS", "ONLINEFIX"}
MAX_TEXT = 2500

# English NFO install lines -> Hungarian (first match wins; ``{dir}`` comes from the pattern)
PHRASES: list[tuple[str, str]] = [
    (r"^(?:unrar|unpack|extract|unzip)(?:\s+(?:the\s+)?(?:release|files?|archives?|rars?))?[.!]?$", "Csomagold ki a release-t."),
    (r"(?:unrar|unpack|extract).*(?:burn|mount).*(?:iso|image)", "Csomagold ki, majd csatold fel (mount) vagy írd ki a képfájlt."),
    (r"(?:burn|mount).*(?:iso|image|disc)|^(?:burn|mount)\b", "Csatold fel (mount) vagy írd ki a képfájlt."),
    (r"(?:run|start|launch|execute)\s+(?:the\s+)?(?:setup|installer|install)(?:\.exe)?.*\bupdate\b", "Futtasd a setup.exe-t, és telepítsd a frissítést a játék könyvtárába."),
    (r"(?:run|start|launch|execute)\s+(?:the\s+)?(?:setup|installer|install)(?:\.exe)?|^install(?:\s+the\s+game)?[.!]?$",
     "Futtasd a setup.exe-t, és telepítsd a játékot."),
    (r"copy.*?(?:crack|content|contents|files|everything|all)\b.*?(?:from|of|in)\s+(?:the\s+)?[\"'“/]?(?P<dir>[\w.\-]+)[\"'”]?\s+"
     r"(?:dir|folder|directory)\b.*?(?:install|game)",
     "Másold a(z) {dir} mappa tartalmát a játék telepítési könyvtárába (a meglévő fájlokat írd felül)."),
    (r"copy.*crack", "Másold a crack fájljait a játék telepítési könyvtárába (a meglévő fájlokat írd felül)."),
    (r"copy.*(?:update|files|content).*(?:install|game)", "Másold a fájlokat a játék telepítési könyvtárába (a meglévő fájlokat írd felül)."),
    (r"\bfirewall\b", "Tiltsd le a játék exe-jét a tűzfalban, hogy ne próbáljon online csatlakozni."),
    (r"\badmin(?:istrator)?\b", "Ha a C: meghajtóra telepítetted, futtasd rendszergazdaként."),
    (r"already\s+cracked|pre-?cracked|crack\w*\s+(?:is\s+)?(?:already\s+)?(?:included|applied)", "A játék már crackelt, külön teendő nincs."),
    (r"install\s+(?:the\s+)?update", "Telepítsd a frissítést."),
    (r"^(?:play|enjoy|have\s+fun)\b.*$|^(?:start|launch|run)\s+(?:the\s+)?game[.!]*$", "Játssz!"),
]
SKIP_LINES = re.compile(r"(?i)support\s+the\s+(?:software\s+)?(?:developers?|companies)|if\s+you\s+(?:like|enjoy)|buy\s+it|"
                        r"^(?:see|read)\s+(?:the\s+)?nfo")


def translate_step(line: str) -> tuple[str, bool]:
    """(Hungarian text, matched?) of one English NFO install line."""

    text = line.strip().rstrip(".")
    for pattern, hungarian in PHRASES:
        match = re.search(pattern, text, re.I)
        if match:
            return hungarian.format(dir=match.groupdict().get("dir") or "crack"), True
    return text, False


def _dedupe(items: list[str]) -> list[str]:
    out: list[str] = []
    for item in items:
        if item and item not in out:
            out.append(item)
    return out


def _numbered(steps: list[str]) -> str:
    return "\n".join(f"{index}. {step}" for index, step in enumerate(steps, 1))


def _cut(text: str, limit: int = MAX_TEXT) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind("\n\n"), cut.rfind(". "))
    return cut[: end + 1].strip() if end > limit // 2 else cut.rsplit(" ", 1)[0] + " …"


HU_MONTH_NAMES = ["január", "február", "március", "április", "május", "június", "július", "augusztus", "szeptember", "október",
                  "november", "december"]


def hungarian_date(iso: str) -> str:
    """'2025-11-20' -> '2025. november 20.'; '2025' stays."""

    match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", iso or "")
    if not match or not 1 <= int(match.group(2)) <= 12:
        return iso or ""
    return f"{match.group(1)}. {HU_MONTH_NAMES[int(match.group(2)) - 1]} {int(match.group(3))}."


def _clean_title(title: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[™®©]", "", title or "")).strip()


# --- installation guide -----------------------------------------------------------------------------------

def install_guide(files: gamefiles.GameFiles, parsed: dict, *, hungarian: bool = False) -> tuple[str, list[str]]:
    """(Hungarian guide, warnings) from the file facts, the name and the NFO install block."""

    warnings: list[str] = []
    layout, kind = files.layout, parsed.get("kind", "full")
    group = (parsed.get("group") or "").upper()
    crack = files.crack_dirs[0] if files.crack_dirs else ""
    image = files.disc_images[0].rsplit("/", 1)[-1] if files.disc_images else ""
    setup = files.setup.rsplit("/", 1)[-1] if files.setup else "setup.exe"
    nfo_steps = files.nfo.get("install") or []
    precracked = files.nfo.get("already_cracked") or group in PRECRACKED_GROUPS or parsed.get("source") == "elamigos"
    notes: list[str] = []
    if files.redist_dirs:
        notes.append(f"Ha a játék hiányzó DLL-re panaszkodik, telepítsd a(z) {files.redist_dirs[0]} mappában lévő csomagokat "
                     "(DirectX, Visual C++).")

    if layout == "gog" or (parsed.get("source") == "gog" and layout in {"unknown", "installer"}):
        main = files.setup.rsplit("/", 1)[-1] if files.setup else "setup_….exe"
        parts = " (a hozzá tartozó .bin fájlok legyenek mellette, ugyanabban a mappában)" if files.gog_parts else ""
        if files.gog_patches and not files.gog_installers:  # a patch-only release
            patch = files.gog_patches[0].rsplit("/", 1)[-1]
            steps = ["Előfeltétel: a játék GOG-verziója legyen telepítve (a patch a fájlnévben szereplő korábbi verziót frissíti).",
                     f"Futtasd a(z) {patch} fájlt, és válaszd ki a játék telepítési mappáját.", "Játssz!"]
            return _numbered(steps), warnings
        if kind == "dlc":
            steps = ["Ha még nincs fent, telepítsd az alapjátékot (GOG-verzió).",
                     f"Futtasd a(z) {main} fájlt{parts}, és telepítsd a játék mappájába.", "Játssz!"]
        else:
            steps = [f"Futtasd a(z) {main} fájlt{parts}."]
            if hungarian:
                steps.append("A telepítő elején válaszd ki a nyelvet (magyar is választható).")
            steps.append("Telepítsd & Játssz!")
        extras = [x.rsplit("/", 1)[-1] for x in files.dlc if x != files.setup]
        if extras:
            steps.append("A DLC-k telepítőit (" + ", ".join(extras[:4]) + ") a játék után futtasd, ugyanabba a mappába.")
        if files.gog_patches:
            steps.append("A patch_….exe a régebbi telepített verziót frissíti; friss telepítéshez nem kell.")
        notes.insert(0, "A GOG-verzió DRM-mentes: crack és internetkapcsolat nem szükséges.")
        return _numbered(steps) + "\n\n" + "\n".join(notes), warnings

    if layout in {"fitgirl", "dodi"}:
        steps = []
        if files.verify:
            verify = next((v.rsplit("/", 1)[-1] for v in files.verify if v.lower().endswith(".bat")), "")
            steps.append("(Ajánlott) Ellenőrizd a letöltött fájlokat: " + (f"futtasd a(z) „{verify}” fájlt, vagy " if verify else "")
                         + "az MD5 mappában a QuickSFV.exe-t.")
        option = ""
        if files.optional or files.selective:
            names = ", ".join(x.rsplit("/", 1)[-1] for x in (files.optional + files.selective)[:6])
            option = f" A választható részeket ({names}) a telepítő elején kapcsolhatod be/ki; amit nem töltöttél le, azt hagyd kikapcsolva."
        steps.append(f"Futtasd a(z) {setup} fájlt.{option}")
        steps.append("Telepítsd & Játssz! A crack a telepítéskor automatikusan felkerül.")
        notes.insert(0, "A kicsomagolás sok időt, processzort és memóriát igényelhet (a telepítő ezt jelzi).")
        return _numbered(steps) + "\n\n" + "\n".join(notes), warnings

    if layout == "elamigos":
        steps = ["Csomagold ki a .rar fájlokat (7-Zip vagy WinRAR: „Kibontás ide” az első köteten)." if files.archives else "",
                 f"Futtasd a(z) {setup} fájlt.", "Telepítsd & Játssz! A játék már crackelt, külön crack nem kell."]
        return _numbered([s for s in steps if s]) + ("\n\n" + "\n".join(notes) if notes else ""), warnings

    if layout == "portable":
        helpers = re.compile(r"(?i)^(?:unins\d*|vc_?redist.*|dxsetup|dotnet.*|crashreport.*|unitycrashhandler\w*|ue4prereqsetup.*)\.exe$")
        mains = [x for x in files.executables if not helpers.match(x.rsplit("/", 1)[-1])
                 and not {d.lower() for d in x.split("/")[:-1]} & gamefiles.REDIST_DIRS]
        exe = (mains or files.executables or ["a játék exe-jét"])[0].rsplit("/", 1)[-1]
        steps = ["Másold a mappát tetszőleges helyre (pl. C:\\Games\\).", f"Indítsd: {exe} — telepítés nem szükséges.", "Játssz!"]
        return _numbered(steps) + ("\n\n" + "\n".join(notes) if notes else ""), warnings

    # scene / P2P style: disc image, packed set, plain installer, update, crackfix
    if nfo_steps:
        translated, unmatched = [], []
        for line in nfo_steps:
            if SKIP_LINES.search(line):
                continue
            text, ok = translate_step(line)
            if not ok:
                unmatched.append(line)
                text = f"({text})"
            translated.append(text)
        translated = _dedupe(translated)
        if unmatched:
            warnings.append("telepítés: az NFO néhány lépését nem tudtam lefordítani, ellenőrizd: " + "; ".join(unmatched[:4]))
        if translated:
            for line in files.nfo.get("notes") or []:
                text, ok = translate_step(line)
                if ok and text not in translated and text not in notes and not text.startswith(("Futtasd", "Csatold", "Csomagold", "Játssz")):
                    notes.append(text)
            head = ""
            if kind == "update":
                head = _update_head(files, parsed) + "\n"
            return head + _numbered(translated) + ("\n\n" + "\n".join(notes) if notes else ""), warnings

    steps: list[str] = []
    if files.archives:
        steps.append("Csomagold ki a .rar fájlokat (7-Zip vagy WinRAR: „Kibontás ide” az első köteten).")
    if image or (files.archives and any(p.lower().endswith(".iso") for p in files.inner)):
        name = image or next((p.rsplit("/", 1)[-1] for p in files.inner if p.lower().endswith(".iso")), "")
        steps.append(f"Csatold fel a képfájlt ({name}) — Windows 10/11-en dupla kattintással —, vagy csomagold ki egy mappába.")
    if kind == "crackfix":
        steps.append(f"Másold a(z) {crack or 'crackfix'} mappa tartalmát a játék telepítési könyvtárába (a meglévő fájlokat írd felül).")
        steps.append("Játssz!")
        return _numbered(steps), warnings
    if kind == "update":
        steps.append(f"Futtasd a(z) {setup} fájlt, és telepítsd a frissítést a játék telepítési könyvtárába." if files.setup or image
                     else "Másold a frissítés fájljait a játék telepítési könyvtárába (a meglévő fájlokat írd felül).")
    else:
        steps.append(f"Telepítsd a játékot a(z) {setup} futtatásával.")
    if crack:
        steps.append(f"Másold a(z) {crack} mappa tartalmát a telepítési könyvtárba (a meglévő fájlokat írd felül).")
    elif precracked:
        steps.append("A játék már crackelt / DRM-mentes, külön crack másolása nem kell.")
    elif group and group not in PRECRACKED_GROUPS and parsed.get("source") in {"scene", "p2p"}:
        steps.append(f"Ha a képfájlon van {parsed['group']} vagy Crack mappa, annak tartalmát másold a telepítési könyvtárba (felülírással).")
        warnings.append("telepítés: a crack mappát nem láttam (a lemezkép/archívum nem volt listázható) — ellenőrizd a crack lépést")
    steps.append("Játssz!")
    head = _update_head(files, parsed) + "\n" if kind == "update" else ""
    return head + _numbered(steps) + ("\n\n" + "\n".join(notes) if notes else ""), warnings


def _update_head(files: gamefiles.GameFiles, parsed: dict) -> str:
    base = parsed.get("update_from") or files.nfo.get("update_from") or ""
    requires = files.nfo.get("requires", "")
    text = "Előfeltétel: a játék telepített " + (f"v{base} verziója" if base else "alapverziója")
    return text + (f" ({requires})" if requires else "") + "; erre telepítsd a frissítést."


# --- naming -------------------------------------------------------------------------------------------------

def torrent_name(title: str, parsed: dict, *, version: str = "") -> str:
    """'Játék neve - Alcím v1.2' / '... v1.2 GoG' / '... v1.2 - FitGirl Repack' / '... v1.2 - MULTi8 - FitGirl Repack'."""

    name = re.sub(r"\s*:\s*|\s+[—–]\s+", " - ", _clean_title(title)).strip(" -")
    kind = parsed.get("kind", "full")
    if kind == "update":
        name += " Update"
    elif kind == "crackfix":
        name += " Crackfix"
    elif kind == "dlc" and not re.search(r"(?i)\bdlc\b", name):
        name += " DLC"
    version = version or parsed.get("version", "")
    if version:
        name += " v" + (version[1:] if re.match(r"[vV]\d", version) else version)
    elif parsed.get("build"):
        name += f" Build {parsed['build']}"
    if "hu" in (parsed.get("languages") or []):
        name += " HUN"
    source = parsed.get("source", "")
    if source == "gog":
        name += " GoG"
    elif parsed.get("repack"):
        multi = f" - MULTi{parsed['multi']}" if parsed.get("multi") else ""
        name += f"{multi} - {parsed['repack']}"
    return name.strip(" -")


def _norm(text: str) -> str:
    return steam.norm_title(text)


def _display_title(store_title: str, parsed: dict) -> tuple[str, str]:
    """(title, subtitle) for Meta: the store title, plus what the release name adds (edition / subtitle)."""

    title = _clean_title(store_title) or gamename.full_title(parsed, edition=False)
    extra = []
    for part in (parsed.get("subtitle", ""), parsed.get("edition", "")):
        if part and _norm(part) not in _norm(title):
            extra.append(part)
    return title, " - ".join(extra)


# --- lookups ------------------------------------------------------------------------------------------------

def gather(parsed: dict, files: gamefiles.GameFiles, hints: dict) -> dict:
    """Every outside lookup; returns raw-but-parsed source data for compose()."""

    data: dict = {"warnings": []}
    titles = gamename.search_titles(parsed)
    dlc = parsed.get("kind") == "dlc"
    match_titles = titles[:-1] if dlc and len(titles) > 1 else titles  # a DLC must not match its base game
    version = str(hints.get("version") or "").strip().lstrip("vV") or parsed.get("version") or files.nfo.get("version", "")
    data["version"] = version
    is_gog = parsed.get("source") == "gog" or files.layout == "gog" or bool(hints.get("gog"))
    bare = titles[-1] if titles else ""
    if not titles and not hints.get("steam") and not hints.get("gog") and not files.gog.get("gog_id"):
        data["warnings"].append("a névből nem derült ki a játék címe — add meg: --title")
        return data

    def gog_lookup(gog_id: str, via: str) -> None:
        data["gog_id"], data["gog_via"] = gog_id, via
        data["gog"] = gog.product(gog_id)
        data["gog_game"] = gog.game(gog_id)
        data["gog_builds"] = gog.builds(gog_id)
        product = data["gog"]
        if product and product.get("type") == "pack" and not product.get("installers"):
            base = gog.find(titles[1:] or titles)  # the edition pack has no installers/builds
            if base and base["id"] != gog_id:
                data["gog_base"] = gog.product(base["id"])
                data["gog_builds"] = gog.builds(base["id"])
        # GOGDB's JSON (/data/) is disallowed by its robots.txt: only its product page is used, as a link

    def steam_lookup(appid: str) -> None:
        data["steam"] = steam.app(appid)
        if data["steam"]:
            item = steam.store_item(appid)
            data["steam_tags"] = steam.tags_hu(appid, item=item)
            data["steam"]["cover_url"] = steam.cover_url(appid, item.get("assets"))
            if net.request("HEAD", data["steam"]["cover_url"], retries=0, quiet=True) is None:
                data["steam"]["cover_url"] = data["steam"].get("header_url", "")

    if is_gog:
        gog_id = str(hints.get("gog") or files.gog.get("gog_id") or "")
        if gog_id:
            gog_lookup(gog_id, "hint" if hints.get("gog") else "innoextract")
        else:
            hit = gog.find(match_titles, terms=titles, dlc=dlc)
            if hit:
                data["gog_hit"] = hit
                gog_lookup(hit["id"], "search")

    appid = str(hints.get("steam") or "")
    if not appid:
        hit = steam.find(match_titles, terms=titles)
        if hit:
            appid, data["steam_hit"] = hit["appid"], hit
    if appid:
        steam_lookup(appid)
    if not data.get("steam") and not data.get("gog"):
        hit = gog.find(match_titles, terms=titles, dlc=dlc)  # titles only on GOG (old games), even without a GoG marker
        if hit:
            data["gog_hit"] = hit
            gog_lookup(hit["id"], "search")
    if not dlc and (not data.get("steam") or (is_gog and not data.get("gog"))):
        # store titles change ("... — Remastered"); PCGamingWiki's page title stays and its infobox has the ids
        data["pcgw"] = pcgw.lookup(steam_appid=(data.get("steam") or {}).get("appid", ""), gog_id=data.get("gog_id", ""), title=bare)
        if not data.get("steam") and data["pcgw"].get("steam_appid"):
            steam_lookup(data["pcgw"]["steam_appid"])
            data["steam_via"] = "pcgw"
        if not data.get("gog") and data["pcgw"].get("gog_id") and (is_gog or not data.get("steam")):
            gog_lookup(data["pcgw"]["gog_id"], "pcgw")
    if is_gog and not data.get("gog"):
        data["warnings"].append("GOG: nem találtam a játékot a GOG katalógusban")

    if not data.get("steam") and not data.get("gog"):
        if keys.get("IGDB_CLIENT_ID") and keys.get("IGDB_CLIENT_SECRET"):
            candidates = [igdb.parse_game(g) for g in igdb.search(bare, "pc", limit=8) if isinstance(g, dict)] if bare else []
            data["igdb"] = steam.best_match(candidates, titles) or {}
        else:
            data["warnings"].append("IGDB kihagyva: nincs IGDB_CLIENT_ID / IGDB_CLIENT_SECRET kulcs")

    steam_info, gog_game = data.get("steam") or {}, data.get("gog_game") or {}
    has_requirements = any(label for label, _ in (steam_info.get("requirements") or {}).get("minimum", [])) or \
        any(label for label, _ in (gog_game.get("requirements") or {}).get("minimum", []))
    hungarian = steam_info.get("hungarian") or gog_game.get("hungarian") or (data.get("gog") or {}).get("hungarian")
    if "pcgw" not in data and (not has_requirements or hungarian or not (steam_info or gog_game)):
        data["pcgw"] = pcgw.lookup(steam_appid=steam_info.get("appid", ""), gog_id=data.get("gog_id", ""), title=bare)

    wiki = {}
    if steam_info.get("appid"):
        wiki = wikidata.lookup("steam", steam_info["appid"])
    slug = (data.get("gog") or {}).get("slug") or (data.get("gog_hit") or {}).get("slug")
    if not wiki and slug:
        wiki = wikidata.lookup("gog", "game/" + slug)
    data["wikidata"] = wiki
    if wiki.get("huwiki"):
        data["huwiki"] = wikipedia.summary(wiki["huwiki"], "hu")
    return data


# --- composition --------------------------------------------------------------------------------------------

def _hungarian_flags(data: dict) -> dict:
    steam_info, gog_game, product = data.get("steam") or {}, data.get("gog_game") or {}, data.get("gog") or {}
    row = (data.get("pcgw") or {}).get("hungarian") or {}
    flags = {"interface": None, "subtitles": None, "audio": None, "fan": bool(row.get("fan"))}
    store_text = None
    if steam_info or gog_game or product:
        store_text = bool(steam_info.get("hungarian") or gog_game.get("hungarian") or product.get("hungarian"))
        flags["interface"] = flags["subtitles"] = store_text
        flags["audio"] = bool(steam_info.get("hungarian_audio") or gog_game.get("hungarian_audio"))
    pcgw_text = bool(row.get("interface") or row.get("subtitles"))
    for key in ("interface", "subtitles", "audio"):
        value = row.get(key)
        if value is None or (value and flags["fan"]):  # a fan translation is not in the release: only noted
            continue
        # PCGW refines the store's single "Hungarian" flag; it never removes it on its own
        if value or flags[key] is None or (store_text and pcgw_text):
            flags[key] = value
    return flags


def _flags_text(flags: dict) -> str:
    if all(flags[k] is None for k in ("interface", "subtitles", "audio")):
        return "ismeretlen"
    words = {True: "igen", False: "nem", None: "?"}
    text = f"felület: {words[flags['interface']]}, felirat: {words[flags['subtitles']]}, szinkron: {words[flags['audio']]}"
    return text + (" (rajongói magyarítás)" if flags.get("fan") else "")


def compose(parsed: dict, files: gamefiles.GameFiles, data: dict, *, hints: dict | None = None, original: bool = False,
            release_name: str = "", code: str = "") -> Meta:
    hints = hints or {}
    meta = Meta(kind="game", platform="PC" if not parsed.get("platform") else parsed["platform"].capitalize())
    meta.warnings.extend(files.warnings + data.get("warnings", []))
    steam_info, product = data.get("steam") or {}, data.get("gog") or {}
    gog_game, base = data.get("gog_game") or {}, data.get("gog_base") or {}
    igdb_info, pcgw_info, wiki = data.get("igdb") or {}, data.get("pcgw") or {}, data.get("wikidata") or {}
    gog_first = parsed.get("source") == "gog" or files.layout == "gog"

    # identity: the first title that matches the release name (stores rename games: "... — Remastered")
    store_title = (product.get("title") if gog_first else "") or steam_info.get("name") or product.get("title") or igdb_info.get("name", "")
    wanted = {_norm(t) for t in gamename.search_titles(parsed)}
    candidates = [product.get("title", ""), steam_info.get("name", ""), igdb_info.get("name", ""), pcgw_info.get("page", ""), wiki.get("label_en", "")]
    if not gog_first:
        candidates[0], candidates[1] = candidates[1], candidates[0]
    display = next((c for c in candidates if c and _norm(c) in wanted), "")
    if not display and store_title and not wanted:
        display = store_title
    meta.title, meta.subtitle = _display_title(display, parsed)
    if hints.get("title"):
        meta.title = hints["title"]
    # a real Hungarian title only: the English label is our title and the Hungarian one differs
    if wiki.get("label_hu") and _norm(wiki.get("label_en", "")) == _norm(meta.title) and _norm(wiki["label_hu"]) != _norm(meta.title):
        meta.title_hu = wiki["label_hu"]
    for name, value in (("steam", steam_info.get("appid")), ("gog", data.get("gog_id")), ("igdb", igdb_info.get("igdb_id")),
                        ("wikidata", wiki.get("qid")), ("pcgw", pcgw_info.get("page"))):
        if value:
            meta.identifiers[name] = str(value)
    for source, present in (("GOG", product or gog_game), ("Steam", steam_info), ("IGDB", igdb_info), ("PCGamingWiki", pcgw_info),
                            ("Wikidata", wiki)):
        if present:
            meta.add_source(source)
    if files.nfo:
        meta.add_source("NFO")

    # version and its proof
    installers = (base or product).get("installers") or []
    builds = data.get("gog_builds") or (data.get("gogdb") or {}).get("builds") or []
    version = data.get("version") or ""
    if not version and files.layout == "gog" and files.setup:
        parsed_installer = gamename.parse_gog_installer(files.setup, versions=[b["version"] for b in builds] + [i["version"] for i in installers])
        version = parsed_installer.get("version", "")
    meta.version = version
    if gog_first and (builds or installers):
        hit = gog.match_version(version, builds) or gog.match_version(version, installers)
        if hit:
            meta.extra["version_proof"] = f"GOG build {hit['version']}" + (f" ({hit['date']})" if hit.get("date") else "")
        elif version:
            latest = (builds or installers)[0].get("version", "")
            meta.warnings.append(f"a verzió (v{version}) nem szerepel a GOG buildek között (legújabb: {latest}) — ellenőrizd")
        elif builds or installers:
            meta.version = (builds or installers)[0].get("version", "")
            meta.warnings.append(f"a verzió nem derült ki a névből; a GOG szerinti legújabb: {meta.version}")
    if parsed.get("build"):
        meta.extra["build"] = parsed["build"]

    # people and dates
    developers = gog_game.get("developers") or steam_info.get("developers") or (data.get("gog_hit") or {}).get("developers") or igdb_info.get("developers") or []
    publishers = gog_game.get("publishers") or steam_info.get("publishers") or (data.get("gog_hit") or {}).get("publishers") or igdb_info.get("publishers") or []
    if not gog_first:
        developers = steam_info.get("developers") or developers
        publishers = steam_info.get("publishers") or publishers
    meta.creators = _dedupe([d.strip() for d in developers])
    meta.publisher = ", ".join(_dedupe([p.strip() for p in publishers]))
    meta.date = steam_info.get("date") or gog_game.get("date") or product.get("release_date") or (data.get("gog_hit") or {}).get("release_date", "")
    if gog_first and gog_game.get("date"):
        meta.date = gog_game["date"]
    meta.year = meta.date[:4] if meta.date else igdb_info.get("year", "")
    if meta.date:
        meta.extra["release_date"] = hungarian_date(meta.date)
    if steam_info.get("early_access") or parsed.get("early_access"):
        meta.extra["early_access"] = "igen"
        meta.warnings.append("korai hozzáférésű (Early Access) játék")
    if steam_info.get("coming_soon"):
        meta.warnings.append("a Steam szerint a játék még nem jelent meg — ellenőrizd az azonosítást")

    # genres, tags
    genres = steam_info.get("genres_hu") or gog.genres_hu((data.get("gog_hit") or {}).get("genres") or gog_game.get("tags") or []) \
        or gog.genres_hu(igdb_info.get("genres") or [])
    meta.genres = _dedupe(genres)
    if data.get("steam_tags"):
        meta.extra["steam_tags"] = ", ".join(data["steam_tags"])

    # languages
    languages = (gog_game.get("languages") or product.get("languages")) if gog_first else []
    languages = languages or steam_info.get("languages") or gog_game.get("languages") or product.get("languages") or \
        [row["name"] for row in pcgw_info.get("languages") or [] if row.get("interface") or row.get("subtitles") or row.get("audio")]
    meta.languages = _dedupe(languages)
    flags = _hungarian_flags(data)
    meta.extra["hungarian"] = _flags_text(flags)
    if flags["interface"] or flags["subtitles"]:
        meta.extra["hun_name_hint"] = "HUN"
        if "magyar" not in meta.languages:
            meta.languages.append("magyar")
    audio = steam_info.get("audio") or gog_game.get("audio") or []
    if audio:
        meta.extra["audio_languages"] = ", ".join(audio)

    # requirements: Steam (Hungarian labels) > GOG > PCGamingWiki; GOG first for GOG releases
    order = [steam_info.get("requirements"), gog_game.get("requirements")]
    for candidate in (order[::-1] if gog_first else order) + [pcgw_info.get("requirements")]:
        text = steam.requirements_text(candidate or {})
        if text:
            meta.requirements = text
            break

    # descriptions
    _describe(meta, steam_info, product or base, gog_game, igdb_info, data.get("huwiki") or {})

    # cover and proof links
    gog_cover = gog_game.get("cover_url") or (data.get("gog_hit") or {}).get("cover_url", "")
    meta.cover_url = (gog_cover if gog_first else "") or steam_info.get("cover_url") or gog_cover or igdb_info.get("cover_url", "")
    gog_links = [product.get("url") or gog_game.get("url") or (data.get("gog_hit") or {}).get("url", ""), gogdb.product_url(data.get("gog_id", ""))]
    if base:
        gog_links.append(gogdb.product_url(base.get("id", "")))
    links = gog_links + [steam_info.get("url", "")] if gog_first else [steam_info.get("url", "")] + gog_links
    website = steam_info.get("website") or pcgw_info.get("official_site") or wikidata.claim(wiki, "website")
    if website and not re.search(r"(?i)web\.archive\.org|wikipedia|fandom\.com", website):
        links.append(website)
    for link in links:
        meta.add_proof(link)
    if not meta.proof_links:
        meta.warnings.append("nincs igazoló link (Steam/GOG oldal) — add meg kézzel")

    # files, install guide, naming
    meta.extra["files"] = "; ".join(files.facts)
    source = parsed.get("source") or {"gog": "gog", "fitgirl": "fitgirl", "dodi": "dodi", "elamigos": "elamigos"}.get(files.layout, "")
    if source:
        label = SOURCE_LABEL.get(source, source)
        meta.extra["source"] = f"{label} ({parsed['group']})" if source in {"scene", "p2p"} and parsed.get("group") else (parsed.get("repack") or label)
    guide, guide_warnings = install_guide(files, {**parsed, "source": source}, hungarian=bool(flags["interface"] or flags["subtitles"]))
    meta.install_hu = guide
    meta.warnings.extend(guide_warnings)
    full = meta.title + (f" - {meta.subtitle}" if meta.subtitle else "")
    if (original or not full.strip()) and release_name:  # an original release keeps its name; no title: nothing better
        meta.extra["torrent_name"] = release_name
    else:
        meta.extra["torrent_name"] = torrent_name(full, {**parsed, "source": source}, version=meta.version)
    has_image = bool(files.disc_images) or any(p.lower().endswith(".iso") for p in files.inner)
    packed_scene = files.layout == "packed" and not files.inner and source in {"scene", "p2p"}
    has_image = has_image or packed_scene
    meta.extra["category_hint"] = "game_iso" if has_image and files.layout in {"disc_image", "packed", "unknown"} else "game_rip"
    if code in {"game_iso", "game_rip"} and code != meta.extra["category_hint"]:
        meta.warnings.append(f"kategória: a fájlok alapján inkább {meta.extra['category_hint']} ({'lemezkép' if has_image else 'telepítő/repack'})")
    if not original:
        meta.warnings.append(SCREENSHOT_WARNING)

    # confidence: exact ids > a store title equal to the release title > a fuzzy or bridged match
    if (data.get("gog_via") in {"innoextract", "hint"} and (product or gog_game)) or (hints.get("steam") and steam_info):
        meta.confidence = 0.95
    elif store_title:
        meta.confidence = 0.9 if display else 0.75
    else:
        meta.confidence = 0.3
        meta.warnings.append("a játékot egyik adatbázisban sem találtam (Steam/GOG/IGDB) — ellenőrizd a nevet, vagy add meg: --title")
    return meta


def looks_hungarian(text: str) -> bool:
    """translate.language() == 'hu' and enough Hungarian accented letters (its no-langdetect fallback also counts
    the English article 'a')."""

    accents = len(re.findall(r"[áéíóöőúüű]", (text or "").lower()))
    return translate.language(text) == "hu" and accents >= max(3, len(text) // 60)


def _describe(meta: Meta, steam_info: dict, product: dict, gog_game: dict, igdb_info: dict, huwiki: dict) -> None:
    """Hungarian text priority: Steam HU store text > huwiki summary (CC BY-SA) > machine translation > English."""

    english = steam_info.get("about_en") or steam_info.get("short_en") or product.get("description_en") or gog_game.get("description_en") \
        or igdb_info.get("summary", "")
    meta.description_en = _cut(english)
    for text in (steam_info.get("about_hu", ""), steam_info.get("short_hu", "")):
        if len(text) >= 40 and looks_hungarian(text):
            meta.description_hu, meta.description_source = _cut(text), "Steam"
            return
    if len(huwiki.get("extract", "")) >= 80:
        meta.description_hu, meta.description_source = _cut(huwiki["extract"]), wikipedia.CREDIT
        return
    short = steam_info.get("short_en") or product.get("lead_en") or gog_game.get("description_en") or igdb_info.get("summary", "")
    short = _cut(short, 1200)
    if short:
        text, translated = translate.to_hungarian(short)
        if translated:
            meta.description_hu, meta.machine_translated = text, True
            meta.description_source = "Steam" if steam_info.get("short_en") else "GOG" if product or gog_game else "IGDB"
            return
    if meta.description_en:
        meta.description_source = "Steam" if steam_info else "GOG" if product or gog_game else "IGDB" if igdb_info else ""
        meta.warnings.append("nincs magyar leírás (DeepL kulcs nélkül angol marad) — fordítsd le vagy írd meg")
    else:
        meta.warnings.append("nincs leírás egyik forrásból sem")


# --- entry point --------------------------------------------------------------------------------------------

def resolve(release, code: str, *, original: bool, hints: dict) -> Meta:
    """Meta(kind="game") for a PC game release (see module docstring)."""

    hints = dict(hints or {})
    parsed = gamename.parse(release.name)
    files = gamefiles.inspect(release, deep=hints.get("deep", True))
    if files.layout == "gog" and files.setup:
        parsed["source"] = parsed["source"] or "gog"
        if not parsed["title"] or release.name.lower().startswith("setup_"):  # a bare installer, not a named folder
            installer = gamename.parse_gog_installer(files.setup)
            parsed.update({"title": installer.get("title", ""), "subtitle": "", "edition": "", "version": installer.get("version", "")})
    if not parsed.get("title") and files.nfo.get("title"):  # "[FitGirl Repack]", "-RUNE": the NFO names the game
        from_nfo = gamename.parse(files.nfo["title"])
        parsed.update({k: from_nfo[k] for k in ("title", "subtitle", "edition")})
    if hints.get("title"):
        override = gamename.parse(hints["title"])
        parsed.update({k: override[k] for k in ("title", "subtitle", "edition") if override.get(k) or k == "subtitle"})
    if hints.get("version"):
        parsed["version"] = str(hints["version"]).lstrip("vV")
    if not parsed.get("version") and files.nfo.get("version"):
        parsed["version"] = files.nfo["version"]
    if not parsed.get("build") and files.nfo.get("build"):
        parsed["build"] = files.nfo["build"]
    data = gather(parsed, files, hints)
    meta = compose(parsed, files, data, hints=hints, original=original, release_name=release.name, code=code)
    nfo.annotate(meta, release)
    nfo.description(meta, release)
    return meta
