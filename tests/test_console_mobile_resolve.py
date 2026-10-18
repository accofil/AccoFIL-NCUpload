"""Offline tests of resolve.console and resolve.mobile: synthetic releases, every outside source mocked, the network
forbidden."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ncore_up import net, translate
from ncore_up.local import apk as apk_reader
from ncore_up.resolve import console, mobile
from ncore_up.sources import (fdroid, gametdb, googleplay, igdb, itunes, libretro_dat, msstore, psstore, titledb, wikidata,
                              wikipedia)

try:
    from test_console_mobile_local import (apk_file, axml, ipa_file, iso9660, macho, make_release, pfs0, ps4_pkg, sfo, stfs,
                                           wii_header)
except ImportError:  # run as tests.test_console_mobile_resolve
    from tests.test_console_mobile_local import (apk_file, axml, ipa_file, iso9660, macho, make_release, pfs0, ps4_pkg, sfo, stfs,
                                                 wii_header)

FIXTURES = Path(__file__).parent / "fixtures" / "console_mobile"
EMPTY = {
    (titledb, "lookup"): {}, (titledb, "noe_search"): {}, (gametdb, "lookup"): {}, (gametdb, "find_cover"): "",
    (gametdb, "product_code_3ds"): {}, (psstore, "lookup"): {}, (psstore, "patches_page"): {}, (libretro_dat, "lookup"): [],
    (libretro_dat, "find_thumbnail"): "",
    (msstore, "lookup_xbox360"): {}, (msstore, "find_game"): {}, (igdb, "search"): [], (wikidata, "lookup"): {},
    (wikipedia, "summary"): {}, (googleplay, "app"): {}, (fdroid, "lookup"): {}, (itunes, "lookup"): {}, (itunes, "search"): [],
}


class ResolverCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.mocks = {}
        for (module, name), value in EMPTY.items():
            patch = mock.patch.object(module, name, return_value=value)
            self.mocks[f"{module.__name__.rsplit('.', 1)[-1]}.{name}"] = patch.start()
            self.addCleanup(patch.stop)
        for target in ("request", "get_json", "get_text"):
            patch = mock.patch.object(net, target, side_effect=AssertionError("network used in an offline test"))
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch.object(translate, "to_hungarian", side_effect=lambda text, *a, **k: (text, False))
        self.translate = patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(gametdb, "WARNING", "")
        patch.start()
        self.addCleanup(patch.stop)

    def set(self, key: str, value):
        self.mocks[key].return_value = value

    def release(self, name: str, files: dict[str, bytes]):
        return make_release(self.tmp / name, files)


class ConsoleTests(ResolverCase):
    def test_ps4_update_pkg(self):
        param = sfo({"TITLE": "Bloodborne™", "TITLE_ID": "CUSA00207", "APP_VER": "01.09", "CATEGORY": "gp", "SYSTEM_VER": 0x05050000})
        release = self.release("Bloodborne.Update.v1.09.PS4-GRP", {"bb.pkg": ps4_pkg("EP9000-CUSA00207_00-BLOODBORNE0000EU", param)})
        store = psstore.parse_container(json.loads((FIXTURES / "chihiro_bloodborne.json").read_text(encoding="utf-8")))
        store["exact"] = True
        self.set("psstore.lookup", store)
        self.set("psstore.patches_page", {"url": "https://orbispatches.com/CUSA00207", "title": "Bloodborne", "publisher": "SIE", "icon": ""})
        self.translate.side_effect = lambda text, *a, **k: ("Vadászd le a rémálmaidat.", True)
        meta = console.resolve(release, "console", original=False, hints={})
        self.assertEqual(meta.kind, "console_game")
        self.assertEqual(meta.extra["torrent_name"], "[PS4] Bloodborne Update v1.09")
        self.assertEqual(meta.platform, "PlayStation 4")
        self.assertEqual(meta.region, "PAL (EUR)")
        self.assertEqual(meta.identifiers["cusa"], "CUSA00207")
        self.assertEqual(meta.identifiers["content_id"], "EP9000-CUSA00207_00-BLOODBORNE0000EU")
        self.assertEqual(meta.proof_links[0], "https://store.playstation.com/en-hu/product/EP9000-CUSA00207_00-BLOODBORNE0000EU")
        self.assertIn("https://orbispatches.com/CUSA00207", meta.proof_links)
        self.assertTrue(meta.cover.startswith(b"\x89PNG"))
        self.assertEqual((meta.description_hu, meta.machine_translated, meta.description_source),
                         ("Vadászd le a rémálmaidat.", True, "PlayStation Store"))
        self.assertIn("Hunt your nightmares", meta.description_en)
        self.assertIn("GoldHEN", meta.requirements)
        self.assertIn("5.05 firmware", meta.requirements)
        self.assertIn("Package Installer", meta.install_hu)
        self.assertEqual(meta.extra["content_type"], "frissítés (update)")
        self.assertIn("akció", meta.genres)
        self.assertGreaterEqual(meta.confidence, 0.9)
        self.assertTrue(any("saját kép" in w for w in meta.warnings))

    def test_switch_base_with_hungarian_name(self):
        release = self.release("Super.Mario.Odyssey.HUN.NSW-GRP", {"smo.nsp": pfs0([("01000000000100000000000000000003.tik", b"\0" * 16)])})
        record = titledb.parse_entry(json.loads((FIXTURES / "titledb_HU.en.json").read_text(encoding="utf-8"))["70010000000127"])
        record["versions"] = {65536: "2017-10-26", 393216: "2025-06-25"}
        self.set("titledb.lookup", record)
        self.set("titledb.noe_search", titledb.parse_noe(json.loads((FIXTURES / "noe_smo.json").read_text(encoding="utf-8")))[0])
        meta = console.resolve(release, "console", original=False, hints={})
        self.assertEqual(meta.extra["torrent_name"], "[NSW] Super Mario Odyssey HUN")
        self.assertEqual(meta.region, "Régiófüggetlen (RF)")
        self.assertEqual(meta.title, "Super Mario Odyssey")
        self.assertEqual(meta.publisher, "Nintendo")
        self.assertEqual(meta.date, "2017-10-27")
        self.assertTrue(meta.proof_links[0].startswith("https://www.nintendo.com/en-gb/Games/"))
        self.assertIn("PS_NSwitch_SuperMarioOdyssey", meta.cover_url)
        self.assertEqual(meta.identifiers["titleid"], "0100000000010000")
        self.assertEqual(meta.identifiers["product_code"], "HACPAAACA")
        self.assertEqual(meta.languages[0], "magyar")
        self.assertIn("Atmosphère", meta.requirements)
        self.assertEqual(meta.extra["latest_update"], "v393216 (2025-06-25)")
        self.assertFalse(any("újabb frissítés" in w for w in meta.warnings))  # a base game alone is not outdated
        update = self.release("SMO.Update.NSW-GRP", {"u.nsp": pfs0([("01000000000108000000000000000005.tik", b"\0" * 16)])})
        update.name = "Super Mario Odyssey [0100000000010800][v65536]"
        meta = console.resolve(update, "console", original=False, hints={})
        self.assertEqual(meta.extra["torrent_name"], "[NSW] Super Mario Odyssey Update v65536")
        self.assertEqual(meta.extra["update_date"], "2017-10-26")
        self.assertTrue(any("újabb frissítés" in w for w in meta.warnings))
        self.assertTrue(any("megjelenített verziót" in w for w in meta.warnings))
        self.assertEqual(meta.description_source, "titledb")
        self.assertTrue(meta.description_en.startswith("Join Mario"))
        self.mocks["gametdb.lookup"].assert_called_with("switch", "AAACA")

    def test_ps2_iso_from_dat(self):
        iso = iso9660({"SYSTEM.CNF": b"BOOT2 = cdrom0:\\SLES_503.30;1\r\nVMODE = PAL\r\n"})
        release = self.release("GTA3", {"gta3.iso": iso})
        self.set("libretro_dat.lookup", [{"name": "Grand Theft Auto III (Europe) (En,Fr,De,Es,It) (v1.40)", "region": "Europe",
                                          "title": "Grand Theft Auto III", "languages": ["en", "fr", "de", "es", "it"]}])
        self.set("libretro_dat.find_thumbnail", libretro_dat.thumbnail_url("ps2", "Grand Theft Auto III (Europe) (En,Fr,De,Es,It) (v1.40)"))
        meta = console.resolve(release, "console", original=False, hints={})
        self.assertEqual(meta.extra["torrent_name"], "[PS2] Grand Theft Auto III")
        self.assertEqual(meta.identifiers["serial"], "SLES-50330")
        self.assertEqual(meta.region, "PAL (EUR)")
        self.assertEqual(meta.languages, ["angol", "francia", "német", "spanyol", "olasz"])
        self.assertEqual(meta.proof_links[0], "http://redump.org/discs/quicksearch/SLES-50330/")
        self.assertIn("Named_Boxarts", meta.cover_url)
        self.assertEqual(meta.extra["dat_name"], "Grand Theft Auto III (Europe) (En,Fr,De,Es,It) (v1.40)")
        self.assertIn("OPL", meta.requirements)
        self.assertTrue(any("leírás" in w for w in meta.warnings))  # no description source without IGDB

    def test_wii_japanese_disc(self):
        release = self.release("Mario Kart Wii (Japan)", {"mkw.iso": wii_header("RMCJ01", "MARIO KART Wii") + b"\0" * 4096})
        self.set("gametdb.lookup", {"id": "RMCJ01", "type": "", "region": "NTSC-J", "languages": ["ja"], "title": "Mario Kart Wii",
                                    "synopsis": "Race.", "developer": "Nintendo EAD", "publisher": "Nintendo", "date": "2008-04-10",
                                    "genres": ["racing"], "rating": "CERO A", "name": "Mario Kart Wii"})
        self.set("gametdb.find_cover", "https://art.gametdb.com/wii/cover/JA/RMCJ01.png")
        meta = console.resolve(release, "console", original=False, hints={})
        self.assertEqual(meta.region, "NTSC-J (JPN)")
        self.assertTrue(any("NTSC-J" in w for w in meta.warnings))
        self.assertEqual(meta.extra["torrent_name"], "[Wii] Mario Kart Wii")
        self.assertEqual(meta.proof_links[0], "https://www.gametdb.com/Wii/RMCJ01")
        self.assertEqual(meta.genres, ["autóverseny"])
        self.assertIn("USB Loader GX", meta.install_hu)

    def test_wii_without_gametdb_dump(self):
        release = self.release("Mario_Kart_Wii_PAL_WII-GRP", {"mkw.iso": wii_header("RMCP01", "MARIO KART Wii") + b"\0" * 4096})

        def blocked(_platform, _game_id):
            gametdb.WARNING = "GameTDB: a robots.txt tiltja a(z) wiitdb.zip automatikus letöltését"
            return {}

        self.mocks["gametdb.lookup"].side_effect = blocked
        self.set("libretro_dat.lookup", [{"name": "Mario Kart Wii (Europe, Australia) (En,Fr,De,Es,It)", "region": "Europe",
                                          "title": "Mario Kart Wii", "languages": ["en", "fr", "de", "es", "it"]}])
        self.set("libretro_dat.find_thumbnail", "https://thumbnails.libretro.com/Nintendo%20-%20Wii/Named_Boxarts/MKW.png")
        meta = console.resolve(release, "console", original=False, hints={})
        self.assertTrue(any("robots.txt" in w for w in meta.warnings))
        self.assertFalse(any("nem ismeri" in w for w in meta.warnings))
        self.assertEqual(meta.cover_url, "https://thumbnails.libretro.com/Nintendo%20-%20Wii/Named_Boxarts/MKW.png")
        self.assertEqual((meta.region, meta.extra["torrent_name"]), ("PAL (EUR)", "[Wii] Mario Kart Wii"))
        self.mocks["libretro_dat.find_thumbnail"].assert_called_once_with("wii", "Mario Kart Wii (Europe, Australia) (En,Fr,De,Es,It)")
        self.mocks["titledb.noe_search"].assert_called_once_with(title="Mario Kart Wii", system="wii")
        self.set("libretro_dat.lookup", [{"name": "Mario Kart Wii (Europe, Australia) (En,Fr,De,Es,It)", "region": "Europe",
                                          "serial": "RVL-RMCP-EUR-B2", "title": "Mario Kart Wii", "languages": ["en"]}])
        self.set("titledb.noe_search", {"title": "Mario Kart Wii", "url": "https://www.nintendo.com/en-gb/Games/Wii/Mario-Kart-Wii-281848.html",
                                        "product_code": "", "image_url": "https://www.nintendo.com/eu/media/mkw.jpg", "publisher": "Nintendo",
                                        "developer": "", "release_date": "2008-04-11", "excerpt": "Race with Mario.", "age_rating": "PEGI 3",
                                        "languages": ["en"], "categories": ["Racing"]})
        meta = console.resolve(release, "console", original=False, hints={})
        self.assertEqual(meta.proof_links, ["http://redump.org/discs/quicksearch/RVL-RMCP-EUR-B2/",
                                            "https://www.nintendo.com/en-gb/Games/Wii/Mario-Kart-Wii-281848.html"])
        self.assertEqual(meta.cover_url, "https://www.nintendo.com/eu/media/mkw.jpg")
        self.assertEqual((meta.description_en, meta.description_source), ("Race with Mario.", "Nintendo"))

    def test_xbox360_god_with_hungarian_store_text(self):
        release = self.release("Forza GOD", {"4D530AA0/00007000/0123456789ABCDEF0123": stfs(0x4D530AA0, "Forza Horizon", "Microsoft")})
        product = msstore.parse_product(json.loads((FIXTURES / "msstore_fh5.json").read_text(encoding="utf-8"))["Products"][0])
        self.set("msstore.lookup_xbox360", product)
        meta = console.resolve(release, "console", original=False, hints={})
        self.assertEqual(meta.description_source, "Microsoft Store")
        self.assertFalse(meta.machine_translated)
        self.assertIn("Mexikó", meta.description_hu)
        self.assertEqual(meta.proof_links[0], "https://www.xbox.com/hu-HU/games/store/x/9NKX70BBCDRN")
        self.assertEqual(meta.extra["torrent_name"], "[XBOX360] Forza Horizon")
        self.assertEqual(meta.extra["format"], "GOD")
        self.assertIn("JTAG/RGH", meta.requirements)
        self.translate.assert_not_called()

    def test_igdb_and_hungarian_wikipedia(self):
        release = self.release("Some_Game_PAL_PS3-GRP", {"PS3_GAME/PARAM.SFO": sfo({"TITLE": "Some Game", "TITLE_ID": "BLES09999"})})
        self.set("igdb.search", [{"id": 1, "name": "Some Game", "summary": "English summary.", "url": "https://www.igdb.com/games/some-game",
                                  "first_release_date": 1262304000, "genres": [{"name": "Shooter"}], "involved_companies": [
                                      {"company": {"name": "Dev Studio"}, "developer": True}]}])
        self.set("wikidata.lookup", {"label_hu": "Some Game", "huwiki": "Some Game"})
        self.set("wikipedia.summary", {"extract": "A Some Game egy 2010-es lövöldözős videójáték, amelyet a Dev Studio fejlesztett és adott ki."})
        meta = console.resolve(release, "console", original=False, hints={})
        self.assertEqual(meta.description_source, "Wikipédia (CC BY-SA)")
        self.assertTrue(meta.description_hu.startswith("A Some Game"))
        self.assertEqual(meta.description_en, "English summary.")
        self.assertEqual(meta.year, "2010")
        self.assertEqual(meta.creators, ["Dev Studio"])
        self.assertIn("https://www.igdb.com/games/some-game", meta.proof_links)
        self.assertFalse(any("wikipedia" in link for link in meta.proof_links))
        self.mocks["wikidata.lookup"].assert_called_with("igdb", "some-game")
        self.assertEqual(meta.extra["torrent_name"], "[PS3] Some Game")
        self.assertEqual(meta.region, "PAL (EUR)")

    def test_clean_title(self):
        self.assertEqual(console.clean_title("Uncharted™ 4: A Thief’s End"), "Uncharted 4 - A Thief's End")
        self.assertEqual(console.clean_title("Marvel's Spider-Man: Miles Morales"), "Marvel's Spider-Man - Miles Morales")

    def test_unknown_release_never_raises(self):
        release = self.release("Mystery Game", {"readme.txt": b"hello"})
        meta = console.resolve(release, "console", original=False, hints={})
        self.assertLessEqual(meta.confidence, 0.2)
        self.assertTrue(any("platform" in w for w in meta.warnings))
        self.assertNotIn("torrent_name", meta.extra)
        meta = console.resolve(release, "console", original=False, hints={"platform": "ps2", "title": "Mystery Game"})
        self.assertEqual(meta.extra["torrent_name"], "[PS2] Mystery Game")

    def test_original_release_keeps_its_name(self):
        release = self.release("Mario_Kart_Wii_PAL_WII-SUSHi", {"mkw.iso": wii_header("RMCP01", "Mario Kart Wii") + b"\0" * 4096})
        self.set("gametdb.lookup", {"id": "RMCP01", "type": "", "region": "PAL", "languages": ["en"], "title": "Mario Kart Wii",
                                    "synopsis": "", "developer": "", "publisher": "Nintendo", "date": "", "genres": [], "rating": "", "name": ""})
        meta = console.resolve(release, "console", original=True, hints={})
        self.assertEqual(meta.extra["torrent_name"], "Mario_Kart_Wii_PAL_WII-SUSHi")
        self.assertEqual(meta.extra["suggested_name"], "[Wii] Mario Kart Wii")

    def test_source_crash_is_a_warning(self):
        release = self.release("Mario Kart Wii", {"mkw.iso": wii_header("RMCP01", "Mario Kart Wii") + b"\0" * 4096})
        self.mocks["gametdb.lookup"].side_effect = RuntimeError("boom")
        meta = console.resolve(release, "console", original=False, hints={})
        self.assertTrue(any("forrás hiba" in w for w in meta.warnings))
        self.assertEqual(meta.region, "PAL (EUR)")


class MobileTests(ResolverCase):
    def play(self) -> dict:
        data = googleplay.parse_app_page((FIXTURES / "googleplay_maps.html").read_text(encoding="utf-8"))
        data["url"] = googleplay.store_url("com.google.android.apps.maps")
        return data

    def test_android_apk_with_play_listing(self):
        apk = apk_file(self.tmp / "maps.apk", axml("com.google.android.apps.maps", 1152, "11.152.0", 28, 34, "Maps"))
        release = make_release(self.tmp / "rel", {"Google.Maps.v11.152.0.apk": apk.read_bytes()})
        self.set("googleplay.app", self.play())
        signed = apk_reader.parse_certs((FIXTURES / "apksigner_signed.txt").read_text(encoding="utf-8"), 0)
        with mock.patch.object(apk_reader, "badging", return_value={}), mock.patch.object(apk_reader, "certs", return_value=signed):
            meta = mobile.resolve(release, "mobil", original=False, hints={})
        self.assertEqual(meta.kind, "app")
        self.assertEqual(meta.extra["torrent_name"], "[Android] Google Térkép v11.152.0")
        self.assertEqual(meta.version, "11.152.0")
        self.assertEqual(meta.identifiers["package"], "com.google.android.apps.maps")
        self.assertEqual((meta.description_source, meta.machine_translated), ("Google Play", False))
        self.assertTrue(meta.description_hu.startswith("A Google Térkép"))
        self.assertEqual(meta.proof_links, ["https://play.google.com/store/apps/details?id=com.google.android.apps.maps&hl=hu"])
        self.assertEqual(meta.extra["min_os"], "Android 9 (API 28)")
        self.assertEqual(meta.publisher, "Google LLC")
        self.assertTrue(meta.extra["signature"].startswith("ismeretlen"))
        self.assertIn("ismeretlen forrásból", meta.install_hu)
        self.assertTrue(any("képernyőkép" in w for w in meta.warnings))
        self.assertEqual(meta.confidence, 0.9)
        self.assertEqual(len(meta.extra["store_screenshots"].splitlines()), 3)

    def test_android_fdroid_original_signature(self):
        apk = apk_file(self.tmp / "fd.apk", axml("org.fdroid.fdroid", 2000052, "2.0.2", 24, 35, "F-Droid"))
        release = make_release(self.tmp / "rel", {"F-Droid_2.0.2.apk": apk.read_bytes()})
        info = {"name": "F-Droid", "description": "Az F-Droid telepíthető szabad szoftveres alkalmazások katalógusa, és nem csak az.",
                "description_locale": "hu", "description_en": "F-Droid is an installable catalogue.", "author": "F-Droid",
                "categories": ["System"], "license": "GPL-3.0-or-later", "icon": "https://f-droid.org/repo/icon.png", "screenshots": [],
                "versions": [{"signers": ["43238d512c1e5eb2d6569f4a3afbf5523418b82e0a3ed1552770abb9a9c9ccab"]}],
                "url": "https://f-droid.org/packages/org.fdroid.fdroid/"}
        self.set("fdroid.lookup", info)
        signed = {"verified": True, "schemes": ["v2"], "errors": [],
                  "signers": [{"dn": "CN=Ciaran Gultnieks, OU=Unknown, O=Unknown, L=Wetherby, ST=Unknown, C=UK",
                               "sha256": "43238d512c1e5eb2d6569f4a3afbf5523418b82e0a3ed1552770abb9a9c9ccab", "sha1": ""}]}
        with mock.patch.object(apk_reader, "badging", return_value={}), mock.patch.object(apk_reader, "certs", return_value=signed):
            meta = mobile.resolve(release, "mobil", original=False, hints={})
        self.assertTrue(meta.extra["signature"].startswith("eredeti"))
        self.assertEqual(meta.description_source, "F-Droid")
        self.assertEqual(meta.proof_links, ["https://f-droid.org/packages/org.fdroid.fdroid/"])
        self.assertEqual(meta.extra["torrent_name"], "[Android] F-Droid v2.0.2")
        self.assertEqual(meta.extra["license"], "GPL-3.0-or-later")

    def test_android_modified_and_english_only(self):
        apk = apk_file(self.tmp / "x.apk", axml("com.example.player", 5, "5.0", 21, 33, "Player"))
        release = make_release(self.tmp / "rel", {"Player.v5.0.MOD.apk": apk.read_bytes()})
        self.set("googleplay.app", {"name": "Example Player", "description": "The best player for every format, fast and easy to use.",
                                    "developer": "Example Inc", "url": "https://play.google.com/store/apps/details?id=com.example.player&hl=hu"})
        bad = apk_reader.parse_certs((FIXTURES / "apksigner_unsigned.txt").read_text(encoding="utf-8"), 1)
        with mock.patch.object(apk_reader, "badging", return_value={}), mock.patch.object(apk_reader, "certs", return_value=bad):
            meta = mobile.resolve(release, "mobil", original=False, hints={})
        self.assertTrue(meta.extra["signature"].startswith("módosított"))
        self.assertIn("eltér", meta.install_hu)
        self.assertEqual(meta.description_en, "The best player for every format, fast and easy to use.")
        self.assertEqual(meta.description_hu, "")
        self.assertTrue(any("csak angol" in w for w in meta.warnings))
        self.assertEqual(meta.extra["torrent_name"], "[Android] Example Player v5.0")

    def test_ios_ipa(self):
        plist = {"CFBundleIdentifier": "com.google.Maps", "CFBundleDisplayName": "Google Maps", "CFBundleShortVersionString": "26.41.1",
                 "MinimumOSVersion": "16.0", "UIDeviceFamily": [1, 2], "CFBundleExecutable": "Maps"}
        ipa = ipa_file(self.tmp / "maps.ipa", plist=plist, binary=macho(0))
        release = make_release(self.tmp / "rel", {"Google Maps 26.41.1.ipa": ipa.read_bytes()})
        store = itunes.parse_result(json.loads((FIXTURES / "itunes_maps.json").read_text(encoding="utf-8"))["results"][0])
        self.set("itunes.lookup", store)
        meta = mobile.resolve(release, "mobil", original=False, hints={})
        self.assertEqual(meta.extra["torrent_name"], "[iPhone] Google Térkép v26.41.1")
        self.assertEqual(meta.version, "26.41.1")
        self.assertEqual(meta.extra["store_version"], "26.42.0")
        self.assertEqual(meta.extra["min_os"], "iOS 16.0")
        self.assertEqual(meta.description_source, "App Store")
        self.assertTrue(meta.description_hu.startswith("A Google Térkép"))
        self.assertTrue(meta.proof_links[0].startswith("https://apps.apple.com/hu/app/"))
        self.assertEqual(meta.languages, ["magyar"])
        self.assertIn("TrollStore", meta.install_hu)
        self.assertEqual(meta.extra["signature"], "dekódolt IPA (sideloadolható)")

    @unittest.skipUnless(shutil.which("7z") or shutil.which("7za"), "7z not installed")
    def test_packed_apk(self):
        apk = apk_file(self.tmp / "x.apk", axml("com.example.player", 5, "5.0", 21, 33, "Player"))
        packed = self.tmp / "p.zip"
        import zipfile

        with zipfile.ZipFile(packed, "w") as archive:
            archive.write(apk, "Player/Player_5.0.apk")
        release = make_release(self.tmp / "rel", {"player.zip": packed.read_bytes()})
        with mock.patch.object(apk_reader, "badging", return_value={}), mock.patch.object(apk_reader, "certs", return_value={}):
            meta = mobile.resolve(release, "mobil", original=False, hints={})
        self.assertEqual(meta.identifiers["package"], "com.example.player")
        self.assertEqual(meta.extra["packed"], "Player/Player_5.0.apk")
        self.assertEqual(meta.extra["torrent_name"], "[Android] Player v5.0")

    def test_no_app_file(self):
        release = make_release(self.tmp / "Some.App.v1.2.Android", {"readme.txt": b"x"})
        meta = mobile.resolve(release, "mobil", original=False, hints={})
        self.assertEqual(meta.extra["torrent_name"], "[Android] Some App v1.2")
        self.assertLessEqual(meta.confidence, 0.4)
        self.assertTrue(any("nincs APK" in w for w in meta.warnings))


if __name__ == "__main__":
    unittest.main()
