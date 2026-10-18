"""nCore client: upload request encoding, success/error detection, the search fallback, search paging, passkey."""

from __future__ import annotations

import json
import unittest

import requests
from requests.structures import CaseInsensitiveDict

from ncore_up import categories as cats
from ncore_up import client as ncore

NAME = "Végrehajtók 1080p"
KEY = "0123456789abcdef0123456789abcdef"


def bencode(value) -> bytes:
    if isinstance(value, int):
        return b"i%de" % value
    if isinstance(value, str):
        value = value.encode("utf-8")
    if isinstance(value, bytes):
        return b"%d:%s" % (len(value), value)
    if isinstance(value, list):
        return b"l" + b"".join(bencode(v) for v in value) + b"e"
    keys = sorted(value, key=lambda k: k.encode("utf-8") if isinstance(k, str) else k)
    return b"d" + b"".join(bencode(k) + bencode(value[k]) for k in keys) + b"e"


def make_torrent(name: str = "Repo.Men.2010.1080p-GRP", pieces: bytes = b"a" * 20, source: str = "", announce: str = "https://ncore.pro/announce") -> bytes:
    info = {"name": name, "piece length": 16384, "pieces": pieces, "length": 5, "private": 1}
    if source:
        info["source"] = source
    return bencode({"announce": announce, "info": info})


def response(url: str, body: str = "", status: int = 200, ctype: str = "text/html; charset=utf-8", history=(), content: bytes | None = None,
             headers: dict | None = None) -> requests.Response:
    r = requests.Response()
    r.status_code = status
    r.url = url
    r._content = content if content is not None else body.encode("utf-8")
    r.headers = CaseInsensitiveDict({"content-type": ctype, **(headers or {})})
    r.encoding = "utf-8"
    r.history = list(history)
    return r


def redirect(url: str, location: str) -> requests.Response:
    return response(url, status=302, headers={"Location": location})


UPLOAD_PAGE = """<html><head><meta charset="utf-8"></head><body>
<form id="kereses_mezo" action="torrents.php"><input type="hidden" name="tipus" value="all_own"><input type="hidden" name="miben" value="name"></form>
<form name="feltoltes" id="feltoltes" action="upload.php" method="post" enctype="multipart/form-data">
<input value="0123456789abcdef0123456789abcdef" type="hidden" name="getUnique">
<input type="hidden" id="mindent_tud_van" name="mindent_tud_van" value="1" />
<input type='hidden' name='MAX_FILE_SIZE' value='10485760'>
<input type="hidden" name="regi" value="x" disabled>
<input type="text" name="torrent_nev" id="torrent_nev" value="">
<input type="hidden" name="imdb_id" id="imdb_orig_results_id" value="">
</form></body></html>"""

ERROR_PAGE = """<div class="fobox_fej">Hiba!</div><div class="fobox_tartalom"><table><tr><td>A felvitelnél néhány hiba történt:
<ul><li>Nem adtál meg techinfo fájlt!</li><li>A torrent &quot;már&quot; fent van!</li></ul></td></tr></table></div>"""


class Stub:
    def __init__(self):
        self.headers: dict = {}
        self.cookies = None


class FakeNcore:
    """ncore.pro as far as the client sees it: search JSON, details page, download, upload answer."""

    def __init__(self, upload_answer, *, before=(), after=(), site_torrent: bytes | None = None, category: str = "hd_hun"):
        self.headers: dict = {}
        self.cookies = None
        self.calls: list[tuple[str, str, dict]] = []
        self.upload_answer = upload_answer
        self.before, self.after = list(before), list(after)
        self.uploaded = False
        self.site_torrent = site_torrent if site_torrent is not None else make_torrent(source="nCore-rewritten")
        self.category = category

    def _results(self, ids):
        return [{"torrent_id": str(i), "release_name": NAME, "category": self.category} for i in ids]

    def get(self, url, params=None, **kw):
        self.calls.append(("GET", url, params or {}))
        params = params or {}
        if url.endswith("/torrents.php") and params.get("jsons"):
            ids = self.after if self.uploaded else self.before
            if not ids:
                return response(url, "<html>Nincs találat</html>")
            return response(url, json.dumps({"results": self._results(ids), "total_results": str(len(ids)), "perpage": "25"}), ctype="application/json")
        if url.endswith("/torrents.php") and params.get("action") == "details":
            tid = params["id"]
            return response(f"{url}?action=details&id={tid}", f'<a href="torrents.php?action=download&amp;id={tid}&amp;key={KEY}">Letöltés</a>')
        if url.endswith("/torrents.php") and params.get("action") == "download":
            return response(url, content=self.site_torrent, ctype="application/x-bittorrent")
        raise AssertionError(f"unexpected GET {url} {params}")

    def post(self, url, data=None, files=None, **kw):
        self.calls.append(("POST", url, {"data": data, "files": files}))
        if url.endswith("/upload.php"):
            self.uploaded = True
            if isinstance(self.upload_answer, Exception):
                raise self.upload_answer
            return self.upload_answer
        raise AssertionError(f"unexpected POST {url}")


def client_for(session) -> ncore.NcoreClient:
    client = ncore.NcoreClient(cookies=None, session=session)
    client._sleep = lambda seconds: None
    return client


def upload_form(**extra) -> dict:
    form = {"getUnique": KEY, "tipus": "hd_hun", "torrent_nev": NAME, "eredeti": "nem", "szoveg": "Hangsáv: magyar 5.1\n\nTartalom: ő ű",
            "keresre": "nem", "keres_kodja": "", "mindent_tud1": "szabalyzat", "mindent_tud3": "seedeles", "imdb_id": "tt1053424"}
    form.update(extra)
    return form


def upload_files(local: bytes | None = None) -> dict:
    return {"torrent_fajl": ("Repo.Men.2010.1080p-GRP.torrent", local or make_torrent(), "application/x-bittorrent"),
            "techinfo_fajl": ("Árvíztűrő tükörfúrógép.txt", "Complete name : Repo.mkv\n".encode("utf-8"), "text/plain")}


class PageParsingTests(unittest.TestCase):
    def test_token_and_hidden_fields_of_the_upload_form_only(self):
        self.assertEqual(ncore.parse_token(UPLOAD_PAGE), KEY)
        hidden = ncore.hidden_fields(UPLOAD_PAGE)
        self.assertEqual(hidden, {"getUnique": KEY, "mindent_tud_van": "1", "MAX_FILE_SIZE": "10485760", "imdb_id": ""})
        self.assertNotIn("tipus", hidden)  # the search form's hidden field is not part of the upload
        self.assertIsNone(ncore.parse_token('<input type="hidden" name="getUnique" value="">'))

    def test_upload_token_keeps_hidden_fields(self):
        class Session(Stub):
            def get(self, url, **kw):
                return response(url, UPLOAD_PAGE)

        client = client_for(Session())
        self.assertEqual(client.upload_token(), KEY)
        self.assertEqual(client.upload_hidden["mindent_tud_van"], "1")

    def test_error_box(self):
        self.assertEqual(ncore.parse_errors(ERROR_PAGE), ["Nem adtál meg techinfo fájlt!", 'A torrent "már" fent van!'])
        box = '<div class="fobox_fej">Hiba!</div><div class="fobox_tartalom">Napi feltöltési limit elérve.</div>'
        self.assertEqual(ncore.parse_errors(box), ["Napi feltöltési limit elérve."])
        self.assertEqual(ncore.parse_errors("<html>Köszönjük a feltöltést</html>"), [])

    def test_details_ids(self):
        self.assertEqual(ncore.details_id("https://ncore.pro/torrents.php?action=details&id=123"), 123)
        self.assertEqual(ncore.details_id("/torrents.php?id=77&action=details"), 77)
        self.assertEqual(ncore.details_id("torrents.php?action=details&amp;id=5"), 5)
        self.assertIsNone(ncore.details_id("https://ncore.pro/upload.php"))
        self.assertIsNone(ncore.details_id("https://ncore.pro/torrents.php?action=download&id=5"))
        self.assertIsNone(ncore.details_id("https://ncore.pro/torrents.php?action=details&id=abc"))


class UploadTests(unittest.TestCase):
    def test_multipart_is_utf8_with_hidden_fields_and_crlf_description(self):
        site = FakeNcore(response("https://ncore.pro/torrents.php?action=details&id=4242", "<html>adatlap</html>"))
        client = client_for(site)
        client.upload_hidden = {"getUnique": "old", "mindent_tud_van": "1", "imdb_id": ""}
        result = client.upload(upload_form(), upload_files())
        self.assertTrue(result.ok)
        self.assertEqual((result.torrent_id, result.url), (4242, "https://ncore.pro/torrents.php?action=details&id=4242"))
        post = next(c for c in site.calls if c[0] == "POST")[2]
        self.assertEqual(post["data"]["getUnique"], KEY)  # the form wins over the page's hidden fields
        self.assertEqual(post["data"]["imdb_id"], "tt1053424")
        self.assertEqual(post["data"]["mindent_tud_van"], "1")
        self.assertEqual(post["data"]["szoveg"], "Hangsáv: magyar 5.1\r\n\r\nTartalom: ő ű")
        prepared = requests.Request("POST", "https://ncore.pro/upload.php", data=post["data"], files=post["files"]).prepare()
        body = prepared.body
        self.assertIn('name="torrent_nev"\r\n\r\nVégrehajtók 1080p\r\n'.encode("utf-8"), body)
        self.assertIn("Tartalom: ő ű".encode("utf-8"), body)
        self.assertIn('filename="Árvíztűrő tükörfúrógép.txt"'.encode("utf-8"), body)
        self.assertIn(b'name="torrent_fajl"; filename="Repo.Men.2010.1080p-GRP.torrent"\r\nContent-Type: application/x-bittorrent', body)
        self.assertTrue(prepared.headers["Content-Type"].startswith("multipart/form-data; boundary="))

    def test_success_from_a_redirect_on_the_way(self):
        history = [redirect("https://ncore.pro/upload.php", "/torrents.php?id=77&action=details")]
        site = FakeNcore(response("https://ncore.pro/index.php", "<html>főoldal</html>", history=history))
        result = client_for(site).upload(upload_form(), upload_files())
        self.assertTrue(result.ok)
        self.assertEqual(result.torrent_id, 77)

    def test_success_from_a_meta_refresh(self):
        page = '<html><head><meta http-equiv="refresh" content="0; url=torrents.php?action=details&amp;id=4243"></head></html>'
        site = FakeNcore(response("https://ncore.pro/upload.php", page))
        result = client_for(site).upload(upload_form(), upload_files())
        self.assertEqual((result.ok, result.torrent_id), (True, 4243))
        self.assertEqual(result.note, "")

    def test_error_page_is_a_failure_without_guessing(self):
        site = FakeNcore(response("https://ncore.pro/upload.php", ERROR_PAGE), after=[999])
        result = client_for(site).upload(upload_form(), upload_files())
        self.assertFalse(result.ok)
        self.assertIn("Nem adtál meg techinfo fájlt!", result.errors)
        searches_after = [c for c in site.calls[2:] if c[0] == "GET"]
        self.assertEqual(searches_after, [])

    def test_unknown_page_without_a_new_torrent_is_not_success(self):
        site = FakeNcore(response("https://ncore.pro/upload.php", "<html>Köszönjük!</html>"))
        result = client_for(site).upload(upload_form(), upload_files())
        self.assertFalse(result.ok)
        self.assertIsNone(result.torrent_id)
        self.assertIn("ismeretlen válasz", result.errors[0])
        self.assertIn("nézd meg az nCore-on", result.errors[0])

    def test_unknown_page_found_by_search_with_the_same_pieces(self):
        local = make_torrent()
        site = FakeNcore(response("https://ncore.pro/upload.php", "<html>Köszönjük!</html>"), before=[10], after=[10, 99],
                         site_torrent=make_torrent(source="[ncore.pro]", announce="https://ncore.pro/announce.php?passkey=x"))
        result = client_for(site).upload(upload_form(), upload_files(local))
        self.assertTrue(result.ok)
        self.assertEqual(result.torrent_id, 99)
        self.assertIn("kereséssel", result.note)
        self.assertEqual(result.body, "<html>Köszönjük!</html>")

    def test_new_torrent_with_other_content_is_not_success(self):
        site = FakeNcore(response("https://ncore.pro/upload.php", "<html>?</html>"), after=[99], site_torrent=make_torrent(pieces=b"b" * 20))
        result = client_for(site).upload(upload_form(), upload_files(make_torrent()))
        self.assertFalse(result.ok)
        self.assertIn("#99", result.errors[0])

    def test_new_torrent_in_another_category_is_not_ours(self):
        site = FakeNcore(response("https://ncore.pro/upload.php", "<html>?</html>"), after=[99], category="ebook")
        result = client_for(site).upload(upload_form(), upload_files())
        self.assertFalse(result.ok)

    def test_two_new_torrents_with_the_name_are_never_guessed(self):
        site = FakeNcore(response("https://ncore.pro/upload.php", "<html>?</html>"), after=[98, 99])
        result = client_for(site).upload(upload_form(), upload_files())
        self.assertFalse(result.ok)
        self.assertIn("#98, #99", result.errors[0])

    def test_lost_connection_then_found(self):
        site = FakeNcore(requests.ConnectionError("reset"), after=[99])
        result = client_for(site).upload(upload_form(), upload_files())
        self.assertTrue(result.ok)
        self.assertEqual(result.torrent_id, 99)

    def test_lost_connection_not_found_raises(self):
        site = FakeNcore(requests.ReadTimeout("timeout"))
        with self.assertRaises(ncore.NcoreError) as ctx:
            client_for(site).upload(upload_form(), upload_files())
        self.assertIn("mielőtt újra feltöltöd", str(ctx.exception))

    def test_login_page_after_upload(self):
        site = FakeNcore(response("https://ncore.pro/login.php", '<form><input name="pass"></form>'))
        with self.assertRaises(ncore.SessionExpired):
            client_for(site).upload(upload_form(), upload_files())


class SearchAndDownloadTests(unittest.TestCase):
    def test_search_lists_every_category_and_follows_pages(self):
        pages = {1: [{"torrent_id": str(i), "release_name": f"r{i}"} for i in range(25)],
                 2: [{"torrent_id": str(i), "release_name": f"r{i}"} for i in range(25, 30)]}
        seen = []

        class Session(Stub):
            def get(self, url, params=None, **kw):
                seen.append(dict(params))
                page = int(params.get("oldal", 1))
                return response(url, json.dumps({"results": pages[page], "total_results": "30", "perpage": "25"}), ctype="application/json")

        results = client_for(Session()).search("Dűne 1080p")
        self.assertEqual(len(results), 30)
        self.assertEqual(seen[0]["tipus"], "kivalasztottak_kozott")
        self.assertEqual(seen[0]["kivalasztott_tipus"].split(","), [c for c, cat in cats.CATEGORIES.items() if cat.group != "xxx"])
        self.assertEqual((seen[0].get("oldal"), seen[1]["oldal"]), (None, "2"))

    def test_exact_matches_compare_unescaped_names(self):
        class Session(Stub):
            def get(self, url, params=None, **kw):
                items = [{"torrent_id": "1", "release_name": "Tom &amp; Jerry  1080p"}, {"torrent_id": "2", "release_name": "Tom & Jerry 720p"}]
                return response(url, json.dumps({"results": items}), ctype="application/json")

        self.assertEqual([m["torrent_id"] for m in client_for(Session()).exact_matches("tom & jerry 1080p")], ["1"])

    def test_passkey_and_download(self):
        body = (f'<a href="torrents.php?action=download&amp;id=5&amp;key={"f" * 32}">más</a>'
                f'<a href="torrents.php?action=download&amp;id=42&amp;key={KEY}">ez</a>')

        class Session(Stub):
            def get(self, url, params=None, **kw):
                if params.get("action") == "details":
                    return response(url, body)
                content = make_torrent() if params.get("key") == KEY else b"<html>nincs jogod</html>"
                return response(url, content=content, ctype="application/x-bittorrent")

        client = client_for(Session())
        self.assertEqual(client.passkey(42), KEY)
        self.assertTrue(client.download_torrent(42, KEY).startswith(b"d"))
        with self.assertRaises(ncore.NcoreError):
            client.download_torrent(42, "f" * 32)

    def test_banned_notice_session_and_http_errors(self):
        class Session:
            headers: dict = {}
            answer = None

            def post(self, url, **kw):
                return self.answer

        session = Session()
        client = client_for(session)
        session.answer = response("https://ncore.pro/ajax.php", "  <div>Tiltott: <b>Valami</b></div> ")
        self.assertEqual(client.banned_notice(KEY, "hd", "Valami 1080p"), "Tiltott: Valami")
        session.answer = response("https://ncore.pro/ajax.php", "")
        self.assertEqual(client.banned_notice(KEY, "hd", "Valami 1080p"), "")
        session.answer = response("https://ncore.pro/login.php", "<form>")
        with self.assertRaises(ncore.SessionExpired):
            client.banned_notice(KEY, "hd", "Valami 1080p")
        session.answer = response("https://ncore.pro/ajax.php", "<h1>500</h1>", status=500)
        with self.assertRaises(ncore.NcoreError):
            client.banned_notice(KEY, "hd", "Valami 1080p")

    def test_network_errors_become_ncore_errors(self):
        class Session(Stub):
            def get(self, url, **kw):
                raise requests.ConnectionError("down")

        with self.assertRaises(ncore.NcoreError):
            client_for(Session()).upload_token()

    def test_login_check_skips_binary_answers(self):
        torrent = response("https://ncore.pro/torrents.php", content=b'd4:name4:pass6:name="pass"e', ctype="application/x-bittorrent")
        self.assertFalse(ncore.is_login_page(torrent))
        self.assertTrue(ncore.is_login_page(response("https://ncore.pro/index.php", '<input name="pass">')))


if __name__ == "__main__":
    unittest.main()
