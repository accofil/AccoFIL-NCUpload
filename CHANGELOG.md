# AccoFIL NCUpload – változások

## 0.7.2

- Minden feltöltés (az eredeti release is) saját, hardlinkes NCUP-könyvtárból seedel, a forrással azonos
  fájlrendszeren (ha a beállított NCUP másik lemezen van: `~/NCUP` vagy a forrás mellé `.ncup`). Így egy másik
  tracker torrentjének törlése vagy áthelyezése nem viszi el az nCore-seed fájljait, és fordítva.
- A torrent `ncupload` qBittorrent-kategóriát (ha nincs beállítva más) és `ncupload` címkét kap.
- Feltöltés előtt a qBittorrentből ellenőrzi, hogy a forrás letöltése befejeződött-e (előfoglalással a félkész
  fájl teljesnek látszik). Ha ez nem ellenőrizhető, feltöltés és `--auto` nincs.
- A `_iNT`/`_INT` végű scene-nevek eredeti release-ként ismerhetők fel.
- A szárazpróba és az elutasított/hibás terv nem hagy NCUP-munkamappát; feltöltés után a köztes kibontott fák
  törlődnek, a jegyzék rögzíti a torrent azonosítóját.
- A seedelés ellenőrzése legfeljebb 20 percig vár (hash-ellenőrzés, sor), a sorban álló torrentet elindítja.
- A 400-nál több találatú dupe-keresés átnézésre tesz, nem állítja le a futást vagy a köteget.
- Az adatlap csak `~/.config/ncore-up/answers/` alól vagy `--answers` kapcsolóval töltődik be, titkos fájlra nem
  hivatkozhat; a leírásban talált titokszerű adat (süti, kulcs, passkey) megállítja a feltöltést.
- Zene: ha a talált kiadás kiadója/katalógusszáma ütközik a fájlokéval, átnézésre kerül (más edíció).
- Új `--seed-audit`: összeveti a feltöltésnaplót a qBittorrenttel.
- Blu-ray ISO csak UDF-ként, `-t udf`-fel csatolható; a root segéd elkülönített Python-módban fut.
- Lejárt gyorsítótár-bejegyzéseknél is érvényes a lekérések ütemezése; az IMDb-javaslat-API ténylegesen
  csak `NCORE_IMDB_SUGGEST=1` mellett megy; borítót csak a robots.txt engedélyével tölt le.
- Az nCore felé küldött User-Agent nem tartalmaz repó-linket; a tesztadatokból kikerültek a valódi tiltólista-nevek.
- A nem használt `lxml`, `isbnlib` és `EbookLib` függőség kikerült.

## 0.7.1

- MIT-licenc a forráskódhoz és a Python-csomaghoz.
- A telepítő megtartja a már telepített rendszercsomagokat, a szerver csomagrögzítéseivel együtt.
- Az új függőségeknél az APT tényleges telepítési jelöltjét ellenőrzi; a nem elérhető csomagokat felsorolja.
- Az APT-telepítés nem távolíthat el meglévő csomagokat.
- Regressziós tesztek a csomagrögzítésekhez; elavult kézi tracklista-elvárás javítása a tesztben.

## 0.7.0

- AccoFIL NCUpload márkanév, rövid `ncupload` indító, kompatibilis `ncore-up` parancs.
- Interaktív Linux-telepítő: összes útvonal, qBittorrent WebUI, 17 API-mező, heti tiltólista.
- Későbbi újrakonfigurálás: `ncupload --configure`.
- Egyedi konfigurációs útvonal és BDInfo-bináris.
- Opcionális Debian/Ubuntu csomagtelepítés, mkbrr-letöltés kiadói SHA-256 ellenőrzéssel.
- Felhasználónévhez nem kötött heti systemd user timer.
- Kezdőbarát magyar telepítési és használati útmutató, generált teljes kapcsolólista,
  API-kulcs leírások és hibaelhárítás.

## A korábbi fejlesztésből megőrzött funkciók

- 27 nem XXX kategória, automatikus felismerés és kézi pontosítás.
- Eredeti és saját mód, részletes zenei leírás mindkét esetben.
- Fájltagek, NFO, CUE/log, ffprobe/MediaInfo és külső adatbázisok feldolgozása.
- Teljes payload-techinfo és feltöltés előtti fájllista-egyeztetés.
- NCUP-előkészítés, szabály szerinti kísérőfájl-kezelés, archívumkibontás és olvasható Blu-ray ISO mount.
- TXT/JSON batch, állapotmentés, dupe- és tiltólista-ellenőrzés, megszakítás utáni folytatás.

Az új 0.7.0 telepítőhöz teljes alkalmazásteszt-futtatás nem történt; a korábbi verziók
teszteredményei nem minősítik automatikusan ezt a változatot.
