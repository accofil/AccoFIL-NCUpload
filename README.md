# AccoFIL NCUpload

**Univerzális nCore-feltöltő Linux torrentszerverre, az XXX kategóriák kivételével.**

Automatikus kategóriafelismerés, eredeti és saját kiadások, metaadatok és leírások,
teljes techinfo, szabályos NCUP-előkészítés, soros batch és qBittorrent seedelés.

## Itt kezdd

1. **[Telepítés teljesen kezdőknek](docs/installation.md)** – belépés a szerverre, letöltés, interaktív telepítő.
2. **[Használat lépésről lépésre](docs/usage.md)** – első szárazpróba, feltöltés, kategóriák, batch és folytatás.
3. **[Az összes kapcsoló](docs/options.md)** – a programból generált 60 parancssori elem és a telepítő kapcsolói.
4. **[API-kulcsok és létrehozásuk](docs/api-keys.md)** – mind a 17 támogatott mező.
5. **[Hibakeresés](docs/troubleshooting.md)** – belépés, API, tárhely, eszközök, seedelés, naplók és hibakódok.
6. **[Személyes beállítások kezelése](docs/security.md)**.

## Gyors indulás

Linux, Python 3.10+, működő nCore-fiók és qBittorrent WebUI szükséges.
A nyilvános repó letöltéséhez nem kell GitHub-fiók vagy GitHub-belépés.

```bash
git clone https://github.com/accofil/AccoFIL-NCUpload.git
cd AccoFIL-NCUpload
bash install.sh
```

A varázsló bekéri az összes útvonalat, qBittorrent-adatot és API-mezőt.
A nem használt API-k Enterrel kihagyhatók; a titkos értékek nem látszanak.

```bash
ncupload --check
ncupload --refresh-banlist
ncupload "/teljes/szerveres/ut/Kiadas" --dry-run
ncupload "/teljes/szerveres/ut/Kiadas"
```

Az utolsó parancs jóváhagyás után valóban feltölt. Beállítások módosítása: `ncupload --configure`.
A korábbi `ncore-up` indító is megmarad.

## Fő funkciók

| Terület | Működés |
|---|---|
| Kategóriák | 27 nem XXX kategória; automatikus felismerés vagy `--category`. |
| Eredeti release | Név/fájlnevek/NFO megtartása; zenei leírás és tracklista is készül. |
| Saját feltöltés | Szabályos fájlösszeállítás külön NCUP-mappába, a forrás megőrzésével. |
| Metaadatok | Fájltagek, NFO, CUE/log, ffprobe/MediaInfo, külső film/zene/könyv/játék/program adatbázisok. |
| Techinfo | A kész torrent összes fájlja, feltöltés előtti fájllista-egyeztetéssel. |
| Előkészítés | Támogatott archívumok kibontása, könyvtárrendezés; Blu-ray ISO olvasható mounttal. |
| Ellenőrzés | Dupe, tiltólista, kötelező adatok, képek és tartalmi eltérések. |
| Batch | TXT/JSON lista, tételenkénti mód és kategória, állapotmentés és folytatás. |
| Seedelés | qBittorrent WebAPI, visszaellenőrzés és `--resume`. |
| Tiltólista | Heti szerveroldali user timer vagy cron; futtatás előtt is ellenőrzi. |

## Mely esetekben kell saját adat?

Saját játék/program/konzol/mobil feltöltésnél a telepítést/futást igazoló saját képek és
szükség esetén magyar telepítési útmutató kell. Ismeretlen videóforrást és kétes dupe-találatot
ellenőrizni kell. Bizonytalan tételeket az automatikus mód átnézésre tesz félre.

A külső parancssori eszközök kategóriánként szükségesek. A telepítő telepítheti a disztribúcióban
elérhető csomagokat és a mkbrr-t; teljes Blu-ray elemzéshez külön megfelelő BDInfo CLI kell.
A program nem transzkódol automatikusan és nem futtat telepítőket.
A sütiket/kulcsokat ne tedd a repóba.

## Verzió és fejlesztés

Verzió: **0.7.3**. [Változások](CHANGELOG.md).

Licenc: [MIT](LICENSE). A 0.7.3 tesztelői kiadás; éles használat előtt nézd át a szárazpróba eredményét.
A forrás és a meglévő tesztek a repóban vannak. Az új telepítő szintaktikai és csomagolási
ellenőrzést kapott; ehhez a kiadáshoz teljes alkalmazásteszt-futtatás nem történt.

A program használatakor az aktuális nCore-szabályokat és a szolgáltatók feltételeit kövesd.
Ez a projekt nem az nCore hivatalos terméke.
