# AccoFIL NCUpload – személyes beállítások és publikálás

- A sütik és API-kulcsok a telepítés konfigurációs mappájába kerülnek, nem a repóba.
- A konfigurációs mappa 700, a titkos konfiguráció/süti/backup fájlok 600 jogosultságot kapnak.
- A varázsló a titkos értékeket rejtve kéri be. A korábbi konfiguráció védett másolata is titkos.
- A `config.env` JSON-idézett `KEY=VALUE` formátumú adatfájl. Ne `source`-old és ne futtasd shell scriptként.
- A `.gitignore` kizárja a sütiket, valódi env-fájlokat, biztonsági másolatokat,
  futási naplókat, cache-t, privát kulcsokat és generált torrenteket.
- Hibajelentés előtt minden személyes fájlt és naplórészletet nézz át.
- A program a tartalomból telepítőt/alkalmazást nem futtat; statikus metaadatot olvas.
- A mkbrr opcionális letöltése a kiadó hivatalos HTTPS GitHub-kiadásából történik,
  a kiadás SHA-256 listájával egyeztetve. A BDInfo saját futtatható függőség.
- A külső szolgáltatások saját feltételeit és az aktuális nCore-szabályokat a használatkor követni kell.
- A GitHub-kiadás tiszta forrás-pillanatkép. Az éles feltöltések naplói és a személyes telepítés története
  a helyi fejlesztői példányban maradnak.

Részletes lépések: [telepítés](installation.md), [használat](usage.md), [hibakeresés](troubleshooting.md).
