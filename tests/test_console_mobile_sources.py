"""Offline tests of the console/mobile sources: parsers on trimmed real responses (tests/fixtures/console_mobile/),
dump indexing with the download replaced by the fixture files."""

from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from ncore_up import net
from ncore_up.sources import fdroid, gametdb, googleplay, itunes, libretro_dat, msstore, psstore, titledb

FIXTURES = Path(__file__).parent / "fixtures" / "console_mobile"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def fixture_json(name: str):
    return json.loads(fixture(name))


class OfflineCase(unittest.TestCase):
    """Every test runs with a private cache dir and with the network forbidden."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        patches = [
            mock.patch.object(net, "cache_dir", return_value=self.tmp),
            mock.patch.object(net, "request", side_effect=AssertionError("network used in an offline test")),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def serve(self, module, files: dict[str, Path]):
        """Replace ``module.fetch_file`` with a copy of local fixtures (keyed by destination file name)."""

        def fake(url, dest, **_kwargs):
            source = files.get(dest.name)
            if source is None:
                return None
            dest.parent.mkdir(parents=True, exist_ok=True)
            if not dest.exists():
                shutil.copy(source, dest)
            return dest

        patch = mock.patch.object(module, "fetch_file", side_effect=fake)
        patch.start()
        self.addCleanup(patch.stop)


class JsonStreamTests(unittest.TestCase):
    def test_items_across_tiny_chunks(self):
        text = '{"a": 12345, "b": [1, 2, {"x": "y}"}], "c": {"d": true, "e": null}, "f": "\\u00e9\\"q"}'
        for chunk in (1, 2, 3, 7, 1 << 20):
            items = list(titledb.JsonStream(io.StringIO(text), chunk=chunk).items())
            self.assertEqual(items, list(json.loads(text).items()), chunk)

    def test_nested_path(self):
        with open(FIXTURES / "fdroid_index-v2.json", encoding="utf-8") as handle:
            names = [name for name, _ in titledb.JsonStream(handle, chunk=64).items(("packages",))]
        self.assertEqual(names, ["aaa.dummy.first", "org.fdroid.fdroid"])
        self.assertEqual(list(titledb.JsonStream(io.StringIO("{}")).items()), [])
        with self.assertRaises(ValueError):
            list(titledb.JsonStream(io.StringIO('{"a": [1, 2')).items())


class FetchFileTests(OfflineCase):
    class Response:
        def __init__(self, status, body=b"", headers=None):
            self.status_code, self.body, self.headers = status, body, headers or {}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def iter_content(self, size):
            for start in range(0, len(self.body), 4):
                yield self.body[start:start + 4]

    def session(self, *responses):
        session = mock.Mock()
        session.get.side_effect = list(responses)
        session.cache_disabled = None
        del session.cache_disabled
        return session

    def test_download_revalidate_and_stale(self):
        dest = self.tmp / "dump" / "file.json"
        with mock.patch.object(net, "session", return_value=self.session(self.Response(200, b'{"ok": 1}', {"ETag": '"v1"'}))):
            self.assertEqual(titledb.fetch_file("https://example.invalid/f", dest, min_size=2), dest)
        self.assertEqual(dest.read_bytes(), b'{"ok": 1}')
        self.assertEqual(json.loads(dest.with_name("file.json.etag").read_text())["etag"], '"v1"')
        self.assertEqual(titledb.fetch_file("https://example.invalid/f", dest), dest)  # fresh: no request at all
        old = time.time() - 3 * 86400
        os.utime(dest, (old, old))
        session = self.session(self.Response(304))
        with mock.patch.object(net, "session", return_value=session):
            self.assertEqual(titledb.fetch_file("https://example.invalid/f", dest), dest)
        self.assertEqual(session.get.call_args.kwargs["headers"]["If-None-Match"], '"v1"')
        self.assertGreater(dest.stat().st_mtime, old + 86400)
        os.utime(dest, (old, old))
        with mock.patch.object(net, "session", return_value=self.session(self.Response(503))):
            self.assertEqual(titledb.fetch_file("https://example.invalid/f", dest), dest)  # stale copy kept
        missing = self.tmp / "dump" / "none.json"
        with mock.patch.object(net, "session", return_value=self.session(self.Response(404))):
            self.assertIsNone(titledb.fetch_file("https://example.invalid/g", missing))
        self.assertFalse(list(missing.parent.glob("*.part")))


class TitledbTests(OfflineCase):
    def test_parse_entry(self):
        entry = fixture_json("titledb_HU.en.json")["70010000000127"]
        item = titledb.parse_entry(entry)
        self.assertEqual(item["title_id"], "0100000000010000")
        self.assertEqual(item["name"], "Super Mario Odyssey™")
        self.assertEqual(item["release_date"], "2017-10-27")
        self.assertEqual(item["publisher"], "Nintendo")
        self.assertTrue(item["icon_url"].startswith("https://img-eshop.cdn.nintendo.net/"))
        self.assertEqual(titledb.parse_entry({"id": None, "name": "Bundle"}), {})

    def test_lookup_from_index(self):
        self.serve(titledb, {"HU.en.json": FIXTURES / "titledb_HU.en.json", "versions.json": FIXTURES / "titledb_versions.json"})
        record = titledb.lookup("0100000000010000")
        self.assertEqual(record["name"], "Super Mario Odyssey™")
        self.assertEqual(record["nsu_id"], "70010000000127")
        self.assertEqual(record["versions"][65536], "2017-10-26")
        self.assertEqual(titledb.lookup("01007EF00011E800")["title_id"], "01007EF00011E000")  # update → its game
        self.assertEqual(titledb.lookup("0100FFFFFFFF0000"), {})  # unknown (US.en unavailable offline)
        self.assertEqual(titledb.lookup("not-an-id"), {})
        self.assertEqual([r["title_id"] for r in titledb.search("super mario odyssey")], ["0100000000010000"])

    def test_noe(self):
        hit = titledb.parse_noe(fixture_json("noe_smo.json"))[0]
        self.assertEqual(hit["title"], "Super Mario Odyssey")
        self.assertTrue(hit["url"].startswith("https://www.nintendo.com/en-gb/Games/"))
        self.assertEqual(hit["product_code"], "HACPAAACA")
        self.assertIn("en", hit["languages"])
        self.assertEqual(hit["release_date"], "2017-10-27")
        self.assertEqual(titledb.product_code_id(hit["product_code"]), "AAACA")
        with mock.patch.object(net, "get_json", return_value=fixture_json("noe_smo.json")) as get:
            self.assertEqual(titledb.noe_search(nsu_id="70010000000127")["product_code"], "HACPAAACA")
            self.assertIn("nsuid_txt:70010000000127", get.call_args.kwargs["params"]["fq"])
            self.assertEqual(titledb.noe_search(title="Super Mario Odyssey")["title"], "Super Mario Odyssey")
            self.assertEqual(titledb.noe_search(title="Something Else"), {})
            titledb.noe_search(product_code="CTR-P-AREP")
            self.assertEqual(get.call_args.kwargs["params"]["fq"], "type:GAME AND product_code_txt:CTRPAREP")
            titledb.noe_search(title="Mario Kart Wii", system="wii")
            self.assertEqual(get.call_args.kwargs["params"]["fq"], "type:GAME AND system_type:wii_*")
            self.assertEqual(titledb.noe_search(title="X", system="ps4"), {})


class GameTdbTests(OfflineCase):
    def zipped(self, name: str, xml_fixture: str) -> Path:
        path = self.tmp / f"src-{name}.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(f"{name}.xml", fixture(xml_fixture))
        return path

    def setUp(self):
        super().setUp()
        for patch in (mock.patch.object(gametdb, "WARNING", ""), mock.patch.object(gametdb, "download_allowed", return_value=True)):
            self.allowed = patch.start()
            self.addCleanup(patch.stop)

    def test_robots_block_without_opt_in(self):
        self.allowed.return_value = False
        self.serve(gametdb, {"wiitdb.zip": self.zipped("wiitdb", "wiitdb_sample.xml")})
        self.assertEqual(gametdb.lookup("wii", "RMCP01"), {})
        self.assertIn("robots.txt", gametdb.WARNING)
        self.assertNotIn("NCORE_GAMETDB_DOWNLOAD=1", gametdb.WARNING)
        gametdb.fetch_file.assert_not_called()
        self.assertEqual(gametdb.find_cover("wii", "RMCP01", "PAL"), "")
        dest = self.tmp / "gametdb" / "wiitdb.zip"  # a dump the user saved by hand is used as it is
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(self.zipped("wiitdb", "wiitdb_sample.xml"), dest)
        self.assertEqual(gametdb.lookup("wii", "RMCP01")["title"], "Mario Kart Wii")
        self.assertEqual(gametdb.WARNING, "")
        gametdb.fetch_file.assert_not_called()

    def test_parse_and_lookup(self):
        games = list(gametdb.iter_games(io.BytesIO(fixture("wiitdb_sample.xml").encode("utf-8"))))
        mkw = games[0]
        self.assertEqual((mkw["id"], mkw["region"], mkw["title"], mkw["publisher"]), ("RMCP01", "PAL", "Mario Kart Wii", "Nintendo"))
        self.assertTrue(mkw["synopsis"].startswith("Start your engines"))
        self.assertEqual(mkw["languages"], ["en", "fr", "de", "es", "it"])
        self.assertEqual(mkw["date"], "2008-04-11")
        self.serve(gametdb, {"wiitdb.zip": self.zipped("wiitdb", "wiitdb_sample.xml"),
                             "3dstdb.zip": self.zipped("3dstdb", "3dstdb_sample.xml"),
                             "3dsdb-GB.json": FIXTURES / "3dsdb_GB.json"})
        self.assertEqual(gametdb.lookup("wii", "RMCP01")["title"], "Mario Kart Wii")
        self.assertEqual(gametdb.lookup("wii", "RMCP99")["id"], "RMCP01")  # same game code, other maker
        self.assertEqual(gametdb.lookup("gc", "GALE01")["type"], "GameCube")
        self.assertEqual(gametdb.lookup("3ds", "AREP")["title"], "Super Mario 3D Land")
        self.assertEqual(gametdb.lookup("switch", "AAACA"), {})  # switchtdb not served here
        self.assertEqual(gametdb.product_code_3ds("0004000000053F00")["product_code"], "CTR-P-AREP")
        self.assertEqual(gametdb.id_from_product_code("CTR-P-AREP"), "AREP")
        self.assertEqual(gametdb.id_from_product_code("HAC-P-AAACA"), "AAACA")
        self.assertEqual(gametdb.cover_url("gc", "GALE01", "US"), "https://art.gametdb.com/wii/cover/US/GALE01.png")
        self.assertEqual(gametdb.page_url("3ds", "AREP"), "https://www.gametdb.com/3DS/AREP")
        self.assertEqual(gametdb.cover_folders("NTSC-J")[0], "JA")


class GameTdbOptInTests(OfflineCase):
    def test_environment_cannot_override_robots(self):
        url = "https://www.gametdb.com/wiitdb.zip?LANG=EN"
        with mock.patch.object(gametdb, "robots_allows", return_value=False):
            with mock.patch.dict(os.environ, {"NCORE_GAMETDB_DOWNLOAD": "1"}):
                self.assertFalse(gametdb.download_allowed(url))
            with mock.patch.dict(os.environ, {"NCORE_GAMETDB_DOWNLOAD": ""}):
                self.assertFalse(gametdb.download_allowed(url))
        rules = googleplay.parse_robots("User-agent: *\nDisallow: /wiitdb.zip\nDisallow: /*tdb.zip\nDisallow: /*tdb.txt\n")
        self.assertFalse(googleplay.robots_match(rules, "/3dstdb.zip?LANG=EN"))
        self.assertTrue(googleplay.robots_match(rules, "/Wii/RMCP01"))
        self.assertFalse(googleplay.robots_match(googleplay.parse_robots("User-agent: *\nDisallow: *.*\n"), "/wii/cover/EN/RMCP01.png"))


class LibretroTests(OfflineCase):
    def test_parse_dat(self):
        games = libretro_dat.parse_dat(fixture("libretro_ps2_sample.dat"))
        self.assertEqual(games[0]["name"], "Grand Theft Auto III (Europe) (En,Fr,De,Es,It) (v1.40)")
        self.assertEqual(games[0]["serials"], ["SLES-50330"])
        self.assertEqual(games[0]["roms"][0]["sha1"], "91B32049BA17B4ACEEBD5C0D1E7AA70790080DBC")
        self.assertEqual(libretro_dat.serial_key("ps2", "SLES-50330/EURO"), "SLES-50330")
        self.assertEqual(libretro_dat.serial_key("ps2", "SLES_503.30"), "SLES-50330")
        self.assertEqual(libretro_dat.serial_key("wii", "RVL-RMCP-EUR-B2"), "RMCP")
        self.assertEqual(libretro_dat.serial_key("wii", "RMCP01"), "RMCP")
        self.assertEqual(libretro_dat.serial_key("3ds", "CTR-P-AREP"), "AREP")
        self.assertEqual(libretro_dat.serial_key("ps3psn", "", "UP4235-NPUB31857_00-KAZPS3000000000U.rap"), "NPUB-31857")

    def test_lookup_and_names(self):
        self.serve(libretro_dat, {"Sony - PlayStation 2.dat": FIXTURES / "libretro_ps2_sample.dat",
                                  "Nintendo - Wii.dat": FIXTURES / "libretro_wii_sample.dat"})
        rows = libretro_dat.lookup("ps2", "SLES-50330")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["title"], "Grand Theft Auto III")
        self.assertEqual(rows[0]["languages"], ["en", "fr", "de", "es", "it"])
        self.assertEqual(libretro_dat.lookup("ps2", "SLUS-20062")[0]["region"], "USA")
        self.assertEqual(libretro_dat.lookup("wii", "RMCP01")[0]["title"], "Mario Kart Wii")
        self.assertEqual(libretro_dat.by_hash("ps2", sha1="91b32049ba17b4aceebd5c0d1e7aa70790080dbc")[0]["key"], "SLES-50330")
        self.assertEqual(libretro_dat.lookup("ps1", "SLUS-00594"), [])  # PS1 DAT not served here
        self.assertEqual(libretro_dat.title_from_name("$1,000,000 Pyramid, The (USA)"), "The $1,000,000 Pyramid")
        self.assertEqual(libretro_dat.languages_from_name("Killzone (Europe, Australia) (En,Fr,De,Es,It,Nl)")[-1], "nl")
        self.assertEqual(libretro_dat.region_from_name("Killzone (Europe, Australia) (En,Fr)"), "Europe, Australia")
        url = libretro_dat.thumbnail_url("ps2", "Kingdom Hearts: Final Mix (Europe)")
        self.assertEqual(url, "https://thumbnails.libretro.com/Sony%20-%20PlayStation%202/Named_Boxarts/Kingdom%20Hearts_%20Final%20Mix%20%28Europe%29.png")
        self.assertEqual(libretro_dat.redump_url("SLES-50330"), "http://redump.org/discs/quicksearch/SLES-50330/")
        with mock.patch.object(net, "request", return_value=object()) as head:
            self.assertTrue(libretro_dat.find_thumbnail("ps2", "Grand Theft Auto III (Europe)").endswith("%28Europe%29.png"))
        self.assertEqual(head.call_args.args[0], "HEAD")
        with mock.patch.object(net, "request", return_value=None):
            self.assertEqual(libretro_dat.find_thumbnail("ps2", "Missing (Europe)"), "")


class PsStoreTests(OfflineCase):
    def test_parse_container(self):
        item = psstore.parse_container(fixture_json("chihiro_bloodborne.json"))
        self.assertEqual(item["content_id"], "EP9000-CUSA00207_00-BLOODBORNE0000EU")
        self.assertEqual(item["title_id"], "CUSA00207")
        self.assertEqual(item["name"], "Bloodborne™")
        self.assertEqual(item["publisher"], "Sony Interactive Entertainment")
        self.assertEqual(item["release_date"], "2015-03-25")
        self.assertTrue(item["description"].startswith("Hunt your nightmares"))
        self.assertNotIn("<br", item["description"])
        self.assertNotIn("PS5", item["description"])  # the backward-compatibility notice is dropped
        self.assertNotIn("https://", item["description"])
        self.assertEqual(item["genres"], ["Action", "Role-Playing Games (RPG)"])
        self.assertEqual(len(item["screenshots"]), 3)
        self.assertTrue(item["cover_url"].startswith("https://"))
        self.assertEqual(item["url"], "https://store.playstation.com/en-hu/product/EP9000-CUSA00207_00-BLOODBORNE0000EU")

    def test_lookup_falls_back_to_title_container(self):
        def fake(url, **_kwargs):
            if "/container/" in url:
                return None
            return fixture_json("chihiro_uc4_bundle.json")

        with mock.patch.object(net, "get_json", side_effect=fake) as get:
            item = psstore.lookup("EP9000-CUSA00917_00-UNCHARTED4000000")
        self.assertFalse(item["exact"])
        self.assertIn("már nem érhető el", item["warning"])
        self.assertIn("titlecontainer/HU/en/999/CUSA00917_00", get.call_args_list[-1].args[0])
        with mock.patch.object(net, "get_json", return_value=fixture_json("chihiro_bloodborne.json")):
            self.assertTrue(psstore.lookup("EP9000-CUSA00207_00-BLOODBORNE0000EU")["exact"])
        self.assertEqual(psstore.stores_for("UP9000-CUSA00341_00-UNCHARTED4000000")[0], ("US", "en"))

    def test_patches_pages(self):
        orbis = psstore.parse_patches_html(fixture("orbispatches_cusa00917.html"), "https://orbispatches.com/CUSA00917")
        self.assertEqual(orbis["title"], "Uncharted™ 4: A Thief’s End")
        self.assertEqual(orbis["content_id"], "EP9000-CUSA00917_00-UNCHARTED4000000")
        self.assertEqual(orbis["publisher"], "Sony Interactive Entertainment Europe")
        self.assertEqual(orbis["region"], "EU")
        self.assertTrue(orbis["icon"].endswith("/icon0.webp"))
        prospero = psstore.parse_patches_html(fixture("prosperopatches_ppsa01411.html"))
        self.assertEqual((prospero["publisher"], prospero["region"]), ("Sony Interactive Entertainment", "US"))
        challenge = "<html><head><title>Just a moment...</title></head><body>cf-chl-widget</body></html>"
        self.assertEqual(psstore.parse_patches_html(challenge), {})
        with mock.patch.object(psstore, "robots_allows", return_value=True), \
                mock.patch.object(net, "get_text", return_value=fixture("orbispatches_cusa00917.html")):
            self.assertEqual(psstore.patches_page("CUSA00917")["url"], "https://orbispatches.com/CUSA00917")
        self.assertEqual(psstore.patches_page("BLES01807"), {})


class MsStoreTests(OfflineCase):
    def test_parse(self):
        item = msstore.parse_product(fixture_json("msstore_fh5.json")["Products"][0])
        self.assertEqual(item["title"], "Forza Horizon 5 Standard kiadás")
        self.assertTrue(item["hungarian"])
        self.assertIn("Mexikó", item["description"])
        self.assertEqual((item["publisher"], item["developer"]), ("Xbox Game Studios", "Playground Games"))
        self.assertTrue(item["box_art"].startswith("https://store-images.s-microsoft.com/"))
        self.assertEqual(len(item["screenshots"]), 2)  # the catalog lists every screenshot twice
        self.assertEqual(item["url"], "https://www.xbox.com/hu-HU/games/store/x/9NKX70BBCDRN")
        hits = msstore.parse_autosuggest(fixture_json("msstore_autosuggest.json"))
        self.assertIn("9NKX70BBCDRN", [h["product_id"] for h in hits])
        self.assertEqual(msstore.legacy_id("4D5307E6"), "66acd000-77fe-1000-9115-d8024d5307e6")

    def test_find_game_ranking(self):
        hits = msstore.parse_autosuggest(fixture_json("msstore_autosuggest.json"))
        self.assertIn("Forza Horizon 5", [h["title"] for h in hits])
        with mock.patch.object(msstore, "search", return_value=hits), \
                mock.patch.object(msstore, "product", side_effect=lambda pid: {"product_id": pid}) as product:
            self.assertEqual(msstore.find_game("Forza Horizon 5")["product_id"], "9NNX1VVR3KNQ")  # the exact title wins
        product.assert_called_once()
        editions = [h for h in hits if h["title"] != "Forza Horizon 5"]
        with mock.patch.object(msstore, "search", return_value=editions), \
                mock.patch.object(msstore, "product", side_effect=lambda pid: {"product_id": pid}):
            self.assertEqual(msstore.find_game("Forza Horizon 5")["product_id"], "9NKX70BBCDRN")  # then the standard edition


class GooglePlayTests(OfflineCase):
    def test_parse_page(self):
        app = googleplay.parse_app_page(fixture("googleplay_maps.html"))
        self.assertEqual(app["name"], "Google Térkép")
        self.assertTrue(app["description"].startswith("A Google Térkép segítségével"))
        self.assertEqual(app["developer"], "Google LLC")
        self.assertEqual(app["category"], "Utazás és helyi információk")
        self.assertEqual(app["category_id"], "TRAVEL_AND_LOCAL")
        self.assertEqual(len(app["screenshots"]), 3)
        self.assertEqual(app["min_android"], "9")
        self.assertRegex(app["updated"], r"^\d{4}-\d\d-\d\d$")
        self.assertEqual(app["content_rating"], "PEGI 3")
        self.assertNotIn("version", app)  # "Varies with device": the APK's own version counts
        self.assertEqual(googleplay.parse_app_page("<html>404</html>"), {})

    def test_robots(self):
        rules = googleplay.parse_robots(fixture("googleplay_robots.txt"))
        self.assertTrue(googleplay.robots_match(rules, "/store/apps/details?id=com.google.android.apps.maps&hl=hu&gl=HU"))
        self.assertFalse(googleplay.robots_match(rules, "/store/search?q=maps"))
        self.assertFalse(googleplay.robots_match(rules, "/apps/testing/x"))
        self.assertFalse(googleplay.robots_match(rules, "/store/apps/datasafety?id=x"))
        self.assertTrue(googleplay.robots_match(googleplay.parse_robots("User-agent: *\nDisallow: /a\nAllow: /a/b$"), "/a/b"))
        self.assertFalse(googleplay.robots_match(googleplay.parse_robots("User-agent: ncore-up\nDisallow: /\n\nUser-agent: *\nAllow: /"), "/x"))

    def test_app(self):
        with mock.patch.object(googleplay, "robots_allows", return_value=True), \
                mock.patch.object(net, "get_text", return_value=fixture("googleplay_maps.html")) as get:
            app = googleplay.app("com.google.android.apps.maps")
        self.assertEqual(app["url"], "https://play.google.com/store/apps/details?id=com.google.android.apps.maps&hl=hu")
        self.assertEqual(get.call_args.kwargs["params"], {"id": "com.google.android.apps.maps", "hl": "hu", "gl": "HU"})
        self.assertEqual(googleplay.app("not a package"), {})


class ItunesTests(OfflineCase):
    def test_parse_and_lookup(self):
        data = fixture_json("itunes_maps.json")
        app = itunes.parse_result(data["results"][0])
        self.assertEqual((app["name"], app["bundle_id"], app["seller"]), ("Google Térkép", "com.google.Maps", "Google LLC"))
        self.assertTrue(app["hungarian"])
        self.assertEqual(app["min_os"], "16.0")
        self.assertNotIn("uo=", app["url"])
        self.assertTrue(app["url"].startswith("https://apps.apple.com/hu/app/"))
        with mock.patch.object(net, "get_json", return_value=data) as get:
            self.assertEqual(itunes.lookup("com.google.Maps")["track_id"], 585027354)
        self.assertEqual(get.call_args.kwargs["params"], {"bundleId": "com.google.Maps", "country": "hu", "lang": "hu_hu"})
        with mock.patch.object(net, "get_json", return_value={"resultCount": 0, "results": []}):
            self.assertEqual(itunes.lookup("com.example.none"), {})

    def test_search_respects_robots(self):
        with mock.patch.object(itunes, "robots_allows", return_value=False), mock.patch.object(net, "get_json") as get:
            self.assertEqual(itunes.search("otp bank"), [])
        get.assert_not_called()
        with mock.patch.object(itunes, "robots_allows", return_value=True), \
                mock.patch.object(net, "get_json", return_value=fixture_json("itunes_maps.json")):
            self.assertEqual(itunes.search("google maps")[0]["bundle_id"], "com.google.Maps")


class FdroidTests(OfflineCase):
    def test_index_and_lookup(self):
        self.serve(fdroid, {"index-v2.json": FIXTURES / "fdroid_index-v2.json"})
        with mock.patch.object(net, "get_json", return_value=fixture_json("fdroid_api.json")):
            info = fdroid.lookup("org.fdroid.fdroid")
        self.assertEqual(info["name"], "F-Droid")
        self.assertEqual(info["description_locale"], "hu")
        self.assertTrue(info["description"].startswith("Az F-Droid"))
        self.assertTrue(info["description_en"])
        self.assertEqual(info["license"], "GPL-3.0-or-later")
        self.assertTrue(info["icon"].startswith("https://f-droid.org/repo/org.fdroid.fdroid/"))
        self.assertEqual(info["url"], "https://f-droid.org/packages/org.fdroid.fdroid/")
        signer = info["versions"][0]["signers"][0]
        self.assertTrue(fdroid.signer_matches(info, signer.upper()))
        self.assertFalse(fdroid.signer_matches(info, "00" * 32))
        with mock.patch.object(net, "get_json", return_value=None):
            self.assertEqual(fdroid.lookup("com.google.android.apps.maps"), {})


if __name__ == "__main__":
    unittest.main()
