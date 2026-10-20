# AccoFIL NCUpload – hibakeresés

## Gyors sorrend

1. Másold ki a pontos hibaüzenetet és a kiírt naplómappa útvonalát.
2. Ellenőrizd a telepítést: `ncupload --version`.
3. Belépés: `ncupload --check`.
4. Kulcsok: `ncupload --doctor`, majd szükség esetén `--doctor --doctor-live --json`.
5. Friss tiltólista: `ncupload --refresh-banlist`.
6. Ugyanazzal a bemenettel `--dry-run`; nézd meg a `requirements.json` és `review.json` fájlt.
7. Ha már létrejött adatlap, új feltöltés helyett a kiírt `--resume` parancsot használd.

## Telepítési hibák

| Jelenség | Teendő |
|---|---|
| `python3: command not found` | `sudo apt-get install python3 python3-venv python3-pip`. |
| Python 3.10 alatti verzió | Újabb támogatott Linux/Python szükséges; ellenőrizd: `python3 --version`. |
| `ensurepip is not available` / venv hiba | Telepítsd a Python-verzióhoz tartozó `python3-venv` csomagot, majd indítsd újra a telepítőt. |
| Rootként indul / jogosultsági hiba | Normál felhasználóval futtasd. A telepítő a csomagokhoz használ sudót. |
| A pip nem éri el a csomagforrást | Ellenőrizd DNS/internet/proxy és a `ca-certificates` csomagot; a telepítést indítsd újra. |
| `ncupload: command not found` | `export PATH="$HOME/.local/bin:$PATH"`, vagy a kiírt abszolút indítóútvonal. |
| Rendszercsomag nem található | `sudo apt-get update`. Az unrarhoz szükség lehet Debian non-free tárolóra. Az adott rendszer támogatott tárolóját használd. A telepítő kihagyja a nem elérhető opcionális csomagokat és felsorolja őket. |
| `has no installation candidate`, miközben az eszköz már telepítve van | Használd a 0.7.1 vagy újabb telepítőt. Ez megtartja a már telepített csomagokat, és tiszteletben tartja a szerver APT-rögzítéseit. Ellenőrzés: `apt-cache policy CSOMAGNÉV`; a rögzítéseket ne töröld a telepítés kedvéért. |
| mkbrr letöltési/checksum hiba | Ne használd a hibás binárist. A [hivatalos kiadásokból](https://github.com/autobrr/mkbrr/releases) válassz Linux és megfelelő CPU-architektúrát, ellenőrizd a checksumot, majd `--configure`. |
| `bad interpreter` / `^M` | Unix LF sorvégek szükségesek; Gitből klónozva a repó beállítja. |
| Titkos bevitelnél nincs kurzormozgás | Normális: a jelszó/kulcs rejtett. Írd be, majd Enter. |
| „Interaktív terminál szükséges” | SSH-terminálból futtasd a `bash install.sh` vagy `ncupload --configure` parancsot; ne pipe-old a varázslót. |

## nCore-belépés

Lejárt vagy hibás sütiknél:

1. Böngészőben jelentkezz be újra.
2. Exportáld csak az nCore sütijeit Netscape formátumban.
3. Másold a szerverre, majd:

```bash
ncupload --import-cookies "/teljes/ut/friss-cookies.txt"
ncupload --check
```

Az importált új sütik csak sikeres belépésellenőrzés után cserélik a korábbi munkamenetet.
JSON-export, hibás domén vagy üres fájl nem megfelelő.
Ha a belépést captcha/emberi ellenőrzés akadályozza, a saját böngésződben fejezd be a belépést.

## API-hibák

| Jelzés / HTTP-hiba | Teendő |
|---|---|
| `MISSING_CREDENTIALS` | A kiírt szolgáltatás kulcsa hiányzik. `--configure` vagy `--import-keys`. |
| `NOT_CHECKED` | Be van állítva, de az élő elérhetőséget még nem igazolta. `--doctor --doctor-live`. |
| `401` / hitelesítési hiba | Ellenőrizd a kulcs típusát, érvényességét és a kulcspár mindkét felét. |
| `403` | Szolgáltatói jogosultság, projekt/API-engedélyezés vagy kulcskorlátozás lehet hibás. |
| `429` | Kvóta/sebességkorlát. Várd meg a keret visszaállását; a program cache-t és ütemezést használ. |
| `INVALID_RESPONSE` / `LIVE_FAILED` | Az adatforrás nem az elvárt adatot adta. Nézd meg a forrás nevét és a szolgáltatói állapotot. |
| Google Books 403 / 0 találat | A Books API ugyanabban a projektben legyen engedélyezve, amelyhez a kulcs tartozik. Ellenőrizd a kvótát és a szerver kimenő IP-korlátozását. Egyetlen keresés üres eredménye nem bizonyít belépési hibát. |
| Spotify / IGDB nem működik | Client ID + Secret együtt szükséges; ellenőrizd a fejlesztői fiók aktuális hozzáférését. |
| DeepL hitelesítés | API-kulcs kell a `DEEPL_KEY` mezőbe, nem a webes fiók jelszava. |

## Kategória, leírás, dupe és szabályok

- Nem felismerhető kategória: `ncupload --categories`, majd `--category KOD`.
- Rossz találat: `--imdb` / `--mbid` / `--discogs` / `--spotify` / `--isbn` / `--steam` /
  `--gog`, illetve a név/év/előadó pontosító kapcsolók.
- Dupe-jelölt: nézd meg a meglévő adatlapot, ne engedélyezd automatikusan.
  A `review.json` megmutatja a döntés helyét; csak tényleges szabályzati indokkal engedélyezd.
- Hiányzó forrás, telepítési útmutató vagy saját képek: pótold a megfelelő mezőket/adatlapot.
- Tiltólista elavult/hiányzik: `ncupload --refresh-banlist`.
  Új, nem értelmezhető tiltólista-szabály kézi programfrissítést/áttekintést igényelhet.
- Saját feltöltés nem engedélyezett fájlokkal: a program NCUP-változatot készíthet.
  Ismeretlen, telepítéshez szükséges fájlt nem dob el automatikusan.
- A `--force` nem kapcsolja ki a kötelező feltöltési ellenőrzéseket.

## NCUP, tárhely, RAR és Blu-ray ISO

- „NCUP célmappa nincs megadva”: `ncupload --configure`, teljes írható útvonal.
- Jogosultsági hiba: ellenőrizd a saját felhasználó írási jogát és a qBittorrent olvasási jogát.
  Ne oldd meg általános `chmod 777` beállítással; a konkrét tulajdonost/csoportot igazítsd.
- Tárhely: `df -h`. Más fájlrendszerre másolásnál a kiadás teljes méretével számolj.
- Ne add új bemenetként az NCUP gyökerét vagy egy korábbi előkészítés teljes munkamappáját.
- RAR: szükséges a teljes multipart készlet és támogatott 7z/unrar. Hiányzó kötet,
  jelszavas vagy sérült archívum nem javítható egyszerű fájlátnevezéssel.
- Blu-ray ISO: a kibontáshoz Linux mount/unshare és sudo jogosultság kell; csak saját mód készít
  belőle külön BDMV kiadást. A mount csak olvasható, elkülönített névtérben történik.
- A program nem transzkódol automatikusan, és nem épít DVD-mappából automatikusan ISO-t.
- Új Inno Setup telepítőnél a disztribúció innoextract verziója régi lehet.
  [Hivatalos innoextract](https://github.com/dscharrer/innoextract): támogatott újabb verzióra lehet szükség.

## qBittorrent / seedelés

| Hiba | Teendő |
|---|---|
| WebUI nem érhető el | Ellenőrizd a címet/portot és hogy a WebUI fut; a `127.0.0.1` a torrentszervert jelenti. |
| Login hiba / 403 | `--configure`: WebUI felhasználónév/jelszó; sok hibás próbálkozás IP-tiltást okozhat. |
| `missingFiles` / `error` | A qBittorrent számára ugyanazon az útvonalon elérhető-e a teljes payload? Docker-útvonalak és fájljogok ellenőrzése. |
| `checking` / seed még nem igazolt | Várd meg az ellenőrzést, majd a kiírt `--resume` paranccsal folytasd. |
| `paused` / `stopped` | Indítsd el a klienstorrentet, majd `--resume`. |
| Feltöltve, de seedhiba | Ne töltsd fel újra: őrizd meg a naplókat, adatokat, és használd a `--resume NAPLOMAPPA` parancsot. |

A feltöltés utáni seedindítás időkorlátjára figyelj; a program az oldal követelményéhez igazodva
30 percen belüli indítást kér. A `--no-seed` használatával ezt neked kell megoldanod.

## Ütemezett tiltólista hibája

```bash
systemctl --user status ncore-up-banlist.timer
systemctl --user status ncore-up-banlist.service
journalctl --user -u ncore-up-banlist.service -n 50 --no-pager
loginctl show-user "$USER" -p Linger
```

`Failed to connect to bus` esetén user systemd munkamenet hiányozhat; jelentkezz be normál
SSH-felhasználóval, és ellenőrizd a linger beállítást. Ennek hiányában a kézi frissítés vagy dokumentált
cron használható. A lejárt süti az ütemezett frissítést is elrontja.

## Kilépési kódok

| Kód | Jelentés |
|---|---|
| 0 | Sikeres művelet / szárazpróba / tudatos kihagyás. |
| 2 | Bemenet, beállítás, helyi fájl vagy eszköz hibája. |
| 3 | nCore-művelet/belépés hibája. |
| 4 | Hálózati vagy munkamenet közbeni kapcsolatprobléma. |
| 5 | Kötelező tartalmi/követelményhiba, feltöltés leállítva. |
| 9 | nCore elutasítás vagy nem igazolt feltöltési eredmény; nézd meg az állapotot. |
| 10 | Átnézés/döntés/keret szükséges; batch szünetelhet. |
| 11 | Feltöltve, seedelés függőben; `--resume` szükséges. |
| 12 | Korábbi feltöltés bizonytalan, nem indít új POST-ot. |

A kilépési kódot közvetlenül a futás után az `echo $?` mutatja.
A konkrét művelet szöveges jelentése és állapotfájlja mindig fontosabb, mint egyetlen kódszám.

## Hibajelentés

Add meg a verziót, Linux/Python-verziót, a titkok nélküli parancsot, a pontos hibát és a
releváns `requirements.json` / `review.json` részletet.
Ne csatold a cookies.txt/config.env/API-kulcsokat, teljes torrentet, passkey-t,
teljes személyes naplócsomagot vagy a feltöltendő médiafájlokat.
