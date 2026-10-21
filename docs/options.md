# AccoFIL NCUpload – az összes parancssori kapcsoló

Ez a lista közvetlenül a program argumentumdefinícióiból készült. A kapcsolók a `ncupload` és a régi `ncore-up` indítóval is használhatók.

`PATH` = a szerveren található teljes release-mappa vagy egyetlen fájl. Szóközös útvonalat idézőjelezz. Albumhoz az egész album mappáját add meg.

## Teljes kapcsolólista

| Kapcsoló | Mit csinál / választható érték |
|---|---|
| `-h`, `--help` | show this help message and exit |
| `--configure` | interaktív beállítóvarázsló: útvonalak, qBittorrent és API-kulcsok |
| `PATH` | a feltöltendő mappa vagy fájl |
| `--category`, `-c` | kategória kód (lásd --categories); alapból automatikus |
| `--original` | eredeti release-ként (NFO kell) |
| `--own` | nem release-ként (saját feltöltés) |
| `--imdb` | IMDb azonosító (tt...), ha az automatikus nem jó |
| `--other-link` | egyéb filmadatbázis link (alapból port.hu / tvmaze / TMDB) |
| `--name` | a torrent neve (nem release-nél alapból a sablon szerinti) |
| `--description` | leírás fájl (BBCode) a generált helyett |
| `--source` | a videó és hang ellenőrzött eredete a leírásban |
| `--extras` | 'Extra:' a leírásban |
| `--menu` | lemez menüje (ismeretlen esetben pontosítás kell); érték: `van`, `nincs` |
| `--tags` | stílus-címkék vesszővel (zene, e-könyv) |
| `--images` | 3 saját kép a generáltak helyett |
| `--cover` | infobar borítókép (≤300x300-ra kicsinyítem) |
| `--no-screens` | eredeti release-nél ne készüljön kép |
| `--no-make-sample` | ne vágjon sample-t, ha hiányzik (alapból vág, hardlinkes másolatba) |
| `--title` | cím |
| `--year` | év |
| `--artist` | előadó |
| `--album` | album |
| `--author` | szerző |
| `--isbn` | ISBN |
| `--narrator` | hangoskönyv felolvasója |
| `--mbid` | MusicBrainz release id vagy URL |
| `--discogs` | Discogs release id vagy URL |
| `--spotify` | Spotify albumazonosító vagy URL |
| `--steam` | Steam app id |
| `--gog` | GOG product id |
| `--platform` | konzol/platform (ps4, switch, ps2, windows, macos, ...) |
| `--version-hint` | verzió |
| `--package` | Android csomagnév |
| `--language` | tartalom nyelve (hu/en/de) |
| `--kind` | tartalomfajta (audiobook, book, comic, manga, magazine) |
| `--auto` | felügyelet nélkül: csak hibátlan és biztos azonosítású tervet tölt fel, a többit félreteszi |
| `--answers` | kiegészítő JSON adatlap; alapból ~/.config/ncore-up/answers/<név>.ncore.json |
| `--proof-link` | igazoló link, ismételhető |
| `--install-guide` | magyar telepítési útmutató UTF-8 fájlja |
| `--batch` | PATH közvetlen almappáinak/fájljainak soros feldolgozása |
| `--batch-list` | TXT/JSON feltöltési lista, tételenkénti beállításokkal |
| `--resume-batch` | megszakadt lista folytatása az eredeti beállításokkal |
| `--resume` | már feltöltött torrent seedelésének folytatása, új feltöltés nélkül |
| `--offline` | --dry-run mellett sütifájl nélkül készít tervet; online ellenőrzések függőben |
| `--daily-limit` | a rang napi kereténél szigorúbb helyi korlát; gördülő 24 óra, külső feltöltéseket nem számolja |
| `--request` | kérés azonosítója, ha kérést teljesítesz |
| `--edit` | feltöltés előtt megnyitja a leírást szerkesztésre ($EDITOR) |
| `--dry-run` | mindent előkészít és kiír, de nem tölt fel |
| `--yes`, `-y` | ne kérdezzen rá a feltöltésre |
| `--no-seed` | ne adja hozzá a qBittorrenthez |
| `--force` | --dry-run mellett a nem kötelező hibákkal is kiírja a tervet; feltöltési kaput nem old fel |
| `--categories` | kategória kódok listája |
| `--check` | nCore munkamenet ellenőrzése |
| `--refresh-banlist` | Wiki-tiltólista frissítése és változásnaplózása, feltöltés nélkül |
| `--import-cookies` | csak ncore.pro sütik ellenőrzött, védett telepítése Netscape fájlból |
| `--import-keys` | API-kulcsok védett beolvasása KEY=VALUE fájlból, értékek kiírása nélkül |
| `--doctor` | adatforrások és kulcsok állapota (alapból hálózati hívás nélkül) |
| `--doctor-live` | --doctor mellett kisméretű, élő adatforrás-próbák, feltöltés nélkül |
| `--json` | --doctor jelentés JSON formátumban |
| `--seed-audit` | a feltöltött torrentek seedelésének ellenőrzése (qBittorrent + fájlok), csak olvas |
| `--version` | show program's version number and exit |

## Kombinációk és elsőbbség

- `--original` és `--own` kizárják egymást. Nélkülük a név és NFO alapján választ módot.
- `--batch`, `--batch-list` és `--resume-batch` kizárják egymást.
- `--batch` közvetlen almappákat/fájlokat dolgoz fel; `--batch-list` kézzel összeállított listát.
- `--resume-batch queue.json`: ne adj mellé új PATH-ot; a mentett beállításokat folytatja.
- `--resume NAPLÓMAPPA`: már elküldött vagy bizonytalan kimenetelű egyetlen feltöltés egyeztetése/seedelése. Nem új feltöltési parancs.
- `--offline` csak `--dry-run` mellett működik. A hiányzó élő ellenőrzéseket függőben jelzi.
- `--doctor-live` mellé `--doctor` is kell. A `--doctor` önmagában nem végez élő hálózati próbát.
- `--json` a doctor és tiltólista jelentését teszi gépileg olvashatóvá.
- `--force` csak szárazpróba mellett segít a terv megtekintésében; kötelező feltöltési szabályt nem kerül meg.
- `--yes` az utolsó feltöltési kérdést hagyja ki. `--auto` a bizonytalan tételeket is félreteszi.
- `--no-seed` után a seedelés elindítása a te feladatod; normál használatnál hagyd el.
- `--no-screens` nem törli a kötelező képkövetelményeket. Saját játék/program/konzol/mobil képeihez `--images` kell.
- `--description` a generált leírást cseréli, ezért a kötelező tartalmakat is tartalmaznia kell.
- `--source` a videó és hang tényleges eredete; ismeretlen eredetet ne találj ki.
- `--images` pontosan 3 fájlt vár; `--proof-link` többször is megadható.
- Az automatikusan betöltött `~/.config/ncore-up/answers/<név>.ncore.json` adatlaphoz képest a kifejezett parancssori pontosítás elsőbbséget kap.
- `--configure` külön beállító művelet: ne kombináld feltöltési kapcsolókkal.

## A telepítő összes kapcsolója

| Kapcsoló (`bash install.sh ...`) | Jelentés |
|---|---|
| nincs kapcsoló | Végigvezet minden beállításon, rákérdez a rendszercsomagokra. |
| `-h`, `--help` | Telepítő súgója. |
| `--system` | Csak kiírja a Debian/Ubuntu csomagtelepítési parancsokat. |
| `--install-system` | A varázsló után telepíti az elérhető rendszercsomagokat sudo/apt segítségével. |
| `--config /teljes/út/config.env` | Egyedi konfigurációs fájl; az indító megjegyzi. |
| `--venv /teljes/út/venv` | Python környezet alapértéke. Módosítása újratelepítést igényel. |
| `--bin-dir /teljes/út/bin` | Az indítók mappájának alapértéke. |
| `--ncup-dir /teljes/út/NCUP` | NCUP célmappa alapértéke. |

## Beépített súgó

```text
usage: ncupload [-h] [--configure] [--category CATEGORY] [--original | --own]
                [--imdb IMDB] [--other-link OTHER_LINK] [--name NAME]
                [--description DESCRIPTION] [--source SOURCE]
                [--extras EXTRAS] [--menu {van,nincs}] [--tags TAGS]
                [--images KÉP KÉP KÉP] [--cover COVER] [--no-screens]
                [--no-make-sample] [--title TITLE] [--year YEAR]
                [--artist ARTIST] [--album ALBUM] [--author AUTHOR]
                [--isbn ISBN] [--narrator NARRATOR] [--mbid MBID]
                [--discogs DISCOGS] [--spotify SPOTIFY] [--steam STEAM]
                [--gog GOG] [--platform PLATFORM]
                [--version-hint VERSION_HINT] [--package PACKAGE]
                [--language LANGUAGE] [--kind KIND] [--auto]
                [--answers ANSWERS] [--proof-link PROOF_LINK]
                [--install-guide INSTALL_GUIDE] [--batch |
                --batch-list LISTA | --resume-batch QUEUE_JSON]
                [--resume NAPLÓMAPPA] [--offline] [--daily-limit DAILY_LIMIT]
                [--request REQUEST] [--edit] [--dry-run] [--yes] [--no-seed]
                [--force] [--categories] [--check] [--refresh-banlist]
                [--import-cookies FÁJL] [--import-keys FÁJL] [--doctor]
                [--doctor-live] [--json] [--version]
                [path]

AccoFIL NCUpload – feltöltés nCore-ra (XXX kivételével)

positional arguments:
  path                  a feltöltendő mappa vagy fájl

options:
  -h, --help            show this help message and exit
  --configure           interaktív beállítóvarázsló: útvonalak, qBittorrent és
                        API-kulcsok
  --category, -c CATEGORY
                        kategória kód (lásd --categories); alapból automatikus
  --original            eredeti release-ként (NFO kell)
  --own                 nem release-ként (saját feltöltés)
  --imdb IMDB           IMDb azonosító (tt...), ha az automatikus nem jó
  --other-link OTHER_LINK
                        egyéb filmadatbázis link (alapból port.hu / tvmaze /
                        TMDB)
  --name NAME           a torrent neve (nem release-nél alapból a sablon
                        szerinti)
  --description DESCRIPTION
                        leírás fájl (BBCode) a generált helyett
  --source SOURCE       a videó és hang ellenőrzött eredete a leírásban
  --extras EXTRAS       'Extra:' a leírásban
  --menu {van,nincs}    lemez menüje (ismeretlen esetben pontosítás kell)
  --tags TAGS           stílus-címkék vesszővel (zene, e-könyv)
  --images KÉP KÉP KÉP  3 saját kép a generáltak helyett
  --cover COVER         infobar borítókép (≤300x300-ra kicsinyítem)
  --no-screens          eredeti release-nél ne készüljön kép
  --no-make-sample      ne vágjon sample-t, ha hiányzik (alapból vág,
                        hardlinkes másolatba)
  --auto                felügyelet nélkül: csak hibátlan és biztos azonosítású
                        tervet tölt fel, a többit félreteszi
  --answers ANSWERS     kiegészítő JSON adatlap; alapból PATH.ncore.json
  --proof-link PROOF_LINK
                        igazoló link, ismételhető
  --install-guide INSTALL_GUIDE
                        magyar telepítési útmutató UTF-8 fájlja
  --batch               PATH közvetlen almappáinak/fájljainak soros
                        feldolgozása
  --batch-list LISTA    TXT/JSON feltöltési lista, tételenkénti beállításokkal
  --resume-batch QUEUE_JSON
                        megszakadt lista folytatása az eredeti beállításokkal
  --resume NAPLÓMAPPA   már feltöltött torrent seedelésének folytatása, új
                        feltöltés nélkül
  --offline             --dry-run mellett sütifájl nélkül készít tervet;
                        online ellenőrzések függőben
  --daily-limit DAILY_LIMIT
                        a rang napi kereténél szigorúbb helyi korlát; gördülő
                        24 óra, külső feltöltéseket nem számolja
  --request REQUEST     kérés azonosítója, ha kérést teljesítesz
  --edit                feltöltés előtt megnyitja a leírást szerkesztésre
                        ($EDITOR)
  --dry-run             mindent előkészít és kiír, de nem tölt fel
  --yes, -y             ne kérdezzen rá a feltöltésre
  --no-seed             ne adja hozzá a qBittorrenthez
  --force               --dry-run mellett a nem kötelező hibákkal is kiírja a
                        tervet; feltöltési kaput nem old fel
  --categories          kategória kódok listája
  --check               nCore munkamenet ellenőrzése
  --refresh-banlist     Wiki-tiltólista frissítése és változásnaplózása,
                        feltöltés nélkül
  --import-cookies FÁJL
                        csak ncore.pro sütik ellenőrzött, védett telepítése
                        Netscape fájlból
  --import-keys FÁJL    API-kulcsok védett beolvasása KEY=VALUE fájlból,
                        értékek kiírása nélkül
  --doctor              adatforrások és kulcsok állapota (alapból hálózati
                        hívás nélkül)
  --doctor-live         --doctor mellett kisméretű, élő adatforrás-próbák,
                        feltöltés nélkül
  --json                --doctor jelentés JSON formátumban
  --version             show program's version number and exit

pontosítás (ha az automatikus azonosítás téved):
  --title TITLE         cím
  --year YEAR           év
  --artist ARTIST       előadó
  --album ALBUM         album
  --author AUTHOR       szerző
  --isbn ISBN           ISBN
  --narrator NARRATOR   hangoskönyv felolvasója
  --mbid MBID           MusicBrainz release id vagy URL
  --discogs DISCOGS     Discogs release id vagy URL
  --spotify SPOTIFY     Spotify albumazonosító vagy URL
  --steam STEAM         Steam app id
  --gog GOG             GOG product id
  --platform PLATFORM   konzol/platform (ps4, switch, ps2, windows, macos,
                        ...)
  --version-hint VERSION_HINT
                        verzió
  --package PACKAGE     Android csomagnév
  --language LANGUAGE   tartalom nyelve (hu/en/de)
  --kind KIND           tartalomfajta (audiobook, book, comic, manga,
                        magazine)
```
