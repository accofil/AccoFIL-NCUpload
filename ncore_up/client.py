"""HTTP client for ncore.pro: session check, search (dupe check), IMDb infobar data, upload, torrent download.

Authentication uses the browser's cookies (Netscape cookies.txt exported by the user); this program never
handles the nCore password.

The upload is one multipart POST to upload.php, as the browser sends it: the upload form's own hidden fields, the
form fields as UTF-8 (the site is UTF-8; requests encodes str values and file names that way, file names in the
browsers' HTML5 style) and the description with CRLF line ends like a textarea. Success is only the new torrent's
details URL (the final URL, a redirect on the way or a meta refresh). Any other answer, or a lost connection, is
checked by a search for a torrent with this name that was not there before the upload, in the same category, whose
pieces match the uploaded torrent; anything less is reported as a failure to look at, never as success.
"""

from __future__ import annotations

import html
import http.cookiejar
import re
import time
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

from . import categories as cats, torrent as torrents
from . import __version__

BASE = "https://ncore.pro"
UA = f"NCUpload/{__version__}"  # honest tool name; no repository link that would tie the account to the GitHub project
INPUT_TAG_RE = re.compile(r"<input\b[^>]*>", re.I | re.S)
ATTR_RE = re.compile(r"""([a-zA-Z_:][-\w:.]*)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+)))?""")
UPLOAD_FORM_RE = re.compile(r"""<form\b[^>]*\bid=["']feltoltes["'][^>]*>(.*?)</form>""", re.I | re.S)
ERROR_BOX_RE = re.compile(r'fobox_fej"[^>]*>\s*Hiba!?\s*</div>(.*?)(?:</table>|class="fobox_fej"|$)', re.S)
LI_RE = re.compile(r"<li>(.*?)</li>", re.S)
PASSKEY_RE = re.compile(r"action=download&(?:amp;)?id=\d+&(?:amp;)?key=([0-9a-fA-F]{32})")
INPUT_RE = re.compile(r'<input[^>]*id="(movie_[a-z_]+)"[^>]*value="([^"]*)"', re.S)
META_REFRESH_RE = re.compile(r"""<meta\b[^>]*http-equiv=["']?refresh["']?[^>]*content=["']?\s*\d+\s*;\s*url=([^"'>\s]+)""", re.I)


class NcoreError(Exception):
    pass


class SearchTruncated(NcoreError):
    """More hits than the paged JSON search returns; ``results`` holds the pages that were read."""

    def __init__(self, message: str, results: list[dict]):
        super().__init__(message)
        self.results = results


class SessionExpired(NcoreError):
    pass


@dataclass
class UploadResult:
    ok: bool
    torrent_id: int | None = None
    url: str = ""
    errors: list[str] = field(default_factory=list)
    body: str = ""
    note: str = ""  # how success was established when the answer itself did not show it
    uncertain: bool = False  # an unknown response must be reconciled before another POST


def load_cookies(path: str) -> http.cookiejar.MozillaCookieJar:
    jar = http.cookiejar.MozillaCookieJar(path)
    jar.load(ignore_discard=True, ignore_expires=True)
    return jar


def _plain(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def parse_errors(body: str) -> list[str]:
    """The items of the "Hiba!" box; its whole text when it has no list; [] when there is no error box."""

    box = ERROR_BOX_RE.search(body)
    if not box:
        return []
    items = [_plain(item) for item in LI_RE.findall(box.group(1))]
    items = [item for item in items if item]
    return items or [_plain(box.group(1))[:300] or "Hiba! (az nCore nem írta meg, mi)"]


def parse_imdb_movie(body: str) -> dict[str, str]:
    return {key: html.unescape(value) for key, value in INPUT_RE.findall(body)}


def parse_description(body: str) -> str:
    """All description blocks, retaining inline label text beside its colon.

    The first .torrent_leiras can be empty. A separator in get_text would split
    ``<span>Előadó</span>: value`` and hide duplicate headings from line checks.
    Only explicit HTML line breaks add newlines within each block.
    """
    soup = BeautifulSoup(body, "html.parser")
    blocks = []
    for block in soup.select(".torrent_leiras"):
        if block.find_parent(class_="torrent_leiras") is not None:
            continue
        for br in block.find_all("br"):
            br.replace_with("\n")
        text = block.get_text().replace("\r\n", "\n").replace("\r", "\n").strip()
        if text:
            blocks.append(text)
    return "\n\n".join(blocks)


def parse_duplicate_facts(body: str) -> dict[str, str]:
    """Only labelled facts in the description, never links with private passkeys.

    .torrent_leiras and Formátum verified in the live UI on 2026-10-18.
    Unknown/missing labels stay empty and require review.
    """
    text = parse_description(body)
    facts = {}
    for key, label in (("format", "Formátum"), ("language", "Nyelv"), ("edition", "Kiadás dátuma"),
                       ("version", "Verzió"), ("platform", "Platform"), ("source", "Forrás")):
        match = re.search(r"(?im)^\s*" + label + r"\s*:\s*([^\n]+)", text)
        if match:
            facts[key] = match[1].strip()[:250]
    return facts


def _rank(page: str) -> str:
    soup = BeautifulSoup(page, "html.parser")
    info = soup.select_one("#infosav_adatok, .infosav_adatok")
    text = info.get_text(" ") if info else ""
    match = re.search(r"\(([^)]+)\)", text)
    return match[1].strip().casefold() if match else ""


def rank_limit(page: str) -> int:
    return {"tag": 10, "elite": 20, "elit": 20, "legend": 30, "vip": 1_000_000_000,
            "helpdesk": 1_000_000_000, "feltöltő": 1_000_000_000}.get(_rank(page), 5)


def rank_minimum_size(page: str) -> int:
    rank = _rank(page)
    if rank in {"legend", "vip", "helpdesk", "feltöltő"}:
        return 0
    return (10 if rank in {"tag", "elite", "elit"} else 50) * 1024**2


def _attrs(tag: str) -> dict[str, str]:
    return {m.group(1).lower(): html.unescape(next((g for g in m.groups()[1:] if g is not None), "")) for m in ATTR_RE.finditer(tag[6:])}


def hidden_fields(page: str) -> dict[str, str]:
    """Hidden inputs of the upload form (``<form id="feltoltes">``), which a browser would post too."""

    form = UPLOAD_FORM_RE.search(page)
    fields: dict[str, str] = {}
    for tag in INPUT_TAG_RE.findall(form.group(1) if form else ""):
        attrs = _attrs(tag)
        if attrs.get("type", "").lower() == "hidden" and attrs.get("name") and "disabled" not in attrs:
            fields.setdefault(attrs["name"], attrs.get("value", ""))
    return fields


def parse_token(page: str) -> str | None:
    for tag in INPUT_TAG_RE.findall(page):
        attrs = _attrs(tag)
        if attrs.get("name") == "getUnique" and re.fullmatch(r"[0-9A-Za-z]{16,64}", attrs.get("value", "")):
            return attrs["value"]
    return None


def details_id(url: str) -> int | None:
    """Torrent id of a details URL (torrents.php?action=details&id=N, any parameter order, relative or absolute)."""

    parts = urlsplit(urljoin(BASE + "/", html.unescape(url or "")))
    if not parts.path.endswith("/torrents.php"):
        return None
    query = parse_qs(parts.query)
    if query.get("action", [""])[0] != "details":
        return None
    value = query.get("id", [""])[0]
    return int(value) if value.isdigit() else None


def details_url(torrent_id: int) -> str:
    return f"{BASE}/torrents.php?action=details&id={torrent_id}"


def response_details_id(response: requests.Response) -> int | None:
    """The new torrent's id from the final URL, a redirect on the way to it, or the page's own meta refresh."""

    candidates = [response.url] + [r.headers.get("Location", "") for r in reversed(response.history or [])]
    if "html" in response.headers.get("content-type", "html"):
        candidates += META_REFRESH_RE.findall(response.text)
    for url in candidates:
        torrent_id = details_id(url)
        if torrent_id:
            return torrent_id
    return None


def is_login_page(response: requests.Response) -> bool:
    if "login.php" in (response.url or ""):
        return True
    content_type = response.headers.get("content-type", "")
    if content_type and "html" not in content_type:
        return False  # JSON, torrent files: never the login form
    text = response.text
    return 'name="pass"' in text and "getUnique" not in text


def _norm_name(value: object) -> str:
    return re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip().casefold()


def _category_matches(item: dict, category: str) -> bool:
    value = str(item.get("category") or "")
    return not category or value == category or value not in cats.CATEGORIES  # a label that is not a code cannot be compared


def _to_int(value: object) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


class NcoreClient:
    def __init__(self, cookies: http.cookiejar.CookieJar, timeout: float = 60.0, session: requests.Session | None = None,
                 user_agent: str = UA):
        self.session = session or requests.Session()
        self.session.cookies = cookies  # type: ignore[assignment]
        self.session.headers["User-Agent"] = user_agent
        self.timeout = timeout
        self.upload_hidden: dict[str, str] = {}  # hidden fields of the upload form, from upload_token()
        self.daily_limit = 5
        self.minimum_size = 50 * 1024**2
        self.find_attempts = 4  # searches for an upload whose answer was unclear
        self.find_delay = 5.0
        self._sleep = time.sleep

    def _get(self, path: str, **kwargs) -> requests.Response:
        try:
            response = self.session.get(urljoin(BASE + "/", path), timeout=self.timeout, **kwargs)
        except requests.RequestException as exc:
            # requests exceptions can embed the download URL's private passkey.
            raise NcoreError(f"az nCore nem érhető el ({path}): {type(exc).__name__}") from None
        if is_login_page(response):
            raise SessionExpired("az nCore süti lejárt vagy hibás: exportáld újra a böngészőből")
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise NcoreError(f"nCore: HTTP {response.status_code} ({path})") from exc
        return response

    def upload_token(self) -> str:
        page = self._get("upload.php").text
        token = parse_token(page)
        if not token:
            raise NcoreError("nem találom a getUnique tokent a feltöltő oldalon")
        self.upload_hidden = hidden_fields(page)
        self.daily_limit = rank_limit(page)
        self.minimum_size = rank_minimum_size(page)
        return token

    def search(self, text: str, by: str = "name", categories: list[str] | None = None, max_pages: int = 4) -> list[dict]:
        """nCore's JSON search over the given categories (default: all of them, listed explicitly as the verified
        ``kivalasztottak_kozott`` form does), following the result pages."""

        params = {"mire": text, "miben": by, "tipus": "kivalasztottak_kozott",
                  "kivalasztott_tipus": ",".join(categories or [c for c, cat in cats.CATEGORIES.items() if cat.group != "xxx"]), "jsons": "true"}
        results: list[dict] = []
        for page in range(1, max_pages + 1):
            if page > 1:
                params["oldal"] = str(page)
            response = self._get("torrents.php", params=params)
            if "json" not in response.headers.get("content-type", ""):
                if re.search(r"(?i)nincs\s+tal[aá]lat", _plain(response.text)):
                    break
                raise NcoreError("a dupe-keresés ismeretlen HTML választ adott")
            try:
                data = response.json()
            except ValueError as exc:
                raise NcoreError("a dupe-keresés JSON válasza hibás") from exc
            if not isinstance(data, dict) or not isinstance(data.get("results", []), list):
                raise NcoreError("a dupe-keresés válaszsémája megváltozott")
            items = [item for item in (data.get("results") if isinstance(data, dict) else None) or [] if isinstance(item, dict)]
            results.extend(items)
            total = _to_int(data.get("total_results"))
            per_page = _to_int(data.get("perpage")) or _to_int(data.get("onpage")) or len(items)
            if not items or not total or len(results) >= total or len(items) < per_page:
                break
        if 'total' in locals() and total > len(results):
            raise SearchTruncated("a dupe-keresés több találatot adott, mint amennyi ellenőrizhető; pontosítsd a keresést", results)
        return results

    def duplicate_facts(self, torrent_id: int) -> dict[str, str]:
        return parse_duplicate_facts(self._get("torrents.php", params={"action": "details", "id": str(torrent_id)}).text)

    def exact_matches(self, release_name: str) -> list[dict]:
        wanted = _norm_name(release_name)
        return [item for item in self.search(release_name) if _norm_name(item.get("release_name")) == wanted]

    def banlist_page(self) -> str:
        return self._get("wiki.php", params={"action": "read", "id": "41"}, headers={"Cache-Control": "no-cache"}).text

    def banned_notice(self, token: str, category: str, name: str, *, original: bool = True, fields: dict | None = None) -> str:
        """Text of nCore's own "tiltott anyagok" hint for this name ('' when nothing matches)."""

        try:
            response = self.session.post(
                urljoin(BASE + "/", "ajax.php?action=up_live&tiltott=1"),
                data={**self.upload_hidden, **(fields or {}), "getUnique": token, "tipus": category,
                      "torrent_nev": name, "eredeti": "igen" if original else "nem"},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise NcoreError(f"a tiltólista-ellenőrzés nem érhető el: {type(exc).__name__}") from None
        if is_login_page(response):
            raise SessionExpired("az nCore süti lejárt vagy hibás: exportáld újra a böngészőből")
        if response.status_code != 200:
            raise NcoreError(f"a tiltólista-ellenőrzés hibát adott: HTTP {response.status_code}")
        return _plain(response.text)

    def imdb_movie(self, imdb_id: str) -> dict[str, str]:
        digits = imdb_id.lower().removeprefix("tt")
        return parse_imdb_movie(self._get("ajax.php", params={"action": "imdb_movie", "imdb_movie": digits}).text)

    def upload(self, form: dict[str, str], files: dict[str, tuple[str, bytes, str]]) -> UploadResult:
        payload = {**self.upload_hidden, **{k: str(v) for k, v in form.items() if v is not None}}
        if "szoveg" in payload:
            payload["szoveg"] = re.sub(r"\r?\n", "\r\n", payload["szoveg"])  # what a browser posts from a textarea
        name, category = payload.get("torrent_nev", ""), payload.get("tipus", "")
        local = files.get("torrent_fajl", ("", b"", ""))[1]
        before = self._ids_named(name)
        try:
            response = self.session.post(urljoin(BASE + "/", "upload.php"), data=payload, files=files, timeout=max(self.timeout, 300))
        except requests.RequestException as exc:
            found, problem = self._find_new(name, category, before, local)
            if found:
                return found
            raise NcoreError(f"a feltöltés kérése megszakadt ({type(exc).__name__}); {problem} — nézd meg az nCore-on, mielőtt újra feltöltöd") from None
        if is_login_page(response):
            raise SessionExpired("az nCore süti lejárt feltöltés közben")
        errors = parse_errors(response.text)
        if errors:
            return UploadResult(ok=False, url=response.url, errors=errors, body=response.text)
        torrent_id = response_details_id(response)
        if torrent_id:
            return UploadResult(ok=True, torrent_id=torrent_id, url=details_url(torrent_id), body=response.text)
        found, problem = self._find_new(name, category, before, local)
        if found:
            found.body = response.text
            return found
        return UploadResult(ok=False, url=response.url, body=response.text, uncertain=True,
                            errors=[f"ismeretlen válasz (HTTP {response.status_code}, {response.url}); {problem} — nézd meg az nCore-on, mielőtt újra feltöltöd"])

    def _ids_named(self, name: str) -> dict[int, dict] | None:
        """Torrents with exactly this name; None when the search failed (then nothing can be compared)."""

        try:
            return {int(item["torrent_id"]): item for item in self.exact_matches(name) if str(item.get("torrent_id", "")).isdigit()}
        except (NcoreError, requests.RequestException, ValueError, KeyError):
            return None

    def _find_new(self, name: str, category: str, before: dict[int, dict] | None, local: bytes) -> tuple[UploadResult | None, str]:
        """The upload, if it went through although the answer did not show it: one torrent with this name that was not
        there before, in this category, with the same pieces as the uploaded torrent. Never a guess."""

        if before is None or not name:
            return None, "a feltöltés előtti keresés nem sikerült, így nem tudom ellenőrizni"
        problem = "a keresés nem talált új torrentet ezzel a névvel"
        for attempt in range(self.find_attempts):
            if attempt:
                self._sleep(self.find_delay)
            after = self._ids_named(name)
            if after is None:
                problem = "a keresés nem sikerült"
                continue
            new = {i: item for i, item in after.items() if i not in before and _category_matches(item, category)}
            if len(new) > 1:
                return None, "több új torrent is van ezzel a névvel: " + ", ".join(f"#{i}" for i in sorted(new))
            if not new:
                continue
            torrent_id = next(iter(new))
            try:
                site = self.download_torrent(torrent_id, self.passkey(torrent_id))
            except NcoreError as exc:
                return None, f"valószínűleg feltöltődött (#{torrent_id}), de nem tudtam ellenőrizni: {exc}"
            if local and not torrents.same_content(site, local):
                return None, f"#{torrent_id} ezzel a névvel nem a feltöltött tartalom"
            return UploadResult(ok=True, torrent_id=torrent_id, url=details_url(torrent_id),
                                note="az nCore válaszából nem derült ki a siker; név szerinti kereséssel és a darab-hashek egyezésével találtam meg"), ""
        return None, problem

    def passkey(self, torrent_id: int) -> str:
        body = self._get("torrents.php", params={"action": "details", "id": torrent_id}).text
        match = re.search(rf"action=download&(?:amp;)?id={int(torrent_id)}&(?:amp;)?key=([0-9a-fA-F]{{32}})", body) or PASSKEY_RE.search(body)
        if not match:
            raise NcoreError("nem találom a passkeyt a torrent adatlapján")
        return match.group(1)

    def download_torrent(self, torrent_id: int, passkey: str) -> bytes:
        response = self._get("torrents.php", params={"action": "download", "id": torrent_id, "key": passkey})
        try:
            torrents.info_hash(response.content)
        except ValueError as exc:
            raise NcoreError(f"a letöltött fájl nem torrent ({exc})") from exc
        return response.content
