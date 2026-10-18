# AccoFIL NCUpload – API-kulcsok

Az API-k az automatikus azonosítást egészítik ki. **A telepítéshez egyik sem kötelező.**
A hiányzó kulcs az adott forrást kapcsolja ki; helyi fájltagek, NFO és kulcs nélküli adatbázisok továbbra is használhatók.

Beállítás: `ncupload --configure`. Titkos mezőnél Enter = megtartás, `-` = törlés.
A következő táblázat mind a 17 támogatott API-mezőt felsorolja.

| Szolgáltatás / mező | Mire való? | Hol hozható létre? |
|---|---|---|
| `TMDB_API` | Film/sorozat cím, ismertető, borító. A v3 API Key szükséges. | [TMDB API-beállítások](https://www.themoviedb.org/settings/api), [hivatalos kezdőlap](https://developer.themoviedb.org/docs/getting-started). |
| `DEEPL_KEY` | Hiányzó magyar ismertetők fordítása. | DeepL API-fiók **API Keys** része; [hitelesítési útmutató](https://developers.deepl.com/docs/getting-started/auth). DeepL API-előfizetés kulcsa kell, nem pusztán a fordítói webes előfizetés. |
| `SPOTIFY_CLIENT_ID` | Albumkeresés és számlista, tartalék zenei adatforrás. | [Spotify Developer Dashboard](https://developer.spotify.com/dashboard), alkalmazás létrehozása → Settings → Client ID. |
| `SPOTIFY_CLIENT_SECRET` | A Spotify alkalmazás hitelesítése. | Ugyanott Client Secret. [Hivatalos alkalmazásútmutató](https://developer.spotify.com/documentation/web-api/concepts/apps). |
| `DISCOGS_TOKEN` | Kiadványok, stílusok, tracklista. | [Discogs Settings → Developers](https://www.discogs.com/settings/developers) → személyes token. |
| `DISCOGS_CONSUMER_KEY` | A személyes token alternatívája. | Ugyanott alkalmazás létrehozása → Consumer Key. |
| `DISCOGS_CONSUMER_SECRET` | A Discogs alkalmazás titkos kulcsa. | Ugyanazon alkalmazás Consumer Secret mezője. |
| `ACOUSTID_KEY` | Hiányos zenetageknél hang-ujjlenyomatos azonosítás. | [AcoustID: új alkalmazás](https://acoustid.org/new-application), az alkalmazás API-kulcsa kell. |
| `LASTFM_KEY` | Zenei stílusok és albumismertető. | [Last.fm: API-fiók létrehozása](https://www.last.fm/api/account/create); API Key. |
| `SETLISTFM_KEY` | Koncertfelvételek számlistája. | [setlist.fm API-dokumentáció](https://api.setlist.fm/docs/1.0/index.html) → API key igénylés. |
| `IGDB_CLIENT_ID` | Játékadatok. | [Twitch Developer Console](https://dev.twitch.tv/console/apps) → Register Your Application → Client ID. |
| `IGDB_CLIENT_SECRET` | IGDB/Twitch alkalmazás hitelesítése. | Ugyanott New Secret; [IGDB hivatalos kezdőlépések](https://api-docs.igdb.com/#getting-started). |
| `GOOGLE_BOOKS_KEY` | Könyvadatok ISBN/cím alapján. | [Google Cloud Console](https://console.cloud.google.com/): projekt → APIs & Services → Library → Books API engedélyezése → Credentials → Create credentials → API key. |
| `COMICVINE_KEY` | Angol képregényadatok. | [Comic Vine API](https://comicvine.gamespot.com/api/), bejelentkezés után API key. |
| `TVDB_KEY` | Fenntartott mező; ebben a verzióban még nincs aktív TheTVDB-adapter. | [TheTVDB API information](https://thetvdb.com/api-information): a fiókhoz engedélyezett API-hozzáférés kulcsa. |
| `FANART_KEY` | Fenntartott mező; ebben a verzióban még nincs aktív fanart.tv-adapter. | [fanart.tv API key](https://fanart.tv/get-an-api-key/), személyes projektkulcs. |
| `GITHUB_TOKEN` | Szoftveradatbázisok olvasása nagyobb GitHub-kvótával. | GitHub Settings → Developer settings → Personal access tokens. Ehhez nyilvános adatok olvasására alkalmas minimális jogosultságú token elég; repóírás nem kell. |

A `TVDB_KEY` és `FANART_KEY` a korábbi konfigurációból megőrzött, fenntartott mező.
Ezekhez most nem szükséges kulcsot beszerezned; a varázslóban hagyd őket üresen.

## Fontos különbségek

- **MusicBrainz:** a feltöltő nyilvános metaadat-kereséséhez nem kell API-kulcs vagy OAuth-alkalmazás.
  [Hivatalos API-leírás](https://musicbrainz.org/doc/MusicBrainz_API).
- **Discogs:** vagy személyes tokent adj meg, vagy a teljes Consumer Key + Consumer Secret párt.
  A Request Token URL / Authorize URL / Access Token URL nem kulcs: ezekhez nincs telepítőmező.
- **Spotify:** a Client ID és Secret együtt szükséges. A fejlesztői hozzáférés aktuális feltételeit
  a Dashboard mutatja. Ha Redirect URI-t kér az alkalmazásregisztráció, a szolgáltató elfogadott
  loopback-címét használd, például `http://127.0.0.1:8888/callback`; a feltöltő az alkalmazástokent használja,
  nem léptet be Spotify-felhasználót.
- **IGDB:** a Twitch megkövetelheti a fejlesztői fiók kétlépcsős hitelesítését. Ha ezt nem szeretnéd
  beállítani, a kulcspár kihagyható, a többi játékadatforrás működik.
- **Google Books:** a kulcsot korlátozd a Books API-ra és a lekérdezést végző szerver kimenő publikus
  IP-jére; szerveres híváshoz a weboldal-referrer korlátozás nem jó.
  [Google Books hitelesítés](https://developers.google.com/books/docs/v1/using#auth).
  A projektben az API engedélyezése, kvótája és a kulcskorlátozás egyaránt számít.
- A `GITHUB_TOKEN` API-beállítás és a repó letöltéséhez szükséges `gh auth login` két külön cél.
  A telepítő a GitHub-belépés tokenjét nem másolja át automatikusan.

## Kulcsok későbbi importja

Készíthetsz a szerveren egy privát UTF-8 fájlt:

```text
TMDB_API=SAJAT_KULCS
DEEPL_KEY=SAJAT_KULCS
SPOTIFY_CLIENT_ID=SAJAT_AZONOSITO
SPOTIFY_CLIENT_SECRET=SAJAT_TITOK
```

Majd:

```bash
chmod 600 /teljes/ut/api-keys.env
ncupload --import-keys /teljes/ut/api-keys.env
ncupload --doctor
```

A példabeli helykitöltőket valódi értékekre kell cserélni. A támogatott mezőneveket használd;
például DeepL esetén `DEEPL_KEY`, nem `DEEPL_API`.
A kulcsértékeket a program nem írja ki. Élő ellenőrzés:
`ncupload --doctor --doctor-live --json`.

A szolgáltatók feltételei, kvótái és felületei változhatnak. Mindig a saját fiókod aktuális
feltételeit és a fenti hivatalos dokumentációkat kövesd.
