"""Console games (nCore "Konzol"): identify the game from its own files (``local.consoleid``), then enrich it from
the console databases and stores, in a fixed chain per platform. Every step is optional; a missing key or a dead
source only adds a warning.

    Switch       titledb (title id) → Nintendo of Europe search (nintendo.com page, packshot, product code) → GameTDB
    PS4          PKG param.sfo + icon0 → PS Store chihiro (exact content id, else title container) → ORBISPatches
    PS5          param.json / PKG → PROSPEROPatches
    PS3          PARAM.SFO / PKG → libretro DAT (serial) → GameTDB ps3tdb
    PS1/PS2/PSP  SYSTEM.CNF / PARAM.SFO serial → libretro DAT (Redump name, region, disc languages) → thumbnails
    Wii/GC       ID6 → GameTDB → libretro DAT (+ Redump page) → Nintendo of Europe (Wii) → thumbnails
    3DS          TMD/NCCH → 3dsdb product code → GameTDB 3dstdb → libretro No-Intro → Nintendo of Europe → thumbnails
    Xbox 360     XEX/STFS title id → Microsoft Store (Hungarian text when the publisher localized it)
    all          IGDB (key needed) for summary, genres, companies, cover and the igdb.com page

GameTDB uses manually saved dumps when its robots.txt forbids automatic
downloads, see ``sources.gametdb``); without it the libretro DATs, Nintendo of Europe and libretro-thumbnails fill in.

Hungarian description: Microsoft Store (hu) > Hungarian Wikipedia (through Wikidata's IGDB id) > machine translation
of the English text > English. Proof links: store / database pages, never Wikipedia.
"""

from __future__ import annotations

import re

from .. import describe, translate
from ..local import consoleid, nfo
from ..meta import Meta
from ..sources import gametdb, igdb, libretro_dat, msstore, psstore, titledb, wikidata, wikipedia

IGDB_PLATFORM = {"gc": "gamecube"}
GENRE_HU = {
    "action": "akció", "adventure": "kaland", "role-playing": "szerepjáték", "role playing": "szerepjáték", "rpg": "szerepjáték",
    "role-playing games (rpg)": "szerepjáték", "role_playing_games": "szerepjáték", "action rpg": "akció-szerepjáték",
    "racing": "autóverseny", "driving": "autóverseny", "racing & flying": "autóverseny", "sports": "sport", "sport": "sport",
    "strategy": "stratégiai", "simulation": "szimulátor", "simulator": "szimulátor", "puzzle": "logikai", "platformer": "platform",
    "platform": "platform", "shooter": "lövöldözős", "first-person shooter": "FPS", "shoot 'em up": "lövöldözős",
    "fighting": "verekedős", "music": "zenei", "rhythm": "zenei", "party": "party", "horror": "horror", "survival horror": "horror",
    "family": "családi", "educational": "oktató", "arcade": "arcade", "board game": "társasjáték", "card & board game": "társasjáték",
    "hack and slash/beat 'em up": "verekedős", "beat 'em up": "verekedős", "point-and-click": "kaland", "tactical": "taktikai",
    "turn based strategy (tbs)": "körökre osztott stratégia", "real time strategy (rts)": "valós idejű stratégia",
    "visual novel": "vizuális regény", "indie": "független", "quiz/trivia": "kvíz", "pinball": "flipper", "open world": "nyílt világ",
    "stealth": "lopakodós", "sandbox": "sandbox", "life simulation": "életszimulátor", "other": "egyéb", "misc": "egyéb",
}
REQUIRED_SYSTEM = {
    "ps4": "PS4: GoldHEN / HEN (5.05–11.00 firmware)",
    "ps5": "PS5: jailbreakelt konzol (pl. etaHEN)",
    "switch": "Switch: CFW (Atmosphère)",
    "ps3": "PS3: HEN/CFW",
    "ps2": "PS2: OPL / ODE",
    "ps1": "PS1: ODE / módosított konzol (vagy PS2 + POPStarter, emulátor)",
    "psp": "PSP: CFW (pl. ARK-4)",
    "vita": "PS Vita: HENkaku / Ensō + NoNpDrm",
    "xbox360": "Xbox 360: JTAG/RGH",
    "xbox": "Xbox: softmod / modchip",
    "wii": "Wii: Homebrew Channel + USB Loader GX",
    "gc": "GameCube: Swiss (vagy Wii-n Nintendont)",
    "wiiu": "Wii U: Aroma CFW",
    "3ds": "3DS: Luma3DS CFW",
}
_GENERIC_NOTE = "(Jellemző lépések; a pontos menüpontok a használt CFW / telepítő verziójától függnek.)"
INSTALL_HU = {
    "ps4": ("1. Másold a .pkg fájlokat exFAT (vagy FAT32) formátumú USB-meghajtóra.\n"
            "2. Indítsd el a GoldHEN-t (vagy a HEN-t), majd nyisd meg a Package Installer-t (Debug Settings → Game → Package Installer).\n"
            "3. Telepítsd előbb az alapjátékot, majd az update-et, végül a DLC-ket.\n"
            "4. Indítsd a játékot a főmenüből (a HEN-t a konzol újraindítása után újra be kell tölteni)."),
    "ps5": ("1. Jailbreakeld a konzolt (pl. etaHEN).\n"
            "2. Másold a játékot (.pkg vagy mappa) exFAT formátumú USB-meghajtóra vagy a belső tárhelyre.\n"
            "3. Telepítsd / csatold a jailbreak csomagkezelőjével, előbb az alapjátékot, majd az update-et.\n"
            "4. Indítsd a játékot a főmenüből."),
    "switch": ("1. Indítsd a konzolt Atmosphère CFW-vel.\n"
               "2. Másold az NSP/NSZ/XCI fájlokat az SD-kártyára (vagy telepíts USB-n / hálózaton keresztül).\n"
               "3. Telepítsd egy telepítővel (pl. DBI vagy Tinfoil): előbb az alapjátékot, majd az update-et és a DLC-ket.\n"
               "4. Indítsd a játékot a főmenüből."),
    "ps3": ("1. A konzolon HEN vagy CFW fusson (webMAN MOD-dal).\n"
            "2. Mappa (JB) formátum: másold a játék mappáját a /dev_hdd0/GAMES (vagy USB-n a /GAMES) mappába; ISO: a /dev_hdd0/PS3ISO mappába.\n"
            "3. PKG: telepítsd a Package Manager → Install Package Files menüből (a .rap licencfájl a /dev_hdd0/exdata mappába kell).\n"
            "4. Indítsd a játékot webMAN MOD-dal / a főmenüből."),
    "ps2": ("1. Másold az ISO-t FAT32/exFAT formátumú USB-meghajtóra (OPL: DVD vagy CD mappa) vagy a belső HDD-re.\n"
            "2. Indítsd az Open PS2 Loader-t (FreeMcBoot / FreeHDBoot alól), és a listából indítsd a játékot.\n"
            "3. ODE-nél másold a képfájlt a meghajtó kártyájára. (Emulátoron: PCSX2.)"),
    "ps1": ("1. BIN/CUE képfájl: ODE-vel (pl. XStation) vagy PS2-n POPStarter-rel futtatható.\n"
            "2. Emulátorban (DuckStation, ePSXe) nyisd meg a .cue fájlt."),
    "psp": ("1. Másold az ISO/CSO fájlt a memóriakártya ISO mappájába.\n"
            "2. CFW (pl. ARK-4) alatt az XMB Játék menüjéből indítható."),
    "vita": ("1. A konzolon HENkaku/Ensō fusson.\n"
             "2. Másold a játékot az ux0: tárhelyre (VitaShell), és telepítsd / frissítsd az adatbázist (NoNpDrm).\n"
             "3. Indítsd a LiveArea-ból."),
    "xbox360": ("1. GOD formátum: másold a <TitleID> mappát a merevlemez Content/0000000000000000 mappájába.\n"
                "2. Kibontott mappa: másold a merevlemezre (pl. Hdd1:/Games), és indítsd a default.xex-et.\n"
                "3. ISO: alakítsd GOD-ra (pl. Iso2God), vagy futtasd ODE-vel.\n"
                "4. Indítsd Aurora / FreeStyle Dash alól (RGH/JTAG)."),
    "xbox": ("1. Softmodolt / modchipes konzolon másold a játékot FTP-n az E:\\Games mappába (XISO-t előbb bontsd ki).\n"
             "2. Indítsd a dashboardból (pl. XBMC4Gamers)."),
    "wii": ("1. Másold a WBFS/ISO fájlt a FAT32 (vagy WBFS) formátumú USB-meghajtó /wbfs/<Játék neve [ID6]>/ mappájába.\n"
            "2. Indítsd a USB Loader GX-et a Homebrew Channelből, és válaszd ki a játékot.\n"
            "(RVZ: csak Dolphin emulátorban fut; konzolhoz alakítsd ISO/WBFS-re.)"),
    "gc": ("1. Wii-n: másold az ISO-t az USB/SD /games/<Játék neve [ID6]>/game.iso helyre, és indítsd Nintendont-tal.\n"
           "2. GameCube-on: Swiss-szel (SD2SP2 / ODE) indítható."),
    "wiiu": ("1. A konzolon Aroma CFW fusson.\n"
             "2. Telepítsd a játékot WUP Installer-rel (vagy másold WUA formátumban az SD-kártyára), majd indítsd a főmenüből."),
    "3ds": ("1. Másold a .cia fájlt az SD-kártyára.\n"
            "2. Luma3DS CFW alatt telepítsd FBI-jal (vagy Universal-Updater-rel).\n"
            "3. A játék a HOME menüből indítható. (.3ds fájl: flash kártyáról vagy emulátorból indítható, CIA-ra alakítva telepíthető.)"),
}
CONTENT_HU = {"base": "alapjáték", "update": "frissítés (update)", "dlc": "DLC", "demo": "demó"}


def _norm(text: str) -> str:
    return re.sub(r"[^0-9a-z]+", " ", (text or "").lower().replace("&", " and ")).strip()


def clean_title(title: str) -> str:
    """Store title → name part of the torrent name: no ™/®, ": " → " - "."""

    title = re.sub(r"[™®©]", "", title or "").translate({0x2019: "'", 0x2018: "'", 0x201C: '"', 0x201D: '"', 0x2013: "-"})
    title = re.sub(r"\s*:\s*", " - ", title)
    return re.sub(r"\s+", " ", title).strip(" -")


class Facts:
    """First-come values from the sources (callers go in priority order) plus where each came from."""

    def __init__(self):
        self.values: dict = {}
        self.origin: dict[str, str] = {}
        self.proofs: list[str] = []
        self.languages: list[str] = []
        self.genres: list[str] = []
        self.screenshots: list[str] = []

    def put(self, source: str, **values) -> None:
        for key, value in values.items():
            if value and not self.values.get(key):
                self.values[key] = value
                self.origin[key] = source

    def get(self, key: str, default=""):
        return self.values.get(key) or default

    def langs(self, codes) -> None:
        for code in codes or []:
            code = str(code).lower().split("-")[0].split("_")[0]
            if code and code not in self.languages:
                self.languages.append(code)

    def proof(self, url: str) -> None:
        if url and url not in self.proofs:
            self.proofs.append(url)


def resolve(release, code: str = "console", *, original: bool = False, hints: dict | None = None) -> Meta:
    hints = hints or {}
    meta = Meta(kind="console_game")
    nfo.annotate(meta, release)
    info = consoleid.identify(release, hints)
    # Header-derived identifiers/platform stay authoritative; the NFO can
    # supply a missing human-readable title/version for the normal lookup.
    if not info.title or info.title_from_name:
        info.title = nfo.value(release, "title", "game", "game name") or info.title
    info.version = info.version or nfo.value(release, "version", "game version")
    if not info.platform:
        platform_hint = nfo.value(release, "platform", "system", "console").lower()
        info.platform = consoleid.PLATFORM_ALIASES.get(platform_hint, "")
    meta.warnings += info.warnings
    facts = Facts()
    facts.langs(info.languages)
    platform = info.platform
    handler = {
        "switch": _switch, "ps4": _ps4, "ps5": _ps5, "ps3": _ps3, "ps2": _ps_disc, "ps1": _ps_disc, "psp": _ps_disc,
        "wii": _wii, "gc": _wii, "3ds": _3ds, "xbox360": _xbox360, "xbox": _xbox360, "vita": _vita,
    }.get(platform)
    exact = False
    if handler:
        try:
            exact = handler(info, facts, meta)
        except Exception as exc:  # noqa: BLE001 - a broken source must not stop the upload
            meta.warnings.append(f"forrás hiba ({platform}): {type(exc).__name__}")
    title = hints.get("title") or facts.get("title") or info.title
    try:
        _igdb(info, facts, meta, title)
    except Exception as exc:  # noqa: BLE001 - IGDB/Wikidata/Wikipedia trouble must not stop the upload
        meta.warnings.append(f"forrás hiba (IGDB): {type(exc).__name__}")
    title = hints.get("title") or facts.get("title") or info.title
    _fill_meta(meta, info, facts, title, hints)
    _description(meta, facts)
    nfo.description(meta, release)
    _naming(meta, info)
    if original and meta.extra.get("torrent_name"):  # an original release keeps its release name on nCore
        meta.extra["suggested_name"], meta.extra["torrent_name"] = meta.extra["torrent_name"], release.name
    _confidence(meta, info, exact)
    return meta


# --- platform chains ----------------------------------------------------------------------------------------------

def _switch(info: consoleid.ConsoleInfo, facts: Facts, meta: Meta) -> bool:
    base = info.base_title_id or info.title_id
    record = titledb.lookup(base) if base else {}
    exact = bool(record)
    if record:
        meta.add_source("titledb")
        meta.identifiers["nsuid"] = record.get("nsu_id", "")
        facts.put("titledb", title=record["name"], publisher=record["publisher"], developer=record["developer"],
                  date=record["release_date"], description_en=record["description"] or record["intro"])
        facts.langs(record.get("languages"))
        facts.genres += record.get("category") or []
        facts.screenshots += record.get("screenshots") or []
        versions = record.get("versions") or {}
        if versions:
            latest = max(versions)
            meta.extra["latest_update"] = f"v{latest} ({versions[latest]})"
            if info.version_int and info.version_int in versions:
                meta.extra["update_date"] = versions[info.version_int]
            if info.version_int and info.version_int < latest and info.content_type != "dlc":
                meta.warnings.append(f"van újabb frissítés is: v{latest} ({versions[latest]}); ez a release v{info.version_int}")
    elif base:
        meta.warnings.append(f"a titledb nem ismeri ezt a title ID-t: {base}")
    noe = titledb.noe_search(nsu_id=record.get("nsu_id", "")) if record.get("nsu_id") else {}
    if not noe and (record.get("name") or info.title):
        noe = titledb.noe_search(title=record.get("name") or info.title)
    _apply_noe(noe, facts, meta)
    tdb_id = titledb.product_code_id(noe.get("product_code", "")) if noe else ""
    if tdb_id:
        game = _gametdb("switch", tdb_id, meta)
        if game:
            meta.add_source("GameTDB")
            facts.put("GameTDB", description_en=game["synopsis"], developer=game["developer"], publisher=game["publisher"],
                      date=game["date"])
            facts.langs(game["languages"])
            facts.proof(gametdb.page_url("switch", game["id"]))
    if record:
        facts.put("titledb", cover_url=record["icon_url"], rating=f"PEGI {record['rating'].rstrip('+')}" if record["rating"] else "")
    return exact


def _apply_noe(noe: dict, facts: Facts, meta: Meta, languages: bool = True) -> None:
    """Nintendo of Europe catalogue record: official nintendo.com page (proof), packshot, short description. Its
    language list describes the European release, so a USA/JPN disc found by title does not take it."""

    if not noe:
        return
    meta.add_source("Nintendo")
    facts.proof(noe["url"])
    facts.put("Nintendo", cover_url=noe["image_url"], title=noe["title"], publisher=noe["publisher"], developer=noe["developer"],
              date=noe["release_date"], description_en=noe["excerpt"], rating=noe["age_rating"])
    if languages:
        facts.langs(noe["languages"])
    facts.genres += noe["categories"]
    if noe["product_code"]:
        meta.identifiers.setdefault("product_code", noe["product_code"])


def _ps4(info: consoleid.ConsoleInfo, facts: Facts, meta: Meta) -> bool:
    if info.icon:
        meta.cover = info.icon  # icon0.png from the PKG: the game's own 512x512 cover
    if info.title and not info.title_from_name:
        facts.put("PARAM.SFO", title=info.title)
    store = psstore.lookup(info.content_id, info.title_id) if (info.content_id or info.title_id.startswith("CUSA")) else {}
    if store:
        meta.add_source("PlayStation Store")
        if store.get("warning"):
            meta.warnings.append(store["warning"])
        if store["exact"]:
            facts.proof(store["url"])
            facts.put("PlayStation Store", title=store["name"], date=store["release_date"], cover_url=store["cover_url"])
        facts.put("PlayStation Store", description_en=store["description"], publisher=store["publisher"], rating=store["rating"])
        facts.genres += store["genres"]
        facts.screenshots += store["screenshots"]
    patches = psstore.patches_page(info.title_id) if info.title_id else {}
    if patches:
        meta.add_source("ORBISPatches")
        facts.proof(patches["url"])
        facts.put("ORBISPatches", title=patches["title"], publisher=patches["publisher"], cover_url=patches["icon"])
        if not info.content_id and patches.get("content_id"):
            info.content_id = patches["content_id"]
    if store and not store["exact"]:  # a bundle / other product: its art only when nothing better was found
        facts.put("PlayStation Store", cover_url=store["cover_url"])
    return bool(store and store.get("exact")) or bool(patches)


def _ps5(info: consoleid.ConsoleInfo, facts: Facts, meta: Meta) -> bool:
    if info.title and not info.title_from_name:
        facts.put("param.json", title=info.title)
    patches = psstore.patches_page(info.title_id) if info.title_id.startswith("PPSA") else {}
    if patches:
        meta.add_source("PROSPEROPatches")
        facts.proof(patches["url"])
        facts.put("PROSPEROPatches", title=patches["title"], publisher=patches["publisher"], cover_url=patches["icon"])
        if not info.content_id and patches.get("content_id"):
            info.content_id = patches["content_id"]
    if info.content_id:
        facts.proof(psstore.PRODUCT_PAGE.format(info.content_id))
    return bool(patches)


def _dat(system: str, key: str, facts: Facts, meta: Meta) -> dict:
    entries = libretro_dat.lookup(system, key) if key else []
    if not entries:
        return {}
    entry = entries[0]
    meta.add_source(libretro_dat.CREDIT)
    meta.extra["dat_name"] = entry["name"]
    facts.put("libretro", title=entry["title"], dat_region=entry["region"] or libretro_dat.region_from_name(entry["name"]))
    facts.langs(entry["languages"])
    return entry


def _thumbnail(system: str, entry: dict, facts: Facts) -> None:
    """libretro-thumbnails box art of the exact DAT name when no other cover was found."""

    if entry and not facts.get("cover_url"):
        facts.put("libretro-thumbnails", cover_url=libretro_dat.find_thumbnail(system, entry["name"]))


def _gametdb(platform: str, game_id: str, meta: Meta) -> dict:
    game = gametdb.lookup(platform, game_id) if game_id else {}
    if not game and gametdb.WARNING and gametdb.WARNING not in meta.warnings:
        meta.warnings.append(gametdb.WARNING)
    return game


def _ps3(info: consoleid.ConsoleInfo, facts: Facts, meta: Meta) -> bool:
    if info.title and not info.title_from_name:
        facts.put("PARAM.SFO", title=info.title)
    psn = info.title_id.startswith("NP")
    entry = _dat("ps3psn" if psn else "ps3", info.serial or info.title_id, facts, meta)
    if entry and not psn and info.serial:
        facts.proof(libretro_dat.redump_url(info.serial))
    game = _gametdb("ps3", info.title_id, meta) if not psn else {}
    if game:
        meta.add_source("GameTDB")
        facts.put("GameTDB", title=game["title"], description_en=game["synopsis"], developer=game["developer"],
                  publisher=game["publisher"], date=game["date"], dat_region=game["region"])
        facts.langs(game["languages"])
        facts.genres += game["genres"]
        facts.proof(gametdb.page_url("ps3", game["id"]))
        facts.put("GameTDB", cover_url=gametdb.find_cover("ps3", game["id"], game["region"]))
    _thumbnail("ps3psn" if psn else "ps3", entry, facts)
    return bool(entry or game)


def _ps_disc(info: consoleid.ConsoleInfo, facts: Facts, meta: Meta) -> bool:
    if info.platform == "psp" and info.title and not info.title_from_name:
        facts.put("PARAM.SFO", title=info.title)
    entry = _dat(info.platform, info.serial, facts, meta)
    if entry:
        facts.proof(libretro_dat.redump_url(info.serial))
        _thumbnail(info.platform, entry, facts)
        facts.screenshots += [libretro_dat.thumbnail_url(info.platform, entry["name"], k) for k in ("Named_Titles", "Named_Snaps")]
    elif info.serial:
        meta.warnings.append(f"a sorozatszám ({info.serial}) nincs a Redump/libretro adatbázisban")
    return bool(entry)


def _wii(info: consoleid.ConsoleInfo, facts: Facts, meta: Meta) -> bool:
    game = _gametdb(info.platform, info.id6, meta)
    if game:
        meta.add_source("GameTDB")
        facts.put("GameTDB", title=game["title"], description_en=game["synopsis"], developer=game["developer"],
                  publisher=game["publisher"], date=game["date"], dat_region=game["region"], rating=game["rating"])
        facts.langs(game["languages"])
        facts.genres += game["genres"]
        facts.proof(gametdb.page_url("wii", game["id"]))
        facts.put("GameTDB", cover_url=gametdb.find_cover("wii", game["id"], game["region"]))
        if game["type"] and game["type"] not in {"Wii", "GameCube"}:
            meta.extra["gametdb_type"] = game["type"]
    elif info.id6 and not gametdb.WARNING:
        meta.warnings.append(f"a GameTDB nem ismeri ezt az ID6-ot: {info.id6}")
    entry = _dat(info.platform, info.id6, facts, meta) if info.id6 else {}
    if entry and entry.get("serial"):
        facts.proof(libretro_dat.redump_url(entry["serial"].split(",")[0].strip()))
    if info.platform == "wii" and not game and facts.get("title"):
        _apply_noe(titledb.noe_search(title=facts.get("title"), system="wii"), facts, meta, languages=info.region in {"", "EUR", "RF"})
    _thumbnail(info.platform, entry, facts)
    return bool(game or entry)


def _3ds(info: consoleid.ConsoleInfo, facts: Facts, meta: Meta) -> bool:
    code = info.product_code
    if not code and (info.base_title_id or info.title_id):
        bridge = gametdb.product_code_3ds(info.base_title_id or info.title_id)
        code = bridge.get("product_code", "")
        facts.put("3dsdb", title=bridge.get("name", ""), publisher=bridge.get("publisher", ""))
    if code:
        meta.identifiers["product_code"] = code
        if not info.region:
            info.region = consoleid.region_from_product_code(code)
    game_id = gametdb.id_from_product_code(code)
    game = _gametdb("3ds", game_id, meta)
    if game:
        meta.add_source("GameTDB")
        facts.put("GameTDB", title=game["title"], description_en=game["synopsis"], developer=game["developer"],
                  publisher=game["publisher"], date=game["date"], dat_region=game["region"])
        facts.langs(game["languages"])
        facts.genres += game["genres"]
        facts.proof(gametdb.page_url("3ds", game["id"]))
        facts.put("GameTDB", cover_url=gametdb.find_cover("3ds", game["id"], game["region"]))
    entry = _dat("3ds", game_id, facts, meta) if game_id else {}
    noe = titledb.noe_search(product_code=code) if code else {}
    _apply_noe(noe, facts, meta)
    _thumbnail("3ds", entry, facts)
    if info.extra.get("publisher"):
        facts.put("SMDH", publisher=info.extra["publisher"])
    if info.title and not info.title_from_name:
        facts.put("SMDH", title=info.title)
    return bool(game or entry)


def _xbox360(info: consoleid.ConsoleInfo, facts: Facts, meta: Meta) -> bool:
    product = msstore.lookup_xbox360(info.title_id) if info.platform == "xbox360" and info.title_id else {}
    if not product and info.title:
        product = msstore.find_game(info.title)
    if info.title:
        facts.put("STFS/XEX", title=info.title)
    if product and product.get("via_title") or product and not info.title_id:
        meta.warnings.append(f"a Microsoft Store-adat cím alapján talált termékből jön ({product['title']}); újabb (PC/Xbox One) "
                             "kiadás leírása is lehet — ellenőrizd")
    if product:
        meta.add_source("Microsoft Store")
        meta.identifiers["msstore"] = product["product_id"]
        facts.proof(product["url"])
        facts.put("Microsoft Store", title=product["title"], publisher=product["publisher"], developer=product["developer"],
                  date=product["release_date"], cover_url=product["box_art"])
        if product["hungarian"]:
            facts.put("Microsoft Store", description_hu=product["description"] or product["short_description"])
        else:
            facts.put("Microsoft Store", description_en=product["description"] or product["short_description"])
        facts.screenshots += product["screenshots"]
        if product["category"]:
            facts.genres.append(product["category"])
    return bool(product) and bool(info.title_id)


def _vita(info: consoleid.ConsoleInfo, facts: Facts, meta: Meta) -> bool:
    entry = _dat("vita", info.title_id, facts, meta) if info.title_id else {}
    return bool(entry)


# --- shared steps -------------------------------------------------------------------------------------------------

def _igdb(info: consoleid.ConsoleInfo, facts: Facts, meta: Meta, title: str) -> None:
    if not title:
        return
    platform = IGDB_PLATFORM.get(info.platform, info.platform)
    games = [igdb.parse_game(g) for g in igdb.search(clean_title(title).replace(" - ", " "), platform)]
    if not games:
        return
    wanted = _norm(clean_title(title))
    game = next((g for g in games if _norm(g["name"]) == wanted), None)
    if game is None:
        game = next((g for g in games if wanted and (wanted in _norm(g["name"]) or _norm(g["name"]) in wanted)), None)
    if game is None:
        return
    meta.add_source("IGDB")
    meta.identifiers["igdb"] = str(game["igdb_id"])
    facts.put("IGDB", title=game["name"], description_en=game["summary"], developer=", ".join(game["developers"][:2]),
              publisher=", ".join(game["publishers"][:2]), year=game["year"], cover_url=game["cover_url"])
    facts.genres += game["genres"]
    if game["url"]:
        facts.proof(game["url"])
        slug = game["url"].rstrip("/").rsplit("/", 1)[-1]
        if not facts.get("description_hu"):
            _huwiki(slug, facts, meta)


def _huwiki(igdb_slug: str, facts: Facts, meta: Meta) -> None:
    item = wikidata.lookup("igdb", igdb_slug) if igdb_slug else {}
    if not item:
        return
    if item.get("label_hu"):
        facts.put("Wikidata", title_hu=item["label_hu"])
    if item.get("huwiki"):
        page = wikipedia.summary(item["huwiki"], "hu")
        if page.get("extract") and translate.is_hungarian(page["extract"]):
            facts.put(wikipedia.CREDIT, description_hu=page["extract"])
            meta.add_source("Wikipédia")


def _genres_hu(genres: list[str]) -> list[str]:
    out = []
    for genre in genres:
        for part in re.split(r"\s*[,/]\s*", str(genre)):
            key = part.strip().lower().replace("_", " ")
            hu = GENRE_HU.get(key) or GENRE_HU.get(part.strip().lower())
            if hu and hu not in out:
                out.append(hu)
    return out


def _fill_meta(meta: Meta, info: consoleid.ConsoleInfo, facts: Facts, title: str, hints: dict) -> None:
    meta.title = clean_title(title)
    meta.title_original = info.title if info.title and not info.title_from_name and _norm(info.title) != _norm(title) else ""
    meta.title_hu = facts.get("title_hu")
    meta.platform = info.platform_name
    meta.version = str(hints.get("version") or info.version)
    meta.publisher = facts.get("publisher") or info.extra.get("publisher", "")
    developer = facts.get("developer")
    meta.creators = [developer] if developer else []
    meta.date = facts.get("date")
    meta.year = (meta.date or facts.get("year"))[:4]
    meta.cover_url = facts.get("cover_url")
    meta.genres = _genres_hu(facts.genres)
    for url in facts.proofs:
        meta.add_proof(url)
    region = info.region
    if not region and facts.get("dat_region"):
        region = consoleid.region_from_dat(facts.get("dat_region"))
    warning = consoleid.region_warning(region)
    if warning and warning not in meta.warnings:  # also for a region found after identify() (3dsdb product code)
        meta.warnings.append(warning)
    meta.region = consoleid.region_label(region)
    if region:
        meta.extra["region_code"] = region
    if info.platform in {"ps4", "ps5"} and region in {"EUR", "USA"}:
        meta.extra["region_note"] = (f"{'EUR' if region == 'EUR' else 'USA'} kiadás ({info.content_id[:6] or '?'}); a játékok "
                                     "régiófüggetlenek, de az update/DLC csak azonos régiójú alapjátékhoz jó")
    meta.warnings = [w for w in meta.warnings if not w.startswith("a régiót nem sikerült")]  # the files' verdict, now superseded
    if not meta.region:
        meta.warnings.append("régió ismeretlen: add meg kézzel (NTSC (USA) / PAL (EUR) / Régiófüggetlen (RF))")
    hungarian = info.hungarian or "hu" in facts.languages
    meta.languages = [describe.language(code) for code in facts.languages]
    if hungarian and "magyar" not in meta.languages:
        meta.languages.insert(0, "magyar")
    meta.extra["hungarian"] = "igen" if hungarian else "nem"
    for key, value in (("titleid", info.title_id), ("base_titleid", info.base_title_id if info.base_title_id != info.title_id else ""),
                       ("content_id", info.content_id), ("serial", info.serial), ("id6", info.id6), ("media_id", info.media_id),
                       ("product_code", info.product_code)):
        if value:
            meta.identifiers[key] = value
    if info.platform in {"ps4", "ps5"} and info.title_id:
        meta.identifiers["cusa"] = info.title_id
    meta.extra.update({k: str(v) for k, v in info.extra.items() if k not in {"publisher"} and v})
    meta.extra["platform_code"] = info.platform
    meta.extra["platform_tag"] = f"[{info.tag}]" if info.tag else ""
    meta.extra["format"] = info.format
    meta.extra["content_type"] = CONTENT_HU.get(info.content_type, info.content_type)
    if facts.get("rating"):
        meta.extra["rating"] = facts.get("rating")
    if facts.screenshots:
        meta.extra["store_screenshots"] = "\n".join(dict.fromkeys(s for s in facts.screenshots if s))
    meta.warnings.append("a bolti / adatbázis-képernyőképek nem számítanak saját képnek: 3 saját kép kell (telepítés + játék közben)")
    meta.requirements = f"Jellemző követelmény: {REQUIRED_SYSTEM[info.platform]}" if info.platform in REQUIRED_SYSTEM else ""
    if info.extra.get("system_ver"):
        meta.requirements += f"; a PKG legalább {info.extra['system_ver']} firmware-t kér (alacsonyabbhoz backport kell)"
    meta.extra["required_system"] = meta.requirements
    meta.install_hu = f"{INSTALL_HU[info.platform]}\n{_GENERIC_NOTE}" if info.platform in INSTALL_HU else ""
    for source in info.sources:
        meta.add_source(source)


def _description(meta: Meta, facts: Facts) -> None:
    native = facts.get("description_hu")
    english = facts.get("description_en")
    meta.description_en = english
    if native:
        meta.description_hu = native
        meta.description_source = facts.origin.get("description_hu", "")
        return
    if not english:
        meta.warnings.append("nem találtam leírást a játékhoz — írd meg a leírásban (--edit)")
        return
    meta.description_source = facts.origin.get("description_en", "")
    text, translated = translate.to_hungarian(english)
    if translated:
        meta.description_hu, meta.machine_translated = text, True
    elif translate.is_hungarian(english):
        meta.description_hu = english
    else:
        meta.warnings.append("csak angol leírás van (magyar forrás nincs, gépi fordítás nélkül) — fordítsd le vagy add meg: --description")


def _naming(meta: Meta, info: consoleid.ConsoleInfo) -> None:
    """extra["torrent_name"]: "[PS4] Játék neve v1.05", "[NSW] Játék Update v65536", "... + DLC", " HUN" when Hungarian."""

    if not info.tag:
        return
    parts = [f"[{info.tag}]", meta.title or clean_title(info.title)]
    version = meta.version
    if info.platform == "switch" and re.fullmatch(r"v\d+", version or ""):
        meta.warnings.append("Switch: a megjelenített verziót (pl. v1.3.0) kulcsok nélkül nem lehet kiolvasni — a névben "
                             f"„{version}” szerepel, javítsd, ha tudod")
    label = version if version.startswith("v") else (f"v{version}" if version else "")
    if info.content_type == "update" and not any(c.get("type") == "base" for c in info.contents):
        parts += ["Update", label]
    elif info.content_type == "dlc" and not any(c.get("type") == "base" for c in info.contents):
        parts.append("DLC")
    else:
        parts.append(label)
        if any(c.get("type") == "dlc" for c in info.contents):
            parts.append("+ DLC")
    if meta.extra.get("hungarian") == "igen":
        parts.append("HUN")
    meta.extra["torrent_name"] = re.sub(r"\s+", " ", " ".join(p for p in parts if p)).strip()


def _confidence(meta: Meta, info: consoleid.ConsoleInfo, exact: bool) -> None:
    local = any(s not in {"név", "fájlnév", "név (régió)"} for s in info.sources)
    if exact and local:
        meta.confidence = 0.9
    elif exact:
        meta.confidence = 0.75
    elif local:
        meta.confidence = 0.6 if meta.description else 0.5
    elif info.platform and meta.title:
        meta.confidence = 0.35 + (0.15 if "IGDB" in meta.sources else 0.0)
    else:
        meta.confidence = 0.1
    if not info.platform:
        meta.confidence = min(meta.confidence, 0.2)
