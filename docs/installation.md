# AccoFIL NCUpload – telepítés lépésről lépésre

## 1. Hová kerül a program?

**A Linux torrentszerverre telepíted.** Windowsból terminállal lépsz be; a feltöltendő
fájloknak a szerveren kell lenniük. A `C:\...` útvonal nem szerveres útvonal.

Kell hozzá Python 3.10+, internet, nCore-fiók feltöltési jogosultsággal, qBittorrent WebUI,
olvasható források és írható NCUP célmappa. A telepítő Linuxra készült, az automatikus
rendszercsomag-telepítés Debian/Ubuntu rendszert használ.
Más Linuxon a külső programokat a saját csomagkezelőddel kell telepíteni.

Az NCUP a szabályosan összeállított kiadások külön mappája. A forrásokat megőrzi:
azonos fájlrendszeren hardlinket, másik lemezen másolatot készít. A másolat helyet foglal.

## 2. Lépj be a szerverre

Nyisd meg Windowsban a Terminál / PowerShell alkalmazást. A nagybetűs részeket cseréld saját adataidra.

```powershell
ssh -p PORT FELHASZNALO@SZERVER_IP
```

A `PORT` az SSH portja. Első belépéskor ellenőrizd a szerver azonosítóját, majd jelentkezz be
jelszóval vagy SSH-kulccsal. PuTTYban ugyanezeket a host/port/felhasználó adatokat add meg.
**A további parancsok már a szerver Linux-termináljába kerülnek.**

## 3. Az induláshoz szükséges csomagok

```bash
sudo apt-get update
sudo apt-get install -y git python3 python3-venv python3-pip ca-certificates gh
python3 --version
```

Legalább `3.10` kell. A sudo jelszavának bevitele nem látszik.
A teljes telepítőt **normál felhasználóként**, lehetőleg a qBittorrent felhasználójával futtasd.

## 4. Töltsd le a repót

A repó nyilvános: letöltéséhez nem kell GitHub-fiók vagy GitHub-bejelentkezés.

```bash
mkdir -p ~/src
cd ~/src
git clone https://github.com/accofil/AccoFIL-NCUpload.git
cd AccoFIL-NCUpload
```

Alternatíva: GitHub **Code → Download ZIP**, kicsomagolás, majd a teljes mappa másolása
WinSCP-vel a szerverre. A `ncore_up`, `docs`, `pyproject.toml` és `install.sh` együtt maradjon.

## 5. Készítsd elő az nCore sütifájlt

1. A saját böngésződben lépj be az nCore-ra.
2. Megbízható, helyben működő süti-exportálóval csak az `ncore.pro` sütijeit exportáld
   **Netscape cookies.txt** formátumban. A JSON-export nem megfelelő.
3. WinSCP/SFTP segítségével másold a szerver saját felhasználói mappájába,
   például `~/cookies-import.txt` néven.
4. Jegyezd meg a teljes szerveres útvonalát. A `pwd` az aktuális mappát mutatja, az `ls` a tartalmát.

A sütifájl belépési jogosultságot ad: GitHubra vagy hibajelentésbe ne másold.
A feltöltő nem kér nCore-jelszót. A telepítő csak az nCore sütijeit menti a védett célfájlba;
érvényességüket később a `--check` ellenőrzi.

## 6. Indítsd a telepítőt

A letöltött repó mappájában:

```bash
bash install.sh
```

A rendszercsomagok automatikus telepítéséhez használható a `bash install.sh --install-system` is.
A varázsló első kérdése a konfigurációs fájl helye; az alapérték Enterrel elfogadható.
Ezután az alábbi négy csoportot kéri be.

### Elérési utak

| Mező | Mit adj meg? |
|---|---|
| Python környezet | Saját Python és függőségek mappája; az alapérték általában jó. |
| Indítóparancsok | Alapból `~/.local/bin`; innen indul a `ncupload`. |
| NCUP | Például `/home/USER/storage/NCUP`; írható, külön célmappa legyen. |
| Védett sütifájl | A telepített munkamenet célfájlja; az alapérték általában jó. |
| Naplók | Futásonkénti tervek, leírások és folytatási állapotok helye. |
| Gyorsítótár | HTTP-cache, adatbázisok, tiltólista és feltöltési nyilvántartás helye. |
| mkbrr | A torrentkészítő teljes útvonala. Ha hiányzik, a telepítő felajánlja a hivatalos kiadás letöltését, SHA-256 ellenőrzéssel. |
| BDInfo | Blu-ray elemző futtatható fájl. A meglévő Upload-Assistant melletti binárist is felismeri. |
| Sütifájl importja | Az 5. lépésben feltöltött fájl teljes útvonala; már létező célfájlnál Enter. |

Teljes, szerveres útvonalakat adj meg; a `~` a home mappádra bővül.
A konfiguráció a repón kívül legyen.
Normál MKV/MP4/MP3/FLAC feldolgozáshoz BDInfo nem kell.
Teljes Blu-ray elemzéshez olyan Linux BDInfo CLI szükséges, amely támogatja a
`-l DISC OUTDIR` és `-m PLAYLIST DISC OUTDIR` hívásokat. Az útvonal bekérése nem telepíti automatikusan.
Később is megadható a beállítóvarázslóban.

#### Opcionális BDInfo telepítés teljes Blu-rayhez

Kompatibilis kiindulás a [BDInfoCLI hivatalos repója](https://github.com/tetrahydroc/BDInfoCLI).
Az alábbi példa a `v1.0.6` Linux x64 kiadást tölti le; ARM64 gépen
`BDInfo-linux-arm64.tar.gz` kell helyette. Az architektúrát az `uname -m` mutatja.

```bash
mkdir -p ~/Downloads/bdinfo
gh release download v1.0.6 --repo tetrahydroc/BDInfoCLI --pattern BDInfo-linux-x64.tar.gz --dir ~/Downloads/bdinfo
mkdir -p ~/.local/share/ncore-up/bin/bdinfo-cli
tar -xzf ~/Downloads/bdinfo/BDInfo-linux-x64.tar.gz --no-same-owner -C ~/.local/share/ncore-up/bin/bdinfo-cli
find ~/.local/share/ncore-up/bin/bdinfo-cli -type f -name BDInfo
```

A `find` megmutatja a kicsomagolt `BDInfo` program teljes útvonalát.
Ennek adj futási jogot: `chmod u+x "/a/find/altal/kiirt/BDInfo"`, majd ugyanezt az útvonalat add meg
a `ncupload --configure` varázsló BDInfo-mezőjében.
A csomag többi fájlját is hagyd mellette; ne csak az indítót másold át.

### qBittorrent WebUI

A qBittorrent **Beállítások → Web UI** részében engedélyezett webes felület adatait add meg.

| Mező | Jelentés |
|---|---|
| URL | Például `http://127.0.0.1:8080`, ha ugyanazon a szerveren fut. Ez nem az SSH portja. |
| Felhasználónév | A WebUI felhasználója. |
| Jelszó | A WebUI jelszava, rejtett bevitellel. |
| Kategória | Opcionális kliensbeli kategória; nem az nCore kategóriája. |

Tudatosan beállított helyi hitelesítésmentes elérésnél a felhasználó/jelszó üres lehet.
A qBittorrentnek ugyanazokat az NCUP-fájlokat ugyanazon az útvonalon kell látnia.
Eltérő Docker-konténeres útvonalakat a program jelenleg nem fordít át automatikusan.

### API-k

A varázsló mind a 17 támogatott mezőt megkérdezi. **Mind opcionális:** Enterrel kihagyhatók,
és csak a hozzájuk tartozó adatforrás marad ki. [Kulcslétrehozás és a mezők magyarázata](api-keys.md).
Spotify/IGDB esetén a Client ID és Client Secret együtt kell.

Titkos mezőnél sem a bevitel, sem a mentett érték nem látszik. Enter = megtartás, `-` = törlés.
A korábbi konfiguráció védett `config.env.previous` másolatba kerül.

### Heti tiltólista-frissítés

Az igen válasz user systemd timert hoz létre. Hétfő **04:15 UTC** körül fut, legfeljebb 5 perc késleltetéssel.
Ez Magyarországon télen 05:15, nyáron 06:15 körüli időpont.
Kijelentkezés után is működő user timerhez:

```bash
sudo loginctl enable-linger "$USER"
systemctl --user list-timers ncore-up-banlist.timer
```

Nem válasznál a saját, meglévő user timert kikapcsolja (a fájljai megmaradnak).
Ha már van rendszer-szintű `ncore-up-banlist.timer`, egyetlen ütemezést válassz.
A régi rendszer-timer állapotát a `systemctl status ncore-up-banlist.timer` mutatja.
Ne fusson egyszerre user timer, rendszer-timer és cron ugyanarra a feladatra.

Systemd nélkül `crontab -e` segítségével például:

```cron
15 4 * * 1 /home/USER/.local/bin/ncupload --refresh-banlist --json
```

A cron a gép időzónáját követi. Cseréld ki az útvonalat; ne állítsd be a systemd timer mellé.

## 7. Tedd elérhetővé a parancsot

Ha a `ncupload` nem található, az alapértelmezett telepítésnél:

```bash
export PATH="$HOME/.local/bin:$PATH"
ncupload --version
```

Ha ez segít, a `~/.profile` végére írd be ugyanezt az export sort.
Egyedi indítómappánál a telepítő végén kiírt sort használd.
Az abszolút útvonal is jó: `~/.local/bin/ncupload --version`.

## 8. Első ellenőrzések

Egyenként indítsd:

```bash
ncupload --version
ncupload --check
ncupload --doctor
ncupload --refresh-banlist
```

`--check`: nCore-belépés és néhány adatforrás. `--doctor`: forrásonkénti beállítási állapot,
élő hívások nélkül. Élő API-próbához `ncupload --doctor --doctor-live`.
Hiányzó opcionális kulcs önmagában nem teljes telepítési hiba.
Folytatás: [első szárazpróba, majd feltöltés](usage.md).

## 9. Módosítás, frissítés és eltávolítás

Beállításmódosítás: `ncupload --configure`.
Venv költöztetéséhez újra a telepítőt használd; a beállítás önmagában nem mozgatja át a Pythont.
Egyedi config esetén az indító a `NCORE_CONFIG` változóval megjegyzi a választott fájlt.

Gitből telepített példány frissítése:

```bash
cd ~/src/AccoFIL-NCUpload
git pull --ff-only
bash install.sh
```

Frissítés előtt várd meg a futó batch végét. Meglévő beállításnál Enter = megtartás.
ZIP-es telepítésnél új ZIP-et tölts le, a Git-parancs helyett.
**A seedelt NCUP-tartalmat tartsd meg.**

Heti user timer kikapcsolása:

```bash
systemctl --user disable --now ncore-up-banlist.timer
```

Eltávolításhoz először állítsd le a timert, majd a saját indítókat és a telepítőben megadott venv-et
távolítsd el. Konfigurációt, naplókat és seedelt anyagot külön kezelj; automatikus adattörlő uninstall nincs.
