# AccoFIL NCUpload – változások

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
