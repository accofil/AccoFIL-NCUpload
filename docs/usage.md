# AccoFIL NCUpload – használat kezdőknek

## 1. Alapfogalmak

| Fogalom | Jelentés |
|---|---|
| PATH | A szerver teljes fájl- vagy kiadásmappa-útvonala. |
| Eredeti release | Változatlan kiadásnév, fájlnevek és NFO. |
| Saját / nem eredeti | A program külön, szabályos NCUP-változatot készíthet. |
| Szárazpróba | Előkészítés és ellenőrzés nCore-feltöltés nélkül. Fájlokat és lekérdezéseket készíthet. |
| Seedelés | A feltöltött torrent visszaosztása qBittorrenttel. |
| Infobar | Az adatlap borítót és alapadatokat mutató része. |
| Techinfo | A kész torrent minden fájljáról készített technikai jelentés. |

## 2. Első szárazpróba

Az `ls /home/USER/storage` megmutatja egy mappa tartalmát; a `USER` helyére a saját neved kerül.
Szóközös útvonalat idézőjelezz. Albumhoz az egész album mappáját add meg, ne egyetlen számot.
Egyetlen kiadásként ne a teljes torrentgyűjteményt válaszd.

```bash
ncupload "/home/USER/storage/Eloado - Album" --dry-run
```

A program felismeri a kategóriát és a módot. Megfelelő név és NFO esetén eredeti módot választ;
egyébként saját feltöltést. Az NFO-t mindkét esetben adatforrásként olvassa.

## 3. Mit olvass el az eredményből?

1. Kategória és eredeti/saját mód.
2. Azonosítás adatforrásai és bizonyossága.
3. Név, leírás és mellékletek.
4. Az NCUP célja és a kihagyott fájlok.
5. Hibák, kérdéses adatok és dupe-találatok.
6. A naplómappa útvonala.

`HIBA` / `FAIL`: kötelező eltérés. `KÉTSÉG` / `UNKNOWN`: döntés vagy ellenőrzött adat hiányzik.
`FIGYELEM`: nem feltétlenül blokkoló figyelmeztetés.
A program a zenei leírást és tracklistát fájltagekből, ffprobe/MediaInfóból, NFO/CUE-ból
és MusicBrainz/Discogs/Spotify adatokból állítja össze; végső tartalék a fájlnév.
A hiányzó tracklista önmagában nem megállási ok.

Sütifájl nélkül, az nCore-ellenőrzések kihagyásával:

```bash
ncupload "/home/USER/storage/Eloado - Album" --dry-run --offline
```

Az `--offline` az nCore-munkamenet nélküli száraz tervet jelenti:
a külső metaadatforrások lekérdezése továbbra is történhet. A hiányzó élő ellenőrzések függőben maradnak.

## 4. Kategória és mód pontosítása

```bash
ncupload --categories
ncupload "/zene/Album" --category lossless --own --dry-run
ncupload "/zene/Artist-Album-WEB-2026-GROUP" --original --dry-run
```

A `--categories` által kiírt kódot használd, ne a kategória teljes nevét.
`_hun` = magyar kategória. Eredeti módhoz NFO kell. Az XXX csoport kizárt.
A qBittorrent telepítőben megadott kategóriája ettől független.

## 5. Első éles feltöltés

Az átnézett szárazpróbával azonos bemenetet és pontosításokat add meg, `--dry-run` nélkül:

```bash
ncupload "/home/USER/storage/Eloado - Album"
```

A program elkészíti a torrentet, majd megkérdezi: **„Feltöltsem nCore-ra?”**
Az igen válasz után küldi el, kiírja az adatlap URL-jét és ellenőrzi a qBittorrent seedelést.
A seedelt NCUP-fájlokat ezután a helyükön kell tartani.

## 6. Gyakori példák

### Zene pontosítása

```bash
ncupload "/zene/Album" --own --artist "Előadó" --album "Album" --year 2026 --tags "rock,pop.rock" --dry-run
ncupload "/zene/Album" --mbid "https://musicbrainz.org/release/RELEASE_ID" --dry-run
```

A saját és eredeti mód ugyanazt a részletes leírást és tracklistát kapja.
A `RELEASE_ID` helyére valódi MusicBrainz-kiadványazonosító kell.
A Spotify metaadatforrás lehet; az igazoló linket a külön kiadványszabályok szerint ellenőrzi.

### Film és sorozatrész

```bash
ncupload "/filmek/Film.2026.1080p-GROUP" --original --dry-run
ncupload "/sorozat/Series.S01E03.mkv" --own --imdb tt1234567 --source "Teljes, ellenőrzött forrásrelease-név" --dry-run
```

Az IMDb-azonosító `tt...` alakú; sorozatnál a sorozat fő adatlapjához tartozzon.
Magyar címet és linket adatbázisból keres, hiányzó magyar címet nem talál ki.
Az ismeretlen videó-/hangforrás külön pontosítást igényel.

### Könyv, játék és program

```bash
ncupload "/konyvek/Konyv.epub" --isbn "9780000000000" --language hu --dry-run
ncupload "/jatekok/Jatek-GROUP" --steam "400" --dry-run
ncupload "/programok/Program" --own --install-guide "/adat/telepites.txt" --images "/kepek/1.png" "/kepek/2.png" "/kepek/3.png" --dry-run
ncupload "/mobil/alkalmazas.apk" --package "org.example.app" --dry-run
```

Az ISBN, Steam ID és csomagnév csak példa: a tényleges anyag azonosítója kell.
A telepítési útmutató magyar, UTF-8 szövegfájl. Saját játék/program/konzol/mobil feltöltéshez
saját telepítési és futási képek szükségesek. Internetes borító nem helyettesíti ezeket.
A program telepítőt vagy lemezkép tartalmát nem futtatja.

### Saját leírás és igazoló link

```bash
ncupload "/adat/Kiadas" --description "/adat/leiras.txt" --proof-link "https://kiado.example/kiadvany" --dry-run
```

A példa URL helyére valódi igazoló oldal kell. A `--description` teljesen lecseréli a generált
szöveget, így a kötelező tartalmakat is tartalmaznia kell.
`--proof-link` többször is megadható, `--images` pontosan három képet vár.

## 7. Adatlap a beállítások megjegyzéséhez

A `/adat/Film.mkv` bemenethez automatikusan keresi a `/adat/Film.mkv.ncore.json` fájlt.
Mappánál a mappa neve után kerül a `.ncore.json`. Példa UTF-8 JSON:

```json
{
  "category": "hdser",
  "original": false,
  "source": "Teljes, ellenőrzött forrásrelease-név",
  "hints": {"title": "Series", "year": "2026"},
  "proof_links": ["https://www.tvmaze.com/shows/123/example"],
  "images": ["/kepek/1.png", "/kepek/2.png", "/kepek/3.png"]
}
```

Más helyen tárolt adatlap: `--answers "/adat/adatlap.json"`.
A kiírt `review.json` további mintát ad. Dupe engedélyezését csak konkrét szabályzati indok alapján add meg;
a mintasémában szereplő `TORRENT_ID` helykitöltő.

## 8. Batch: lista szerinti soros feldolgozás

Másold a [TXT-mintát](examples/batch-list.txt), írd át saját útvonalakra.
Egy sor egy kiadás; `#` kezdetű sor megjegyzés.

```bash
ncupload --batch-list "/adat/lista.txt" --dry-run
ncupload --batch-list "/adat/lista.txt"
```

A [JSON-minta](examples/batch-list.json) tételenként saját kategóriát, módot, adatlapot,
címkét és `enabled` jelölést enged. `enabled: false` = kihagyás.

Mappa közvetlen elemei:

```bash
ncupload "/adat/feltoltendo" --batch --dry-run
```

Az elemek önálló kiadások legyenek; az album számai ne külön batch-tételek legyenek.
Átnézett lista automatikus feltöltése:

```bash
ncupload --batch-list "/adat/lista.json" --auto
```

A batch sorban halad, `queue.json` állapotot ment. Tartalmi hibánál továbbléphet;
belépési/keret/seedelési hiba vagy bizonytalan elküldés esetén megáll.
Folytatás a program által kiírt tényleges queue útvonallal:

```bash
ncupload --resume-batch "/naplok/batch-.../queue.json"
```

A szárazpróbás queue folytatva is szárazpróba. Éles futáshoz az eredeti listát indítsd a megfelelő módban.
`--yes` csak az utolsó kérdést hagyja ki; `--auto` a bizonytalan tételeket is félreteszi.

## 9. Megszakadt egyedi feltöltés folytatása

Ha az adatlap létrejött, de a seedelés nem igazolt, ne új feltöltést indíts:

```bash
ncupload --resume "/naplok/A_PROGRAM_ALTAL_KIIRT_FUTASMAPPA"
```

Bizonytalan elküldésnél az nCore-on egyezteti a torrentet.
A naplómappát és az NCUP-adatokat tartsd meg. [Részletes hibakeresés](troubleshooting.md).

## 10. Naplófájlok

| Fájl | Tartalom |
|---|---|
| NCUP `manifest.json` | Az előkészítés jegyzéke, a payloadon kívül. |
| `preview.json` | Név, kategória, útvonal, bizonyosság és hibák. |
| `leiras.txt` | Adatlap szövege. |
| `requirements.json` | Követelményenként PASS/FAIL/UNKNOWN. |
| `review.json`, `atnezesre.txt` | Pontosítandó adatok. |
| `state.json` | Feltöltési és seedelési folytatási állapot. |
| `local.torrent` és az oldal torrentje | Egyeztetés és seedelés. |
| `response.html` | nCore-válasz hibakereséshez. |
| Batch `queue.json` | A lista állapota és folytatási pontja. |

Ezekben személyes fájlnevek, útvonalak vagy hitelesítő adatok is lehetnek.
Hibajelentésbe csak átnézett, titkoktól megtisztított részletet másolj.

Folytatás: [összes kapcsoló](options.md), [API-k](api-keys.md), [hibakeresés](troubleshooting.md).
