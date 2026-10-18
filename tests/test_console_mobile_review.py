"""Regression tests for the console/mobile review fixes: odd data never raises, name-only titles do not outrank exact
sources, localized SFO titles are no Hungarian claim, robots.txt that cannot be fetched blocks the page."""

from __future__ import annotations

import json
import shutil
import struct
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from ncore_up import net, translate
from ncore_up.local import apk, consoleid, ipa
from ncore_up.resolve import console, mobile
from ncore_up.sources import (fdroid, gametdb, googleplay, igdb, itunes, libretro_dat, msstore, psstore, titledb, wikidata,
                              wikipedia)

try:
    from test_console_mobile_local import apk_file, axml, make_release, ncch, ps4_pkg, sfo, wii_header
except ImportError:  # run as tests.test_console_mobile_review
    from tests.test_console_mobile_local import apk_file, axml, make_release, ncch, ps4_pkg, sfo, wii_header

EMPTY = {
    (titledb, "lookup"): {}, (titledb, "noe_search"): {}, (gametdb, "lookup"): {}, (gametdb, "find_cover"): "",
    (gametdb, "product_code_3ds"): {}, (psstore, "lookup"): {}, (psstore, "patches_page"): {}, (libretro_dat, "lookup"): [],
    (libretro_dat, "find_thumbnail"): "", (msstore, "lookup_xbox360"): {}, (msstore, "find_game"): {}, (igdb, "search"): [],
    (wikidata, "lookup"): {}, (wikipedia, "summary"): {}, (googleplay, "app"): {}, (fdroid, "lookup"): {}, (itunes, "lookup"): {},
    (itunes, "search"): [],
}


class Offline(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for target in ("request", "get_json", "get_text"):
            patch = mock.patch.object(net, target, side_effect=AssertionError("network used in an offline test"))
            patch.start()
            self.addCleanup(patch.stop)


class Resolving(Offline):
    def setUp(self):
        super().setUp()
        self.mocks = {}
        for (module, name), value in EMPTY.items():
            patch = mock.patch.object(module, name, return_value=value)
            self.mocks[f"{module.__name__.rsplit('.', 1)[-1]}.{name}"] = patch.start()
            self.addCleanup(patch.stop)
        for patch in (mock.patch.object(translate, "to_hungarian", side_effect=lambda text, *a, **k: (text, False)),
                      mock.patch.object(gametdb, "WARNING", "")):
            patch.start()
            self.addCleanup(patch.stop)


class LocalReaderTests(Offline):
    def test_content_id_next_to_underscores(self):
        tags = consoleid.parse_name("Uncharted_EP9000-CUSA00917_00-UNCHARTED4000000_PS4")
        self.assertEqual(tags["content_id"], "EP9000-CUSA00917_00-UNCHARTED4000000")
        self.assertEqual(tags["title"], "Uncharted")

    def test_size_and_backport_tags_leave_the_title(self):
        self.assertEqual(consoleid.clean_title("Animal Crossing New Horizons [0100620011960000][v0] (16.95 GB).nsp"),
                         "Animal Crossing New Horizons")
        self.assertEqual(consoleid.clean_title("Crash.Bandicoot.N.Sane.Trilogy.Backport.5.05.PS4-DUPLEX"), "Crash Bandicoot N Sane Trilogy")

    def test_cart_has_no_title_version_from_the_ncch_format_field(self):
        header = bytearray(0x4000)
        header[0x100:0x104] = b"NCSD"
        struct.pack_into("<Q", header, 0x108, 0x0004000000053F00)
        struct.pack_into("<II", header, 0x120, 0x4000 // 0x200, 0x10)
        part = bytearray(ncch(0x0004000000053F00, "CTR-P-AREP"))
        struct.pack_into("<H", part, 0x112, 2)  # NCCH format version 2, not "0.0.2"
        read = consoleid.read_3ds(consoleid.Blob(data=bytes(header) + bytes(part)))
        self.assertEqual(read["product_code"], "CTR-P-AREP")
        self.assertNotIn("version", read)
        self.assertEqual(consoleid.parse_ncch(b"\0" * 0x100 + b"NCCH"), {})  # cut header

    def test_param_json_odd_shapes(self):
        self.assertEqual(consoleid.parse_param_json("[1, 2]"), {})
        self.assertEqual(consoleid.parse_param_json('{"titleId": "PPSA01411", "localizedParameters": []}')["title_id"], "PPSA01411")

    def test_identify_never_raises(self):
        release = make_release(self.tmp / "Odd.PS5-GRP", {"sce_sys/param.json": b'{"localizedParameters": {"defaultLanguage": ["x"]}}'})
        info = consoleid.identify(release, {})
        self.assertEqual(info.platform, "ps5")

    def test_localized_sfo_title_is_only_a_hint(self):
        param = sfo({"TITLE": "Bloodborne", "TITLE_ID": "CUSA00207", "APP_VER": "01.09", "CATEGORY": "gp", "TITLE_24": "Bloodborne"})
        release = make_release(self.tmp / "Bloodborne.Update.v1.09.PS4-GRP", {"bb.pkg": ps4_pkg("EP9000-CUSA00207_00-BLOODBORNE0000EU", param)})
        info = consoleid.identify(release, {})
        self.assertFalse(info.hungarian)
        self.assertEqual(info.languages, [])
        self.assertEqual(info.title_languages, ["hu"])
        self.assertTrue(any("TITLE_24" in w for w in info.warnings))

    def test_bundle_with_unreadable_base_apk(self):
        bundle = self.tmp / "x.xapk"
        with zipfile.ZipFile(bundle, "w") as archive:
            archive.writestr("manifest.json", json.dumps({"package_name": "com.x.y", "version_name": "1.0", "name": "X"}))
            archive.writestr("com.x.y.apk", b"not a zip")
        with mock.patch.object(apk.shutil, "which", return_value=None):
            data = apk.inspect(bundle)
        self.assertEqual((data["package"], data["version_name"], data["label"]), ("com.x.y", "1.0", "X"))
        self.assertTrue(any("x.xapk → com.x.y.apk" in w for w in data["warnings"]))
        self.assertEqual(data["signature"][0], "ismeretlen")
        self.assertEqual(apk.parse_bundle_manifest({"split_apks": "abc"})["splits"], [])

    def test_ipa_with_malformed_plists(self):
        path = self.tmp / "a.ipa"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("Payload/A.app/Info.plist", b"<?xml version='1.0'?><plist><dict><key>a</key>")
            archive.writestr("iTunesMetadata.plist", b"<?xml version='1.0'?><plist><dict>")
        data = ipa.inspect(path)
        self.assertTrue(any("Info.plist" in w for w in data["warnings"]))
        self.assertEqual(ipa.parse_info_plist(b"<plist version='1.0'><dict><key>CFBundleSupportedPlatforms</key>"
                                              b"<string>iPhoneOS</string></dict></plist>")["platforms"], ["iPhoneOS"])


class SourceShapeTests(Offline):
    def test_play_json_ld_odd_shapes(self):
        page = ('<script type="application/ld+json">{"@type": "SoftwareApplication", "name": "X", "offers": {"price": 0}, '
                '"aggregateRating": {"ratingValue": "4.5", "ratingCount": "1,234"}, "author": [{"name": "A"}]}</script>')
        app = googleplay.parse_app_page(page)
        self.assertEqual((app["name"], app["ratings"], app["price"], app["developer"]), ("X", 1234, "0", ""))
        ds5 = ("<script>AF_initDataCallback({key: 'ds:5', hash: '1', data:" + json.dumps([None, [None, None, [["Name"]] + [7] * 150]])
               + ", sideChannel: {}});</script>")
        self.assertEqual(googleplay.parse_app_page(ds5)["name"], "Name")

    def test_store_records_with_holes(self):
        self.assertEqual(itunes.parse_result({"kind": "software", "trackName": "X", "fileSizeBytes": "n/a"})["size"], 0)
        self.assertEqual(msstore.parse_product({"ProductId": "9ABC", "LocalizedProperties": [None]})["title"], "")
        self.assertEqual(psstore.parse_container({"id": "EP9000-CUSA00917_00-UNCHARTED4000000", "images": [None],
                                                  "mediaList": []})["screenshots"], [])
        self.assertEqual(fdroid.compact({"versions": {"a": None}})["versions"][0]["signers"], [])

    def test_titledb_odd_record_does_not_break_the_index(self):
        entry = titledb.parse_entry({"id": "0100000000010000", "name": 5, "size": "1 GB", "languages": "en"})
        self.assertEqual((entry["name"], entry["size"], entry["languages"]), ("", 0, []))
        dump = self.tmp / "HU.en.json"
        dump.write_text(json.dumps({"1": {"id": "0100000000020000", "name": ["odd"], "size": "big"},
                                    "2": {"id": "0100000000010000", "name": "Super Mario Odyssey"}}), encoding="utf-8")
        db = titledb.open_db(self.tmp / "index.sqlite", titledb.SCHEMA)
        try:
            self.assertEqual(titledb.build_index(db, dump, "HU.en"), 2)
        finally:
            db.close()

    def test_unreachable_robots_txt_blocks_and_is_asked_again(self):
        googleplay._ROBOTS.pop("https://robots.invalid", None)
        failures = list(net.FAILURES)
        self.addCleanup(lambda: net.FAILURES.__setitem__(slice(None), failures))
        with mock.patch.object(net, "get_text", return_value=None) as get:
            self.assertFalse(googleplay.robots_allows("https://robots.invalid/page"))
            self.assertFalse(googleplay.robots_allows("https://robots.invalid/page"))
        self.assertEqual(get.call_count, 2)
        with mock.patch.object(net, "get_text", return_value="User-agent: *\nDisallow: /private\n"):
            self.assertTrue(googleplay.robots_allows("https://robots.invalid/page"))
        self.assertFalse(googleplay.robots_allows("https://robots.invalid/private/x"))  # cached rules now
        googleplay._ROBOTS.pop("https://robots.invalid", None)


class ConsoleReviewTests(Resolving):
    def test_name_title_does_not_outrank_the_patches_title(self):
        release = make_release(self.tmp / "Uncharted_EP9000-CUSA00917_00-UNCHARTED4000000_PS4", {"x.nfo": b"x"})
        self.mocks["psstore.lookup"].return_value = {
            "content_id": "EP9000-CUSA00917_00-U4UTLLBUNDLE0000", "name": "UNCHARTED 4 & Lost Legacy Bundle", "exact": False,
            "warning": "csomag", "description": "Two games.", "publisher": "SIE", "rating": "", "cover_url": "https://x/bundle.png",
            "genres": [], "screenshots": [], "release_date": "", "url": ""}
        self.mocks["psstore.patches_page"].return_value = {"url": "https://orbispatches.com/CUSA00917", "title": "Uncharted™ 4: A Thief’s End",
                                                           "publisher": "SIE Europe", "icon": "https://cdn.orbispatches.com/icon0.webp"}
        meta = console.resolve(release, "console", original=False, hints={})
        self.assertEqual(meta.extra["torrent_name"], "[PS4] Uncharted 4 - A Thief's End")
        self.assertEqual(meta.cover_url, "https://cdn.orbispatches.com/icon0.webp")  # not the bundle's art
        self.assertEqual(meta.title_original, "")
        self.assertEqual(meta.identifiers["content_id"], "EP9000-CUSA00917_00-UNCHARTED4000000")

    def test_sfo_title_24_does_not_make_the_name_hun(self):
        param = sfo({"TITLE": "Bloodborne", "TITLE_ID": "CUSA00207", "APP_VER": "01.09", "CATEGORY": "gp", "TITLE_24": "Bloodborne"})
        release = make_release(self.tmp / "Bloodborne.Update.v1.09.PS4-GRP", {"bb.pkg": ps4_pkg("EP9000-CUSA00207_00-BLOODBORNE0000EU", param)})
        meta = console.resolve(release, "console", original=False, hints={})
        self.assertEqual(meta.extra["torrent_name"], "[PS4] Bloodborne Update v1.09")
        self.assertEqual(meta.extra["hungarian"], "nem")
        self.assertNotIn("magyar", meta.languages)

    def test_european_catalogue_languages_skip_a_japanese_disc(self):
        release = make_release(self.tmp / "Mario Kart Wii (Japan)", {"mkw.iso": wii_header("RMCJ01", "MARIO KART Wii") + b"\0" * 4096})
        self.mocks["libretro_dat.lookup"].return_value = [{"name": "Mario Kart Wii (Japan)", "region": "Japan", "title": "Mario Kart Wii",
                                                           "languages": []}]
        self.mocks["titledb.noe_search"].return_value = {
            "title": "Mario Kart Wii", "url": "https://www.nintendo.com/en-gb/Games/Wii/Mario-Kart-Wii-281848.html", "product_code": "",
            "image_url": "", "publisher": "Nintendo", "developer": "", "release_date": "2008-04-11", "excerpt": "Race.",
            "age_rating": "PEGI 3", "languages": ["en", "de", "hu"], "categories": []}
        meta = console.resolve(release, "console", original=False, hints={})
        self.assertEqual(meta.region, "NTSC-J (JPN)")
        self.assertEqual(meta.languages, [])
        self.assertEqual(meta.extra["hungarian"], "nem")
        self.assertIn("https://www.nintendo.com/en-gb/Games/Wii/Mario-Kart-Wii-281848.html", meta.proof_links)

    def test_igdb_crash_is_a_warning(self):
        release = make_release(self.tmp / "Mario Kart Wii", {"mkw.iso": wii_header("RMCP01", "Mario Kart Wii") + b"\0" * 4096})
        self.mocks["igdb.search"].side_effect = KeyError("company")
        meta = console.resolve(release, "console", original=False, hints={})
        self.assertTrue(any("IGDB" in w for w in meta.warnings))
        self.assertEqual(meta.extra["torrent_name"], "[Wii] Mario Kart Wii")


class MobileReviewTests(Resolving):
    def test_app_name_without_tagline(self):
        self.assertEqual(mobile.app_name("Spotify — Zene és podcastok", "Spotify"), "Spotify")
        self.assertEqual(mobile.app_name("Duolingo: Language Lessons", "Duolingo"), "Duolingo")
        self.assertEqual(mobile.app_name("Google Térkép", "Maps"), "Google Térkép")
        self.assertEqual(mobile.app_name("TikTok – Videos, Music & LIVE", ""), "TikTok")
        self.assertEqual(mobile.app_name("Star Wars: KOTOR", "KOTOR"), "Star Wars: KOTOR")

    def test_library_locales_are_no_hungarian_claim(self):
        package = apk_file(self.tmp / "x.apk", axml("com.example.player", 5, "5.0", 21, 33, "Player"))
        release = make_release(self.tmp / "rel", {"Player.v5.0.apk": package.read_bytes()})
        badging = {"package": "com.example.player", "version_name": "5.0", "locales": ["de", "hu", "it"], "labels": {}}
        self.mocks["googleplay.app"].return_value = {"name": "Player", "url": "https://play.google.com/store/apps/details?id=x&hl=hu",
                                                     "description": "The best player for every format, fast and easy to use."}
        with mock.patch.object(apk, "badging", return_value=badging), mock.patch.object(apk, "certs", return_value={}):
            meta = mobile.resolve(release, "mobil", original=False, hints={})
            self.assertEqual((meta.extra["hungarian"], meta.languages), ("nem", []))
            self.assertTrue(any("magyar erőforrások" in w for w in meta.warnings))
            badging["labels"] = {"hu": "Lejátszó"}
            meta = mobile.resolve(release, "mobil", original=False, hints={})
        self.assertEqual((meta.extra["hungarian"], meta.languages), ("igen", ["magyar"]))

    def test_store_crash_is_a_warning(self):
        package = apk_file(self.tmp / "x.apk", axml("com.example.player", 5, "5.0", 21, 33, "Player"))
        release = make_release(self.tmp / "rel", {"Player.v5.0.apk": package.read_bytes()})
        self.mocks["googleplay.app"].side_effect = TypeError("page changed")
        with mock.patch.object(apk, "badging", return_value={}), mock.patch.object(apk, "certs", return_value={}):
            meta = mobile.resolve(release, "mobil", original=False, hints={})
        self.assertTrue(any("Google Play" in w for w in meta.warnings))
        self.assertEqual(meta.extra["torrent_name"], "[Android] Player v5.0")

    def test_bundle_name_and_play_tagline(self):
        bundle = self.tmp / "spotify.xapk"
        base = apk_file(self.tmp / "base.apk", axml("com.spotify.music", 123, "9.0.80", 26, 34, "Spotify"))
        with zipfile.ZipFile(bundle, "w") as archive:
            archive.writestr("manifest.json", json.dumps({"package_name": "com.spotify.music", "version_name": "9.0.80", "name": "Spotify"}))
            archive.write(base, "com.spotify.music.apk")
        release = make_release(self.tmp / "rel", {"Spotify_9.0.80.xapk": bundle.read_bytes()})
        self.mocks["googleplay.app"].return_value = {
            "name": "Spotify — Zene és podcastok", "url": "https://play.google.com/store/apps/details?id=com.spotify.music&hl=hu",
            "description": "A Spotify mostantól mobilon és tableten is ingyenes, és bárhol hallgathatod a kedvenc zenéidet."}
        with mock.patch.object(apk, "badging", return_value={}), mock.patch.object(apk, "certs", return_value={}):
            meta = mobile.resolve(release, "mobil", original=False, hints={})
        self.assertEqual(meta.extra["torrent_name"], "[Android] Spotify v9.0.80")


if __name__ == "__main__":
    unittest.main()
